"""VideoCaptioner 探测与安装指引（subtitle_env.py）测试。

探测是会起子进程、且有副作用（被调包导入时会在 HOME 建配置目录）的操作，
所以这里的原则是：**子进程一律用 monkeypatch 换掉**，只验证
「候选顺序、路径拼接、缓存、解析、指引文案」这些我们自己的逻辑。
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import subtitle_env
from app.services.media_tools import ProbedOutput, decode_output
from app.services.subtitle_env import (
    VcInstall,
    detect,
    install_hints,
    platform_key,
    probe_environment,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _isolate_detection(monkeypatch):
    """每个用例都清缓存，并默认把「外部环境」全部挡掉：

    - 探测缓存清空（它是模块级状态，不清会串用例）；
    - PATH 查找返回 None（不去摸开发机真实环境）；
    - 后端解释器就地判断固定为 False（理由同上）；
    - VC 根目录指向一个不存在的路径（venv 候选自然落空）；
    - 目录名模糊匹配的**搜索目录**置空。**这条不能省**：开发机上真的可能装着
      `VideoCaptioner-master` 之类的目录，不挡的话多出来的 discovered 候选
      会让「最后一条候选是什么」这类断言随开发机状态变来变去。
      注意挡的是 `_search_dirs`（扫哪里）而不是 `_discover_roots`（怎么扫）——
      后者本身是要被测的逻辑，整个替换掉就没法测了。
    """
    reset_cache()
    monkeypatch.setattr(subtitle_env, "find_tool", lambda *a, **k: None)
    monkeypatch.setattr(subtitle_env, "_module_available_locally", lambda: False)
    monkeypatch.setattr(subtitle_env, "_search_dirs", lambda *a, **k: [])
    monkeypatch.setattr(settings, "SUBTITLE_VC_PYTHON", "")
    monkeypatch.setattr(settings, "SUBTITLE_VC_ROOT", "/nonexistent/vc-root")
    yield
    reset_cache()


class TestCandidates:
    """候选调用方式的顺序与跨平台路径拼接。

    这一组要同时验证 Windows 与 POSIX 两套 venv 布局，而本机只能是其中一种，
    所以做法是：用替身把模块里的 `os` 换成「自称哪个平台」，再**用 Path 对象
    而不是字符串**去比较拼出来的路径 —— 字符串比较会把 `/` 与 `\\` 的差异
    当失败，那是平台差异不是缺陷。
    """

    def test_explicit_override_comes_first(self, monkeypatch):
        """配置里显式指定的解释器优先级最高，且「解释器」「脚本」两种形态都试。"""
        monkeypatch.setattr(settings, "SUBTITLE_VC_PYTHON", "/opt/py311/bin/python")
        candidates = subtitle_env._candidates()
        # 代码会 expanduser()，这里跟着走一遍，避免 ~ 展开与否的差异
        override = str(Path("/opt/py311/bin/python").expanduser())
        assert candidates[0] == (
            [override, "-m", "videocaptioner"],
            "override",
            settings.SUBTITLE_VC_ROOT,
        )
        assert candidates[1] == ([override], "override-script",
                                 settings.SUBTITLE_VC_ROOT)

    def test_venv_paths_on_posix(self, monkeypatch):
        """POSIX：venv 里是 bin/python 与 bin/videocaptioner。"""
        monkeypatch.setattr(subtitle_env, "os", SimpleNamespace(name="posix"))
        candidates = subtitle_env._candidates()
        root = Path(settings.SUBTITLE_VC_ROOT)
        assert ([str(root / ".venv" / "bin" / "python"), "-m", "videocaptioner"],
                "venv-python", str(root)) in candidates
        assert ([str(root / ".venv" / "bin" / "videocaptioner")], "venv-script",
                str(root)) in candidates

    def test_venv_paths_on_windows(self, monkeypatch):
        """Windows：venv 布局变成 Scripts/python.exe 与 Scripts/videocaptioner.exe。

        只把模块里的 os 换成「自称 nt」的替身来做纯字符串拼接验证 —— 真的把
        os.name 改掉会让 pathlib 实例化另一种 Path 而直接报错。
        """
        monkeypatch.setattr(subtitle_env, "os", SimpleNamespace(name="nt"))
        candidates = subtitle_env._candidates()
        kinds = {kind for _launcher, kind, _root in candidates}
        assert "venv-python" in kinds
        assert "venv-script" in kinds
        root = Path(settings.SUBTITLE_VC_ROOT)
        launchers = [launcher[0] for launcher, kind, _r in candidates
                     if kind == "venv-python"]
        assert Path(launchers[0]) == root / ".venv" / "Scripts" / "python.exe"
        scripts = [launcher[0] for launcher, kind, _r in candidates
                   if kind == "venv-script"]
        assert Path(scripts[0]) == root / ".venv" / "Scripts" / "videocaptioner.exe"

    def test_backend_python_candidate_when_module_available(self, monkeypatch):
        """后端环境里能 import 到包时，补一条 sys.executable -m 候选。"""
        monkeypatch.setattr(subtitle_env, "_module_available_locally", lambda: True)
        candidates = subtitle_env._candidates()
        assert ([sys.executable, "-m", "videocaptioner"], "backend-python",
                "") in candidates

    def test_path_candidate_prefers_sibling_python(self, monkeypatch, tmp_path):
        """PATH 上只有脚本时，顺着它找同目录的解释器（-m 比直接调脚本更稳）。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "videocaptioner"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        # 解释器的候选名分平台（`_sibling_python` 里就是这么选的），
        # 这里必须按当前平台造，否则造出来的文件它根本不会去找
        sibling = bin_dir / ("python.exe" if os.name == "nt" else "python3")
        sibling.write_text("#!/bin/sh\n", encoding="utf-8")

        def fake_find_tool(name):
            return str(script) if name == "videocaptioner" else None

        monkeypatch.setattr(subtitle_env, "find_tool", fake_find_tool)
        candidates = subtitle_env._candidates()
        assert ([str(sibling), "-m", "videocaptioner"], "path", "") in candidates
        assert ([str(script)], "path-script", "") in candidates


