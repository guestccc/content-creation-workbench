"""带 HTTP Range 支持的文件响应。

浏览器播视频（拖动进度条、边下边播）靠的就是 Range 请求：
``Range: bytes=1024-`` 过来，服务端回 ``206 Partial Content`` 和对应字节段。
Starlette 的 ``FileResponse`` 不认识 Range，收到也会整文件从头发 ——
大文件下浏览器拖进度条就会卡住转圈，所以这里自己实现。

只做 **GET、单段** Range：多段（``bytes=0-1,3-4``）返回 416。
浏览器播放视频不会发多段请求，真需要再说。

单元测试在 tests/test_file_range.py。
"""

from pathlib import Path
from typing import Optional, Tuple

from fastapi import Response
from fastapi.responses import FileResponse, StreamingResponse

#: 流式读文件的块大小：64KB，够平滑又不会把响应憋太久才起头
_CHUNK_SIZE = 64 * 1024

#: 标准的状态短语，写进 416 的 Content-Range 响应里方便排查
_MEDIA_TYPE_FALLBACK = "application/octet-stream"


class RangeNotSatisfiable(Exception):
    """Range 头语法没错，但要的区间落在文件外（或区间为空）。"""

    def __init__(self, size: int) -> None:
        super().__init__(f"range not satisfiable, size={size}")
        self.size = size


def parse_range_header(header: str, size: int) -> Tuple[int, int]:
    """把 ``Range: bytes=...`` 解析成闭区间 (start, end)，含两端。

    支持三种形态（RFC 7233 里单段 Range 的全部形态）：

    - ``bytes=0-99``   → (0, 99)
    - ``bytes=100-``   → (100, size-1)
    - ``bytes=-100``   → 最后 100 字节

    Args:
        header: Range 头的原始值（不含 "Range:" 本身）。
        size: 文件总字节数。

    Returns:
        (start, end)，均已按文件大小裁剪（end 最多 size-1）。

    Raises:
        RangeNotSatisfiable: 语法不对、多段、或区间落在文件外。
    """
    if size <= 0:
        # 空文件没有可服务的字节，任何 Range 都不可满足
        raise RangeNotSatisfiable(size)

    if not header.startswith("bytes="):
        raise RangeNotSatisfiable(size)
    spec = header[len("bytes="):].strip()
    # 多段 Range 不支持：播放视频用不到，真遇到说明是别的客户端在试探
    if "," in spec:
        raise RangeNotSatisfiable(size)

    start_s, sep, end_s = spec.partition("-")
    if not sep:
        raise RangeNotSatisfiable(size)

    try:
        if start_s == "":
            # bytes=-N：最后 N 个字节
            length = int(end_s)
            if length <= 0:
                raise RangeNotSatisfiable(size)
            start = max(size - length, 0)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else size - 1
            if start < 0 or end < start:
                raise RangeNotSatisfiable(size)
    except ValueError:
        raise RangeNotSatisfiable(size) from None

    if start >= size:
        raise RangeNotSatisfiable(size)
    return start, min(end, size - 1)


def ranged_file_response(
    path: Path,
    range_header: Optional[str],
    *,
    media_type: Optional[str] = None,
    filename: Optional[str] = None,
) -> Response:
    """按 Range 头返回整文件（200）或字节段（206）。

    Args:
        path: 要发出去的文件（调用方负责确认它存在）。
        range_header: 请求里的 Range 头；没有就是普通的整文件下载。
        media_type: Content-Type；None 时按扩展名猜。
        filename: 可选，整文件下载时带上 Content-Disposition 文件名。

    Returns:
        200 FileResponse / 206 StreamingResponse / 416 空响应。
    """
    size = path.stat().st_size
    base_headers = {
        # 即使本次没带 Range 也要声明支持：浏览器据此决定要不要发分段请求
        "Accept-Ranges": "bytes",
    }

    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            headers=base_headers,
        )

    try:
        start, end = parse_range_header(range_header, size)
    except RangeNotSatisfiable as exc:
        # 416 必须带 Content-Range: */size，客户端才知道文件到底多大
        return Response(
            status_code=416,
            headers={
                **base_headers,
                "Content-Range": f"bytes */{exc.size}",
            },
        )

    def stream():
        """按块吐出 [start, end] 区间；迭代器内打开文件，连接断开时随之关闭。"""
        with path.open("rb") as fp:
            fp.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fp.read(min(_CHUNK_SIZE, remaining))
                if not chunk:  # 读途中文件被外部截短：提前收尾，不补零
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        stream(),
        status_code=206,
        media_type=media_type or _MEDIA_TYPE_FALLBACK,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        },
    )
