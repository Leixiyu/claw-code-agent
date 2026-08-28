# 视频处理 Agent：当前进度与待开发适配需求

> 本文档是 Harness 接入视频业务的阶段性记录，初始快照日期为
> **2026-07-25**，最近更新日期为 **2026-08-23**。
>
> 当前代码状态：Video Analysis 的 3 个 Functions 已实现 HTTP POC；
> Video Processing 和 Model Training 的 6 个 Functions 已注册 Tool
> Schema、函数签名和安全占位处理，但尚未连接 Business MCP Backend。

## 1. 目标业务

Video Agent System 最终负责协调三个业务模块：

1. **Video Processing**：将用户上传的一批 raw videos 交给 Business
   Backend 进行标注和数据集构建。
2. **Model Training**：使用 Processing 产生的 dataset reference 训练新模型。
3. **Video Analysis**：使用已有模型或训练完成的模型分析新视频，并向用户
   返回分析结果。

LLM 只负责理解用户意图、识别 `scenario` 和选择受控 Function。视频
标注、文件保存、数据集构建、模型训练和推理均由 Business Backend 完成。
视频二进制内容不应加载到 LLM context。

## 2. 最新系统边界

Harness Service 和 Business Service 必须完全隔离，不共享源代码、文件系统或
数据库。最终业务控制入口只能是已注册的 MCP Tools。

```text
User / 上层应用
        |
        v
Harness Service
  - LLM Agent
  - Tool Registry
  - MCP Client
  - raw video workspace
        |
        | MCP Tool Contract
        v
Business MCP Server
  - Video Processing
  - Model Training
  - Video Analysis
        |
        v
Business-owned datasets / models / results
```

当前 Video Analysis 仍通过 `VIDEO_ANALYSIS_API` 直接调用 HTTP API，这是
过渡期 POC，还不符合最终 MCP-only 边界。

## 3. 不同角色的信息视图

### User

用户只需要知道：

- 视频是否开始标注、是否标注完成或失败；
- 模型训练是否开始、是否完成或失败；
- 视频分析是否开始、是否完成或失败；
- 最终视频分析结果。

用户不应看到 dataset reference、dataset manifest、model name、model artifact、
Backend 物理路径或内部日志。

### Agent

Agent 可以知道：

- 用户的问题和意图；
- 当前注册的 9 个 Function Schema 及其安全返回值；
- Harness Workspace 中用户上传的 raw videos 或其受控引用；
- Processing 返回的 public dataset manifest 和 `dataset_ref`；
- Training 返回的逻辑 `model_name`。

Agent 不知道 Function 的内部处理流程、处理后视频和 label 的物理位置、
model checkpoint 位置、Business 内部 API 或存储路径。

### Business Backend

Business Backend 负责：

- 在对应 `scenario` 下保存 processed datasets、labels 和内部 manifest；
- 在对应 `scenario` 下保存 model artifacts、metrics 和内部 metadata；
- 保存分析任务状态和结果；
- 将内部记录转换成不包含物理路径的 Agent-safe MCP 返回值。

## 4. 数据所有权和文件边界

| 数据 | 所有者 | Agent 可见性 |
|---|---|---|
| 用户上传的 raw videos | Harness | 可通过受控 Workspace 或 opaque reference 使用 |
| processed videos 和 labels | Video Processing Backend | 不可见 |
| public dataset manifest / `dataset_ref` | Business 返回给 Harness | Agent 可见，用户不可见 |
| internal dataset manifest | Video Processing Backend | 不可见 |
| model artifacts / checkpoints | Model Training Backend | 不可见 |
| 逻辑 `model_name` | Business 返回给 Harness | Agent 可见，用户不可见 |
| Video Analysis result | Video Analysis Backend | Agent 可见并可向用户汇报 |

Harness Workspace 不应保存 processed dataset 实体、label 文件、checkpoint 或
model weights。Harness 与 Business 不共享物理路径；Agent Tool Arguments 只传递
`raw_video_ref`、`dataset_ref`、`model_name` 和 `task_id` 等逻辑引用。raw video
二进制内容如需跨边界传输，必须交给 MCP 层的受控 Data Transfer Contract，
不进入 LLM arguments。

## 5. 当前九个业务 Functions

