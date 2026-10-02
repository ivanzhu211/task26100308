import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contact_import import (
    BatchConflictError,
    BatchStatus,
    ContactImporter,
    ContactRow,
    RowClassification,
)


class ImporterTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.importer = ContactImporter(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def contacts(self, list_id=None):
        conn = sqlite3.connect(self.db_path)
        try:
            sql = "SELECT list_id, identity, email, name, batch_id FROM contacts"
            args = ()
            if list_id is not None:
                sql += " WHERE list_id = ?"
                args = (list_id,)
            return conn.execute(sql + " ORDER BY contact_id", args).fetchall()
        finally:
            conn.close()

    def test_basic_import_classifications(self):
        result = self.importer.import_batch(
            "b1",
            "list-a",
            [
                ("Alice@Example.com ", "Alice"),
                ("alice@example.com", "Alice Again"),   # 批内重复（规范化后同身份）
                ("BOB@example.com", "Bob"),
            ],
        )
        self.assertEqual(result.status, BatchStatus.SUCCEEDED)
        self.assertEqual(
            [r.classification for r in result.rows],
            [
                RowClassification.NEW,
                RowClassification.DUPLICATE_IN_BATCH,
                RowClassification.NEW,
            ],
        )
        # 批内重复行归属第一个候选的联系人
        self.assertEqual(result.rows[1].contact_id, result.rows[0].contact_id)
        self.assertNotEqual(result.rows[0].contact_id, result.rows[2].contact_id)
        # 联系人资料取该邮箱在成功批次中的第一行
        contacts = self.contacts("list-a")
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0][2:], ("Alice@Example.com", "Alice", "b1"))
        self.assertEqual(contacts[1][2:], ("BOB@example.com", "Bob", "b1"))

    def test_list_duplicate_and_list_isolation(self):
        first = self.importer.import_batch(
            "b1", "list-a", [("shared@example.com", "First")]
        )
        second = self.importer.import_batch(
            "b2",
            "list-a",
            [(" SHARED@example.com", "Second"), ("new@example.com", "New")],
        )
        self.assertEqual(second.rows[0].classification, RowClassification.DUPLICATE_IN_LIST)
        self.assertEqual(second.rows[0].contact_id, first.rows[0].contact_id)
        self.assertEqual(second.rows[1].classification, RowClassification.NEW)
        # 命中已有联系人不覆盖资料
        contacts = self.contacts("list-a")
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0][3], "First")
        # 不同名单互不影响
        other = self.importer.import_batch(
            "b3", "list-b", [("shared@example.com", "OtherList")]
        )
        self.assertEqual(other.rows[0].classification, RowClassification.NEW)

    def test_stream_failure_leaves_nothing(self):
        self.importer.import_batch("ok", "list-a", [("keep@example.com", "Keep")])

        def broken():
            yield ("lost@example.com", "Lost")
            yield ("keep@example.com", "Dup Would Be")
            raise RuntimeError("csv read failed")

        result = self.importer.import_batch("bad", "list-a", broken())
        self.assertEqual(result.status, BatchStatus.FAILED)
        self.assertEqual(result.rows, [])
        # 失败批次不贡献任何联系人，既有批次成果不变
        self.assertEqual(
            [c[1] for c in self.contacts("list-a")], ["keep@example.com"]
        )
        # 失败批次中的邮箱不污染名单：后续批次同邮箱仍为新增
        later = self.importer.import_batch(
            "later", "list-a", [("lost@example.com", "Recovered")]
        )
        self.assertEqual(later.rows[0].classification, RowClassification.NEW)

    def test_cancel_during_import(self):
        def rows():
            yield ("a@example.com", "A")
            yield ("b@example.com", "B")
            self.importer.cancel_batch("cancel-me")
            yield ("c@example.com", "C")

        result = self.importer.import_batch("cancel-me", "list-a", rows())
        self.assertEqual(result.status, BatchStatus.CANCELLED)
        self.assertEqual(result.rows, [])
        self.assertEqual(self.contacts("list-a"), [])
        # 取消成功的批次不能再发布
        with self.assertRaises(BatchConflictError):
            self.importer.import_batch("cancel-me", "list-a", [])
        # 取消不撤销其他批次成果
        other = self.importer.import_batch(
            "other", "list-a", [("a@example.com", "A2")]
        )
        self.assertEqual(other.rows[0].classification, RowClassification.NEW)

    def test_cancel_unknown_batch_blocks_future_publish(self):
        result = self.importer.cancel_batch("never-started")
        self.assertEqual(result.status, BatchStatus.CANCELLED)
        with self.assertRaises(BatchConflictError):
            self.importer.import_batch("never-started", "list-a", [])

    def test_cancel_succeeded_returns_existing_result(self):
        success = self.importer.import_batch(
            "done", "list-a", [("x@example.com", "X"), ("x@example.com", "X2")]
        )
        cancelled = self.importer.cancel_batch("done")
        self.assertEqual(cancelled.status, BatchStatus.SUCCEEDED)
        self.assertEqual(cancelled.rows, success.rows)
        # 资料与结果不被回滚
        self.assertEqual(len(self.contacts("list-a")), 1)
        again = self.importer.get_result("done")
        self.assertEqual(again.status, BatchStatus.SUCCEEDED)
        self.assertEqual(again.rows, success.rows)

    def test_terminal_state_immutable(self):
        def broken():
            raise RuntimeError("boom")
            yield

        failed = self.importer.import_batch("f", "list-a", broken())
        self.assertEqual(failed.status, BatchStatus.FAILED)
        # 失败终态不可被取消改写
        still = self.importer.cancel_batch("f")
        self.assertEqual(still.status, BatchStatus.FAILED)
        # 失败终态不可被重新导入改写
        with self.assertRaises(BatchConflictError):
            self.importer.import_batch("f", "list-a", [])
        # 成功终态不可被重新导入改写
        self.importer.import_batch("s", "list-a", [("y@example.com", "Y")])
        with self.assertRaises(BatchConflictError):
            self.importer.import_batch("s", "list-a", [])

    def test_shared_email_with_other_succeeded_batch(self):
        """取消/失败批次不留联系人；共享邮箱的其他成功批次成果保留。"""
        good = self.importer.import_batch(
            "good", "list-a", [("shared@example.com", "Good Name")]
        )

        def broken():
            yield ("shared@example.com", "Evil Overwrite")
            yield ("ghost@example.com", "Ghost")
            raise RuntimeError("interrupt")

        failed = self.importer.import_batch("bad", "list-a", broken())
        self.assertEqual(failed.status, BatchStatus.FAILED)

        cancelling = self.importer.cancel_batch("to-cancel")
        self.assertEqual(cancelling.status, BatchStatus.CANCELLED)

        # 失败/取消批次均未留下联系人
        batches = {c[4] for c in self.contacts("list-a")}
        self.assertEqual(batches, {"good"})
        # 既有成功批次资料未被覆盖
        contact = self.contacts("list-a")[0]
        self.assertEqual(contact[2:], ("shared@example.com", "Good Name", "good"))
        # 既有成功批次结果仍可完整读取
        reread = self.importer.get_result("good")
        self.assertEqual(reread.rows, good.rows)
        # 新批次命中共享邮箱为名单重复，归属既有联系人
        nxt = self.importer.import_batch(
            "next", "list-a", [("shared@example.com", "Another")]
        )
        self.assertEqual(nxt.rows[0].classification, RowClassification.DUPLICATE_IN_LIST)
        self.assertEqual(nxt.rows[0].contact_id, good.rows[0].contact_id)

    def test_contact_row_objects_accepted(self):
        result = self.importer.import_batch(
            "obj", "list-a", [ContactRow(email="o@example.com", name="O")]
        )
        self.assertEqual(result.rows[0].classification, RowClassification.NEW)


if __name__ == "__main__":
    unittest.main()


class ThreadedCancelTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.importer = ContactImporter(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_cancel_from_another_thread(self):
        import threading
        import time

        started = threading.Event()

        def rows():
            started.set()
            for i in range(1000):
                yield (f"user{i}@example.com", f"U{i}")
                time.sleep(0.002)

        canceller = threading.Thread(
            target=lambda: (started.wait(), time.sleep(0.02),
                            self.importer.cancel_batch("bg"))
        )
        canceller.start()
        result = self.importer.import_batch("bg", "list-a", rows())
        canceller.join()

        self.assertEqual(result.status, BatchStatus.CANCELLED)
        conn = sqlite3.connect(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE batch_id = 'bg'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)
