# 视频处理 Agent：当前进度与待开发适配需求

> 本文档是 Harness 接入视频业务的阶段性记录，初始快照日期为
> **2026-07-25**，最近更新日期为 **2026-08-28**。
>
> 当前代码状态：Video Analysis 的 3 个 Functions 已实现 HTTP POC；
> Video Processing 的 submit/status 已实现 HTTP POC，result 尚未实现；
> Model Training 的 3 个 Functions 已注册 Tool Schema 和安全占位处理。

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

Harness Service 和 Business Service 保持隔离，不共享源代码、文件系统或
数据库。当前统一由 Harness 中已注册的业务 Functions 调用受信任的 Business
HTTP API；LLM 不能直接构造 URL、HTTP 请求或认证信息。

```text
User / 上层应用
        |
        v
Harness Service
  - LLM Agent
  - Tool Registry
  - Business HTTP Functions
  - raw video workspace
        |
        | Registered Function + HTTP API Contract
        v
Business Backend APIs
  - Video Processing
  - Model Training
  - Video Analysis
        |
        v
Business-owned datasets / models / results
```

Analysis、Processing 和未来的 Training HTTP 请求都由 Harness Function
执行。Business API 地址来自 Harness 的受信任配置，不从用户输入或 LLM
arguments 获取。

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
- 将内部记录转换成不包含物理路径的 Agent-safe Function 返回值。

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
`dataset_ref`、`model_name` 和 `task_id` 等逻辑引用。当前 Processing 的
`raw_video_refs` 是 Harness Workspace 中用户上传视频的路径，由 Harness
Function 读取并通过 multipart HTTP 上传；视频二进制内容不进入 LLM context。

## 5. 当前九个业务 Functions

| 模块 | Function | Tool Schema | Python 骨架 | Backend 实现 |
|---|---|---:|---:|---:|
| Analysis | `submit_video_analysis` | 已注册 | 已实现 | HTTP POC |
| Analysis | `get_video_analysis_status` | 已注册 | 已实现 | HTTP POC |
| Analysis | `get_video_analysis_result` | 已注册 | 已实现 | HTTP POC |
| Processing | `submit_video_processing` | 已注册 | 已实现 | HTTP POC |
| Processing | `get_video_processing_status` | 已注册 | 已实现 | HTTP POC |
| Processing | `get_video_processing_result` | 已注册 | 已创建 | 未连接 |
| Training | `submit_model_training` | 已注册 | 已创建 | 未连接 |
| Training | `get_model_training_status` | 已注册 | 已创建 | 未连接 |
| Training | `get_model_training_result` | 已注册 | 已创建 | 未连接 |

当前四个占位 Function 被调用时会返回受控错误：

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

### Video Processing

```text
submit_video_processing(scenario, raw_video_refs, idempotency_key)
get_video_processing_status(task_id)
get_video_processing_result(task_id)
```

- `raw_video_refs` 是 Harness 服务器上一个或多个用户上传视频的文件路径；
  相对路径从 Agent Workspace 解析，并由 Harness multipart 上传。
- `submit` 和 `status` 的状态及返回 envelope 与 Analysis 对齐。
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
└── model_training.py

