"""导入器：批量导入与取消的对外入口。"""
from __future__ import annotations

from typing import Iterable, Optional

from .store import Store
from .types import (
    BatchAlreadyTerminalError,
    BatchCancelledError,
    BatchNotFoundError,
    BatchStatus,
    ContactRow,
    ImportResult,
)

_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def normalize_identity(email: str) -> str:
    """去首尾空格，ASCII 字母转小写；不折叠点号或加号别名。"""
    return email.strip().translate(_ASCII_LOWER)


class Importer:
    """对指定 SQLite 文件执行批次导入与取消。"""

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "Importer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def import_batch(
        self,
        batch_id: str,
        list_id: str,
        rows: Iterable[ContactRow],
    ) -> ImportResult:
        """消费行流并原子发布。

        行流抛异常时整批置为失败且不留任何数据；发布期间被并发取消
        则整批不生效。成功时返回每行的最终分类与联系人归属。
        """
        self._store.begin_batch(batch_id, list_id)
        seen: set[str] = set()
        pending = []
        try:
            for index, row in enumerate(rows):
                status = self._store.get_batch_status(batch_id)
                if status is BatchStatus.CANCELLED:
                    raise BatchCancelledError(f"批次 {batch_id} 已被取消")
                identity = normalize_identity(row.email)
                is_candidate = identity not in seen
                seen.add(identity)
                pending.append((index, identity, row.email, row.name, is_candidate))
        except BatchCancelledError:
            raise
        except Exception:
            self._store.finalize_batch(batch_id, BatchStatus.FAILED)
            raise
        return self._store.publish_batch(batch_id, list_id, pending)

    def cancel(self, batch_id: str) -> Optional[ImportResult]:
        """取消批次。

        进行中批次：置为已取消并返回 None，此后不能再发布。
        已成功批次：返回既有成功结果，不撤销其成果。
        失败/已取消批次：终态不可改写，抛出异常。
        """
        status = self._store.get_batch_status(batch_id)
        if status is None:
            raise BatchNotFoundError(f"批次 {batch_id} 不存在")
        if status is BatchStatus.SUCCEEDED:
            return self._store.load_result(batch_id)
        if status is BatchStatus.RUNNING:
            self._store.finalize_batch(batch_id, BatchStatus.CANCELLED)
            return None
        raise BatchAlreadyTerminalError(f"批次 {batch_id} 已终态（{status.value}），不可改写")
