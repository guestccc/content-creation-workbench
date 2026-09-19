"""智能配音（Voicebox）接口。

路由注册顺序：/environment、/profiles、/generations、/audios 等静态路径
必须写在 /generations/{generation_id}、/audios/{name} 这类动态路径之前。

接口一览：
- GET    /environment                    Voicebox 服务环境自检（含安装指引）
- PUT    /environment/base-url           手动指定 Voicebox 服务地址（写进 backend/.env）
- GET    /profiles                       音色列表（透传上游；建音色在 Voicebox GUI 里做）
- POST   /generations                    提交一次配音生成（入队后立即返回，生成在 worker 里跑）
- GET    /generations/{generation_id}    单次生成的进度（前端轮询它）
- GET    /audios                         配音产物清单（materials/dubbing/ 扫盘 + 索引）
- GET    /audios/{name}/file             音频流（支持 Range，<audio> 标签直接放）
- DELETE /audios/{name}                  删除一份配音产物（文件 + 索引条目）
"""

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import Response

from app.core.config import default_voicebox_base_url, settings
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.logging import get_logger
from app.schemas.common import ApiResponse
from app.schemas.voicebox import (
    DubbingAudioListData,
    DubbingGenerationCreate,
    DubbingGenerationResponse,
    VoiceProfileItem,
    VoiceProfileListData,
    VoiceboxBaseUrlUpdate,
    VoiceboxEnvironmentResponse,
)
from app.services import (
    dubbing_library,
    voicebox_client,
    voicebox_env,
    voicebox_generation,
    voicebox_settings,
)
from app.services.file_range import ranged_file_response
from app.services.voicebox_client import VoiceboxError

router = APIRouter(prefix="/voicebox", tags=["智能配音"])
logger = get_logger(__name__)


# --------------------------------------------------------------------------
# 环境自检与配置（静态路径，先于动态路径注册）
# --------------------------------------------------------------------------


@router.get(
    "/environment",
    response_model=ApiResponse[VoiceboxEnvironmentResponse],
    summary="Voicebox 环境自检",
)
def get_environment(
    refresh: bool = Query(default=False, description="绕过探测缓存重新检测"),
) -> ApiResponse[VoiceboxEnvironmentResponse]:
    """探测本机（或指定地址）的 Voicebox 服务：连没连上、模型下没下、有没有 GPU。

    连不上**不是**错误：Voicebox 是用户自己开的桌面应用，没开是正常状态，
    页面上按 install_hints 指引去打开它就是了。指引只是文本，后端绝不替用户
    下载安装任何软件。
    """
    # 「重新检测」顺带捡起用户手工改过的 .env：settings 单例只在进程启动时读过
    # 一次，不同步的话页面显示的值会跟文件里写的对不上（与字幕提取同一套编排）。
    if refresh and voicebox_settings.sync_from_env_file():
        voicebox_env.reset_cache()
    env = voicebox_env.probe_environment(refresh=refresh)
    return ApiResponse(data=VoiceboxEnvironmentResponse(**env))


@router.put(
    "/environment/base-url",
    response_model=ApiResponse[VoiceboxEnvironmentResponse],
    summary="手动指定 Voicebox 服务地址",
)
def set_base_url(payload: VoiceboxBaseUrlUpdate) -> ApiResponse[VoiceboxEnvironmentResponse]:
    """把服务地址写进 backend/.env，当前进程就地生效，并返回最新自检结果。

    为什么要有这个入口：Voicebox 一般跑在本机 127.0.0.1:17493，但远程 GPU 部署
    （Remote Mode / Docker）时地址会变 —— 让用户在页面上指一下，比让他去手改
    配置文件靠谱。body 里 base_url 传空串表示清除指定、恢复默认地址。
    """
    try:
        base_url = voicebox_settings.normalize_base_url(payload.base_url)
        voicebox_settings.write_base_url(base_url)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    # 写盘只保证「下次启动也生效」；当前进程要立刻用上新值，得原地改这个单例。
    # 环境变量在（优先级更高）时则不动它 —— 运行中行为必须与重启后一致。
    if not voicebox_settings.env_var_shadowing():
        settings.VOICEBOX_BASE_URL = base_url or default_voicebox_base_url()
    voicebox_env.reset_cache()

    env = voicebox_env.probe_environment(refresh=True)

    warning = voicebox_settings.shadowing_warning()
    if warning:
        env["warnings"] = [*(env.get("warnings") or []), warning]
    return ApiResponse(data=VoiceboxEnvironmentResponse(**env))


# --------------------------------------------------------------------------
# 音色（只读：建音色在 Voicebox 自己的界面里做）
# --------------------------------------------------------------------------