tests/
├── test_video_analysis.py
├── test_video_analysis_unit.py
├── test_video_processing.py
└── test_model_training.py
```

- `test_video_analysis.py` 是需要显式启用的真实 HTTP 集成测试。
- `test_video_analysis_unit.py` 验证已实现的 Analysis Functions。
- `test_video_processing.py` 验证 Processing submit/status HTTP POC、Tool Schema
  和尚未实现的 result。
- `test_model_training.py` 验证 Training 函数签名、Tool Schema、注册和受控
  未实现错误。
- 当前三个业务单元测试文件共 37 个相关测试通过。

## 8. 当前限制

- Video Analysis 和 Video Processing 仍是直接 HTTP POC，尚未加入生产级
  服务认证、限流、审计和可恢复任务存储。
- Analysis Tool Contract 仍支持 Business 服务器路径和 COS 路径；Processing
  当前只支持 Harness 服务器上的上传文件。
- Video Processing result 和三个 Model Training Functions 尚未实现。
- Harness 负责通过 HTTP 上传 raw video；应继续验证大文件超时、流式传输、
  文件大小限制和失败重试，不应将视频 Base64 放入 LLM Tool Arguments。
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
| `VIDEO_ANALYSIS_API` | 已接入 | Video Analysis HTTP API |
| `VIDEO_PROCESSING_API` | 已接入 | Video Processing HTTP API |
| `MODEL_TRAINING_API` | 待接入 | Model Training HTTP API |

`BUSINESS_API_*`、`TASK_API_*`、`VIDEO_OUTPUT_DIR` 和 `TASK_LOG_DIR` 等早期预留
变量尚未接入当前代码。Harness 可持有调用 Business API 所需的最小权限
服务凭据，但不应持有 processed dataset 目录或 model 目录的文件系统访问权。

## 10. 待开发：P0（九个 Functions POC）

- [ ] 与 Business 同事确认 Video Processing 的 input、status、result 和 error
  Contract。
- [ ] 与 Business 同事确认 Model Training 的 input、status、result 和 error
  Contract。
- [x] 实现 `submit_video_processing` 和 `get_video_processing_status` 的
  Harness HTTP 调用。
- [ ] 实现 `get_video_processing_result` 的 Harness HTTP 调用。
- [ ] 实现 `submit_model_training`、`get_model_training_status` 和
  `get_model_training_result` 的 Harness HTTP 调用。
- [ ] 定义 raw video 从 Harness 上传到 Business API 的大文件传输、
  超时、重试和大小限制 Contract。
- [ ] 统一三个模块的 submit/status/result envelope，但不擅自重命名 Business
  Backend 的权威状态。
- [ ] 为两个新模块实现参数校验、幂等、超时、错误转换和安全
  result rendering。
- [ ] 实现 Scenario Registry，统一管理 scenario、允许的操作和 Business
  API 映射。
- [ ] 使用 SQLite 或 PostgreSQL 保存可恢复的业务任务引用，不依赖 LLM
  session 作为权威状态。
- [ ] 为未实现 Functions 增加 HTTPX Mock 测试；实现后再增加
  显式启用的真实 Business HTTP 集成测试。
- [ ] 当 scenario 或必填参数不明确时让 Agent 请求最小必要补充，不得猜测或
  生成未注册引用。

## 11. 待开发：运行时 Agent `CLAUDE.md` 适配

当前 [agent_operation.md](agent_operation.md) 是未来完整业务 Agent 的运行规则草稿。
它暂时保存在 Harness 仓库中用于版本管理，尚未作为生产运行时
`CLAUDE.md` 部署。

- [ ] 将运行时能力限定为当前 9 个 Functions，将部署、回滚、日志读取等
  未注册能力放入 future extension。
- [ ] 明确 Tool 可用的条件为 Schema 已注册、handler 已实现、Business API
  已配置且 policy 允许；不能只以“已注册”判断可用。
- [ ] 写入 User / Agent / Business Backend 三层信息视图和输出过滤规则。
- [ ] 规定 dataset manifest、`dataset_ref` 和 `model_name` 只能用于 Agent 内部
  Tool 编排，不向普通用户展示。
- [ ] 更新 Workspace 边界：Harness 只保存 raw videos 和安全逻辑引用，
  processed datasets、labels 和 model artifacts 由 Business Backend 所有。
- [ ] 加入六个 Skeleton Functions 的精确参数，并明确当前调用只能得到
  `registered but not implemented` 错误。
- [ ] 在 Business Contract 确认后补充 Processing/Training 的返回字段、权威
  状态、幂等、超时、重试和轮询规则。
- [ ] 使用 HTTPX Mock 验证场景路由、缺失参数、Tool 不可用、越权请求、
  异步状态和 Prompt Injection 防护。
- [ ] 审核通过后，将 `agent_operation.md` 正式部署为 Agent Data Folder 中只读的
  `CLAUDE.md`。

## 12. 待开发：P1（生产级业务接入）

- [ ] 为 Business HTTP API 实现服务身份认证、最小权限、超时、
  限流和错误分类。
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
- [ ] 针对 Prompt Injection、恶意视频元数据、伪造 manifest、恶意 API 结果和
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
- [ ] 不创建、复制、打印或覆盖 `.env`、LLM API Key 和 Business API 凭据。
- [ ] 更新虚拟环境和 Python 依赖，并在需要时执行配置迁移。
- [ ] 重启或平滑重载 Harness，检查 `CLAUDE.md` 加载、Business API 连通性、九个 Tool
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
