# 记忆失物招领处

> 一家只收藏“没什么用、却舍不得忘”的小事博物馆。

访客把一件小事交给“灰灯馆长”。馆长会把它写成一张荒诞但温柔的展签，
并把展品存入该访客独有的 PowerContext Scope。下次再提到相同线索时，
旧展品会先被召回，新展品再与它组成“联动展”。

本作品的工程验证方法参考公开仓库中“如何验证真实 Dify 运行”的实践。
游戏概念、角色、提示词、后端协议、验证模型、DSL 图和证据格式均为本作品独立实现。

## 作品体验

- 输入一条日常琐事，例如“一片香菜在冷冻层变成了绿色书签”。
- 灰灯馆长生成展名、年代、馆藏部件、讲解词和给访客的回声。
- 同一 Dify 用户在新会话里提到相关线索时，PowerContext 会召回旧展品。
- 第二次策展会明确引用旧展名，形成续篇，并再次写入新的馆藏记忆。
- 不同 Dify 用户使用不同 PowerContext Scope，互不可见。

## 架构

```text
Dify Chatflow
  → Code：隐私与长度初筛
  → IF：允许进入博物馆？
      ├─ 否 → 直接说明原因
      └─ 是 → Code：封装 user_id / conversation_id / workflow_run_id
                → HTTP：博物馆 BFF /v1/prepare
                → Code：校验召回结果
                → LLM：灰灯馆长策展
                → Code：校验并整理展品 JSON
                → Code：封装入藏请求
                → HTTP：博物馆 BFF /v1/remember
                → Code：校验真实 Artifact 与 Source 回执
                → Answer
```

### 组件分工

| 组件 | 实际职责 |
| --- | --- |
| Dify 1.17.1 | 聊天入口、Code 节点、IF/ELSE、HTTP 节点、LLM 调用、失败分支和最终回答 |
| 博物馆 BFF | 校验服务密钥、把 Dify `sys.user_id` 稳定映射到独立 PowerContext Scope、二次敏感内容检查、封装 Source 与 Memory 请求 |
| PowerContext 1.5.0 | 按用户保存展品记忆；在后续新会话中返回有界 PreparedContext |
| OpenAI-API-compatible 插件 | 接入用户自己的真实模型 |
| `museum-fixture-v1` | 自动 E2E 使用的确定性 OpenAI-compatible 响应模型，覆盖完整 Dify 执行链路 |

PowerContext 承担跨会话体验：它在第二次访问恢复旧展品，让“联动展”持续推进。

## 提交内容

| 路径 | 用途 |
| --- | --- |
| `dsl/memory-museum.yml` | 面向真实模型的 20 节点 Dify Chatflow |
| `dsl/memory-museum.e2e.yml` | 只把 LLM 名替换为验证模型的同图 DSL |
| `dsl/code/` | 可由 Dify Code 节点直接执行的安全筛选、请求封装和回执校验源码 |
| `scripts/build_dsl.py` | 按实际 provider/model 重建 DSL，避免手工改错 |
| `backend/` | FastAPI 隔离层与 OpenAI-compatible 验证模型，含 pytest |
| `deploy/` | Dify、PowerContext、博物馆 BFF 的 Compose 配置 |
| `scripts/bootstrap.py` | 获取固定版本上游并生成本机密钥与工作目录 |
| `scripts/manage.py` | 启动、停止和检查本项目自己的 Compose 项目 |
| `scripts/configure_dify.py` | 真实 Dify 初始化、模型插件安装、DSL 导入、发布和 API Key 创建 |
| `scripts/verify_e2e.py` | 通过 Dify SSE 执行两轮会话，并读取同一 `workflow_run_id` 的节点执行记录 |
| `samples/` | 两轮连续体验输入和预期表现 |
| `evidence/` | DSL、节点、真实 Dify 运行、SSE transcript 和节点执行证据 |

## 固定版本

- Dify：`1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`
- PowerContext：`1.5.0`，commit `729dc4c5548f915785a06c318f8cb7ed6cbea45b`
- OpenAI-API-compatible：
  `langgenius/openai_api_compatible:0.0.72@87f3c4c7acee9ff03ac2c04ba8191edc5ea87cd53e36af1bbf1ccd13f00352a4`
- Python：3.12
- Dify 数据库：官方 PostgreSQL Compose profile
- 用户到 Scope 映射：博物馆 BFF SQLite
- PowerContext：默认 SQLite
- Embedding / Rerank：可选的扩展能力

## 快速运行

要求 Windows + WSL2 + Docker Desktop，或可运行 Docker Compose 的 Linux 主机。
另需 Python 3.12、`uv` 和 Git。建议至少预留 25 GB 磁盘。

在 `dify/memory-museum/` 目录执行：

