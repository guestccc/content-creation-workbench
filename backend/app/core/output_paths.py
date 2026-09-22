"""产物路径分配：一批输入 → 一批互不覆盖的输出路径。

这段逻辑原本长在 `subtitle_job_service.allocate_output_paths` 里。一键换背景
要做一模一样的事（同一批原图各出一个文件、重名的往后找编号、预建输出目录），
命名规则不同但**编号规则完全一样** —— 再抄一份就意味着「重名加 -2」这个规则
将来会在两个地方各自演化。

所以把编号规则抽到这里，命名规则由调用方用一个 `namer` 回调传进来：

    allocate_unique_paths(videos, out_dir, lambda src, n: f"{src.stem}{suffix}")
    allocate_unique_paths(images, out_dir, lambda src, n: f"{src.stem}-换背景.png")

`subtitle_job_service` 保留同名薄封装，调用方与测试都不必改。
"""

from pathlib import Path
from typing import Callable, Dict, List

from app.core.exceptions import BadRequestError

#: namer(source, count) -> 文件名。count 从 1 开始，1 是不带后缀的那个。
PathNamer = Callable[[Path, int], str]


def allocate_unique_paths(
    sources: List[Path], output_dir: Path, namer: PathNamer
) -> Dict[Path, Path]:
    """为每个输入分配一个输出路径，重名的（磁盘上已有的、或同批次撞车的）加 -2 / -3。

    这里**允许**目标目录里已经有同名文件：产物是「一条输入一份」的东西，用户
    很可能分批处理同一条素材（换个参数再跑一次）。所以不报错，改成换个名字，
    谁都不覆盖谁。

    预建输出目录（而不是等到真正写文件时）：不少下游工具按「完整文件路径」
    接收输出参数，自己不会创建父目录 —— 目录必须在这里就位。

    ⚠️ 本函数只**分配**路径、不创建文件，所以「同批次内撞车」不能靠
    `exists()` 发现（那一轮文件还没写出来），必须另记一份已分配名单。

    Args:
        sources: 输入路径列表。
        output_dir: 产物目录，不存在会被建出来。
        namer: 命名规则，`namer(source, count)` 返回文件名。count 从 1 开始递增，
            调用方据此决定第一个用不用带编号。

    Returns:
        输入路径 → 输出路径的映射（与 sources 同序）。

    Raises:
        BadRequestError: 输出目录建不出来（权限、只读盘），或 namer 返回了
            带路径分隔符的名字（那就不再是「输出目录下的一个文件」了）。
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BadRequestError(f"创建输出目录失败：{output_dir}（{exc}）") from exc

    allocation: Dict[Path, Path] = {}
    #: 输入基名 → 下一个该试的编号。同批次内重名（recursive 模式下不同子目录
    #: 可能有同名文件）靠它接着往后找，而不是每次都从 1 重新撞一遍。
    next_count: Dict[str, int] = {}
    #: 本批次已经分出去的文件名 —— 磁盘上还没有它们，exists() 看不见。
    taken: set[str] = set()

    for source in sources:
        base_name = source.stem
        count = next_count.get(base_name, 1)

        while True:
            name = namer(source, count)
            if not name or "/" in name or "\\" in name or name in (".", ".."):
                raise BadRequestError(f"产物名不合法：{name!r}（不能带路径分隔符）")
            candidate = output_dir / name
            if name not in taken and not candidate.exists():
                break
            count += 1

        taken.add(candidate.name)
        next_count[base_name] = count + 1
        allocation[source] = candidate

    return allocation
