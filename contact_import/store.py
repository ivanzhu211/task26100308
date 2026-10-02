"""SQLite 持久层：批次、联系人、行结果的存储与事务。"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional, Sequence

from .types import (
    BatchAlreadyTerminalError,
    BatchCancelledError,
    BatchError,
    BatchNotFoundError,
    BatchStatus,
    ImportResult,
    RowCategory,
    RowResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    list_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS contacts (
    contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id    TEXT NOT NULL,
    identity   TEXT NOT NULL,
    email      TEXT NOT NULL,
    name       TEXT NOT NULL,
    batch_id   TEXT NOT NULL,
    UNIQUE (list_id, identity)
);
CREATE TABLE IF NOT EXISTS row_results (
    batch_id    TEXT NOT NULL,
    row_index   INTEGER NOT NULL,
    category    TEXT NOT NULL,
    contact_id  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, row_index)
);
"""

# 待发布行：(行号, 去重身份, 原始邮箱, 姓名, 是否批内首行)
PendingRow = tuple[int, str, str, str, bool]


class Store:
    """封装调用方指定的 SQLite 文件。所有方法线程安全。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_batch_status(self, batch_id: str) -> Optional[BatchStatus]:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return BatchStatus(row[0]) if row else None

    def begin_batch(self, batch_id: str, list_id: str) -> None:
        """登记新批次；批次号已存在时按其状态抛出对应错误。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO batches (batch_id, list_id, status) VALUES (?, ?, ?)",
                    (batch_id, list_id, BatchStatus.RUNNING.value),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                status = self.get_batch_status(batch_id)
                assert status is not None
                raise self._terminal_error(batch_id, status) from None

    @staticmethod
    def _terminal_error(batch_id: str, status: BatchStatus) -> BatchError:
        if status is BatchStatus.CANCELLED:
            return BatchCancelledError(f"批次 {batch_id} 已取消，不能再发布")
        if status is BatchStatus.RUNNING:
            return BatchError(f"批次 {batch_id} 正在导入中")
        return BatchAlreadyTerminalError(f"批次 {batch_id} 已终态（{status.value}），不可改写")

    def find_contact_id(self, list_id: str, identity: str) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT contact_id FROM contacts WHERE list_id = ? AND identity = ?",
                (list_id, identity),
            ).fetchone()
        return row[0] if row else None

    def publish_batch(
        self,
        batch_id: str,
        list_id: str,
        rows: Sequence[PendingRow],
    ) -> ImportResult:
        """单事务内完成分类、写联系人、写行结果并置批次成功。

        候选行在发布时刻判定：名单已存在则名单重复，否则新增。
        未发布的批次对其他批次不可见，因为联系人只在发布时写入。
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                status = self.get_batch_status(batch_id)
                if status is None:
                    raise BatchNotFoundError(f"批次 {batch_id} 不存在")
                if status is not BatchStatus.RUNNING:
                    raise self._terminal_error(batch_id, status)

                identity_map: dict[str, tuple[int, RowCategory]] = {}
                results: list[RowResult] = []
                for row_index, identity, email, name, is_candidate in rows:
                    if not is_candidate:
                        contact_id = identity_map[identity][0]
                        category = RowCategory.DUPLICATE_IN_BATCH
                    else:
                        existing = self.find_contact_id(list_id, identity)
                        if existing is not None:
                            contact_id = existing
                            category = RowCategory.DUPLICATE_IN_LIST
                        else:
                            cur = self._conn.execute(
                                "INSERT INTO contacts (list_id, identity, email, name, batch_id)"
                                " VALUES (?, ?, ?, ?, ?)",
                                (list_id, identity, email, name, batch_id),
                            )
                            contact_id = cur.lastrowid
                            category = RowCategory.NEW
                        identity_map[identity] = (contact_id, category)
                    results.append(RowResult(row_index, category, contact_id))

                self._conn.executemany(
                    "INSERT INTO row_results (batch_id, row_index, category, contact_id)"
                    " VALUES (?, ?, ?, ?)",
                    [
                        (batch_id, r.row_index, r.category.value, r.contact_id)
                        for r in results
                    ],
                )
                self._conn.execute(
                    "UPDATE batches SET status = ? WHERE batch_id = ?",
                    (BatchStatus.SUCCEEDED.value, batch_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        results.sort(key=lambda r: r.row_index)
        return ImportResult(batch_id=batch_id, list_id=list_id, rows=tuple(results))

    def finalize_batch(self, batch_id: str, status: BatchStatus) -> None:
        """将进行中的批次置为终态（FAILED/CANCELLED）。终态不可改写。"""
        assert status.is_terminal and status is not BatchStatus.SUCCEEDED
        with self._lock:
            cur = self._conn.execute(
                "UPDATE batches SET status = ? WHERE batch_id = ? AND status = ?",
                (status.value, batch_id, BatchStatus.RUNNING.value),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                current = self.get_batch_status(batch_id)
                if current is None:
                    raise BatchNotFoundError(f"批次 {batch_id} 不存在")
                raise self._terminal_error(batch_id, current)

    def load_result(self, batch_id: str) -> ImportResult:
        """读取成功批次的既有结果。"""
        with self._lock:
            batch = self._conn.execute(
                "SELECT list_id, status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise BatchNotFoundError(f"批次 {batch_id} 不存在")
            list_id, status = batch
            if BatchStatus(status) is not BatchStatus.SUCCEEDED:
                raise BatchError(f"批次 {batch_id} 未成功，没有最终分类")
            rows = self._conn.execute(
                "SELECT row_index, category, contact_id FROM row_results"
                " WHERE batch_id = ? ORDER BY row_index",
                (batch_id,),
            ).fetchall()
        return ImportResult(
            batch_id=batch_id,
            list_id=list_id,
            rows=tuple(
                RowResult(idx, RowCategory(cat), cid) for idx, cat, cid in rows
            ),
        )
