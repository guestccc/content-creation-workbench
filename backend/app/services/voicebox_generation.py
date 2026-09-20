"""配音生成的运行时：进程内注册表 + 单 worker 串行执行。

**为什么不用任务表**：这是给「Voicebox 同步阻塞的 /generate」做的一层薄代理，
不是像切割/字幕那样的重任务。记录只回答前端轮询要问的三件事（到哪一步了、
产物在哪、失败了为什么），这些信息在进程内存里就够 —— 重启丢掉它们没有损失：
产物在磁盘上，扫盘清单（dubbing_library）才是历史。

**为什么串行**：Voicebox 自己就是串行跑 GPU 任务的，我们这边开多条并发只会把
上游压出 OOM。一条 worker 线程逐条消化队列，「谁先谁后」因此是确定的。

**为什么提交即返回**：前端 fetch 超时 15 秒（api/client.ts 写死且无单次覆写入口），
而长文案在 CPU 上要跑几分钟。POST 只入队，HTTP 立刻返回，前端轮询
GET /voicebox/generations/{id} 看进度 —— 与「后端任务是异步跑的，进度一律靠
轮询」的项目约定一致。

id 是进程内自增 int（不是 uuid），对齐前端 useJobPolling / useJobRunner 的
`J extends { id: number }` 泛型约束。
"""

import queue
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.logging import get_logger
from app.models.content import utcnow
from app.services import dubbing_library, voicebox_client
from app.services.voicebox_client import VoiceboxError

logger = get_logger(__name__)

#: 状态机（比任务表那套简单得多：没有取消、没有暂停，只有走完或走死）。
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

#: 文案摘要截多长：列表/轮询响应里展示用，全文不让每 2 秒传一遍。
_EXCERPT_CHARS = 60

_records: Dict[int, Dict[str, Any]] = {}
_records_lock = threading.Lock()
_next_id = 1

_queue: "queue.Queue[int]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _excerpt(text: str) -> str:
    """文案摘要：压掉换行、截前若干字。"""
    flat = " ".join((text or "").split())
    return flat[:_EXCERPT_CHARS]


def _pending_count() -> int:
    with _records_lock:
        return sum(1 for record in _records.values() if record["status"] == STATUS_QUEUED)


def submit(
    text: str,
    *,
    profile_id: str,
    profile_name: str = "",
    filename: str = "",
    language: str = "zh",
    engine: str = "qwen",
    model_size: str = "1.7B",
) -> Dict[str, Any]:
    """入队一次生成，立即返回 queued 状态的记录。

    Raises:
        BadRequestError: 等待队列已满（VOICEBOX_MAX_PENDING）—— 早点告诉用户，
            好过让请求在队列里无声地排到天荒地老。
    """
    global _next_id

    if _pending_count() >= settings.VOICEBOX_MAX_PENDING:
        raise BadRequestError(
            f"等待中的配音已有 {_pending_count()} 条（上限 {settings.VOICEBOX_MAX_PENDING}），"
            "请稍等当前生成完成后再提交"
        )

    _ensure_worker()

    with _records_lock:
        record_id = _next_id
        _next_id += 1
        record: Dict[str, Any] = {
            "id": record_id,
            "status": STATUS_QUEUED,
            "text": text,
            "text_excerpt": _excerpt(text),
            "profile_id": profile_id,
            "profile_name": profile_name,
            "requested_name": filename,
            "language": language,
            "engine": engine,
            "model_size": model_size,
            "filename": "",
            "output_path": "",
            "duration": None,
            "error_message": "",
            "progress_hint": "排队中，Voicebox 一次只跑一条",
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
        }
        _records[record_id] = record

    _queue.put(record_id)
    logger.info("配音生成已入队：id=%s profile=%s 文案 %d 字", record_id, profile_id, len(text))
    return _public(record)


def get(record_id: int) -> Dict[str, Any]:
    """取一条记录（给前端轮询）。

    Raises:
        NotFoundError: id 不存在 —— 重启后内存清空了也会走到这里，属正常形态。
    """
    with _records_lock:
        record = _records.get(record_id)
        if record is None:
            raise NotFoundError(f"没有这条配音生成记录（id={record_id}）：后端重启后记录会清空")
        return _public(record)


