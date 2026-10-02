"""联系人批量导入库的自动化测试。"""
from __future__ import annotations

import pytest

from contact_import import (
    BatchAlreadyTerminalError,
    BatchCancelledError,
    BatchNotFoundError,
    ContactRow,
    Importer,
    RowCategory,
    normalize_identity,
)


@pytest.fixture()
def importer(tmp_path):
    imp = Importer(str(tmp_path / "contacts.db"))
    yield imp
    imp.close()


def contact_rows(*pairs):
    return [ContactRow(email=e, name=n) for e, n in pairs]


class TestNormalize:
    def test_strip_and_ascii_lower(self):
        assert normalize_identity("  Alice@Example.COM ") == "alice@example.com"

    def test_no_dot_or_plus_folding(self):
        assert normalize_identity("a.b+x@example.com") == "a.b+x@example.com"
        assert normalize_identity("ab@example.com") != normalize_identity("a.b@example.com")

    def test_non_ascii_not_folded(self):
        # 仅 ASCII 字母转小写，其他字符保持原样
        assert normalize_identity("Ä@example.com") == "Ä@example.com"


class TestImportBasics:
    def test_new_and_in_batch_duplicates(self, importer):
        result = importer.import_batch(
            "b1",
            "list-a",
            contact_rows(
                ("alice@example.com", "Alice"),
                ("ALICE@example.com", "Alice Again"),
                (" bob@example.com ", "Bob"),
            ),
        )
        cats = [r.category for r in result.rows]
        assert cats == [
            RowCategory.NEW,
            RowCategory.DUPLICATE_IN_BATCH,
            RowCategory.NEW,
        ]
        # 批内重复行归属本批第一行的联系人
        assert result.rows[1].contact_id == result.rows[0].contact_id
        assert result.rows[2].contact_id != result.rows[0].contact_id

    def test_list_duplicate_does_not_overwrite(self, importer):
        first = importer.import_batch(
            "b1", "list-a", contact_rows(("alice@example.com", "Alice"))
        )
        second = importer.import_batch(
            "b2", "list-a", contact_rows(("alice@example.com", "Evil Alice"))
        )
        assert second.rows[0].category is RowCategory.DUPLICATE_IN_LIST
        assert second.rows[0].contact_id == first.rows[0].contact_id
        # 命中已有联系人不覆盖资料
        cur = importer._store._conn.execute(
            "SELECT name, batch_id FROM contacts WHERE contact_id = ?",
            (first.rows[0].contact_id,),
        )
        assert cur.fetchone() == ("Alice", "b1")

    def test_lists_are_isolated(self, importer):
        importer.import_batch("b1", "list-a", contact_rows(("a@example.com", "A")))
        other = importer.import_batch("b2", "list-b", contact_rows(("a@example.com", "A2")))
        assert other.rows[0].category is RowCategory.NEW

    def test_unpublished_batch_not_visible(self, importer):
        # 行流中断的批次（未发布）不会让后续批次误判为名单重复
        def broken():
            yield ContactRow("shared@example.com", "First")
            raise RuntimeError("stream broken")

        with pytest.raises(RuntimeError):
            importer.import_batch("b-broken", "list-a", broken())
        result = importer.import_batch(
            "b-ok", "list-a", contact_rows(("shared@example.com", "Second"))
        )
        assert result.rows[0].category is RowCategory.NEW


class TestStreamFailure:
    def test_failed_batch_leaves_no_contacts(self, importer):
        # 存在共享邮箱的其他成功批次
        ok = importer.import_batch(
            "b-ok", "list-a", contact_rows(("shared@example.com", "Keeper"))
        )

        def broken():
            yield ContactRow("shared@example.com", "Shared")
            yield ContactRow("unique@example.com", "Unique")
            raise IOError("read interrupted")

        with pytest.raises(IOError):
            importer.import_batch("b-fail", "list-a", broken())

        # 失败批次不贡献任何联系人
        count = importer._store._conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE batch_id = 'b-fail'"
        ).fetchone()[0]
        assert count == 0
        # 成功批次的资料与结果不被回滚
        again = importer.cancel("b-ok")
        assert again == ok
        # 失败批次是终态，不可改写
        with pytest.raises(BatchAlreadyTerminalError):
            importer.import_batch("b-fail", "list-a", contact_rows(("x@example.com", "X")))
        with pytest.raises(BatchAlreadyTerminalError):
            importer.cancel("b-fail")

    def test_shared_email_still_owned_by_successful_batch(self, importer):
        ok = importer.import_batch(
            "b-ok", "list-a", contact_rows(("shared@example.com", "Keeper"))
        )

        def broken():
            yield ContactRow("shared@example.com", "Shared")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            importer.import_batch("b-fail", "list-a", broken())
        # 后续批次命中共享邮箱时仍归属成功批次的联系人
        later = importer.import_batch(
            "b-later", "list-a", contact_rows(("shared@example.com", "Later"))
        )
        assert later.rows[0].category is RowCategory.DUPLICATE_IN_LIST
        assert later.rows[0].contact_id == ok.rows[0].contact_id


class TestCancel:
    def test_cancel_during_stream(self, importer):
        importer.import_batch(
            "b-ok", "list-a", contact_rows(("shared@example.com", "Keeper"))
        )

        def cancelling_stream():
            yield ContactRow("shared@example.com", "Shared")
            yield ContactRow("new@example.com", "New")
            assert importer.cancel("b-cancel") is None
            yield ContactRow("never@example.com", "Never")

        with pytest.raises(BatchCancelledError):
            importer.import_batch("b-cancel", "list-a", cancelling_stream())

        # 取消成功的批次不贡献任何联系人
        count = importer._store._conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE batch_id = 'b-cancel'"
        ).fetchone()[0]
        assert count == 0
        # 取消成功的批次不能再发布
        with pytest.raises(BatchCancelledError):
            importer.import_batch(
                "b-cancel", "list-a", contact_rows(("x@example.com", "X"))
            )
        # 已取消是终态，不可再取消
        with pytest.raises(BatchAlreadyTerminalError):
            importer.cancel("b-cancel")
        # 共享邮箱仍归属成功批次
        later = importer.import_batch(
            "b-later", "list-a", contact_rows(("shared@example.com", "Later"))
        )
        assert later.rows[0].category is RowCategory.DUPLICATE_IN_LIST

    def test_cancel_succeeded_returns_existing_result(self, importer):
        result = importer.import_batch(
            "b1",
            "list-a",
            contact_rows(("a@example.com", "A"), ("b@example.com", "B")),
        )
        again = importer.cancel("b1")
        assert again == result
        # 联系人未被撤销
        count = importer._store._conn.execute(
            "SELECT COUNT(*) FROM contacts WHERE batch_id = 'b1'"
        ).fetchone()[0]
        assert count == 2

    def test_cancel_unknown_batch(self, importer):
        with pytest.raises(BatchNotFoundError):
            importer.cancel("nope")


class TestPersistence:
    def test_result_survives_reopen(self, tmp_path):
        db = str(tmp_path / "contacts.db")
        with Importer(db) as imp:
            result = imp.import_batch(
                "b1", "list-a", contact_rows(("a@example.com", "A"))
            )
        with Importer(db) as imp2:
            assert imp2.cancel("b1") == result
            dup = imp2.import_batch(
                "b2", "list-a", contact_rows(("a@example.com", "A2"))
            )
            assert dup.rows[0].category is RowCategory.DUPLICATE_IN_LIST
            assert dup.rows[0].contact_id == result.rows[0].contact_id
