# contact-import

联系人批量导入库：纯 Python + SQLite，无外部服务依赖。联系人与批次状态
保存在调用方指定的 SQLite 文件中，提供导入与取消能力。

## 用法

```python
from contact_import import Importer, ContactRow

with Importer("contacts.db") as imp:
    # 批次号全局唯一；行流按原顺序提供，取行异常则整批失败
    result = imp.import_batch(
        batch_id="batch-001",
        list_id="list-a",
        rows=[ContactRow(email="a@example.com", name="Alice")],
    )
    for row in result.rows:
        print(row.row_index, row.category, row.contact_id)

    imp.cancel("batch-002")  # 进行中→取消；已成功→返回既有结果
```

## 语义

- 去重身份：邮箱去首尾空格 + ASCII 字母小写，不折叠点号/加号别名。
- 行分类：`NEW`（新增）、`DUPLICATE_IN_BATCH`（批内重复）、
  `DUPLICATE_IN_LIST`（发布时名单已存在）。同一名单内身份唯一，名单间隔离。
- 新增联系人取所属成功批次中该邮箱首行资料；命中已有联系人不覆盖、不合并。
- 新联系人、成功状态与行分类在单个事务内同时可见，无半批数据。
- 行流异常→批次 `FAILED`；取消→`CANCELLED`。两者均不贡献联系人，
  不影响其他批次。终态不可改写，取消已成功批次返回既有成功结果。

## 测试

```bash
python3 -m pytest tests/ -q
```