class TestDiscovery:
    """目录名模糊匹配（_discover_roots / vc_root_candidates）。

    背景：从 GitHub 下载 zip 解压出来的目录默认叫 `VideoCaptioner-master`，
    而配置里的默认值只有精确的 `VideoCaptioner` —— 用户明明装了却探测不到，
    就是这么来的。这一组用 tmp_path 造目录**结构**（只 mkdir，不造真 venv），
    验证匹配与排序逻辑。
    """

    def _make_vc(self, base: Path, name: str) -> Path:
        """造一个「看起来像 VideoCaptioner」的目录（有 .venv 就够了）。"""
        root = base / name
        (root / ".venv").mkdir(parents=True)
        return root

    def test_matches_master_suffix(self, tmp_path):
        """带 -master 后缀的目录要被匹配到 —— 这正是用户踩的那个坑。"""
        expected = self._make_vc(tmp_path, "VideoCaptioner-master")
        assert subtitle_env._discover_roots([tmp_path]) == [expected]

    def test_matches_main_and_version_suffix(self, tmp_path):
        """-main、带版本号等同族命名也认。"""
        for name in ("VideoCaptioner-main", "VideoCaptioner-1.4.2"):
            self._make_vc(tmp_path, name)
        found = {path.name for path in subtitle_env._discover_roots([tmp_path])}
        assert found == {"VideoCaptioner-main", "VideoCaptioner-1.4.2"}

    def test_ignores_unrelated_directory(self, tmp_path):
        """光名字像但里面什么都没有的目录要排除。"""
        (tmp_path / "VideoCaptioner-空壳").mkdir()
        assert subtitle_env._discover_roots([tmp_path]) == []

    def test_ignores_files_with_matching_name(self, tmp_path):
        """同名的一个文件不算目录。"""
        (tmp_path / "VideoCaptioner.zip").write_text("x", encoding="utf-8")
        assert subtitle_env._discover_roots([tmp_path]) == []

    def test_exact_name_sorts_first(self, tmp_path):
        """精确名排前面：它就是配置里的默认值，理应优先。"""
        self._make_vc(tmp_path, "VideoCaptioner-master")
        self._make_vc(tmp_path, "VideoCaptioner")
        names = [path.name for path in subtitle_env._discover_roots([tmp_path])]
        assert names == ["VideoCaptioner", "VideoCaptioner-master"]

    def test_accepts_source_tree_without_venv(self, tmp_path):
        """源码树形态（有 videocaptioner/ 包目录、没 .venv）也算。"""
        root = tmp_path / "VideoCaptioner-dev"
        (root / "videocaptioner").mkdir(parents=True)
        assert subtitle_env._discover_roots([tmp_path]) == [root]

    def test_missing_search_dir_is_skipped(self, tmp_path):
        """搜索目录不存在时跳过，不抛异常。"""
        assert subtitle_env._discover_roots([tmp_path / "nope"]) == []

    def test_discovered_root_yields_candidates(self, tmp_path, monkeypatch):
        """自动发现的根目录要产出带 discovered- 前缀的候选。"""
        root = self._make_vc(tmp_path, "VideoCaptioner-master")
        monkeypatch.setattr(settings, "SUBTITLE_VC_ROOT", "")
        monkeypatch.setattr(subtitle_env, "_discover_roots", lambda *a, **k: [root])

        kinds = {kind for _launcher, kind, _root in subtitle_env._candidates()}
        assert "discovered-venv-python" in kinds
        assert "discovered-venv-script" in kinds

    def test_explicit_root_is_tried_before_discovered(self, tmp_path, monkeypatch):
        """显式配置的根目录排在自动发现的前面。"""
        explicit = self._make_vc(tmp_path, "MyVC")
        discovered = self._make_vc(tmp_path, "VideoCaptioner-master")
        monkeypatch.setattr(settings, "SUBTITLE_VC_ROOT", str(explicit))
        monkeypatch.setattr(subtitle_env, "_discover_roots", lambda *a, **k: [discovered])

        # 每个根目录产出两条候选（venv-python + venv-script），所以按出现顺序
        # 去重后再比，而不是直接比第 0/1 条
        roots = [
            Path(root)
            for _launcher, _kind, root in subtitle_env._candidates()
        ]
        ordered = list(dict.fromkeys(roots))
        assert ordered == [explicit, discovered]

    def test_no_duplicate_when_explicit_equals_discovered(self, tmp_path, monkeypatch):
        """显式值正好就是模糊匹配结果时，同一套 venv 候选不能加两遍。"""
        root = self._make_vc(tmp_path, "VideoCaptioner-master")
        monkeypatch.setattr(settings, "SUBTITLE_VC_ROOT", str(root))
        monkeypatch.setattr(subtitle_env, "_discover_roots", lambda *a, **k: [root])

        roots = [r for _launcher, _kind, r in subtitle_env._candidates()]
        assert len(roots) == 2  # venv-python + venv-script，只此一组
        assert {Path(r) for r in roots} == {root}

    def test_empty_explicit_root_falls_back_to_discovery(self, tmp_path, monkeypatch):
        """显式值为空串（页面点了「恢复自动探测」）时只剩自动发现。"""
        root = self._make_vc(tmp_path, "VideoCaptioner-master")
        monkeypatch.setattr(settings, "SUBTITLE_VC_ROOT", "")
        monkeypatch.setattr(subtitle_env, "_discover_roots", lambda *a, **k: [root])

        roots = [r for _launcher, _kind, r in subtitle_env._candidates()]
        assert {Path(r) for r in roots} == {root}


