# 运行证据

本目录区分静态检查、PowerContext 直连检查与完整 Dify 引擎执行证据，不混用结论等级。

| 文件 | 覆盖范围 |
| --- | --- |
| `submission-validation.json` | 两份 DSL 的图结构、HTTP 节点、工具变量、模型依赖和密钥检查 |
| `dify-node-schema.json` | 使用 Dify 1.17.1 同版本 `graphon==0.7.0` 校验全部节点数据模型 |
| `backend-tests.log` | BFF、用户 Scope 映射、双重敏感输入检查、验证模型和流式兼容测试 |
| `workflow-logic-tests.log` | DSL Code 节点逻辑及生成器可重复性测试 |
| `dify-import.json` | 真实 Dify 1.17.1 DSL 导入结果 |
| `dify-reimport.json` | 在一个全新应用中再次导入的结果 |
| `dify-publish.json` | 真实 Dify 发布与 App API Key 创建结果，不含 Key |
| `dify-e2e.json` | 真实 Dify 引擎执行两轮聊天并读取节点执行记录 |
| `ci-sse-session-1.jsonl` | 第一轮 Dify `/v1/chat-messages` 原始 SSE 事件 |
| `ci-sse-session-2.jsonl` | 新会话召回与续篇轮次的原始 SSE 事件 |
| `ci-runtime/` | Compose 状态、Dify 日志和博物馆服务日志 |

## 证据等级

### 静态

- `submission-validation.json`
- `dify-node-schema.json`
- 两份自动化测试日志

这些结果只能证明 DSL 和代码符合预期结构，不能证明 Dify 已导入或运行。

### 真实 Dify 引擎

- `dify-import.json`
- `dify-reimport.json`
- `dify-publish.json`
- `dify-e2e.json`
- `ci-sse-session-*.jsonl`
- `ci-runtime/`

`dify-e2e.json` 由 `scripts/verify_e2e.py` 直接生成。每条聊天记录都包含真实
`conversation_id`、`workflow_run_id`、聊天回答，以及从 Dify Console API 读取的
同一次运行的 `召回旧展品（HTTP）` 和 `写入馆藏（HTTP）` 节点输出。

第二轮使用同一 Dify `user`、新的 `conversation_id`。验收断言：

1. `召回旧展品（HTTP）` 返回 `status=ready`。
2. 召回内容包含第一轮展品和真实 Atomic Artifact ID。
3. 第二轮回答实质引用第一轮展品。
4. 第二个用户相同查询返回 `status=empty`。

## 自动 E2E 的模型边界

GitHub Actions 使用 `museum-fixture-v1`。它实现了 OpenAI Chat Completions 的非流式
与 SSE 流式协议，保证在无第三方模型凭据时仍能真实执行 Dify 图。它不会冒充真实模型，
证据中固定写有 `model_execution_mode: deterministic_fixture`。

人工验收真实创作质量时，应在 Dify 中配置自己的模型，导入
`dsl/memory-museum.yml`，再重复同样的两轮聊天。不得把 fixture 结果改写为真实模型结果。

## 凭据

仓库不包含 Dify 管理员密码、App API Key、模型 API Key、PowerContext Token 或博物馆服务密钥。
运行时文件位于 `--root` 外部工作目录和本机 `deploy/.env`，均不进入 Git。
