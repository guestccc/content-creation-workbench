"""
video-subtitle-remover 运行环境的安装引导。

这个工具的源码和模型都在本目录里了（模型约 600MB），唯独缺 Python 依赖。
`vct` 不会偷偷帮你装 —— 装依赖要下 2-4GB、跑十几分钟，这种事得你点头。

本模块负责两件事：
    explain()          把「为什么缺、怎么装」讲清楚，并给出照抄即可的命令
    run_install()      真正调起 scripts/setup-vsr.sh

注意：VSR 的 backend/config.py 顶部就 import qfluentwidgets（PySide6 系），
所以即使是纯命令行模式也依赖 GUI 库，环境必须按 requirements.txt 全量安装，
不存在「只装命令行部分」的省事路径。
"""

from __future__ import annotations

import sys

from .. import env, runner, ui


def add_arguments(parser) -> None:
    """给 vct setup-vsr 子命令注册参数。"""
    parser.add_argument(
        "--run",
        action="store_true",
        help="直接执行安装脚本（默认只显示说明）",
    )


def run(args) -> int:
    """执行 vct setup-vsr。"""
    if getattr(args, "run", False):
        return run_install()
    explain()
    return 0


def explain() -> None:
    """打印安装说明。"""
    probe = env.probe_vsr()

    ui.section("video-subtitle-remover 环境安装")

    if probe.ok:
        ui.ok("环境已就绪，无需安装。")
        ui.hint(probe.detail)
        return

    ui.hint(f"当前状态：{probe.detail}")
    ui.blank()

    print("  " + ui.bold_text("为什么需要单独装？"))
    ui.hint(
        "这个工具的源码和 AI 模型（约 600MB）都已经在工具箱里了，\n"
        "但运行它还需要 Python 依赖：PyTorch、PaddlePaddle、PaddleOCR 等。\n"
        "依赖体积较大（约 2-4GB），所以没有预装。"
    )

    ui.blank()
    print("  " + ui.bold_text("方式一：一键脚本（推荐）"))
    ui.hint(
        f"    bash {env.SETUP_VSR_SCRIPT}\n"
        f"\n"
        f"脚本会：用 Homebrew 的 Python 3.12 建独立虚拟环境 →\n"
        f"依次安装 PaddlePaddle、PyTorch、以及 requirements.txt →\n"
        f"最后自动跑一次 vct doctor 验证。"
    )

    ui.blank()
    print("  " + ui.bold_text("方式二：用已有的 conda 环境"))
    ui.hint(
        f"如果你之前按 VSR 的 README 建过 videoEnv 环境，直接指定即可：\n"
        f"\n"
        f"    export VCT_VSR_PYTHON=/你的环境路径/bin/python\n"
        f"    vct doctor    # 确认能识别到"
    )

    ui.blank()
    print("  " + ui.bold_text("装完后"))
    ui.hint(
        f"    vct doctor                    # 复查环境\n"
        f"    vct desub 你的视频.mp4        # 开始擦除硬字幕"
    )

    ui.blank()
    print("  " + ui.bold_text("常见问题"))
    ui.hint(
        "· 首次运行会自动合并模型分片文件（模型被切成几块存放），\n"
        "  这一步会多花几十秒，属正常现象。\n"
        "· 本机是 Apple 芯片，PyTorch 会走 MPS 加速；OCR 检测固定用 CPU。\n"
        "· 如果 PaddlePaddle 装不上（macOS ARM 的轮子偶尔会有问题），\n"
        f"  可以改用官方预构建的 Docker 镜像，详见\n"
        f"  {env.VSR_ROOT}/README.md 的 Docker 章节。"
    )


def run_install() -> int:
    """执行安装脚本。"""
    ui.header("安装 video-subtitle-remover 环境")

    probe = env.probe_vsr()
    if probe.ok:
        ui.ok("环境已就绪，无需安装。")
        ui.hint(probe.detail)
        return 0

    if not env.SETUP_VSR_SCRIPT.exists():
        ui.error(f"找不到安装脚本：{env.SETUP_VSR_SCRIPT}")
        ui.hint("请确认工具箱目录结构完整。")
        return 1

    # ---- 前置检查：需要 Homebrew 的 python3.12 ----
    python312 = _find_python312()
    if python312 is None:
        ui.warn("没找到 Python 3.12")
        ui.hint(
            "video-subtitle-remover 要求 Python 3.12 及以上。\n"
            "先安装：\n"
            "    brew install python@3.12"
        )
        if not ui.ask_yes_no("仍然继续？（脚本会自己再找一次）", default=False):
            return 1
    else:
        ui.ok(f"找到 Python 3.12：{python312}")

    ui.blank()
    ui.warn("接下来会下载约 2-4GB 依赖，耗时 10-20 分钟，请保持网络畅通。")
    ui.hint("中途可以按 Ctrl-C 中断，已下载的部分会保留在 pip 缓存里。")
    ui.blank()

    if not ui.ask_yes_no("确认开始安装？", default=True):
        return 0

    result = runner.run(
        ["bash", str(env.SETUP_VSR_SCRIPT)],
        cwd=env.TOOLBOX_ROOT,
        tag="setup-vsr",
        title="正在安装 VSR 运行环境",
    )

    if result.ok:
        # 环境变了，清掉探测缓存再复查
        env.clear_cache()
        ui.blank()
        if env.probe_vsr(refresh=True).ok:
            ui.ok("安装完成，环境验证通过。")
            ui.hint("现在可以用了：vct desub 你的视频.mp4")
            return 0
        ui.warn("脚本执行完了，但环境检查仍未通过。")
        ui.hint("请查看上面的输出定位问题，或运行 vct doctor 查看详情。")
        return 1

    ui.blank()
    ui.error("安装未成功完成。")
    ui.hint(
        f"完整日志：{result.log_path}\n"
        f"常见原因是 PaddlePaddle 的 macOS ARM 轮子下载失败，\n"
        f"可以重跑一次（会复用 pip 缓存），或改用 README 里的 Docker 方案。"
    )
    return result.returncode


def _find_python312() -> str | None:
    """找一个可用的 Python 3.12+ 解释器。

    优先 Homebrew 的常见路径，其次 PATH 上的 python3.12 / python3.13。
    检查实际版本而不是只看文件名，避免找到指向别的版本的软链接。
    """
    import os
    import shutil
    import subprocess
    from pathlib import Path

    candidates = [
        Path("/opt/homebrew/bin/python3.12"),
        Path("/usr/local/bin/python3.12"),
        Path("/opt/homebrew/bin/python3.13"),
        Path("/usr/local/bin/python3.13"),
    ]
    for name in ("python3.12", "python3.13", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    for path in candidates:
        if not (path.exists() and os.access(path, os.X_OK)):
            continue
        try:
            out = subprocess.run(
                [str(path), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            try:
                major, minor = (int(part) for part in out.stdout.strip().split("."))
            except ValueError:
                continue
            if (major, minor) >= (3, 12):
                return str(path)

    return None


# --------------------------------------------------------------------------
# 交互式
# --------------------------------------------------------------------------


def interactive_setup() -> int:
    """交互式：安装引导。"""
    explain()
    ui.blank()
    if env.probe_vsr().ok:
        return 0
    if ui.ask_yes_no("现在开始安装吗？", default=False):
        return run_install()
    ui.hint("随时可以再运行：vct setup-vsr --run")
    return 0
