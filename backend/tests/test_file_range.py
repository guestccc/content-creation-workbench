"""HTTP Range 解析与文件响应的单元测试。

播放视频能不能拖进度条全看这个：Range 头解析错一个边界，浏览器就是
「拖到 30 秒结果从头开始放」。所以三种形态、各种坏输入、越界都要钉死。

响应体的断言走 TestClient 发真请求（见 tests/test_scene_jobs.py 的
TestClipVideo）：StreamingResponse 的 body_iterator 是异步的，FileResponse
的 Content-Length 更是要等到真正发响应时才填，直接戳响应对象只能拿到半个
真相，不如把整个 HTTP 栈跑一遍。
"""

from pathlib import Path

import pytest

from app.services.file_range import RangeNotSatisfiable, parse_range_header


class TestParseRangeHeader:
    """三种合法形态 + 该拒的都要拒。"""

    def test_open_ended(self):
        assert parse_range_header("bytes=100-", 1000) == (100, 999)

    def test_closed_range(self):
        assert parse_range_header("bytes=0-99", 1000) == (0, 99)

    def test_suffix(self):
        """bytes=-100：最后 100 个字节。"""
        assert parse_range_header("bytes=-100", 1000) == (900, 999)

    def test_end_is_clamped_to_file_size(self):
        """浏览器经常发 end 很大的请求（比如 bytes=0- 的变体），裁到文件尾。"""
        assert parse_range_header("bytes=900-5000", 1000) == (900, 999)

    def test_suffix_longer_than_file_gives_whole_file(self):
        assert parse_range_header("bytes=-5000", 1000) == (0, 999)

    def test_exact_last_byte(self):
        assert parse_range_header("bytes=999-", 1000) == (999, 999)

    @pytest.mark.parametrize("header", [
        "bytes=1000-",     # 起点已在文件外
        "bytes=200-100",   # 终点在起点前面
        "bytes=0-1,3-4",   # 多段：不支持
        "items=0-99",      # 单位不是 bytes
        "bytes=abc-",      # 不是数字
        "bytes=",          # 空
        "",                # 整个是空的
        "bytes=1",         # 没有连字符
        "bytes=-0",        # 要「最后 0 个字节」没有意义
    ])
    def test_rejected(self, header):
        with pytest.raises(RangeNotSatisfiable):
            parse_range_header(header, 1000)

    def test_empty_file_rejects_everything(self):
        """空文件没有可服务的字节。"""
        with pytest.raises(RangeNotSatisfiable):
            parse_range_header("bytes=0-", 0)
