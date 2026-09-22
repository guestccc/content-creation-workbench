"""视频字幕提取接口。

路由注册顺序说明：/environment 等静态路径必须写在 /{job_id} 之前，
否则会被路径参数吞掉返回 422。

接口一览：
- GET  /environment              运行环境自检（VideoCaptioner / ffmpeg / 安装指引）
- PUT  /environment/vc-root      手动指定 VideoCaptioner 目录（空串 = 恢复自动探测）
- POST /jobs                     创建字幕提取任务
- GET  /jobs                     历史任务分页列表
- GET  /jobs/{id}                任务详情（轮询进度也用它）
- GET  /jobs/{id}/subtitles      任务产出的字幕文件列表
- GET  /jobs/{id}/subtitles/{n}  单份字幕的文本内容（预览用，超长截断）
- POST /jobs/{id}/cancel         取消任务
- PUT  /jobs/{id}/remark        更新任务备注（空串 = 清空）
- DELETE /jobs/{id}              删除任务记录
"""

from pathlib import Path

from fastapi import APIRouter, Path as PathParam, Query

from app.api.deps import SubtitleJobServiceDep
from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.core.logging import get_logger
from app.models.subtitle_job import SubtitleJobStatus
from app.schemas.common import ApiResponse, JobBatchDeleteRequest, JobRemarkUpdate
from app.schemas.subtitle_job import (
    SubtitleEnvironmentResponse,
    SubtitleFileResponse,
    SubtitleJobCreate,
    SubtitleJobListData,
    SubtitleJobResponse,
    SubtitleTextData,
    SubtitleVcRootUpdate,
    engines_payload_response,
)
from app.services.subtitle_env import looks_like_vc, probe_environment, reset_cache
from app.services.subtitle_settings import (
    normalize_vc_root,
    sync_from_env_file,
    write_vc_root,
)

router = APIRouter(prefix="/subtitle", tags=["视频字幕提取"])
logger = get_logger(__name__)


@router.get(
    "/environment",
    response_model=ApiResponse[SubtitleEnvironmentResponse],
    summary="字幕提取运行环境自检",
)
def get_environment(
    refresh: bool = Query(default=False, description="绕过探测缓存重新检测"),
) -> ApiResponse[SubtitleEnvironmentResponse]:
    """探测 VideoCaptioner 与 ffmpeg 是否可用，未安装时给出按平台的安装指引。

    指引只是文本，后端绝不替用户执行安装 —— 网页触发的安装命令既不可靠
    （权限、网络、杀毒软件），也不该有（网页进程不该有装软件的权力）。
    """
    # 「重新检测」顺带捡起用户手工改过的 .env：settings 单例在进程启动时读过
    # 一次就放手了，不同步的话页面显示的值会跟文件里写的对不上。
    if refresh and sync_from_env_file():
        reset_cache()
    env = probe_environment(refresh=refresh)
    return ApiResponse(
        data=SubtitleEnvironmentResponse(**env, asr_engines=engines_payload_response())
    )


@router.put(
    "/environment/vc-root",
    response_model=ApiResponse[SubtitleEnvironmentResponse],
    summary="手动指定 VideoCaptioner 安装目录",
)
def set_vc_root(payload: SubtitleVcRootUpdate) -> ApiResponse[SubtitleEnvironmentResponse]:
    """把用户选定的目录写进 backend/.env，并就地重新探测。

    为什么要有这个入口：VideoCaptioner 不在本仓库里，装在哪台机器上都不一样，
    而且从 GitHub 下载解压出来的目录常带 `-master` 之类后缀，光靠猜路径必然
    有猜不中的时候。让用户直接在页面上指一下，比让他去改配置文件靠谱。

    校验只拦「根本用不了」的情况（路径不存在 / 不是目录 / 含换行引号这类
    写不进 .env 的字符）。选了**看起来不像** VideoCaptioner 的目录不拦 ——
    报错走 warnings，让用户看着探测结果自己判断，而不是被一个自以为是的
    规则挡在门外。

    body 里 path 传空串表示清除指定、恢复自动探测。
    """
    path = normalize_vc_root(payload.path)

    if path:
        target = Path(path)
        if not target.exists():
            raise BadRequestError(f"路径不存在：{path}")
        if not target.is_dir():
            raise BadRequestError(f"路径不是目录：{path}")

    try:
        write_vc_root(path)
    except ValueError as exc:
        # 值里带了换行/引号之类写不进 .env 的字符
        raise BadRequestError(str(exc)) from exc

    # 写盘只保证「下次启动也生效」；当前进程要立刻用上新值，得原地改这个单例
    settings.SUBTITLE_VC_ROOT = path
    reset_cache()

    env = probe_environment(refresh=True)

    # 目录看着不像 VideoCaptioner 时补一句提醒，但不阻止操作
    warnings = list(env.get("warnings") or [])
    if path and not looks_like_vc(Path(path)):
        warnings.append(
            f"所选目录里没有看到 .venv 或 videocaptioner/，可能不是 VideoCaptioner 的安装目录。"
        )
    env["warnings"] = warnings

    return ApiResponse(
        data=SubtitleEnvironmentResponse(**env, asr_engines=engines_payload_response())
    )


@router.post(
    "/jobs",
    response_model=ApiResponse[SubtitleJobResponse],
    status_code=201,
    summary="创建字幕提取任务",
)
def create_job(
    payload: SubtitleJobCreate,
    service: SubtitleJobServiceDep,
) -> ApiResponse[SubtitleJobResponse]:
    """创建任务并排入队列，真正的转写由后台工作线程执行。"""
    job = service.create_job(payload)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.get(
    "/jobs",
    response_model=ApiResponse[SubtitleJobListData],
    summary="分页查询字幕提取任务",
)
def list_jobs(
    service: SubtitleJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(SubtitleJobStatus.ALL)}",
    ),
) -> ApiResponse[SubtitleJobListData]:
    """分页查询历史任务，列表不携带每条视频的明细。"""
    items, total = service.list_jobs(page=page, page_size=page_size, status=status_filter)
    return ApiResponse(
        data=SubtitleJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[SubtitleJobResponse.from_model(item, include_items=False) for item in items],
        )
    )


