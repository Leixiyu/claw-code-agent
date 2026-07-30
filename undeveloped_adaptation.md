# 视频处理 Agent：当前进度与待开发适配需求

> 本文档是项目接入视频业务的阶段性记录，初始快照日期为 **2026-07-25**，
> 最近更新日期为 **2026-07-30**。
> 当前目标是先完成本地 Agent Harness 的部署和验证；视频处理项目、业务接口及模型服务仍在设计中。

## 目标业务

Agent 最终需要根据用户 Prompt 和视频信息识别业务场景，并完成以下流程：

1. 将用户上传的视频提交到对应场景的视频分析接口。
2. 查询分析任务进度并返回分析结果。
3. 将视频或数据集提交到对应场景的模型训练接口。
4. 查询训练进度、验证训练结果。
5. 在满足指标和审批条件后部署模型。
6. 使用已部署模型处理后续同类视频。

视频二进制数据不应放入 LLM 上下文。推荐先写入对象存储或共享文件系统，仅向 Agent 提供
`video_id`、对象地址、元数据、用户描述和任务状态摘要。

## 推荐架构边界

```text
用户 Prompt / 视频元数据
          |
          v
Qwen：识别 scenario、operation 和缺失参数
          |
          v
Agent Harness：选择经过注册和授权的业务工具
          |
          v
视频分析 API / 模型训练 API / 模型部署 API
          |
          v
持久化任务状态 + 异步 Worker + Webhook/轮询
          |
          v
Agent 恢复任务并向用户汇报进度和结果
```

Qwen 和 Harness 负责意图理解与受控工具选择，不负责直接生成任意 URL、认证 Header、
Shell 命令或模型部署命令。场景到接口、模型和工作流的映射应由后端配置决定。

## 已完成

- [x] 保留上游 Git 历史，并建立独立项目仓库。
- [x] 支持 OpenAI-compatible Chat Completions 和标准 `tools/tool_calls` Agent loop。
- [x] 可接入阿里云百炼 Qwen API；当前本地配置默认使用 `qwen3-coder-next`。
- [x] CLI 和 GUI 启动时自动读取当前启动目录的 `.env`。
- [x] 外部进程环境变量优先于 `.env`。
- [x] `DASHSCOPE_API_KEY` 可自动映射为 Harness 使用的 `OPENAI_API_KEY`。
- [x] `AGENT_WORKSPACE` 可作为 CLI 和 GUI 的默认工作目录。
- [x] 新保存的 Agent session 不再持久化模型 API Key。
- [x] 后台 Agent 通过进程环境继承 API Key，不再将 Key 写入命令行和后台任务记录。
- [x] `.env` 已被 Git 忽略，并在本地设置为仅当前用户可读写。
- [x] 已有文件读取、搜索、写入、Shell、后台任务、日志、Session、预算和结构化输出等基础能力。
- [x] `.env` 加载、Agent runtime、Session、后台任务和模型兼容层等 147 个核心测试通过。

## 当前限制

- `workflow_run` 当前只记录一次运行，不会真正依次执行工作流步骤。
- `web_fetch` 只适合读取普通 HTTP GET 文本，不能完成带认证的业务 POST、文件上传和幂等调用。
- 后台任务状态主要表示 Agent 进程是否运行，不代表视频分析或训练任务的真实进度。
- 当前文件工具主要面向文本；不应使用 `read_file` 将视频内容传给 LLM。
- 通用 Shell 不是操作系统级沙箱，生产环境不应依赖 Shell 调用业务接口。
- `.port_sessions` 适合本地调试，不适合作为生产任务数据库。
- `BUSINESS_API_*`、`TASK_API_*` 和 `VIDEO_*` 环境变量目前只是预留，尚未连接业务工具。
- 当前本地环境尚未完成 GUI 集成测试所需依赖的部署验证。

## 环境变量约定

