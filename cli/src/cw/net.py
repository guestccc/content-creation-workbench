"""地址相关的小工具。

单独放一个模块，是因为「把监听地址转成可连接地址」这件事同时被展示层
（拼 url_hint）和探测层（发 HTTP）需要，放在任何一边都会造成多余依赖。
"""

# 通配监听地址到回环地址的映射。
# 服务监听 0.0.0.0 / :: 时不能拿它当连接目标：macOS 上 http://0.0.0.0:8000
# 虽然连得上，但语义混乱，写进日志和提示里也会让人困惑。
_WILDCARD_HOSTS = {
    "0.0.0.0": "127.0.0.1",
    "::": "::1",
    "[::]": "::1",
}


def normalize_probe_host(host: str) -> str:
    """把监听地址转成可以用来连接/展示的地址。"""
    stripped = host.strip()
    return _WILDCARD_HOSTS.get(stripped, stripped or "127.0.0.1")


def format_url(host: str, port: int, path: str = "") -> str:
    """拼一个展示用的 URL。

    IPv6 字面量需要方括号，否则 http://::1:8000 是非法 URL。
    """
    display_host = normalize_probe_host(host)
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}{path}"
