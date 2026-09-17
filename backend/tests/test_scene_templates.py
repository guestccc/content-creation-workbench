"""镜头分割模板与参数解析的单元测试。

模板是前端展示的参数真源，这里守住两条底线：
1. 每个模板的参数都落在合法区间内（防止改模板时手滑写错阈值）；
2. resolve_params 的覆盖语义符合预期（显式参数 > 模板值 > 标准默认值）。
"""

from app.core.scene_templates import (
    CUSTOM_TEMPLATE_KEY,
    DETECTORS,
    MAX_MIN_LEN,
    MAX_THRESHOLD,
    MIN_MIN_LEN,
    MIN_THRESHOLD,
    TEMPLATE_MAP,
    TEMPLATES,
    get_template,
    resolve_params,
)


class TestTemplates:
    """模板定义本身。"""

    def test_keys_unique(self):
        """模板 key 不允许重复，否则前端选中态会串。"""
        keys = [item.key for item in TEMPLATES]
        assert len(keys) == len(set(keys))

    def test_params_in_legal_range(self):
        """每个模板的检测器、阈值、最短镜头都必须在合法区间。"""
        for item in TEMPLATES:
            assert item.detector in DETECTORS, item.key
            if item.threshold is not None:
                assert MIN_THRESHOLD <= item.threshold <= MAX_THRESHOLD, item.key
            assert MIN_MIN_LEN <= item.min_len <= MAX_MIN_LEN, item.key

    def test_exactly_one_recommended(self):
        """推荐模板有且仅有一个，前端据此加默认选中态。"""
        assert sum(1 for item in TEMPLATES if item.recommended) == 1

    def test_template_map_consistent(self):
        """TEMPLATE_MAP 与 TEMPLATES 一一对应。"""
        assert set(TEMPLATE_MAP) == {item.key for item in TEMPLATES}
        assert get_template("standard") is TEMPLATE_MAP["standard"]
        assert get_template("不存在的key") is None


class TestResolveParams:
    """模板 + 显式覆盖 → 最终参数。"""

    def test_builtin_template_uses_template_values(self):
        """选中内置模板时直接采用模板参数。"""
        params = resolve_params("coarse")
        assert params == {
            "detector": "content",
            "threshold": 35.0,
            "min_len": 2.0,
            "copy": False,
        }

    def test_explicit_overrides_win_over_template(self):
        """显式传入的字段覆盖模板值（前端切模板后又微调的场景）。"""
        params = resolve_params("coarse", threshold=22.5, copy_mode=True)
        assert params["threshold"] == 22.5
        assert params["copy"] is True
        # 未覆盖的字段保留模板值
        assert params["detector"] == "content"
        assert params["min_len"] == 2.0

    def test_custom_falls_back_to_standard_defaults(self):
        """自定义/空模板时，缺项回落到标准切分的默认值。"""
        for key in (None, CUSTOM_TEMPLATE_KEY):
            params = resolve_params(key)
            assert params["detector"] == "adaptive"
            assert params["threshold"] is None
            assert params["min_len"] == 0.6
            assert params["copy"] is False

    def test_custom_with_full_overrides(self):
        """自定义模板 + 全部显式参数 = 完全按显式参数来。"""
        params = resolve_params(
            CUSTOM_TEMPLATE_KEY,
            detector="threshold",
            threshold=15.0,
            min_len=1.2,
            copy_mode=True,
        )
        assert params == {
            "detector": "threshold",
            "threshold": 15.0,
            "min_len": 1.2,
            "copy": True,
        }
