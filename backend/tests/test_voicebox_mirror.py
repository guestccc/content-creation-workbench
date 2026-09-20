"""模型下载源策略层（services/voicebox_mirror.py）的测试。

这一层没有平台分支，只有「设哪个键、设成什么、现在算不算已生效」—— 所以这里
把 `user_env` 整个换成假的（一个 dict 就是「用户级环境变量」），只验策略。
`user_env` 自己的读写正确性由 tests/test_user_env.py 负责，两边不重复。
"""

import pytest

from app.services import user_env, voicebox_mirror


class FakeUserEnv:
    """假的 user_env 模块：一个 dict 就是「用户级环境变量」。

    `UserEnvError` 直接借真的：`voicebox_mirror` 的 except 子句认的就是这个类，
    换掉它测的就不是真实边界了。
    """

    UserEnvError = user_env.UserEnvError

    def __init__(
        self,
        *,
        supported: bool = True,
        persistent: bool = True,
        fail_read: bool = False,
        fail_write: bool = False,
    ) -> None:
        self.vars = {}
        self._supported = supported
        self._persistent = persistent
        self.fail_read = fail_read
        self.fail_write = fail_write

    def supported(self) -> bool:
        return self._supported

    def read_state(self, key: str) -> user_env.UserEnvValue:
        if self.fail_read:
            raise user_env.UserEnvError("读取系统环境变量失败")
        return user_env.UserEnvValue(
            value=self.vars.get(key), persistent=self._persistent
        )

    def set_value(self, key: str, value: str) -> None:
        if self.fail_write:
            raise user_env.UserEnvError("写入失败")
        self.vars[key] = value

    def clear_value(self, key: str) -> None:
        if self.fail_write:
            raise user_env.UserEnvError("清除失败")
        self.vars.pop(key, None)


@pytest.fixture()
def fake_env(monkeypatch):
    fake = FakeUserEnv()
    monkeypatch.setattr(voicebox_mirror, "user_env", fake)
    return fake


class TestConstants:
    def test_key_is_hf_endpoint(self):
        """HuggingFace 全家（transformers / huggingface_hub / diffusers）都认它。"""
        assert voicebox_mirror.HF_MIRROR_KEY == "HF_ENDPOINT"

    def test_mirror_stays_huggingface_api_compatible(self):
        """**别换成 ModelScope**：它更快，但 API 形状不是 HuggingFace 那套，
        设成 HF_ENDPOINT 会让 huggingface_hub 直接解析失败。

        这条断言是给「顺手换一个更快的源」这个改动设的路障。
        """
        assert "modelscope" not in voicebox_mirror.HF_MIRROR_URL
        assert voicebox_mirror.HF_MIRROR_URL.startswith("https://")


class TestEnableDisable:
    def test_enable_sets_the_recommended_value(self, fake_env):
        status = voicebox_mirror.enable()

        assert fake_env.vars["HF_ENDPOINT"] == voicebox_mirror.HF_MIRROR_URL
        assert status.supported is True
        assert status.value == voicebox_mirror.HF_MIRROR_URL
        assert status.is_recommended is True
        assert status.persistent is True
        assert status.error == ""

    def test_enable_is_idempotent(self, fake_env):
        """用户多点两下按钮不该报错，也不该攒出第二条 LaunchAgent。"""
        first = voicebox_mirror.enable()
        second = voicebox_mirror.enable()

        assert first.value == second.value == voicebox_mirror.HF_MIRROR_URL

    def test_disable_clears_the_value(self, fake_env):
        voicebox_mirror.enable()

        status = voicebox_mirror.disable()

        assert "HF_ENDPOINT" not in fake_env.vars
        assert status.value is None
        assert status.is_recommended is False

    def test_disable_is_idempotent(self, fake_env):
        """本来就没设过时点「清除镜像」不该报错（缓存清干净、自检里也会调它）。"""
        status = voicebox_mirror.disable()

        assert status.value is None

    def test_write_failure_propagates(self, fake_env):
        """写不进去必须抛出来 —— 报成功的代价是用户半小时后撞上「模型还是下不动」。"""
        fake_env.fail_write = True

        with pytest.raises(user_env.UserEnvError):
            voicebox_mirror.enable()


class TestIsRecommended:
    @pytest.mark.parametrize(
        "value",
        [
            "https://hf-api.gitee.com",
            "https://hf-api.gitee.com/",  # 手敲时爱多打一个斜杠
            "HTTPS://HF-API.GITEE.COM",  # 大小写
            "  https://hf-api.gitee.com/  ",  # 粘贴带上的空白
        ],
    )
    def test_tolerates_slash_case_and_space(self, value):
        """同一个东西的几种写法不该被显示成「未设置镜像」反复劝用户再点一次。"""
        assert voicebox_mirror.is_recommended_value(value) is True

    @pytest.mark.parametrize(
        "value", ["https://hf-mirror.com", "https://hf-api.gitee.com.cn", "", None]
    )
    def test_other_values_are_not_recommended(self, value):
        assert voicebox_mirror.is_recommended_value(value) is False

    def test_is_customized_means_someone_else_set_it(self):
        """用户自己设的源要能认出来（提醒一句就好，**不覆盖**）。"""
        assert voicebox_mirror.is_customized("https://hf-mirror.com") is True
        assert voicebox_mirror.is_customized("https://hf-api.gitee.com") is False
        assert voicebox_mirror.is_customized("") is False
        assert voicebox_mirror.is_customized(None) is False
        assert voicebox_mirror.is_customized("   ") is False


class TestStatusNeverRaises:
    """自检里读不到环境变量不该让整个自检崩掉 —— 页面只是少一条信息。"""

    def test_unsupported_platform_degrades(self, fake_env):
        fake_env._supported = False

        status = voicebox_mirror.status()

        assert status.supported is False
        assert status.value is None
        assert status.error == ""

    def test_unsupported_current_value_is_none(self, fake_env):
        fake_env._supported = False

        assert voicebox_mirror.current_value() is None

    def test_read_failure_becomes_an_error_field(self, fake_env):
        fake_env.fail_read = True

        status = voicebox_mirror.status()

        assert status.supported is True
        assert status.value is None
        assert "读取" in status.error
        assert status.is_recommended is False

    def test_read_failure_does_not_block_restart(self, fake_env):
        """重启路径**不能被读环境变量挡住**：读不到就按「没设镜像」处理。"""
        fake_env.fail_read = True

        assert voicebox_mirror.current_value() is None

    def test_current_value_returns_what_is_set(self, fake_env):
        fake_env.vars["HF_ENDPOINT"] = "https://hf-mirror.com"

        assert voicebox_mirror.current_value() == "https://hf-mirror.com"

    def test_not_persistent_is_reported(self, fake_env):
        """「设了但没持久化」要如实报出来：注销或重启电脑后它就没了。"""
        fake_env._persistent = False
        fake_env.vars["HF_ENDPOINT"] = "https://hf-api.gitee.com"

        status = voicebox_mirror.status()

        assert status.persistent is False
        assert status.is_recommended is True


class TestHostOf:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("https://hf-mirror.com/", "hf-mirror.com"),
            ("https://hf-api.gitee.com", "hf-api.gitee.com"),
            ("http://127.0.0.1:8080/v1", "127.0.0.1"),
            ("", ""),
            (None, ""),
            ("不是个地址", "不是个地址"),  # 解析不出来就原样返回，文案里不能出现空白
        ],
    )
    def test_extracts_host(self, value, expected):
        assert voicebox_mirror.host_of(value) == expected
