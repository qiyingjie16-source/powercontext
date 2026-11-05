# 运行证据

本目录按验证层次组织静态检查、PowerContext 检查和完整 Dify 引擎执行证据。

| 文件 | 覆盖范围 |
| --- | --- |
| `submission-validation.json` | 两份 DSL 的图结构、HTTP 节点、工具变量、模型依赖和密钥检查 |
| `dify-node-schema.json` | 使用 Dify 1.17.1 同版本 `graphon==0.7.0` 校验全部节点数据模型 |
| `backend-tests.log` | BFF、用户 Scope 映射、双重敏感输入检查、验证模型和流式兼容测试 |
| `configure-dify-tests.log` | 插件安装任务轮询、失败诊断、已安装分支和模型凭证 schema 测试 |
| `workflow-logic-tests.log` | DSL Code 节点逻辑及生成器可重复性测试 |
| `dify-plugin-install.json` | Marketplace 插件安装任务、状态迁移、供应商可见性与失败诊断 |
| `dify-model-config.json` | 真实 Dify 引擎中的验证模型凭证配置结果 |
| `dify-import.json` | 真实 Dify 1.17.1 DSL 导入结果 |
| `dify-reimport.json` | 在一个全新应用中再次导入的结果 |
| `dify-publish.json` | 真实 Dify 发布与 App API Key 创建结果，凭据字段已脱敏 |
| `dify-e2e.json` | 真实 Dify 引擎执行两轮聊天并读取节点执行记录 |
| `ci-sse-session-1.jsonl` | 第一轮 Dify `/v1/chat-messages` 原始 SSE 事件 |
| `ci-sse-session-2.jsonl` | 新会话召回与续篇轮次的原始 SSE 事件 |
| `ci-runtime/` | Compose 状态、Dify API/Worker、插件守护进程和博物馆服务日志 |

## 最近一次完整运行

- GitHub Actions：
  [Dify Memory Museum E2E #7](https://github.com/qiyingjie16-source/powercontext/actions/runs/42011438340)
- 提交：`ac27c30c6751cffc6e4dbbcc4ba896b243f210f8`
- 结果：全部步骤通过。
- 第一次会话：`6ae4256b-71ec-40eb-9180-b0dc91eea35e`
- 第一次工作流：`505ac722-4eb3-4fba-a2c4-d585f9627e0f`
- 新会话：`116130c4-20d9-4788-87fa-3f3f7001947b`
- 新会话工作流：`e73af9b9-660d-4128-b652-c9710556147c`
- 第一轮 Artifact：`art_6ce155abcfc245d38829ccf97950e07b`
- 第二轮 Artifact：`art_f4a5b6514c6e45f49ad3dda76ce4790d`
- 用户隔离工作流：`5ba7b941-0d31-46a2-9318-53b0e4c6f1aa`

## 证据等级

### 静态

- `submission-validation.json`
- `dify-node-schema.json`
- `backend-tests.log`
- `configure-dify-tests.log`
- `workflow-logic-tests.log`

这些文件覆盖 DSL 结构、节点数据模型和纯逻辑测试；真实 Dify 执行结果见下一节。

### 真实 Dify 引擎

- `dify-plugin-install.json`
- `dify-model-config.json`
- `dify-import.json`
- `dify-reimport.json`
- `dify-publish.json`
- `dify-e2e.json`
- `ci-sse-session-*.jsonl`
- `ci-runtime/`

`dify-e2e.json` 由 `scripts/verify_e2e.py` 直接生成。每条聊天记录都包含真实
`conversation_id`、`workflow_run_id`、聊天回答，以及从 Dify Console API 读取的
同一次运行的 `召回旧展品（HTTP）` 和 `写入馆藏（HTTP）` 节点输出。
SSE transcript 保留原始事件结构，博物馆服务密钥字段已替换为 `[REDACTED]`。

第二轮使用同一 Dify `user`、新的 `conversation_id`。验收断言：

1. `召回旧展品（HTTP）` 返回 `status=ready`。
2. 召回内容包含第一轮展品和真实 Atomic Artifact ID。
3. 第二轮回答实质引用第一轮展品。
4. 第二个用户相同查询返回 `status=empty`。

## 自动 E2E 的模型配置

GitHub Actions 使用 `museum-fixture-v1`。它实现了 OpenAI Chat Completions 的非流式
与 SSE 流式协议，在无第三方模型凭据时执行完整 Dify 图。证据中固定标注
`model_execution_mode: deterministic_fixture`。

真实模型创作体验使用 `dsl/memory-museum.yml`，在 Dify 中配置自己的模型后
重复同样的两轮聊天。

## 凭据

仓库中的配置使用占位值。Dify 管理员密码、App API Key、模型 API Key、
PowerContext Token 和博物馆服务密钥保存在 `--root` 外部工作目录及本机
`deploy/.env`，由 `.gitignore` 管理。
