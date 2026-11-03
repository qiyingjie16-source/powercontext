---
title: Sources 与采集
description: 采集证据、查看 Source 记录并配置后续处理。
---

# Sources 与采集

Sources 在信息被筛选为知识之前保留证据。Content Source 保存采集的文本，其他 Source 类型可引用 Connector 提供的材料。
Work Contract 和 Task Outcome 也会保留 Source 证据。

## 采集与检查

1. 选择已有 Scope。[API Quick Start](../develop/api-quickstart.md)演示通过 `POST /v1/sources/content` 采集内容，
   并保留返回的 Source 引用。
2. 使用 `GET /v1/scopes/{scope_id}/sources` 浏览 Source journal，再通过
   `GET /v1/scopes/{scope_id}/sources/{source_type}/{source_id}` 读取选中的 Source。
3. 将符合条件的证据处理为 Memory，需要配置生成模型，并启用定时处理或显式执行 Memory flush。
   步骤见[启用提取与向量搜索](../get-started/configure-models.md)。

采集成功表示证据已保存，不表示提取成功，也不表示内容一定值得长期保留。
通过 Source 引用追溯生成结果的输入。不要在同一身份下修改内容并重试采集，将其当作新的观察。

## 选择采集入口

- Agent Prompt Hook：在[接入指南](../integrations/index.md)中选择宿主对应的采集开关。
- 文件与对象存储：使用 [OpenDAL Connector](ingest-text-files-with-opendal.md)。
- Scope Profile 的主体证据：按 [Scope Profile](use-profiles.md)写入。
- 应用或评测事件：使用应用对应适配器；[Bub 采集](../integrations/evaluation.md)需要显式启用。

Source 读写受所选 [Scope 与访问策略](scopes-and-access.md)约束。

## 离线 CSV 行级采集

`powercontext.sources.csv_rows` 提供逐行保存原始字段的 Source Definition。
完整文档先通过校验，再返回行输入；无效 UTF-8、表头、主键、记录宽度或 CSV 格式以 `ValueError` 失败。
列名和字段值保留原字符串，主键列名精确匹配；每行身份由文档 ID 和主键值分别百分号编码组成，总长不超过 256 字符。

```python
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, parse_csv_document
from powercontext.builtin.runtime import SubmitSourceObservation

async def ingest_csv(runtime, scope_id: str, content: bytes) -> None:
    rows = parse_csv_document(CSVDocumentInput(document_id="airports", primary_key="id", content=content))
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
    for row in rows:
        source = await registry.resolve(row)
        observation = project_source_for_transport(registry, source)
        await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope_id, observation=observation))
```

Scope 须已创建。整份文档的解析校验是原子的；逐行提交不构成整份文档的数据库事务。
标准文本投影保存按原列序排列的紧凑 JSON 及完整行元数据，Runtime 保存观察与投影，重启读取不依赖原文件。
Memory 处理后，使用搜索结果的 `memory_ref`、`entry_id`、`entry_version_id` 构造精确 `MemoryCitation` 回查。
稳定行身份不表示允许覆盖已采集内容；同一身份下的冲突内容仍遵循 Runtime 的冲突处理规则。
