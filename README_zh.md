# Semantic DeerFlow

[English](./README.md) | [中文](./README_zh.md) | [日本語](./README_ja.md) |
[Français](./README_fr.md) | [Русский](./README_ru.md)

Semantic DeerFlow 是基于
[ByteDance DeerFlow](https://github.com/bytedance/deer-flow) 的非官方下游项目。
本项目与 ByteDance 及 DeerFlow 官方不存在隶属、维护或背书关系。

本项目面向多租户 SaaS 后端，重点提供受治理的语义查询与受控 Action。主要集成方式
是 Gateway、Semantic 和 Action 后端 API。仓库内的 Next.js 前端目前仅用于开发与
调试，独立的产品化前端尚未完成。

[上游来源与致谢](./ORIGINAL_README.md)

## 核心能力

### Agent Runtime

- 基于 LangGraph 的 Lead Agent，支持流式运行、持久化 Thread、Checkpoint、长期记忆和
  上下文压缩。
- 可按研究、编码、数据分析等任务动态委派 SubAgent，并隔离上下文和工具权限。
- 支持本地、容器、Provisioner 和云端 Sandbox，可控执行文件操作与命令。
- 内置工具、Community Tool、MCP 和 Skill 可组合扩展，并支持延迟工具发现和请求级
  Secret 隔离。
- 可配置多种模型 Provider、模型路由、Token 统计、Tracing、Guardrail 和熔断策略。
- Web、API、定时任务与多种 IM Channel 共用统一的 Thread 和 Run 生命周期。

### 语义治理与 Action

- 校验 tenant、principal、role、Scope、audience、有效期和 permission version 的短期
  签名授权上下文。
- 通过 Ontology 定义对象、关系、指标、维度和可解释的 Metric 口径。
- SQL Scope Policy 在查询到达数据源前约束数据库、表、字段和行级谓词。
- SaaS Query 采用语义优先路由，只在策略允许时进入 scoped SQL fallback；策略服务
  不可用时 fail closed。
- Action 经过提案、审批、IAM 权限重验、关联信息、幂等和审计流程，且只有隔离的
  Action Worker 持有写凭据。

### 开发与评测

- Fake IAM 和 Fake Domain API 覆盖 JWT、角色、Scope、permission version、审批、
  Worker Token 和 `Idempotency-Key` 边界。
- 内存 SQLite 演示数据无需租户配置数据库或 MySQL 即可运行。
- 离线 SaaS Agent Evals 覆盖语义读取、路由、越权拒绝、审批、执行、幂等、工具轨迹和
  禁止副作用。

上游 DeerFlow 的兼容边界保持不变，包括 `deerflow.*` import、`DEER_FLOW_*` 环境
变量、Gateway API、核心运行时数据结构和 Docker 内部兼容名称。

## 演示数据

仓库内 SaaS 示例使用明显虚构且一致的值：

| 字段                    | 演示值                               |
| ----------------------- | ------------------------------------ |
| tenant_id / tenant_code | `public-tenant-001` / `public_demo`  |
| principal_id            | `public-user-001`                    |
| site_id / project_id    | `site-demo-001` / `project-demo-001` |
| system_code             | `demo`                               |
| 数据库                  | `semantic_demo`                      |
| 表                      | `demo_sites`、`demo_projects`        |

`DEER_FLOW_SEMANTIC_DEMO_DATA=true` 会启用仓库内置的内存 SQLite 演示数据。连接
真实租户数据源注册表之前必须关闭该选项。

## 服务结构

运行时由 Gateway 和内部语义服务组成：

```text
浏览器 / API 调用方 / IM / Scheduler
                 |
              Nginx :2026
                 |
       +---------+---------+
       |                   |
  Frontend :3000     Gateway :8001
                         |
              +----------+----------+
              |                     |
       Semantic API :8003     Action Worker
              |                     |
       Ontology + Scope       Fake/real Domain API
       Policy + Query
```

Gateway 负责认证、Thread、Run、流式输出、模型选择、Memory、Skills、MCP、上传、
Artifacts、定时任务和 IM Channel。Semantic API 负责 Ontology、授权 Scope、语义
查询规划、SQL Policy 和 Action 预检。只有 Action Worker 应接收写凭据。Frontend
只是这些 API 的开发调试客户端，并不包含第二套 Agent Runtime。

| 服务          | 端口   | 作用                                              |
| ------------- | ------ | ------------------------------------------------- |
| Nginx         | `2026` | 浏览器与 API 统一入口                             |
| Gateway API   | `8001` | REST API 与 LangGraph 兼容 Agent Runtime          |
| Semantic API  | `8003` | 内部 Ontology、Scope Query、Policy 与 Action 编排 |
| Action Worker | 无     | 隔离的审批后写入执行器                            |
| Frontend      | `3000` | 开发调试界面                                      |
| Eval Fixture  | `8004` | 可选的 Fake IAM / Domain API                      |

Semantic API 和 Action Worker 不经 nginx 暴露。只有 Action Worker 可以获得 Domain
API 写凭据。

## 快速开始

需要 Python 3.12+、Node.js 22+、pnpm 10.26.2+、`uv`，以及可运行 GNU Make 的
bash 兼容环境。Docker 可选。

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
make setup
make dev
```

浏览器打开 <http://localhost:2026>。运行 Agent 前至少配置一个模型。Semantic
演示本身不依赖租户配置数据库或 MySQL。

### 配置说明

| 文件                     | 作用                                                                           |
| ------------------------ | ------------------------------------------------------------------------------ |
| `.env`                   | API Key、服务 Token、JWT 验证密钥、数据库 URL 和部署 Secret                    |
| `config.yaml`            | 模型、Sandbox、持久化、认证、Scheduler、Channel、Guardrail、Tracing 和运行行为 |
| `extensions_config.json` | MCP Server、扩展工具和启用的 Skills                                            |

`make setup` 会通过交互向导生成最小配置；`make config` 用于复制完整示例；
`make doctor` 用于检查工具链和配置。最小 OpenAI-compatible 模型示例：

```yaml
models:
  - name: primary-model
    display_name: Primary Model
    use: langchain_openai:ChatOpenAI
    model: provider-model-id
    api_key: $OPENAI_API_KEY
    base_url: https://api.openai.com/v1
    supports_thinking: false
    supports_vision: false

sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
```

Agent 和 Semantic 流程应使用 Tool Calling 稳定的模型。Provider、本地模型、Responses
API、CLI-backed Model、模型路由、Sandbox、MCP、Tracing、Memory 和 Channel 示例见
[config.example.yaml](./config.example.yaml) 与
[extensions_config.example.json](./extensions_config.example.json)。

### 不同启动方式

| 方式         | 命令                                    | 适用场景                                      |
| ------------ | --------------------------------------- | --------------------------------------------- |
| 本地开发     | `make dev`                              | Gateway、Semantic API、Frontend、Nginx 热更新 |
| 本地生产模式 | `make start`                            | 不启用热更新的本地完整服务                    |
| 本地后台模式 | `make dev-daemon` / `make start-daemon` | 将服务放到后台运行                            |
| Docker 开发  | `make docker-init && make docker-start` | 带源码挂载的可复现环境                        |
| Docker 生产  | `make up`                               | 构建镜像并启动持久化服务                      |
| 单模块开发   | `cd backend && make dev`                | 只调试 Gateway，Frontend 另行启动             |

本地服务使用 `make stop` 停止，Docker 开发环境使用 `make docker-stop`，生产容器使用
`make down`。浏览器入口统一是 `http://localhost:2026`，直接访问其他端口主要用于
开发和内部检查。

## SaaS API 集成

可信调用方先在 Gateway 完成认证，并提交包含 tenant、principal、roles、Scope 和
permission version 的短期签名上下文。Gateway 验证后转交 Semantic API。语义查询
必须经过 Ontology 和 SQL Scope Policy；Action 必须依次经过提案、审批、权限重验和
Action Worker 幂等执行。

接入真实系统时：

1. 设置 `DEER_FLOW_SEMANTIC_DEMO_DATA=false`。
2. 通过环境变量配置 JWT 验证、内部服务 Token 和租户数据源注册表。
3. 提供部署自己的 Ontology 与 SQL Scope Policy。
4. 仅为 Action Worker 配置 IAM 重验和 Domain API 写入地址。
5. 严格分离读写凭据，不对公网暴露 Semantic API 或 Action Worker。

完整边界见 [.env.example](./.env.example)、[config.example.yaml](./config.example.yaml)
和 [backend/AGENTS.md](./backend/AGENTS.md)。

## API 调用示例

以下示例使用 Nginx 统一入口。根据部署方式补充本地登录会话，或补充签名的
`X-SaaS-Authorization-Context` Header；示例中的占位符不是凭据。

### 健康检查与模型列表

```bash
curl http://localhost:2026/health
curl http://localhost:2026/api/models
```

### LangGraph 兼容 Thread 与 Run

```bash
BASE=http://localhost:2026/api/langgraph
THREAD_ID=$(curl -s -X POST "$BASE/threads" \
  -H 'Content-Type: application/json' \
  -d '{"metadata":{"source":"demo"}}' | jq -r .thread_id)

curl -N -X POST "$BASE/threads/$THREAD_ID/runs/stream" \
  -H 'Content-Type: application/json' \
  -d '{
    "assistant_id":"lead_agent",
    "input":{"messages":[{"role":"user","content":"总结演示工作区。"}]},
    "context":{"thinking_enabled":false},
    "stream_mode":["values","messages-tuple","custom"]
  }'
```

返回值是 Server-Sent Events。可使用 `values`、`messages-tuple`、`custom`、`updates`、
`events`、`debug`、`tasks`、`checkpoints`；不要发送已经废弃的 `tools` 模式。

### 语义优先的 SaaS Query

无 Thread 的等待接口：

```bash
curl -s -X POST http://localhost:2026/api/runs/saas-query/wait \
  -H 'Content-Type: application/json' \
  -H 'X-SaaS-Authorization-Context: <signed-jwt>' \
  -d '{
    "input":{"messages":[{"role":"user","content":"统计我可见的场地数量。"}]},
    "context":{"model_name":"configured-model"}
  }'
```

需要 SSE 时使用 `/api/runs/saas-query/stream`；已有 Thread 时使用
`/api/threads/{thread_id}/runs/saas-query/{wait,stream}`。签名上下文携带 tenant、
principal、roles、Scope 和 permission version。调用方不能在用户问题中传入数据库或
Scope，也不能用客户端 context 覆盖受信任授权上下文。

完整接口说明见 [backend/docs/API.md](./backend/docs/API.md)。

## Python Client 与 TUI

配置模型后，可以不启动 Frontend，直接使用内嵌 Harness：

```python
from deerflow.client import DeerFlowClient

client = DeerFlowClient()
print(client.chat("介绍这个工作区的主要能力"))

for event in client.stream("列出下一步", thread_id="demo-thread"):
    print(event)
```

可选终端工作台复用同一 Client 和持久化层：

```bash
cd backend
uv run deerflow --tui
uv run deerflow --print "输出当前状态摘要"
uv run deerflow --json "列出已配置工具"
```

`deerflow` 命令和 Python import 命名空间保持不变。

## 定时任务与 IM Channel

在 `config.yaml` 中启用 `scheduler.enabled` 后，可通过 Gateway 的 scheduled-task API
创建周期任务。定时运行复用正常 Run 生命周期，但属于非交互运行，不会提供
`ask_clarification`。请先配置模型和持久化 Run 存储。

Gateway 还支持 Telegram、Slack、Feishu/Lark、DingTalk、Discord、WeChat/WeCom 等
Channel。长轮询或 WebSocket 通道通常不需要公网回调地址；凭据应放在 `.env` 和通道
配置中。详见 [backend/docs/IM_CHANNEL_CONNECTIONS.md](./backend/docs/IM_CHANNEL_CONNECTIONS.md)。

## Evals

Fake IAM/Domain 服务会继续验证控制 Token、Worker Token、JWT、角色、Scope、
permission version、审批流、Action 关联请求头和 `Idempotency-Key`。

```bash
make eval-fixture
make eval-smoke
```

Evals 是可选的一次性 CLI 流程，不是常驻服务。内置 12 个 case，覆盖语义读取、路由、
Scope 拒绝、Action 审批、Action 执行、幂等和禁止副作用。Fixture 只提供虚构的
IAM/Domain 状态，eval compose overlay 会启用内存 SQLite 数据。

本地运行时，在 `.env` 配置 eval 专用变量，在一个终端启动 Fixture，在另一个终端启动
Gateway/Semantic/Worker，然后执行：

```bash
make eval-fixture
make eval-smoke EVALS_GATEWAY_URL=http://127.0.0.1:8001
```

Docker 环境可将基础 compose 与 `docker/docker-compose-evals.yaml` 叠加使用；该 overlay
会启动 Fixture、Semantic API、Action Worker，并在本机端口启用
`DEER_FLOW_SEMANTIC_DEMO_DATA=true`。Evals 需要已配置模型和短期 eval Token，禁止在
生产环境复用 eval 密钥或 Fixture Token。

## 开发与安全

修改前请阅读 [AGENTS.md](./AGENTS.md)、[backend/AGENTS.md](./backend/AGENTS.md) 和
[frontend/AGENTS.md](./frontend/AGENTS.md)。功能和修复必须包含测试，并同步更新用户
文档与架构说明。

不要提交 `.env`、运行时配置、`.deer-flow/`、凭据、数据库导出、租户数据、支持包
或私有验收证据。部署与漏洞报告要求见 [SECURITY.md](./SECURITY.md)。

## License 与来源

项目使用 [MIT License](./LICENSE)，并保留原有 ByteDance 与 DeerFlow Authors 版权
声明。上游来源和致谢见 [ORIGINAL_README.md](./ORIGINAL_README.md)。
