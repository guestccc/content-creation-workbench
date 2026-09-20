"""智能配音（Voicebox）接口。

路由注册顺序：/environment、/profiles、/generations、/audios 等静态路径
必须写在 /generations/{generation_id}、/audios/{name} 这类动态路径之前。

接口一览：
- GET    /environment                    Voicebox 服务环境自检（含安装指引）
- PUT    /environment/base-url           手动指定 Voicebox 服务地址（写进 backend/.env）
- PUT    /environment/hf-mirror          一键设置模型下载镜像（写用户级环境变量）
- DELETE /environment/hf-mirror          清除下载镜像、恢复官方默认源
- POST   /environment/restart            重启 Voicebox 桌面端（不等就绪，前端轮询）
- GET    /models                         能用来配音的模型清单（含下载状态）
- GET    /profiles                       音色列表（透传上游；建音色在 Voicebox GUI 里做）
- POST   /generations                    提交一次配音生成（入队后立即返回，生成在 worker 里跑）
- GET    /generations/{generation_id}    单次生成的进度（前端轮询它）
- GET    /audios                         配音产物清单（materials/dubbing/ 扫盘 + 索引）
- GET    /audios/{name}/file             音频流（支持 Range，<audio> 标签直接放）
- DELETE /audios/{name}                  删除一份配音产物（文件 + 索引条目）
"""

from typing import NoReturn

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import Response

from app.core.config import default_voicebox_base_url, settings
from app.core.exceptions import AppException, BadRequestError, NotFoundError
from app.core.logging import get_logger
from app.schemas.common import ApiResponse
from app.schemas.voicebox import (
    LANGUAGE_OPTIONS,
    DubbingAudioListData,
    DubbingGenerationCreate,
    DubbingGenerationResponse,
    DubbingModelItem,
    DubbingModelListData,
    VoiceProfileItem,
    VoiceProfileListData,
    VoiceboxBaseUrlUpdate,
    VoiceboxEnvironmentResponse,
    VoiceboxRestartResponse,
)
from app.services import (
    dubbing_library,
    user_env,
    voicebox_client,
    voicebox_env,
    voicebox_generation,
    voicebox_mirror,
    voicebox_models,
    voicebox_restart,
    voicebox_settings,
)
from app.services.file_range import ranged_file_response
from app.services.voicebox_client import VoiceboxError

router = APIRouter(prefix="/voicebox", tags=["智能配音"])
logger = get_logger(__name__)


def _environment_response() -> ApiResponse[VoiceboxEnvironmentResponse]:
    """清掉探测缓存、重新自检，并包成响应。

    三个写接口（指定地址 / 设镜像 / 清镜像）统一用它返回**最新**的自检结果，
    前端拿到就能直接 `setData`，省掉一次 GET —— 与 PUT /environment/base-url
    一直以来的形状一致。
    """
    voicebox_env.reset_cache()
    env = voicebox_env.probe_environment(refresh=True)
    return ApiResponse(data=VoiceboxEnvironmentResponse(**env))


def _assert_local_voicebox() -> None:
    """「设置镜像」「重启桌面端」只对本机的 Voicebox 有意义。

    地址指向远程 GPU 机器时这两个动作作用不到它（环境变量设在本机用户会话里、
    进程也是本机的进程），所以当场拒绝并说清楚该去哪台机器上做 —— 比给一个
    点了没反应的按钮强。
    """
    if not voicebox_settings.is_local_base_url():
        raise BadRequestError(
            f"当前服务地址是远程的（{settings.VOICEBOX_BASE_URL}），本机改不了它："
            "请在跑 Voicebox 的那台机器上设置下载源 / 重启它。"
        )


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

    response = _environment_response()
    warning = voicebox_settings.shadowing_warning()
    if warning:
        response.data.warnings.append(warning)
    return response


# --------------------------------------------------------------------------
# 模型下载源（一键设置 / 清除）
# --------------------------------------------------------------------------


def _raise_user_env_error(exc: user_env.UserEnvError) -> NoReturn:
    """把 `user_env.UserEnvError` 翻成 HTTP。

    - 系统不支持（Linux）→ 400：请求本身没问题，是这台机器做不到，
      用户需要的是「换怎么做的说明」，不是「服务器挂了」；
    - 其它（写盘失败、验证不过）→ 500：确实是我们这边的故障。
    """
    if not user_env.supported():
        raise BadRequestError(exc.user_message) from exc
    raise AppException(exc.user_message, code="USER_ENV_FAILED", status_code=500) from exc


@router.put(
    "/environment/hf-mirror",
    response_model=ApiResponse[VoiceboxEnvironmentResponse],
    summary="设置模型下载镜像",
)
def enable_hf_mirror() -> ApiResponse[VoiceboxEnvironmentResponse]:
    """把模型下载源换成镜像（写**用户级**环境变量，不是 .env）。

    为什么不能写 .env：那是我方后端的配置，Voicebox 是另一个进程；也不能写
    `~/.zshrc`：macOS 上 GUI 应用由 LaunchServices 拉起，不继承 shell 环境。
    只有用户级环境变量（macOS 的 launchd 会话 / Windows 的注册表）这一层，
    桌面应用才拿得到 —— 具体实现见 services/user_env.py。

    **幂等**，重复调用不报错。设完必须重启 Voicebox 才生效（环境变量只对之后
    启动的进程有效），所以响应里会带上「还没重启」的提示，页面据此引导用户点
    「重启 Voicebox」。
    """
    _assert_local_voicebox()
    try:
        mirror = voicebox_mirror.enable()
    except user_env.UserEnvError as exc:
        _raise_user_env_error(exc)
    logger.info(
        "模型下载源已设为 %s | 值=%s | 持久化=%s",
        voicebox_mirror.HF_MIRROR_URL,
        mirror.value,
        mirror.persistent,
    )
    return _environment_response()


