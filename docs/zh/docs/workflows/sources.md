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

## Pi 会话分支历史

`powercontext.sources.pi_session` 提供 `PiSessionInput`、`parse_pi_session`、
`PI_EVENT_SOURCE_DEFINITION` 和 `PiSessionConnector(path, leaf_id=None)`。
解析器验证完整 UTF-8/BOM JSONL v3 会话图，默认选择最后节点，也可显式选择节点，按根到叶返回可见原始事件。
控制节点保留连接关系但不生成文本；压缩和分支摘要不会替换早先证据。工具结果完整保存，不受 Memory 单条文本长度限制。

用 `SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))` resolve/read 事件，
通过 `manifest_for_definition` 和 `project_source_for_transport` 接入 Runtime ingestion，方式与 CSV 相同。
Source 身份是 session ID 与 event ID 的 JSON 数组经 SHA-256 得到的摘要，带 `pi:` 前缀，固定 67 字符。
Source 保留完整原始 record，标准文本投影保存完整正文及事件身份元数据。

增量采集使用现有 `ConnectorLifecycle`，绑定 connector 名 `pi-session-history`、版本 `1`，
由宿主提供持久化的 sink 和 checkpoint store。每次运行完整读取并校验文件，比较已导入记录的完整 JSON 摘要，
只提交所选分支的新事件。checkpoint 按 session 保存累计事件 ID 和摘要；键序或 JSON 排版变化不视为改写。
切分支时共享事件去重，已采集分支继续保留；当前会话中已导入记录改写或消失会拒绝运行。
成功写入后由 Lifecycle 提交 checkpoint；中途失败可留下已持久化 Source，但不推进 checkpoint，重试依靠 Runtime 幂等提交恢复。
采集每次扫描整个文件，checkpoint 随已导入事件数增长；它不是仅按新增字节读取的文件尾随器。

## PDF 全文页级采集与检索

`powercontext.sources.pdf_pages` 提供 `PDFDocumentInput`、`PDFPageInput`、
`parse_pdf_document`、`PDF_PAGE_SOURCE_DEFINITION` 和 `search_pdf_pages`。
解析依赖 pypdf，比赛固定环境已提供。输入完整原 PDF 字节，逐页提取所有物理页，包括空白页；
不合并相邻页面，不做 OCR，也不截断正文。空密码即可解密的权限 PDF 可读，需要非空密码或无法完整解析的文件以 `ValueError` 失败。

文档 ID 须非空白。每页记录完整输入字节的 SHA-256 和从 1 开始的物理页码，不能使用财报印刷页码。
Source 身份是文档 ID、PDF 摘要和物理页码的 JSON 数组经 SHA-256 得到的摘要，带 `pdf:` 前缀。
PDF 修订产生新身份，旧观察保留原修订。标准文本投影为 `文档ID\n页面正文`，
元数据恰好包含 `document_id`、`document_sha256` 和 `page_number`。
通过 Definition registry、manifest、observation 和 Runtime ingestion 保存，接入方式与 CSV 相同。

```python
from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document, search_pdf_pages

pages = parse_pdf_document(PDFDocumentInput(document_id="annual-report", content=pdf_bytes))
# 将 pages 解析成 observation 并提交 ingestion，完成 Memory 处理后：
citations = await search_pdf_pages(runtime.memory.for_scope(scope_id), question, limit=20)
```

检索只访问调用方提供的 scoped Memory，使用离线 FTS 查询、通用财务词汇扩展和候选文本 BM25 重排，
并考虑文档标签中的公司和年份词项。返回前通过 `memory.get` 回查并按页 Source 身份去重，
不通过扫描原文件、读取标注或生成引用来补齐结果；零上限、空白问题及没有匹配时返回空 tuple。
每个查询最多执行一次主查询和命中词汇组的扩展查询，每次候选上限为 `max(100, limit * 5)`；
不维护模块级索引或跨 Scope 缓存。独立页面引用要求一个 Memory 条目关联一个 PDF 页 Source。
完整提取与可回查引用不等同于问题的证据页召回达标；Recall 与解析质量须由相应带标注评测另行验证。
