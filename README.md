# task26100308

联系人批量导入库（Python + SQLite），由上层调用，联系人与批次状态保存在调用方指定的 SQLite 文件中。

## 用法

```python
from contact_import import ContactImporter

importer = ContactImporter("/path/to/contacts.sqlite")

# 导入：调用方提供全局唯一批次号、名单 ID，并按原顺序提供行流
result = importer.import_batch("batch-001", "list-a", [
    ("Alice@Example.com", "Alice"),
    ("alice@example.com", "Alice Again"),  # 批内重复
])
for row in result.rows:
    print(row.row_index, row.classification, row.contact_id)

# 取消：已成功批次返回既有成功结果；未开始/导入中批次变为已取消且不可再发布
importer.cancel_batch("batch-001")

# 查询批次当前结果
importer.get_result("batch-001")
```

## 语义

- 邮箱去首尾空格并将 ASCII 字母转小写后作为去重身份；同一名单内唯一，不同名单互不影响。
- 行分类：`new`（新增）、`dup_batch`（批内重复）、`dup_list`（名单重复）。
- 新联系人取所属成功批次中该邮箱第一行的全部资料；命中已有联系人不覆盖、不合并。
- 联系人、成功状态与逐行分类在单个 SQLite 事务中同时可见，不存在半批数据。
- 行流异常 → 批次 `failed`；取消成功 → `cancelled`；两者均不贡献联系人、不影响其他批次。
- 终态（succeeded/failed/cancelled）不可改写；只有成功批次具有最终分类与联系人归属。

## 测试

```sh
python3 -m unittest discover -s tests -v
```