| 变量 | 当前状态 | 用途 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 已接入 | 阿里云百炼 API Key |
| `OPENAI_API_KEY` | 已接入 | OpenAI-compatible 客户端实际读取的 Key |
| `OPENAI_BASE_URL` | 已接入 | 模型服务地址 |
| `OPENAI_MODEL` | 已接入 | 模型 ID |
| `AGENT_WORKSPACE` | 已接入 | Agent 默认工作目录 |
| `BUSINESS_API_BASE_URL` | 预留 | 视频业务接口根地址 |
| `BUSINESS_API_TOKEN` | 预留 | 视频业务接口凭据 |
| `TASK_API_BASE_URL` | 预留 | 任务状态服务地址 |
| `TASK_API_TOKEN` | 预留 | 任务状态服务凭据 |
| `VIDEO_INPUT_DIR` | 预留 | 视频输入或暂存目录 |
| `VIDEO_OUTPUT_DIR` | 预留 | 视频结果目录 |
| `VIDEO_TEMP_DIR` | 预留 | 视频处理中间文件目录 |
| `TASK_LOG_DIR` | 预留 | 业务任务日志目录 |

## 待开发：运行时 Agent `CLAUDE.md` 适配

当前 [agent_operation.md](agent_operation.md) 是视频业务 Agent 运行规则的
first draft。它暂时保存在 Harness 仓库中用于版本管理，尚未作为正式运行时
`CLAUDE.md` 部署到 Agent Data Folder。

真实业务代码、接口和场景定义进入项目后，需要完成：

- [ ] 根据正式的 `scenario` 和 `operation` 枚举更新场景识别与路由规则。
- [ ] 根据注册后的业务 Function Call Schema 更新工具名称、必填参数、返回字段和错误处理规则。
- [ ] 根据真实工作流更新分析、训练、验证、部署、回滚及终态判断规则。
- [ ] 补充用户、项目、租户权限，以及训练、部署和破坏性操作的审批要求。
- [ ] 将幂等键、任务去重、超时、重试、Webhook、轮询和任务恢复策略与后端实现对齐。
- [ ] 删除只适用于开发阶段的占位说明，避免把开发路线图重复放入运行时 Prompt。
- [ ] 使用 Mock API 验证场景路由、缺失参数、低置信度、工具缺失、越权请求和异步状态处理。
- [ ] 接入真实 API 后验证工具选择、参数生成、任务恢复、结果汇报和 Prompt Injection 防护。
- [ ] 审核通过后，将 `agent_operation.md` 的正式版本部署为 Agent Data Folder 中只读的 `CLAUDE.md`。
- [ ] 验证生产业务 Agent 只加载 Data Folder 中的 `CLAUDE.md`，不会加载或修改 Harness 仓库中的开发规则。

## 待开发：Harness 自动部署脚本

计划新增 `auto-deploy.sh`，用于在 Harness 仓库代码更新后，将指定 Git
版本自动部署到目标服务器。

当前尚未确定最终服务器、代码目录、Agent Data Folder、运行用户、服务名称和触发方式，
因此本阶段只记录需求，不创建或执行脚本。

脚本至少需要完成：

- [ ] 将服务器上的 Harness 仓库更新到配置的 Git remote、branch 和 commit。
- [ ] 默认使用可审计的 fast-forward 更新方式，不静默覆盖服务器上的未提交修改。
- [ ] 检查配置的 Agent Data Folder 是否存在；不存在时创建目录及必要的运行子目录。
- [ ] 根据服务器运行用户设置 Data Folder 的 owner、group 和最小必要权限。
- [ ] 将部署版本中的 `agent_operation.md` 复制到 Agent Data Folder，并命名为 `CLAUDE.md`。
- [ ] 使用临时文件加原子替换更新 `CLAUDE.md`，避免 Agent 读取到只复制了一部分的文件。
- [ ] 将运行时 `CLAUDE.md` 设置为只读，防止业务 Agent 修改自身运行规则。
- [ ] 不覆盖 Data Folder 中已有的视频、任务状态、结果、日志、Session 或其他业务数据。
- [ ] 不创建、复制、打印或覆盖 `.env`、API Key 和业务 Token；部署前只检查所需配置是否存在。
- [ ] 根据部署方式更新虚拟环境和 Python 依赖，并在需要时执行数据库或配置迁移。
- [ ] 重启或平滑重载 Harness 服务，并执行 CLI、GUI/API 和 `CLAUDE.md` 加载检查。
- [ ] 记录部署时间、Git commit、目标目录、服务状态和健康检查结果。
- [ ] 部署或健康检查失败时停止流程，并支持恢复到上一个已验证版本。
- [ ] 确保重复执行脚本不会重复破坏目录、数据或服务状态。

