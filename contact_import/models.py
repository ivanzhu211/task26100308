"""数据类型定义：批次状态、行分类、结果对象。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List, Optional


class BatchStatus(str, enum.Enum):
    IMPORTING = "importing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            BatchStatus.SUCCEEDED,
            BatchStatus.FAILED,
            BatchStatus.CANCELLED,
        )


class RowClassification(str, enum.Enum):
    NEW = "new"                      # 新增联系人
    DUPLICATE_IN_BATCH = "dup_batch"  # 批内重复
    DUPLICATE_IN_LIST = "dup_list"    # 名单重复


@dataclass(frozen=True)
class ContactRow:
    """调用方提供的单行联系人资料。"""
    email: str
    name: str


@dataclass(frozen=True)
class RowResult:
    """成功批次中每一行的最终分类与联系人归属。"""
    row_index: int
    email: str
    name: str
    classification: RowClassification
    contact_id: int


@dataclass(frozen=True)
class BatchResult:
    """批次终态结果；仅成功批次带有逐行分类。"""
    batch_id: str
    list_id: str
    status: BatchStatus
    rows: List[RowResult] = field(default_factory=list)


class ContactImportError(Exception):
    """库级异常基类。"""


class BatchConflictError(ContactImportError):
    """批次号已存在且处于不可再发布的终态，或正在导入中。"""