| 模块 | Function | Tool Schema | Python 骨架 | Backend 实现 |
|---|---|---:|---:|---:|
| Analysis | `submit_video_analysis` | 已注册 | 已实现 | HTTP POC |
| Analysis | `get_video_analysis_status` | 已注册 | 已实现 | HTTP POC |
| Analysis | `get_video_analysis_result` | 已注册 | 已实现 | HTTP POC |
| Processing | `submit_video_processing` | 已注册 | 已创建 | 未连接 |
| Processing | `get_video_processing_status` | 已注册 | 已创建 | 未连接 |
| Processing | `get_video_processing_result` | 已注册 | 已创建 | 未连接 |
| Training | `submit_model_training` | 已注册 | 已创建 | 未连接 |
| Training | `get_model_training_status` | 已注册 | 已创建 | 未连接 |
| Training | `get_model_training_result` | 已注册 | 已创建 | 未连接 |

当前六个占位 Function 被调用时会返回受控错误：

```text
<function_name> is registered but not implemented
```

因此“Tool Schema 已注册”不等于“业务能力已可用”。

## 6. 当前 Tool Contract

### Video Analysis

```text
submit_video_analysis(scenario, video_ref, idempotency_key)
get_video_analysis_status(task_id)
get_video_analysis_result(task_id)
```

- `scenario` 当前只支持 `fire_inspection`。
- `video_ref.type` 支持 `local_file` / `upload_file` / `cos_file`。
- 任务状态为 `pending` / `running` / `done` / `failed`。
- `submit` 返回 `task_id`、`status="pending"`、`scenario`、`video_ref`、
  `idempotency_key` 和 `idempotency_replayed`。
- `status` 返回 `task_id`、`status`、`is_terminal` 和 `result_ready`。
- `result` 返回 `task_id`、`status="done"`、`result_count` 和 `results`。

### Video Processing Skeleton

```text
submit_video_processing(scenario, raw_video_refs, idempotency_key)
get_video_processing_status(task_id)
get_video_processing_result(task_id)
```

- `raw_video_refs` 是由 Harness 创建的一个或多个 opaque references。
- `get_video_processing_result` 未来返回 public dataset manifest 和
  `dataset_ref`，不返回 Business 物理路径。

### Model Training Skeleton

```text
submit_model_training(scenario, dataset_ref, idempotency_key)
get_model_training_status(task_id)
get_model_training_result(task_id)
```

- `dataset_ref` 必须来自已完成的 Processing 结果，不是文件系统路径。
- `get_model_training_result` 未来返回逻辑 `model_name` 和安全的公开元数据，
  不返回 model artifact 路径。

Processing 和 Training 的真实状态枚举、result schema 和错误合约尚未与
Business Backend 确认，不应根据文档自行假定。

## 7. 当前代码和测试布局

```text
src/
├── agent_tools.py
├── video_analysis.py
├── video_processing.py
└── model_training.py

tests/
├── test_video_analysis.py
├── test_video_analysis_unit.py
├── test_video_processing.py
└── test_model_training.py
```

- `test_video_analysis.py` 是需要显式启用的真实 HTTP 集成测试。
- `test_video_analysis_unit.py` 验证已实现的 Analysis Functions。
- `test_video_processing.py` 和 `test_model_training.py` 当前只验证函数签名、
  Tool Schema、注册和受控未实现错误。
- 上述三个业务单元测试文件目前共 29 个相关测试通过。

## 8. 当前限制

- Video Analysis 仍是直接 HTTP POC，未迁移到 Business MCP Server。
- Video Analysis 仍可以在 Tool Contract 中接收和返回物理路径，与最终
  opaque-reference 设计尚未完全对齐。
- Video Processing 和 Model Training 只有 Skeleton，没有参数校验、MCP
  调用、幂等持久化、状态处理或 result rendering。
- `MCPRuntime` 当前支持本地 manifest 和 stdio MCP transport；最终
  Business MCP 的部署位置、传输方式、认证和超时尚未确定。
- Harness 和 Business 不共享文件系统，大视频如何跨越 MCP 边界尚需
  明确 Data Transfer Contract；不应将视频 Base64 放入 LLM Tool Arguments。
- 当前只有 `fire_inspection` 一个 scenario，尚无正式 Scenario Registry。
- Agent session 和 `.port_sessions` 不是业务任务的权威数据库。
- Harness 后台进程状态不代表 Business 任务状态。
- 当前没有多租户权限、生产级审计、限流、Webhook、任务恢复和
  保留/删除策略。