@router.get(
    "/profiles",
    response_model=ApiResponse[VoiceProfileListData],
    summary="音色列表",
)
def get_profiles() -> ApiResponse[VoiceProfileListData]:
    """透传 Voicebox 的音色清单。

    Raises:
        BadRequestError: 服务连不上 / 上游报错（错误信息已翻成用户可读的话）。
    """
    try:
        profiles = voicebox_client.list_profiles()
    except VoiceboxError as exc:
        raise BadRequestError(exc.user_message) from exc
    items = [
        VoiceProfileItem(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            description=item.get("description"),
            language=str(item.get("language") or ""),
        )
        for item in profiles
        if item.get("id")
    ]
    return ApiResponse(data=VoiceProfileListData(items=items, total=len(items)))


# --------------------------------------------------------------------------
# 生成（提交即返回，进度靠轮询）
# --------------------------------------------------------------------------


@router.post(
    "/generations",
    response_model=ApiResponse[DubbingGenerationResponse],
    status_code=201,
    summary="提交一次配音生成",
)
def create_generation(
    payload: DubbingGenerationCreate,
) -> ApiResponse[DubbingGenerationResponse]:
    """入队后立即返回 queued 记录；生成由后台 worker 串行执行，前端轮询进度。

    为什么不是同步等结果：Voicebox 的 /generate 长文案在 CPU 上要跑几分钟，
    而前端请求 15 秒就超时 —— 与切割/字幕同一套「任务异步跑、进度靠轮询」。
    """
    text = payload.text.strip()
    if not text:
        raise BadRequestError("文案不能为空")
    if payload.language not in ("zh", "en"):
        raise BadRequestError(f"语言只支持 zh / en：{payload.language}")
    if payload.model_size not in ("1.7B", "0.6B"):
        raise BadRequestError(f"模型规模只支持 1.7B / 0.6B：{payload.model_size}")

    record = voicebox_generation.submit(
        text,
        profile_id=payload.profile_id.strip(),
        profile_name=payload.profile_name.strip(),
        filename=payload.filename.strip(),
        language=payload.language,
        model_size=payload.model_size,
    )
    return ApiResponse(data=DubbingGenerationResponse(**record))


@router.get(
    "/generations/{generation_id}",
    response_model=ApiResponse[DubbingGenerationResponse],
    summary="单次生成的进度（轮询用）",
)
def get_generation(
    generation_id: int = PathParam(..., ge=1, description="生成记录 id（进程内自增）"),
) -> ApiResponse[DubbingGenerationResponse]:
    """记录只活在进程内存里：后端重启后旧 id 会返回 404，产物本身不受影响
    （去产物列表里找，那是扫盘得到的）。"""
    record = voicebox_generation.get(generation_id)
    return ApiResponse(data=DubbingGenerationResponse(**record))


# --------------------------------------------------------------------------
# 产物（materials/dubbing/ 扫盘 + 索引）
# --------------------------------------------------------------------------


@router.get(
    "/audios",
    response_model=ApiResponse[DubbingAudioListData],
    summary="配音产物清单",
)
def get_audios() -> ApiResponse[DubbingAudioListData]:
    """磁盘是唯一真相：重启后端、或用户自己往目录里拷音频，清单都会如实反映。"""
    items = dubbing_library.list_audios()
    return ApiResponse(
        data=DubbingAudioListData(
            items=items,
            total=len(items),
            # create=False：列清单不该顺手把目录建出来（只读接口别写盘）
            dir=str(dubbing_library.dubbing_dir(create=False)),
        )
    )


@router.get(
    "/audios/{name}/file",
    summary="音频流（支持 Range）",
)
def get_audio_file(
    request: Request,
    name: str = PathParam(..., description="产物文件名（裸文件名，不含目录）"),
) -> Response:
    """在线播放配音产物。安全模型与混剪一致：只收文件名，路径由服务端在固定
    目录下拼，前端传 ../ 之类的一律 400。"""
    path = dubbing_library.resolve_audio(name)
    if path is None:
        raise NotFoundError(f"这份配音不存在或已被删除：{name}")
    return ranged_file_response(
        path,
        request.headers.get("range"),
        media_type=dubbing_library.media_type_for(path.name),
    )


@router.delete(
    "/audios/{name}",
    response_model=ApiResponse[dict],
    summary="删除一份配音产物",
)
def delete_audio(
    name: str = PathParam(..., description="产物文件名（裸文件名，不含目录）"),
) -> ApiResponse[dict]:
    """连同索引条目一起删。与「删记录不删文件」的任务约定相反 —— 这里删的
    对象本来就是文件本身，用户点删除就是要它从磁盘消失。"""
    if not dubbing_library.delete_audio(name):
        raise NotFoundError(f"这份配音不存在或已被删除：{name}")
    logger.info("配音产物已删除：%s", name)
    return ApiResponse(data={"name": name})
