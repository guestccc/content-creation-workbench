"""
统一环境诊断。

一条命令看清：vct 本身、两个工具的运行环境、ffmpeg、以及各项功能的可用性。
缺什么就给出可以直接照抄的修复命令。

用法：
    vct doctor            人类可读的中文报告
    vct doctor --json     JSON 输出，方便脚本或 AI 读取
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from .. import TOOL_CMD, __version__, config, env, media, ui


def add_arguments(parser) -> None:
    """给 vct doctor 子命令注册参数。"""
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式，便于脚本处理")
    parser.add_argument("--refresh", action="store_true", help="重新探测，忽略缓存")


def run(args) -> int:
    """执行 vct doctor。"""
    probes = env.probe_all(refresh=getattr(args, "refresh", False))
    runtime = env.python_runtime_ok()
    all_probes = [runtime] + probes

    # 汇总功能可用性：哪些功能现在就能用
    capabilities = _build_capabilities()

    if getattr(args, "json", False):
        _print_json(all_probes, capabilities)
    else:
        _print_human(all_probes, capabilities)

    # 退出码：只要有硬性依赖缺失就返回非 0，方便脚本判断。
    # 软依赖（比如只影响交互体验的 questionary）缺失不算环境故障，不计入。
    missing = [p for p in all_probes if not p.ok and not p.extra.get("soft")]
    return 0 if not missing else 1


def _build_capabilities() -> list[tuple[str, bool, str]]:
    """按功能列出现在能不能用。

    Returns:
        [(功能名, 是否可用, 说明), ...]
    """
    vc = env.probe_videocaptioner()
    vsr = env.probe_vsr()
    ffmpeg = env.probe_ffmpeg()
    scene = env.probe_scenedetect()

    vc_ok = vc.ok
    ffmpeg_ok = ffmpeg.ok
    vsr_ok = vsr.ok
    # 切割那一步走的是 media.find_tool，它还会回退到工具箱自带的 ffmpeg-bin，
    # 比 probe_ffmpeg 的「只看 PATH」宽松。这里按实际能做到的口径报。
    can_split = media.find_tool("ffmpeg") is not None

    if not scene.ok:
        scene_note = "需要 PySceneDetect：uv tool install scenedetect"
    elif not can_split:
        scene_note = "能看切点清单，但需要 ffmpeg 才能切出片段"
    else:
        scene_note = "可用"

    return [
        ("智能镜头分割", scene.ok and can_split, scene_note),
        ("语音转字幕（ASR）", vc_ok and ffmpeg_ok,
         "免费引擎，无需 API Key" if vc_ok and ffmpeg_ok else "需要 VideoCaptioner 环境 + ffmpeg"),
        ("字幕翻译（必应/谷歌）", vc_ok, "免费，无需 API Key" if vc_ok else "需要 VideoCaptioner 环境"),
        ("字幕优化 / 断句（LLM）", vc_ok and _has_llm_key(),
         "已配置 LLM" if _has_llm_key() else "需要配置 LLM API Key：vct config set llm.api_key <key>"),
        ("烧录 / 嵌入字幕", vc_ok and ffmpeg_ok, "需要 ffmpeg" if not ffmpeg_ok else "可用"),
        ("TTS 配音（Edge）", vc_ok and ffmpeg_ok, "免费，需要联网"),
        ("下载在线视频", vc_ok and ffmpeg_ok, "需要 yt-dlp 与 ffmpeg"),
        ("擦除硬字幕", vsr_ok, "需要 video-subtitle-remover 环境" if not vsr_ok else "可用"),
    ]


def _has_llm_key() -> bool:
    """判断 VideoCaptioner 是否配了 LLM Key。

    只看配置文件和环境变量，不做网络请求（doctor 不该有副作用）。
    配置文件的格式是 TOML，这里用极简的文本匹配来解析，
    避免为了读一个键就把 tomli 依赖进来。
    """
    import os

    if os.environ.get("OPENAI_API_KEY"):
        return True

    # VideoCaptioner 的配置位置由 platformdirs 决定，macOS 下是这个路径
    candidates = [
        Path.home() / "Library" / "Application Support" / "videocaptioner" / "config.toml",
        Path.home() / ".config" / "videocaptioner" / "config.toml",
    ]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("api_key") and "=" in stripped:
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return True
    return False


def _print_human(probes: list[env.Probe], capabilities: list[tuple[str, bool, str]]) -> None:
    """打印人类可读的中文报告。"""
    ui.header(f"环境诊断 — {TOOL_CMD} v{__version__}")

    # ---- 运行环境 ----
    ui.section("运行环境")
    ui.kv_table([
        ("操作系统", f"{platform.system()} {platform.release()}（{platform.machine()}）"),
        ("Python", f"{sys.version.split()[0]} — {sys.executable}"),
        ("工具箱目录", str(env.TOOLBOX_ROOT)),
        ("配置文件", str(config.CONFIG_PATH) + ("（已创建）" if config.CONFIG_PATH.exists() else "（尚未创建，用默认值）")),
    ])

    # ---- 依赖检查 ----
    ui.section("依赖检查")
    for probe in probes:
        if probe.ok:
            ui.ok(f"{probe.name}：{probe.detail}")
        else:
            ui.error(f"{probe.name}：{probe.detail}")
            if probe.fix:
                ui.hint(probe.fix)

    # ---- 功能可用性 ----
    ui.section("功能可用性")
    usable = 0
    for name, ok, note in capabilities:
        if ok:
            usable += 1
            ui.ok(f"{name}")
        else:
            ui.error(f"{name} — {note}")
    ui.blank()
    ui.info(f"{usable}/{len(capabilities)} 项功能当前可用")

    # ---- 视频信息读取能力 ----
    _print_media_capability()

    # ---- 结尾建议 ----
    missing = [p for p in probes if not p.ok]
    ui.blank()
    if not missing:
        ui.ok("环境完好，所有功能都能用。")
    else:
        ui.warn(f"有 {len(missing)} 项待修复，按上面的建议处理后重新运行 vct doctor 复查。")


def _print_media_capability() -> None:
    """检查能否读出视频宽高（去字幕换算选区时要用）。"""
    if media.find_tool("ffprobe") or media.find_tool("ffmpeg"):
        return
    ui.section("视频信息读取")
    ui.warn("没有 ffprobe / ffmpeg，擦除字幕时无法按比例推算区域（可以改用 -c 传绝对像素）")


def _print_json(probes: list[env.Probe], capabilities: list[tuple[str, bool, str]]) -> None:
    """打印 JSON 格式的诊断结果。"""
    payload = {
        "version": __version__,
        "toolbox_root": str(env.TOOLBOX_ROOT),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "dependencies": [
            {
                "name": probe.name,
                "ok": probe.ok,
                "detail": probe.detail,
                "fix": probe.fix,
                "path": str(probe.path) if probe.path else None,
            }
            for probe in probes
        ],
        "capabilities": [
            {"name": name, "ok": ok, "note": note}
            for name, ok, note in capabilities
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# 交互式
# --------------------------------------------------------------------------


def interactive_doctor() -> int:
    """交互式：环境诊断。"""
    from types import SimpleNamespace

    args = SimpleNamespace(json=False, refresh=True)
    code = run(args)

    # 如果 VSR 缺失，顺手给出安装引导
    if not env.probe_vsr().ok:
        ui.blank()
        from . import setup_vsr

        if ui.ask_yes_no("要查看 video-subtitle-remover 环境的安装方法吗？", default=False):
            setup_vsr.explain()

    return code
