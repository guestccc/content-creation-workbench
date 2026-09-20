"""任务表共用的列。

六个任务域（镜头分割 / 字幕提取 / 混剪 / 素材抓取 / 一键成品的文案与合成）
各有一套自己的状态常量与进度字段 —— 那些**故意**不共享，跨功能引用会把它们
的演进焊死在一起。但「备注」是纯粹的用户标记，六个域没有任何差异，所以放在
这里共用，不做六份逐字拷贝（与 schemas/common.py 里 JobBatchDeleteRequest 的
取舍同理）。

这里放的是 mixin 而不是模型：它没有自己的表，因此**不需要**在 app/models/__init__.py
里导出 —— 那个文件的约定是「所有模型必须在此导出，否则 Base.metadata 收集不全」，
而 mixin 的列会随继承它的模型一起被收集。
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class JobRemarkMixin:
    """任务备注：用户给这条任务起的标记。

    各任务表原本没有 name / title 之类的标识字段，同一条输入反复跑几遍就会攒出
    几条长得一模一样的记录，用户没法标记「哪条是给客户 A 的那版」，备注就是补
    这个缺口的。

    长度取 200，与 Creator / CrawlCookie 的备注列以及前端编辑弹窗的上限一致。
    """

    remark: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", comment="备注（用户自己看的标记）"
    )