def _public(record: Dict[str, Any]) -> Dict[str, Any]:
    """把内部记录翻成响应字段（补 elapsed_seconds，不带全文 text）。"""
    end = record.get("finished_at") or utcnow()
    started = record.get("started_at") or record.get("created_at") or end
    elapsed = max((end - started).total_seconds(), 0.0)
    return {
        "id": record["id"],
        "status": record["status"],
        "text_excerpt": record["text_excerpt"],
        "profile_id": record["profile_id"],
        "profile_name": record["profile_name"],
        "engine": record["engine"],
        "model_size": record["model_size"],
        "filename": record["filename"],
        "output_path": record["output_path"],
        "duration": record["duration"],
        "error_message": record["error_message"],
        "progress_hint": record["progress_hint"],
        "created_at": record.get("created_at"),
        "finished_at": record.get("finished_at"),
        "elapsed_seconds": round(elapsed, 1),
    }


def _ensure_worker() -> None:
    """懒启动 worker 线程（第一次 submit 时才起，测试不 submit 就不会有线程）。"""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="voicebox-generation", daemon=True)
        _worker.start()


def _worker_loop() -> None:
    """逐条消化队列。单条失败只标记那条记录，绝不让线程死掉。"""
    while True:
        record_id = _queue.get()
        try:
            _run(record_id)
        except Exception:  # noqa: BLE001 —— 兜底：任何异常都不能杀死 worker
            logger.exception("配音生成发生未预期异常：id=%s", record_id)
            _mark_failed(record_id, "生成时发生未预期的错误，请看后端日志")


def _run(record_id: int) -> None:
    with _records_lock:
        record = _records.get(record_id)
    if record is None:
        return

    with _records_lock:
        record["status"] = STATUS_RUNNING
        record["started_at"] = utcnow()
        record["progress_hint"] = "正在生成语音（长文案在 CPU 上可能要几分钟）"

    try:
        result = voicebox_client.generate(
            record["text"],
            profile_id=record["profile_id"],
            language=record["language"],
            engine=record["engine"],
            model_size=record["model_size"],
        )
        record["progress_hint"] = "生成完毕，取回音频"
        generation_id = str(result.get("id") or "")
        duration = result.get("duration")
        audio_path_hint = str(result.get("audio_path") or "")
        content, extension = voicebox_client.fetch_audio(
            generation_id, hint=audio_path_hint
        )

        output_path = dubbing_library.allocate_path(
            record["requested_name"] or dubbing_library.default_base_name(), extension
        )
        output_path.write_bytes(content)
        if not output_path.exists() or output_path.stat().st_size == 0:
            # 对齐 subtitle_runner 的口径：产物存在且非空才算成功。
            # 顺手把这份空文件删掉 —— 失败的生成不该在产物目录里留下半截文件，
            # 扫盘清单（list_audios）看见什么就列什么，没有「状态」可依据。
            output_path.unlink(missing_ok=True)
            raise VoiceboxError("bad_response", "Voicebox 返回的音频是空的，本次生成作废")

        record["duration"] = float(duration) if isinstance(duration, (int, float)) else None
        record["filename"] = output_path.name
        record["output_path"] = str(output_path)
        dubbing_library.write_entry(
            output_path.name,
            duration=record["duration"],
            profile_id=record["profile_id"],
            profile_name=record["profile_name"],
            text_excerpt=record["text_excerpt"],
            size_bytes=output_path.stat().st_size,
        )
        with _records_lock:
            record["status"] = STATUS_SUCCESS
            record["finished_at"] = utcnow()
            record["progress_hint"] = "完成"
        logger.info(
            "配音生成完成：id=%s 产物=%s 时长=%s 秒",
            record_id,
            output_path.name,
            record["duration"],
        )
    except VoiceboxError as exc:
        logger.warning("配音生成失败：id=%s kind=%s detail=%s", record_id, exc.kind, exc.detail)
        _mark_failed(record_id, exc.user_message)
    except OSError as exc:
        logger.warning("配音产物写盘失败：id=%s（%s）", record_id, exc)
        _mark_failed(record_id, f"配音产物写盘失败：{exc}")


def _mark_failed(record_id: int, message: str) -> None:
    with _records_lock:
        record = _records.get(record_id)
        if record is None:
            return
        record["status"] = STATUS_FAILED
        record["error_message"] = message
        record["progress_hint"] = "失败"
        record["finished_at"] = utcnow()


def _reset_for_tests() -> None:
    """测试专用：清空注册表与队列。生产代码不许调。"""
    global _next_id
    with _records_lock:
        _records.clear()
        _next_id = 1
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