## 9. 当前配置约定

| 配置 | 状态 | 用途 |
|---|---|---|
| `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` | 已接入 | LLM provider 凭据 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | 已接入 | OpenAI-compatible 模型服务 |
| `AGENT_WORKSPACE` | 已接入 | Harness 的受控 Agent Workspace |
| `VIDEO_ANALYSIS_API` | 过渡期已接入 | Video Analysis HTTP POC |
| `.claw-mcp.json` / `.mcp.json` | Harness 能力已有 | 发现 MCP Server、Resources 和 Tools |
| Business MCP Server 配置与凭据 | 未确定 | 最终三个业务模块的唯一控制入口 |

`BUSINESS_API_*`、`TASK_API_*`、`VIDEO_OUTPUT_DIR` 和 `TASK_LOG_DIR` 等早期预留
变量尚未接入当前代码。最终 MCP-only 设计不应默认让 Harness 直接持有
Business API Token、processed dataset 目录或 model 目录。

## 10. 待开发：P0（九个 Functions POC）

- [ ] 与 Business 同事确认 Video Processing 的 input、status、result 和 error
  Contract。
- [ ] 与 Business 同事确认 Model Training 的 input、status、result 和 error
  Contract。
- [ ] 定义 raw video 跨越 Harness/Business 边界的 Data Transfer Contract，不向
  LLM 暴露上传凭据或物理路径。
- [ ] 实现 `submit_video_processing`、`get_video_processing_status` 和
  `get_video_processing_result` 的 Business MCP 调用。
- [ ] 实现 `submit_model_training`、`get_model_training_status` 和
  `get_model_training_result` 的 Business MCP 调用。
- [ ] 将 Video Analysis 从直接 HTTP 调用迁移到 Business MCP Tool，同时决定是否
  保留当前 HTTP adapter 作为过渡或测试层。
- [ ] 统一三个模块的 submit/status/result envelope，但不擅自重命名 Business
  Backend 的权威状态。
- [ ] 为两个新模块实现参数校验、幂等、超时、错误转换和安全
  result rendering。
- [ ] 实现 Scenario Registry，统一管理 scenario、允许的操作和 Business
  MCP Tool 映射。
- [ ] 使用 SQLite 或 PostgreSQL 保存可恢复的业务任务引用，不依赖 LLM
  session 作为权威状态。
- [ ] 为六个新 Functions 增加 Mock MCP 测试；实现后再增加显式启用的真实
  Business MCP 集成测试。
- [ ] 当 scenario 或必填参数不明确时让 Agent 请求最小必要补充，不得猜测或
  生成未注册引用。

## 11. 待开发：运行时 Agent `CLAUDE.md` 适配

当前 [agent_operation.md](agent_operation.md) 是未来完整业务 Agent 的运行规则草稿。
它暂时保存在 Harness 仓库中用于版本管理，尚未作为生产运行时
`CLAUDE.md` 部署。

- [ ] 将运行时能力限定为当前 9 个 Functions，将部署、回滚、日志读取等
  未注册能力放入 future extension。
- [ ] 明确 Tool 可用的条件为 Schema 已注册、handler 已实现、Business MCP
  已连接且 policy 允许；不能只以“已注册”判断可用。
- [ ] 写入 User / Agent / Business Backend 三层信息视图和输出过滤规则。
- [ ] 规定 dataset manifest、`dataset_ref` 和 `model_name` 只能用于 Agent 内部
  Tool 编排，不向普通用户展示。
- [ ] 更新 Workspace 边界：Harness 只保存 raw videos 和安全逻辑引用，
  processed datasets、labels 和 model artifacts 由 Business Backend 所有。
- [ ] 加入六个 Skeleton Functions 的精确参数，并明确当前调用只能得到
  `registered but not implemented` 错误。
- [ ] 在 Business Contract 确认后补充 Processing/Training 的返回字段、权威
  状态、幂等、超时、重试和轮询规则。
- [ ] 使用 Mock MCP 验证场景路由、缺失参数、Tool 不可用、越权请求、
  异步状态和 Prompt Injection 防护。
- [ ] 审核通过后，将 `agent_operation.md` 正式部署为 Agent Data Folder 中只读的
  `CLAUDE.md`。