@router.delete(
    "/environment/hf-mirror",
    response_model=ApiResponse[VoiceboxEnvironmentResponse],
    summary="清除模型下载镜像",
)
def disable_hf_mirror() -> ApiResponse[VoiceboxEnvironmentResponse]:
    """清除下载源、恢复 HuggingFace 官方默认（幂等）。

    留着这个入口是为了可逆：给用户设了的东西，页面上就得有地方撤销。
    """
    _assert_local_voicebox()
    try:
        voicebox_mirror.disable()
    except user_env.UserEnvError as exc:
        _raise_user_env_error(exc)
    logger.info("模型下载源已清除，恢复 huggingface.co 默认")
    return _environment_response()


# --------------------------------------------------------------------------
# 重启桌面端
# --------------------------------------------------------------------------


@router.post(
    "/environment/restart",
    response_model=ApiResponse[VoiceboxRestartResponse],
    summary="重启 Voicebox 桌面端",
)
def restart_voicebox() -> ApiResponse[VoiceboxRestartResponse]:
    """重启 Voicebox，让它捡起刚设置的下载源。**不等就绪就返回。**

    为什么不等：冷启动到 /health 可用约 30 秒，而前端 fetch 15 秒超时 —— 同步等
    必然撞超时，用户会以为重启失败。就绪交给前端轮询 GET /environment（与切割 /
    字幕 / 生成同一套「异步跑、靠轮询」）。

    重启会连**残留的 voicebox-server 进程**一起清掉：只退启动器的话，新拉起的
    启动器会复用那个还在跑的旧服务，新环境变量对它无效 —— 那不叫重启。

    Raises:
        BadRequestError: 服务地址是远程的（本机重启不了它）。
        NotFoundError: 没找到 Voicebox 的安装位置。
    """
    _assert_local_voicebox()
    try:
        result = voicebox_restart.restart()
    except voicebox_restart.VoiceboxNotFoundError as exc:
        raise NotFoundError(exc.user_message) from exc
    except voicebox_restart.RestartError as exc:
        raise AppException(
            exc.user_message, code="VOICEBOX_RESTART_FAILED", status_code=500
        ) from exc

    logger.info(
        "Voicebox 已重启 | 安装位置=%s | 清掉的残留进程=%s",
        result.app_path,
        result.killed_pids,
    )
    # 清缓存：刚重启完 reachable 必然是 False，缓存里那份可能还是重启前的
    voicebox_env.reset_cache()
    return ApiResponse(
        data=VoiceboxRestartResponse(
            started=True,
            app_path=result.app_path,
            detail=result.detail,
            wait_hint="Voicebox 正在启动，一般 30 秒左右就绪，本页会自动刷新。",
        )
    )


# --------------------------------------------------------------------------
# 模型（只读：下载 / 删除在 Voicebox 自己的界面里做）
# --------------------------------------------------------------------------


@router.get(
    "/models",
    response_model=ApiResponse[DubbingModelListData],
    summary="配音可选模型清单",
)
def get_models() -> ApiResponse[DubbingModelListData]:
    """页面上那个模型下拉的候选，顺带带上每个的下载状态。

    为什么不让前端自己拼这份列表：上游 /models/status 是**全部**模型（还混着
    whisper 转写、qwen3 大模型），而且它给的是扁平的 model_name，`/generate`
    要的却是 (engine, model_size) 两个参数 —— 这层对照只有后端知道（见
    services/voicebox_models.py 的 CATALOG）。模型清单跟着上游版本走，写死在
    前端就等于每个 Voicebox 版本都要跟着发一次前端。

    只读，**不代下载**：模型在 Voicebox 里下（首次生成时它自己会下）。页面按
    `downloaded` 标注，让用户知道选中的那个要不要等下载。

    Raises:
        BadRequestError: 服务连不上 / 上游报错（错误信息已翻成用户可读的话）。
    """
    try:
        items = voicebox_models.list_models()
    except VoiceboxError as exc:
        raise BadRequestError(exc.user_message) from exc
    return ApiResponse(
        data=DubbingModelListData(
            items=[DubbingModelItem(**item) for item in items],
            total=len(items),
        )
    )


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
    if payload.language not in LANGUAGE_OPTIONS:
        raise BadRequestError(f"语言只支持 {' / '.join(LANGUAGE_OPTIONS)}：{payload.language}")
    # 模型按目录校验：engine 与 model_size 合起来指一个具体模型，只认目录里的组合
    # （下拉就是照目录渲染的，能选的一定能过，过不了一律是绕过前端来的请求）
    if voicebox_models.find(payload.engine, payload.model_size) is None:
        raise BadRequestError(
            f"不支持的模型：engine={payload.engine} model_size={payload.model_size}。"
            "可选项见 GET /api/v1/voicebox/models"
        )

    record = voicebox_generation.submit(
        text,
        profile_id=payload.profile_id.strip(),
        profile_name=payload.profile_name.strip(),
        filename=payload.filename.strip(),
        language=payload.language,
        engine=payload.engine,
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