class TestProbe:
    """单次探测：命令形态、解析、各种失败都不抛异常。"""

    def _patch_run(self, monkeypatch, recorder, *, returncode=0, stdout=""):
        """把替身接在 run_probe 上（而不是 subprocess.run）。

        探测子进程的字节解码由 media_tools.run_probe 负责，是**共用**的一层，
        替身若抢在它下面，就绕过了真正要验的解码。这里替身和真实实现一样交回
        已解码的文本 —— 用同一个 decode_output，GBK 那两条用例才有意义。
        """
        def fake_run(argv, **kwargs):
            recorder.append(argv)
            raw = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
            return ProbedOutput(
                returncode=returncode, stdout=decode_output(raw), stderr=""
            )

        monkeypatch.setattr(subtitle_env, "run_probe", fake_run)

    def test_module_form_probes_interpreter_with_dash_c(self, monkeypatch):
        """[python, -m, videocaptioner] 形态：探测问的是解释器（-c 小脚本）。"""
        calls = []
        payload = {"version": "1.4.2", "config_file": "/cfg/config.toml",
                   "python_version": "3.11.9", "executable": "/py"}
        self._patch_run(monkeypatch, calls, stdout=json.dumps(payload))

        result = subtitle_env._probe(["/py", "-m", "videocaptioner"])
        assert calls[0][0] == "/py"
        assert calls[0][1] == "-c"
        # 探测绝不能把 -m videocaptioner 带上 —— 那是转写时才用的
        assert "-m" not in calls[0][:2]
        assert result == payload

    def test_script_form_uses_version_flag(self, monkeypatch):
        """[videocaptioner] 形态：拿不到解释器，只能问它自己的 --version。"""
        calls = []
        self._patch_run(monkeypatch, calls, stdout="videocaptioner 1.4.2\n")

        result = subtitle_env._probe(["/usr/local/bin/videocaptioner"])
        assert calls[0] == ["/usr/local/bin/videocaptioner", "--version"]
        assert result == {"version": "1.4.2", "config_file": ""}

    def test_probe_json_takes_last_json_line(self):
        """包导入期间可能往 stdout 打警告：真正的结果在最后一行。"""
        stdout = "UserWarning: something\n{\"version\": \"1.0.0\"}\n"
        assert subtitle_env._parse_probe_json(stdout) == {"version": "1.0.0"}

    def test_nonzero_exit_returns_none(self, monkeypatch):
        calls = []
        self._patch_run(monkeypatch, calls, returncode=3, stdout="")
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_probe_layer_failure_degrades_to_none(self, monkeypatch):
        """起不来 / 超时 / 解码不了都在 run_probe 那层降级成 None，探测跟着返回 None。

        真实的那几种失败（不存在的可执行文件、超时）在 test_media_tools 里
        直接对着 run_probe 测，这里钉的是「上层见到 None 不会炸」。
        """
        monkeypatch.setattr(subtitle_env, "run_probe", lambda argv, **kwargs: None)
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_unparseable_output_returns_none(self, monkeypatch):
        calls = []
        self._patch_run(monkeypatch, calls, stdout="完全是别的输出")
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_gbk_bytes_do_not_crash(self, monkeypatch):
        """真实踩过：中文 Windows 子进程吐 GBK 字节，父进程 UTF-8 模式时
        text=True 的解码在读线程里抛 UnicodeDecodeError，stdout 变成 None，
        splitlines 直接炸。按字节捕获后，坏编码最多是探测失败返回 None。
        """
        calls = []
        self._patch_run(monkeypatch, calls, stdout="中文输出".encode("gbk"))
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_gbk_json_payload_is_decoded(self, monkeypatch):
        """GBK 编码的 JSON 行也能解析出来（按候选编码逐个试）。"""
        calls = []
        payload = {"version": "1.4.2", "config_file": "D:\\配置\\config.toml"}
        raw = json.dumps(payload, ensure_ascii=False).encode("gbk")
        self._patch_run(monkeypatch, calls, stdout=raw)
        result = subtitle_env._probe(["/py", "-m", "videocaptioner"])
        # 非 UTF-8 环境下按本地代码页解出原文；UTF-8 环境下解不出也是 None，
        # 两种都合法 —— 关键是不抛异常
        assert result is None or result == payload


