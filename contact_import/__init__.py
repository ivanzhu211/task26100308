"""联系人批量导入库。"""
from .importer import Importer, normalize_identity
from .types import (
    BatchAlreadyTerminalError,
    BatchCancelledError,
    BatchError,
    BatchNotFoundError,
    BatchStatus,
    ContactRow,
    ImportResult,
    RowCategory,
    RowResult,
)

__all__ = [
    "Importer",
    "normalize_identity",
    "BatchAlreadyTerminalError",
    "BatchCancelledError",
    "BatchError",
    "BatchNotFoundError",
    "BatchStatus",
    "ContactRow",
    "ImportResult",
    "RowCategory",
    "RowResult",
]
