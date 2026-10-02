"""对外 API：批量导入与取消。"""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Tuple, Union

from .models import (
    BatchConflictError,
    BatchResult,
    BatchStatus,
    ContactImportError,
    ContactRow,
    RowClassification,
    RowResult,
)
from .store import Store, normalize_identity

RowLike = Union[ContactRow, Tuple[str, str]]


def _coerce_row(raw: RowLike) -> ContactRow:
    if isinstance(raw, ContactRow):
        return raw
    email, name = raw
    return ContactRow(email=email, name=name)


class ContactImporter:
    """联系人批量导入库入口，由上层调用。

    联系人与批次状态保存在调用方指定的 SQLite 文件中。
    """

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def import_batch(
        self,
        batch_id: str,
        list_id: str,
        rows: Iterable[RowLike],
    ) -> BatchResult:
        """导入一个批次。

        行流异常或取消成功时，该批次不留任何联系人，也不影响其他批次。
        """
        self._begin_import(batch_id, list_id)

        collected: List[ContactRow] = []
        iterator = iter(rows)
        while True:
            status = self._store.get_status(batch_id)
            if status is BatchStatus.CANCELLED:
                return self._finish_import(batch_id, list_id, BatchStatus.CANCELLED)
            try:
                raw = next(iterator)
            except StopIteration:
                break
            except BaseException:
                return self._finish_import(batch_id, list_id, BatchStatus.FAILED)
            collected.append(_coerce_row(raw))

        return self._publish(batch_id, list_id, collected)

    def cancel_batch(self, batch_id: str) -> BatchResult:
        """取消批次。

        未开始的批次登记为已取消（之后不能再发布）；导入中的批次标记取消；
        已成功批次返回既有成功结果；终态不可改写。
        """
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT list_id, status FROM batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO batches (batch_id, list_id, status, finished_at)"
                    " VALUES (?, '', ?, datetime('now'))",
                    (batch_id, BatchStatus.CANCELLED.value),
                )
                return BatchResult(batch_id, "", BatchStatus.CANCELLED)
            list_id, status = row[0], BatchStatus(row[1])
            if status is BatchStatus.IMPORTING:
                conn.execute(
                    "UPDATE batches SET status = ?, finished_at = datetime('now')"
                    " WHERE batch_id = ? AND status = ?",
                    (BatchStatus.CANCELLED.value, batch_id, BatchStatus.IMPORTING.value),
                )
                return BatchResult(batch_id, list_id, BatchStatus.CANCELLED)
            return self._load_result(conn, batch_id)

    def get_result(self, batch_id: str) -> BatchResult:
        """查询批次当前结果。"""
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise ContactImportError(f"unknown batch: {batch_id!r}")
            return self._load_result(conn, batch_id)

    # ---- 内部实现 ----

    def _begin_import(self, batch_id: str, list_id: str) -> None:
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO batches (batch_id, list_id, status) VALUES (?, ?, ?)",
                    (batch_id, list_id, BatchStatus.IMPORTING.value),
                )
        except sqlite3.IntegrityError:
            status = self._store.get_status(batch_id)
            raise BatchConflictError(
                f"batch {batch_id!r} already exists with status {status.value}"
            ) from None

    def _finish_import(
        self, batch_id: str, list_id: str, final: BatchStatus
    ) -> BatchResult:
        """非成功终态落库；若已被并发取消则保留取消态。"""
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE batches SET status = ?, finished_at = datetime('now')"
                " WHERE batch_id = ? AND status = ?",
                (final.value, batch_id, BatchStatus.IMPORTING.value),
            )
            return self._load_result(conn, batch_id)

    def _publish(
        self, batch_id: str, list_id: str, rows: List[ContactRow]
    ) -> BatchResult:
        """单事务发布：联系人、逐行分类、成功状态同时可见。"""
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if current is None or current[0] != BatchStatus.IMPORTING.value:
                return self._load_result(conn, batch_id)

            results: List[RowResult] = []
            batch_seen: dict[str, int] = {}
            for index, row in enumerate(rows):
                identity = normalize_identity(row.email)
                if identity in batch_seen:
                    contact_id = batch_seen[identity]
                    classification = RowClassification.DUPLICATE_IN_BATCH
                else:
                    existing = conn.execute(
                        "SELECT contact_id FROM contacts"
                        " WHERE list_id = ? AND identity = ?",
                        (list_id, identity),
                    ).fetchone()
                    if existing is not None:
                        contact_id = existing[0]
                        classification = RowClassification.DUPLICATE_IN_LIST
                    else:
                        cursor = conn.execute(
                            "INSERT INTO contacts"
                            " (list_id, identity, email, name, batch_id)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (list_id, identity, row.email.strip(), row.name, batch_id),
                        )
                        contact_id = cursor.lastrowid
                        classification = RowClassification.NEW
                    batch_seen[identity] = contact_id
                results.append(
                    RowResult(
                        row_index=index,
                        email=row.email,
                        name=row.name,
                        classification=classification,
                        contact_id=contact_id,
                    )
                )

            conn.executemany(
                "INSERT INTO batch_rows"
                " (batch_id, row_index, email, name, classification, contact_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (batch_id, r.row_index, r.email, r.name,
                     r.classification.value, r.contact_id)
                    for r in results
                ],
            )
            conn.execute(
                "UPDATE batches SET status = ?, finished_at = datetime('now')"
                " WHERE batch_id = ?",
                (BatchStatus.SUCCEEDED.value, batch_id),
            )
            return BatchResult(batch_id, list_id, BatchStatus.SUCCEEDED, results)

    def _load_result(self, conn: sqlite3.Connection, batch_id: str) -> BatchResult:
        row = conn.execute(
            "SELECT list_id, status FROM batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise ContactImportError(f"unknown batch: {batch_id!r}")
        list_id, status = row[0], BatchStatus(row[1])
        rows: List[RowResult] = []
        if status is BatchStatus.SUCCEEDED:
            rows = [
                RowResult(
                    row_index=r[0],
                    email=r[1],
                    name=r[2],
                    classification=RowClassification(r[3]),
                    contact_id=r[4],
                )
                for r in conn.execute(
                    "SELECT row_index, email, name, classification, contact_id"
                    " FROM batch_rows WHERE batch_id = ? ORDER BY row_index",
                    (batch_id,),
                )
            ]
        return BatchResult(batch_id, list_id, status, rows)
