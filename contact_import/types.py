"""公共类型定义：批次状态、行分类、结果对象。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class BatchStatus(enum.Enum):
    """批次状态。终态不可改写；只有 SUCCEEDED 批次具有最终分类和联系人归属。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not BatchStatus.RUNNING


class RowCategory(enum.Enum):
    """每行的最终分类。"""

    NEW = "new"                       # 新增联系人
    DUPLICATE_IN_BATCH = "dup_batch"  # 批内重复（同批次同邮箱非首行）
    DUPLICATE_IN_LIST = "dup_list"    # 名单重复（发布时已存在）


@dataclass(frozen=True)
class ContactRow:
    """调用方提供的单行联系人资料。"""

    email: str
    name: str


@dataclass(frozen=True)
class RowResult:
    """单行最终结果：分类与归属的联系人标识。"""

    row_index: int
    category: RowCategory
    contact_id: int


@dataclass(frozen=True)
class ImportResult:
    """成功批次的不可变结果。"""

    batch_id: str
    list_id: str
    rows: tuple[RowResult, ...] = field(default_factory=tuple)


class BatchError(Exception):
    """批次相关错误的基类。"""


class BatchNotFoundError(BatchError):
    """批次不存在。"""


class BatchAlreadyTerminalError(BatchError):
    """批次已处于终态，不可改写。"""


class BatchCancelledError(BatchError):
    """批次已取消，不能再发布。"""