后续需要确定的配置包括：

- `REPOSITORY_DIR`
- `GIT_REMOTE`
- `GIT_BRANCH`
- `AGENT_DATA_DIR`
- `RUNTIME_USER`
- `RUNTIME_GROUP`
- `VENV_DIR`
- `SERVICE_NAME`
- `HEALTH_CHECK_COMMAND` 或 `HEALTH_CHECK_URL`
- 自动触发方式，例如 CI/CD、Webhook、定时任务或服务器端部署服务

自动触发机制应调用 `auto-deploy.sh`，但不应把 Git 凭据、服务器密钥或业务密钥直接写入脚本。

## 待开发：P0（最小可行 POC）

- [ ] 定义有限且可校验的 `scenario` 和 `operation` 枚举。
- [ ] 让 Qwen 输出结构化路由结果：
  `scenario`、`operation`、`confidence`、`required_inputs`。
- [ ] 实现 Scenario Registry，统一维护场景、工具、接口和允许使用的模型。
- [ ] 实现 `submit_video_analysis` 工具。
- [ ] 实现 `get_analysis_status` 和 `get_analysis_result` 工具。
- [ ] 实现 `submit_model_training` 工具。
- [ ] 实现 `get_training_status` 和 `get_training_result` 工具。
- [ ] 使用 Mock API 模拟分析和训练任务，先验证 Agent 的场景路由与工具选择。
- [ ] 使用 SQLite 或 PostgreSQL 保存业务任务，而不是依赖 Agent session。
- [ ] 建立基础异步状态机和可恢复任务执行。
- [ ] 当场景置信度不足或输入参数缺失时，要求用户确认或补充信息。

建议的初始状态机：

```text
视频分析：
uploaded -> classified -> analysis_queued -> analyzing -> succeeded/failed

训练部署：
training_queued -> training -> validating -> awaiting_approval
                -> deploying -> active/failed/rolled_back
```

## 待开发：P1（真实接口接入）

- [ ] 增加视频上传、对象存储和 `video_id` 管理。
- [ ] 为业务接口实现统一认证、超时、指数退避、限流和错误分类。
- [ ] 所有创建类接口支持 idempotency key 和请求去重。
- [ ] 支持业务 Webhook，并验证回调签名。
- [ ] 对不支持 Webhook 的接口实现低频、可恢复的后台轮询。
- [ ] 实现任务取消、失败补偿和超时处理。
- [ ] 将业务 API 地址映射放入注册表，不允许 LLM 提供任意 URL。
- [ ] 为训练、部署及高成本操作增加明确的审批步骤。
- [ ] 为 Prompt、文件、任务和业务接口增加用户及项目级授权检查。

## 待开发：P2（模型部署与生产能力）

- [ ] 建立 Model Registry，记录场景、模型版本、训练数据、评估指标和部署状态。
- [ ] 建立训练结果验收阈值，未达标模型禁止部署。
- [ ] 支持灰度部署、健康检查、流量切换和自动回滚。
- [ ] 保留上一稳定模型版本，并支持人工回滚。
- [ ] 增加 Trace ID、结构化日志、指标、告警和完整操作审计。
- [ ] 增加多租户数据隔离、配额、并发限制和成本控制。
- [ ] 使用容器、非 root 用户、只读挂载和网络白名单隔离 Agent。
- [ ] 针对 Prompt injection、恶意日志内容和越权工具调用建立安全测试。
- [ ] 对候选 Qwen 模型进行场景分类、工具调用成功率、延迟和成本评测。

## POC 验收标准

首个 POC 至少应满足：

1. 两个不同视频场景可以通过 Prompt 稳定区分。
2. Agent 只能从注册过的场景、操作和业务工具中选择。
3. 分析与训练任务可以提交、持久化、恢复和查询。
4. 重复请求不会创建重复任务。
5. 低置信度分类不会自动执行。
6. 训练完成后必须经过指标验证和审批，不能无条件部署。
7. API Key、业务 Token、视频内容和敏感日志不会进入 Git 或普通任务日志。
8. Agent 进程重启后仍可从任务数据库恢复真实业务进度。