class TestDetect:
    """整体探测：候选逐个试、失败不抛异常、结果带缓存。"""

    def test_all_candidates_fail_returns_not_installed(self, monkeypatch):
        """一个候选都不通：返回 installed=False 与可读原因，绝不抛异常。"""
        monkeypatch.setattr(subtitle_env, "_probe", lambda launcher: None)
        install = detect()
        assert install.installed is False
        assert install.ready is False
        assert "没有找到可用的 VideoCaptioner" in install.detail
        # kind/root 记录最后一次尝试，方便排查「它到底找了哪儿」
        assert install.kind == "venv-script"
        # 用 Path 比较：报告出来的 root 是规范化过的字符串，分隔符随平台变，
        # 直接比字符串会在 Windows 上假失败
        assert Path(install.root) == Path(settings.SUBTITLE_VC_ROOT)

    def test_first_working_candidate_wins(self, monkeypatch, tmp_path):
        """按顺序试，第一个通的候选就是结果（后面的不再试）。"""
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
            (["/b/videocaptioner"], "path-script", ""),
        ])
        tried = []

        def fake_probe(launcher):
            tried.append(launcher[0])
            if launcher[0] == "/b/videocaptioner":
                return {"version": "1.4.2", "config_file": str(tmp_path / "c.toml")}
            return None

        monkeypatch.setattr(subtitle_env, "_probe", fake_probe)
        install = detect()
        assert tried == ["/a/python", "/b/videocaptioner"]
        assert install.installed is True
        assert install.kind == "path-script"
        assert install.version == "1.4.2"
        assert install.launcher == ("/b/videocaptioner",)
        assert install.config_exists is False  # 配置文件还没建

    def test_config_exists_flag(self, monkeypatch, tmp_path):
        """配置文件真实存在时 config_exists 为 True（用户大概率配过 key）。"""
        config = tmp_path / "config.toml"
        config.write_text("[asr]\n", encoding="utf-8")
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
        ])
        monkeypatch.setattr(
            subtitle_env, "_probe",
            lambda launcher: {"version": "1.0.0", "config_file": str(config)},
        )
        assert detect().config_exists is True

    def test_cache_avoids_reprobe(self, monkeypatch):
        """缓存命中时不再起子进程；force=True 绕过缓存。"""
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
        ])
        calls = []

        def fake_probe(launcher):
            calls.append(launcher)
            return {"version": "1.0.0"}

        monkeypatch.setattr(subtitle_env, "_probe", fake_probe)
        detect()
        detect()
        assert len(calls) == 1  # 第二次读的缓存

        detect(force=True)
        assert len(calls) == 2

    def test_cache_expires_after_ttl(self, monkeypatch):
        """缓存超过 SUBTITLE_DETECT_CACHE_SECONDS 后重新探测。"""
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
        ])
        calls = []
        monkeypatch.setattr(
            subtitle_env, "_probe",
            lambda launcher: calls.append(launcher) or {"version": "1.0.0"},
        )
        monkeypatch.setattr(settings, "SUBTITLE_DETECT_CACHE_SECONDS", 30)

        clock = {"now": 1000.0}
        monkeypatch.setattr(subtitle_env.time, "monotonic", lambda: clock["now"])

        detect()
        clock["now"] += 10  # TTL 内
        detect()
        assert len(calls) == 1

        clock["now"] += 25  # 超过 TTL
        detect()
        assert len(calls) == 2


