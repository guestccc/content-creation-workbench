"""
vct 的子命令实现。

每个模块负责一类事情：
    vc.py         —— VideoCaptioner 的全部子命令（参数原样透传 + 交互式收集）
    vsr.py        —— video-subtitle-remover 的擦除硬字幕
    combo.py      —— 组合流程：提取字幕 + 擦除字幕
    doctor.py     —— 统一环境诊断
    setup_vsr.py  —— VSR 运行环境的安装引导

模块内的约定：
* `run_*` 函数接收已经拼好的参数列表，返回进程退出码。
* `interactive_*` 函数负责中文交互式地收集参数，然后调用对应的 run_*。
"""

from __future__ import annotations

from .. import env, ui


def ensure(probe: env.Probe, dry_run: bool = False) -> bool:
    """检查某个探测结果是否可用，不可用时打印原因与修复建议。

    Args:
        probe: env.probe_* 返回的探测结果。
        dry_run: dry-run 模式下即使环境缺失也放行，方便用户先看命令长什么样。
    Returns:
        True 表示可以继续执行。
    """
    if probe.ok:
        return True

    if dry_run:
        ui.warn(f"{probe.name} 环境不可用，但当前是 dry-run，仍然继续：{probe.detail}")
        return True

    ui.error(f"{probe.name} 环境不可用")
    ui.hint(probe.detail)
    if probe.fix:
        ui.blank()
        print("  " + ui.bold_text("修复建议："))
        ui.hint(probe.fix)
    return False


def output_path_for(input_path: str, suffix: str, extension: str) -> str:
    """根据输入文件推一个输出路径，放在与输入相同的目录下。

    例如 input=/a/b/video.mp4, suffix='_no_sub', extension='.mp4'
    得到 /a/b/video_no_sub.mp4

    Args:
        input_path: 输入文件路径。
        suffix: 加在文件名主干后面的后缀。
        extension: 输出扩展名（含点）。
    Returns:
        输出文件的绝对路径字符串。
    """
    from pathlib import Path

    source = Path(input_path)
    return str(source.with_name(f"{source.stem}{suffix}{extension}"))
