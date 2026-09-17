"""services 模块的单元测试。"""

import pytest

from cw.services import (
    BACKEND_DEFAULT_PORT,
    build_services,
    check_proxy_consistency,
    find_root,
    full_start_order,
)


@pytest.fixture(scope="module")
def root():
    return find_root()


@pytest.fixture(scope="module")
def specs(root):
    return build_services(root, ("127.0.0.1", 8000))


class TestFindRoot:
    def test_locates_repo_root(self, root):
        assert (root / "backend").is_dir()
        assert (root / "frontend").is_dir()
        assert (root / "desktop").is_dir()
        assert (root / "cli").is_dir()

    def test_root_contains_wrapper_script(self, root):
        assert (root / "cw").is_file()


class TestBuildServices:
    def test_three_services(self, specs):
        assert set(specs) == {"backend", "frontend", "desktop"}

    def test_backend_uses_venv_python_not_uv_run(self, specs):
        """直接用 venv 解释器，少一层进程且不需要联网校验环境。"""
        argv = specs["backend"].argv
        assert argv[0].endswith("python") or argv[0].endswith("python.exe")
        assert ".venv" in argv[0]
        assert "uv" not in argv

    def test_backend_passes_host_and_port_explicitly(self, specs):
        """不显式传的话 uvicorn 会用自带默认值，不读 .env 的 HOST/PORT。"""
        argv = specs["backend"].argv
        assert "--host" in argv
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert "--port" in argv
        assert argv[argv.index("--port") + 1] == "8000"

    def test_backend_port_follows_bind(self, root):
        """bind 端口变了，命令行和展示值都要跟着变。"""
        specs = build_services(root, ("127.0.0.1", 9123))
        argv = specs["backend"].argv
        assert argv[argv.index("--port") + 1] == "9123"
        assert specs["backend"].expected_port == 9123
        assert "9123" in specs["backend"].guidance

    def test_backend_cwd_is_backend_dir(self, root, specs):
        """cwd 是硬约束：DATABASE_URL 是相对路径，换了目录会新建空数据库。"""
        assert specs["backend"].cwd == root / "backend"
        assert specs["backend"].cwd.is_dir()

    def test_wildcard_host_displays_as_loopback(self, root):
        specs = build_services(root, ("0.0.0.0", 8000))
        # 命令行里仍然绑通配地址，但展示给用户的应该是可点的回环地址
        argv = specs["backend"].argv
        assert argv[argv.index("--host") + 1] == "0.0.0.0"
        assert "127.0.0.1:8000" in specs["backend"].guidance

    def test_frontend_and_desktop_use_npm(self, specs):
        assert specs["frontend"].argv == ("npm", "run", "dev")
        assert specs["desktop"].argv == ("npm", "run", "dev")

    def test_desktop_has_no_expected_port(self, specs):
        assert specs["desktop"].expected_port is None

    def test_all_cwd_exist(self, specs):
        for spec in specs.values():
            assert spec.cwd.is_dir(), f"{spec.id} 的工作目录不存在：{spec.cwd}"

    def test_backend_has_health_path(self, specs):
        assert specs["backend"].health_path == "/api/v1/health"
        assert specs["frontend"].health_path is None

    def test_every_service_has_label_and_guidance(self, specs):
        for spec in specs.values():
            assert spec.label
            assert spec.guidance


class TestFullStartOrder:
    def test_order_is_backend_frontend_desktop(self, specs):
        """前端要拿到约定俗成的 5173，所以必须排在同样会抢它的桌面端前面。"""
        ordered = [spec.id for spec in full_start_order(specs)]
        assert ordered == ["backend", "frontend", "desktop"]


class TestCheckProxyConsistency:
    def test_matching_port_produces_no_warning(self, root):
        assert check_proxy_consistency(root, BACKEND_DEFAULT_PORT) is None

    def test_mismatched_port_warns_and_names_the_file(self, root):
        """改后端端口后前端会静默失败，这条告警是唯一的线索。"""
        warning = check_proxy_consistency(root, 8001)
        assert warning is not None
        assert "8001" in warning
        assert "vite.config.ts" in warning
        assert "8000" in warning

    def test_missing_config_returns_none(self, tmp_path):
        assert check_proxy_consistency(tmp_path, 8001) is None

    def test_unparsable_config_returns_none(self, tmp_path):
        """读不出目标时当作无法判断，而不是崩溃或误报。"""
        config = tmp_path / "frontend" / "vite.config.ts"
        config.parent.mkdir(parents=True)
        config.write_text("export default {}  // 没有 proxy 配置\n", encoding="utf-8")
        assert check_proxy_consistency(tmp_path, 8001) is None

    def test_detects_mismatch_in_synthetic_config(self, tmp_path):
        config = tmp_path / "frontend" / "vite.config.ts"
        config.parent.mkdir(parents=True)
        config.write_text(
            "server: {\n  proxy: {\n    '/api': { target: 'http://127.0.0.1:7000' }\n  }\n}\n",
            encoding="utf-8",
        )
        warning = check_proxy_consistency(tmp_path, 8000)
        assert warning is not None
        assert "7000" in warning