```powershell
uv run --project backend python scripts/bootstrap.py --root C:/DifyMemoryMuseum
uv run --project backend python scripts/manage.py up --root C:/DifyMemoryMuseum
uv run --project backend --group dev python scripts/configure_dify.py `
  --root C:/DifyMemoryMuseum `
  --dsl dsl/memory-museum.e2e.yml `
  --clean-reimport
uv run --project backend --group dev python scripts/verify_e2e.py `
  --root C:/DifyMemoryMuseum `
  --output evidence/dify-e2e.json
```

服务入口：

- Dify 控制台与 WebApp：`http://localhost:18080`
- 博物馆 BFF 健康检查：`http://localhost:18081/health`
- PowerContext：`http://localhost:18082`

`bootstrap.py` 会在本机生成 `deploy/.env` 和 Dify 本地环境文件。
这些文件由 `.gitignore` 管理，凭据保留在本机运行时目录。

## 使用真实模型

自动 E2E 使用 `museum-fixture-v1`，在无第三方模型凭据的环境中执行完整的
Dify 图、HTTP 节点、SSE、PowerContext 与节点执行记录验证。真实模型创作体验
通过同一份生产 DSL 接入用户自己的模型。

人工体验真实模型时：

1. 安装并授权 `OpenAI-API-compatible` 0.0.72。
2. 在 Dify 模型供应商中配置自己的真实聊天模型。
3. 重新生成 DSL，确保模型名与 Dify 中的配置一致：

```powershell
uv run --project backend --group dev python scripts/build_dsl.py `
  --model <你的模型名> `
  --output dsl/memory-museum.yml
```

4. 导入 `dsl/memory-museum.yml`，发布应用。
5. 使用真实模型时，在 Dify 控制台配置对应的 provider；`configure_dify.py`
   的 fixture 配置用于自动化 E2E。

模型 API Key 保存在本机 Dify 配置中；DSL、源码和证据使用占位配置。

## 技术结合证据

真实 E2E 脚本同时保存：

- Dify 插件安装任务和验证模型凭证配置结果。
- 第一轮和第二轮 Dify 的 `conversation_id`。
- 每轮真实 `workflow_run_id`。
- Dify SSE 原始事件流：`evidence/ci-sse-session-*.jsonl`。
- `召回旧展品（HTTP）` 节点的实际响应。
- `写入馆藏（HTTP）` 节点的真实 Artifact / Source 回执。
- 第二轮新会话中实际返回的 `memory_status=ready` 和 `memory_context`。
- 另一个 Dify 用户的 `memory_status=empty`，用于证明用户隔离。
- 每轮聊天回答 transcript。

验证脚本通过 Dify Console API：

```text
/apps/{app_id}/workflow-runs/{workflow_run_id}/node-executions
```

读取同一运行的真实节点输出，并把节点数据与 SSE 聊天回答分别归档。

## 安全与失败处理

- Dify Code 节点先拦截明显凭据、邮箱、手机号、身份证号和超长输入。
- 博物馆 BFF 在调用 PowerContext 前再次做同等级检查，降低绕过单层正则的风险。
- HTTP 写入采用单次提交。超时或结果不明时返回 `status=unknown`，提示到馆藏记录中核实。
- 召回失败、模型提案失败、写入前校验失败、写入回执失败和传输异常各有独立终点。
- Scope 的唯一来源是服务端根据 Dify `sys.user_id` 哈希幂等创建的结果。

## 全文检索说明

本作品依赖 PowerContext 的显式 Memory 和默认全文召回，并在查询中加入稳定的
“记忆失物招领处”检索前缀及本轮生活线索。样例刻意让两次输入共享“香菜、冰箱、
绿色书签”等词，以稳定展示跨会话召回。

验收环境采用 PowerContext 默认全文召回。配置 Embedding / Hybrid Search 后，
同一工作流可使用语义召回扩展跨会话体验。

## 自动验证

```powershell
uv run --project backend --group dev pytest -q
uv run --project backend --group dev python validation\validate_submission.py `
  --output evidence\submission-validation.json
uv run --with graphon==0.7.0 --python 3.12 python validation\validate_dify_nodes.py `
  --output evidence\dify-node-schema.json
```

[GitHub Actions 运行 #7](https://github.com/qiyingjie16-source/powercontext/actions/runs/42011438340)
已完整通过，并归档 `dify-e2e.json`、两轮 SSE transcript、Compose 状态、
Dify API/Worker、插件守护进程和博物馆服务日志。

## 运行边界与扩展

- Dify、PowerContext 与模型服务按固定版本和 Compose 配置部署。
- 自动 E2E 使用确定性验证模型；真实模型创作体验按“使用真实模型”步骤操作。
- 默认全文召回适合包含共同线索的互动样例；Embedding / Hybrid Search 可扩展语义召回。
- 记忆隔离以 Dify `user_id` 为边界；多用户部署在应用入口层完成身份认证。
- 高合规场景可在 BFF 前接入 DLP 或内容安全服务，与本作品的双层正则过滤组合使用。
