"""SQLite 持久化层：表结构与事务辅助。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from .models import BatchStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    list_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
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

CREATE TABLE IF NOT EXISTS batch_rows (
    batch_id       TEXT NOT NULL,
    row_index      INTEGER NOT NULL,
    email          TEXT NOT NULL,
    name           TEXT NOT NULL,
    classification TEXT NOT NULL,
    contact_id     INTEGER NOT NULL,
    PRIMARY KEY (batch_id, row_index)
);
"""


def normalize_identity(email: str) -> str:
    """去首尾空格并将 ASCII 字母转小写；不折叠点号/加号别名。"""
    stripped = email.strip()
    return "".join(
        chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in stripped
    )


class Store:
    """封装调用方指定的 SQLite 文件。每次操作使用独立连接以支持跨线程取消。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_status(self, batch_id: str) -> Optional[BatchStatus]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return BatchStatus(row[0]) if row else None
