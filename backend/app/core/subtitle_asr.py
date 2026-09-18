"""VideoCaptioner 的 ASR 引擎清单。

与 `scene_templates.py` 同一条思路：**参数真源在后端**，前端只负责渲染。
这份清单会随环境自检一起回给页面，所以两边的选项不会各说各话。

清单里只有 `videocaptioner transcribe --asr` 的 argparse choices 认的四个值。
`faster-whisper` 不在此列 —— 文档提过它，但 CLI 的 choices 里没有，传进去
会被 argparse 直接拒绝（退出码 2）。想用它只能改 VideoCaptioner 的配置文件，
不是我们这个页面该做的手术。

本期只做「语音转字幕」，所以 `--language` 在语义上是**识别语言**（源语言），
不是翻译目标语言 —— 翻译是另一个子命令，本期不碰。
"""

from typing import Dict, NamedTuple, Optional


class AsrEngine(NamedTuple):
    """一个 ASR 引擎的展示信息。"""

    key: str
    name: str
    summary: str
    #: 是否需要先在 VideoCaptioner 里配好 key（whisper-api 需要）
    requires_key: bool
    #: 是否推荐（页面上排在最前并默认选中）
    recommended: bool


#: 顺序即页面上的展示顺序：免费免配置的排前面。
ASR_ENGINES = (
    AsrEngine(
        key="bijian",
        name="必剪（免费）",
        summary="开箱即用，不需要任何配置，中英识别；国内网络直连",
        requires_key=False,
        recommended=True,
    ),
    AsrEngine(
        key="jianying",
        name="剪映（免费）",
        summary="同样免配置，中英识别；与必剪互为备份，一个不通时换另一个",
        requires_key=False,
        recommended=False,
    ),
    AsrEngine(
        key="whisper-api",
        name="Whisper API",
        summary="支持多语种；需要先在 VideoCaptioner 里配好 API Key",
        requires_key=True,
        recommended=False,
    ),
    AsrEngine(
        key="whisper-cpp",
        name="whisper.cpp（本地）",
        summary="本地推理、支持多语种；需要自行下载模型文件",
        requires_key=False,
        recommended=False,
    ),
)

#: key → 引擎定义，供校验与展示查表。
ASR_ENGINE_MAP: Dict[str, AsrEngine] = {engine.key: engine for engine in ASR_ENGINES}

#: 识别语言：「自动检测」用空串表示，其余是 ISO 639-1 代码。
LANGUAGE_AUTO = ""
#: 页面上展示的语言候选；不是白名单 —— CLI 接受任意 ISO 639-1 代码。
LANGUAGE_OPTIONS = (
    (LANGUAGE_AUTO, "自动检测"),
    ("zh", "中文"),
    ("en", "英文"),
    ("ja", "日文"),
    ("ko", "韩文"),
)


def resolve_engine(key: Optional[str]) -> str:
    """把外部传入的引擎名收敛成合法值。

    留空或传了不认识的引擎时回退到默认（配置项 SUBTITLE_DEFAULT_ASR），
    这样前端发来一个过期的 key 也不会让整个任务创建失败。

    Raises:
        ValueError: 连配置里的默认值都不在清单里 —— 那是配置写错，该报出来。
    """
    if key in ASR_ENGINE_MAP:
        return key

    from app.core.config import settings

    default = settings.SUBTITLE_DEFAULT_ASR
    if default not in ASR_ENGINE_MAP:
        raise ValueError(
            f"默认 ASR 引擎配置有误：{default}（可选：{', '.join(ASR_ENGINE_MAP)}）"
        )
    return default


def engines_payload() -> list:
    """清单的接口形态（与 schemas/subtitle_job.py 的 AsrEngineResponse 对应）。"""
    return [
        {
            "key": engine.key,
            "name": engine.name,
            "summary": engine.summary,
            "requires_key": engine.requires_key,
            "recommended": engine.recommended,
        }
        for engine in ASR_ENGINES
    ]
