"""删除任务产物文件的公共实现（best-effort）。

四个任务域的「删记录时顺便清产物」共用这一个函数。两条规则：

1. **只删任务记录里登记的产物路径**，不接受外部传入的路径 —— 与缩略图/
   播放接口同一套安全模型（防任意文件删除）；
2. **best-effort**：文件被占用、已被用户挪走这类失败只记日志，不阻断
   记录删除（记录先提交，产物清理是善后，失败留在磁盘上等用户手工清）。
"""

import shutil
from pathlib import Path
from typing import Iterable, List

from app.core.logging import get_logger

logger = get_logger(__name__)


def remove_paths_best_effort(paths: Iterable[Path]) -> List[Path]:
    """删除一组文件/目录（目录整棵删），单个失败只告警、继续下一个。

    Args:
        paths: 产物路径清单（文件或目录；不存在的直接跳过）。

    Returns:
        删除失败的路径清单；全成功时为空列表。
    """
    failed: List[Path] = []
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("清理任务产物失败 | %s | %s", path, exc)
            failed.append(path)
    return failed