# 静态路径 /jobs/batch-delete 必须写在 /jobs/{job_id} 之前（见文件头说明）
@router.post(
    "/jobs/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除字幕提取任务记录",
)
def batch_delete_jobs(
    payload: JobBatchDeleteRequest,
    service: SubtitleJobServiceDep,
) -> ApiResponse[dict]:
    """整批删除（全成功或全失败）；purge_files=true 时字幕文件一并清掉。"""
    ids = service.delete_jobs(payload.ids, purge_files=payload.purge_files)
    return ApiResponse(data={"ids": ids, "count": len(ids)})


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="获取字幕提取任务详情",
)
def get_job(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SubtitleJobResponse]:
    """按 ID 获取任务详情（含每条视频的结果），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.get(
    "/jobs/{job_id}/subtitles",
    response_model=ApiResponse[list],
    summary="任务产出的字幕文件列表",
)
def list_subtitles(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[list]:
    """列出任务已产出的字幕文件（尚未生成的不返回，前端从任务详情的条目状态看进度）。"""
    subtitles = service.list_subtitles(job_id)
    return ApiResponse(
        data=[
            SubtitleFileResponse(
                index=item["index"],
                item_index=item["item_index"],
                name=item["name"],
                source_name=item["source_name"],
                size_bytes=item["size_bytes"],
                segment_count=item["segment_count"],
            ).model_dump()
            for item in subtitles
            if item["exists"]
        ]
    )


@router.get(
    "/jobs/{job_id}/subtitles/{index}",
    response_model=ApiResponse[SubtitleTextData],
    summary="单份字幕的文本内容（预览）",
)
def get_subtitle_text(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    index: int = PathParam(..., ge=1, description="字幕序号（全局，从 1 开始）"),
) -> ApiResponse[SubtitleTextData]:
    """返回一份 .srt 的文本内容，超过大小上限时只返回开头一段。

    安全说明：接口只接受「任务 ID + 字幕序号」，文件路径完全由任务记录
    推导 —— 如果放开让前端传路径，这就是一个任意文件读取漏洞。
    长视频的字幕可能有几百 KB，一次性返回会把响应撑爆，所以按
    SUBTITLE_PREVIEW_MAX_BYTES 截断并打上 truncated 标记，前端据此提示
    「只显示了开头」并引导用户去目录里看完整文件。
    """
    path, name, source_name = service.get_subtitle_path(job_id, index)
    size_bytes = path.stat().st_size

    max_bytes = settings.SUBTITLE_PREVIEW_MAX_BYTES
    truncated = size_bytes > max_bytes
    # 按字节截断后再解码，避免一次读入超大文件；srt 是 utf-8（可能带 BOM）
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1 if truncated else max_bytes)
    content = raw[:max_bytes].decode("utf-8-sig", errors="replace")
    if truncated:
        # 砍掉最后一个可能截断到一半的字幕块，让预览结尾是完整的
        last_boundary = content.rfind("\n\n")
        if last_boundary > 0:
            content = content[:last_boundary]

    return ApiResponse(
        data=SubtitleTextData(
            index=index,
            name=name,
            source_name=source_name,
            content=content,
            size_bytes=size_bytes,
            truncated=truncated,
        )
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="取消字幕提取任务",
)
def cancel_job(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SubtitleJobResponse]:
    """取消排队中或执行中的任务，执行中的会整组杀掉当前转写子进程。"""
    job = service.cancel_job(job_id)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.put(
    "/jobs/{job_id}/remark",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="更新字幕提取任务备注",
)
def update_job_remark(
    payload: JobRemarkUpdate,
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SubtitleJobResponse]:
    """更新任务备注，空串表示清空。"""
    job = service.update_remark(job_id, payload)
    return ApiResponse(data=SubtitleJobResponse.from_model(job, include_items=False))


@router.post(
    "/jobs/{job_id}/items/{index}/retry",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="重试单条失败 / 跳过的视频",
)
def retry_job_item(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    index: int = PathParam(..., ge=1, description="视频在任务内的序号，从 1 开始"),
) -> ApiResponse[SubtitleJobResponse]:
    """把该条重置回 pending 并让任务重新入队，其余条目的结果保持不动。

    任务还在排队 / 执行中时拒绝（409）—— 正在跑的任务重跑没有意义。上一轮写坏的
    .srt 会先删掉，免得新结果出来之前用户看到的是旧文本。
    """
    job = service.retry_item(job_id, index)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="重试全部未完成的视频",
)
def retry_job_items(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SubtitleJobResponse]:
    """把该任务所有「失败」「跳过」的视频一起重新入队，已成功的保持原样。

    走这一个接口而不是让前端循环调单条重试：第一次调用就会把任务置回 pending，
    第二次会撞上「任务尚未结束」的校验。
    """
    job = service.retry_items(job_id)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除字幕提取任务记录",
)
def delete_job(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    purge_files: bool = Query(default=False, description="是否连同磁盘上的任务产物一起删除"),
) -> ApiResponse[dict]:
    """删除任务记录（级联删条目）；默认保留磁盘产物，purge_files=true 时一并清掉。"""
    service.delete_job(job_id, purge_files=purge_files)
    return ApiResponse(data={"id": job_id})