## 12. 待开发：P1（生产级业务接入）

- [ ] 为 Business MCP 实现服务身份认证、最小权限、超时、限流和错误分类。
- [ ] 为所有创建类操作实现 application-created idempotency key 和跨实例请求去重。
- [ ] 明确 Business 任务的 Webhook 或可恢复低频轮询机制，并验证回调身份。
- [ ] 建立 dataset 和 model 的 scenario 隔离、版本、保留、归档和删除策略。
- [ ] 建立 Harness 对 raw videos 的用户/项目/租户级授权和文件生命周期。
- [ ] 将 Business 返回值分为 Agent View 和 User View，确保用户无法获得
  dataset、model 和 Backend 内部信息。
- [ ] 增加 Trace ID、结构化日志、指标、告警和完整操作审计。
- [ ] 增加失败补偿、超时处理、安全重试、任务恢复和并发/配额限制。

## 13. 待开发：P2（未来扩展）

- [ ] 建立 Model Registry，记录 scenario、逻辑模型名、版本、训练数据引用、
  评估指标和生命周期。
- [ ] 在业务需求确认后，再设计模型验证、审批、部署、灰度、健康检查和回滚
  Functions；这些能力不属于当前 9 个 Functions。
- [ ] 增加多 scenario 路由、数据隔离和模型选择评测。
- [ ] 针对 Prompt Injection、恶意视频元数据、伪造 manifest、恶意 MCP 结果和
  越权 Tool Call 建立安全测试。
- [ ] 对候选 Qwen 模型进行 scenario 分类、Tool Call 成功率、延迟和成本评测。

## 14. 待开发：Harness 自动部署脚本

计划新增 `auto-deploy.sh`，用于在 Harness 仓库代码更新后，将指定 Git 版本
自动部署到目标服务器。当前尚未确定最终服务器、目录、运行用户、服务名称和
触发方式，因此本阶段只记录需求。

- [ ] 使用可审计的 fast-forward 方式更新指定 Git remote、branch 和 commit，
  不覆盖服务器未提交修改。
- [ ] 检查 Agent Data Folder 是否存在，不存在时创建 Harness 需要的 raw
  video、reference、task、session 和 temp 子目录。
- [ ] 不在 Harness Data Folder 创建 processed dataset 或 model artifact 目录。
- [ ] 按最小必要权限设置运行用户和目录 owner/group。
- [ ] 将部署版本中的 `agent_operation.md` 原子替换到 Agent Data Folder，并命名为
  只读 `CLAUDE.md`。
- [ ] 不覆盖 raw videos、references、任务状态、Session 或其他业务数据。
- [ ] 不创建、复制、打印或覆盖 `.env`、LLM API Key 和 Business MCP 凭据。
- [ ] 更新虚拟环境和 Python 依赖，并在需要时执行配置迁移。
- [ ] 重启或平滑重载 Harness，检查 `CLAUDE.md` 加载、MCP 连接、九个 Tool
  Schema 和基础健康状态。
- [ ] 记录部署时间、Git commit、目标目录、服务状态和健康检查结果，并在
  部署失败时恢复到上一个已验证版本。
- [ ] 确保重复执行脚本不会破坏已有目录、数据或服务状态。

## 15. POC 验收标准

1. Agent 只能从已注册的 scenario、operation 和 9 个 Business Functions 中选择。
2. 未实现或未连接的 Tool 不会返回伪造的成功结果。
3. Processing、Training 和 Analysis 均可通过 `submit -> task_id -> status -> result`
   流程执行和恢复。
4. 创建类请求可幂等重放，不会意外创建重复任务。
5. Harness 和 Business 不共享文件系统；Tool Arguments 只传递受控逻辑引用，
   raw video 数据只能通过经批准的 MCP Data Transfer Contract 跨边界传输。
6. Agent 可使用 public dataset manifest、`dataset_ref` 和 `model_name` 进行内部
   编排，但不向普通用户展示这些信息。
7. 用户只看到 Processing/Training/Analysis 的业务进度、成败和最终 Analysis
   结果。
8. API Key、MCP 凭据、视频二进制内容、Backend 路径和敏感日志不进入 Git、
   LLM context 或普通用户回复。
9. Harness 或 Agent 重启后仍可从任务存储恢复真实 Business 进度。