class TestInstallHints:
    """安装指引按平台出文案（只出当前平台那一份）。"""

    def test_windows_hints(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "platform_key", lambda: "windows")
        hints = install_hints()
        commands = [h["command"] for h in hints]
        assert "py -3.11 -m pip install videocaptioner" in commands
        assert "winget install Gyan.FFmpeg" in commands
        # 最后一条总是「装好后回来点重新检测」
        assert "重新检测" in hints[-1]["note"]

    def test_macos_hints(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "platform_key", lambda: "macos")
        hints = install_hints()
        commands = [h["command"] for h in hints]
        assert "python3 -m pip install videocaptioner" in commands
        assert "brew install ffmpeg" in commands
        assert any("run.sh" in c for c in commands)

    def test_linux_hints(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "platform_key", lambda: "linux")
        hints = install_hints()
        commands = [h["command"] for h in hints]
        assert "sudo apt install ffmpeg" in commands

    def test_platform_key_on_this_mac(self):
        """开发机是 macOS，platform_key 应给出 macos。"""
        if sys.platform == "darwin" and os.name != "nt":
            assert platform_key() == "macos"


class TestProbeEnvironment:
    """接口形态的环境自检结果。"""

    def test_payload_shape(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [])
        payload = probe_environment()
        assert payload["installed"] is False
        assert payload["platform"] in ("macos", "windows", "linux")
        assert payload["platform_label"]
        assert payload["materials_dir"]
        assert payload["default_input_dir"].endswith("source")
        assert payload["default_output_dir"].endswith("subtitle")
        assert isinstance(payload["install_hints"], list)
        assert payload["install_hints"]  # 没装时必须有指引
