# Claw Code Agent 服务器部署指南

本文记录当前 Harness 的服务器部署方法。默认部署方案为：

- Linux 服务器（示例使用 Ubuntu）
- Python 3.10 或更高版本
- Harness 和 GUI 部署在本机
- LLM 通过阿里云百炼 OpenAI-compatible API 调用
- systemd 负责进程守护
- 4090 POC 使用现有非 root 用户 `atis`
- Harness 路径为 `/home/atis/Documents/RAY/claw_code_agent`
- 运行数据存放在相邻的 `/home/atis/Documents/RAY/claw_agent_data`
- GUI 只监听 `127.0.0.1`

当前 Harness 是 Agent runtime 和本地管理 GUI。Video Analysis 和 Video
Processing 的 submit/status/result 六个 Functions 已实现 HTTP POC，Model
Training submit/status/result 也已接入；Scenario Registry、Webhook、生产级
任务数据库和多租户授权仍待开发。

文中的 `atis` 和 `/home/atis/Documents/RAY/...` 都是可替换的部署示例，
不是 Harness 的固定要求。下文保留同一组完整路径，便于逐段执行；换服务器时
应统一替换命令、`.env`、`CLAUDE.md` 和 systemd 配置中的对应值。

| 配置项 | 本文示例 | 用途 |
| --- | --- | --- |
| 服务用户 | `atis` | 非 root 用户，拥有运行数据的读写权限 |
| 代码目录 | `/home/atis/Documents/RAY/claw_code_agent` | Git 仓库、虚拟环境和 `.env` |
| 运行数据目录 | `/home/atis/Documents/RAY/claw_agent_data` | 容纳 workspace、会话和运行时 HOME |
| Agent workspace | 运行数据目录下的 `workspace/` | Agent 的业务文件操作根目录，名称和位置可自选 |

部署分三层验收：GUI 服务可用、模型调用可用、业务闭环可用。Backend 未接通时
可以完成前两层；工具已注册或 GUI 已启动，不代表业务任务可以成功执行。

## 1. 部署前检查

服务器至少需要：

```text
Linux
Python >= 3.10
Git
可访问模型 API 的 HTTPS 网络
非 root 运行用户 atis
一个 Agent 工作目录
```

检查 Python：

```bash
python3 --version
```

如果低于 3.10，请先使用操作系统的软件源安装较新的 Python。Ubuntu 可先尝试：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv curl ca-certificates
```

再次确认版本满足要求后再继续。

## 2. 创建运行目录

4090 POC 直接使用现有的 `atis` 用户，不需要再创建 `claw-agent` 系统用户。

代码和运行数据分开放置：

```text
/home/atis/Documents/RAY/claw_code_agent   # Git 代码和 Python 虚拟环境
/home/atis/Documents/RAY/claw_agent_data   # Session、Agent Workspace 和运行时 HOME
```

### 2.1 部署时需要准备的内容

以服务用户 `atis` 执行：

```bash
mkdir -p /home/atis/Documents/RAY/claw_agent_data/workspace/uploads
mkdir -p /home/atis/Documents/RAY/claw_agent_data/runtime-home/.claude
mkdir -p /home/atis/Documents/RAY/claw_agent_data/sessions
chmod 700 /home/atis/Documents/RAY/claw_agent_data
```

- workspace 根目录需提前存在，并允许服务用户读写。它不必叫 `workspace`。
- 第 5.2 节会生成根目录下的 `CLAUDE.md`。
- 原始视频需自行放入或由上传服务写入。`uploads/` 是本文采用的输入目录；
  仅创建空目录不会产生可用的视频输入。
- `runtime-home/` 是本文 systemd 配置的可写 HOME；提前准备它，避免运行时
  组件尝试写入受保护的 `/home/atis/.claude`。
- GUI 会话目录 `sessions/` 保存时也会自动创建；这里提前准备以便检查权限。

最小业务 workspace（完成第 5.2 节并放入测试视频后）：

```text
workspace/                  # 根目录名可自选
├── CLAUDE.md                # 从 agent_operation.md 生成
└── uploads/
    └── example.mp4         # 自行提供的原始视频
```

### 2.2 运行时自动生成的内容

以下目录不用预先创建；相应业务调用首次写入时会创建父目录：

| workspace 内路径 | 生成时机和内容 |
| --- | --- |
| `datasets/` | 获取处理结果后保存 dataset manifest |
| `models/` | 获取训练结果后保存模型元数据 |
| `tasks/analysis/` | 保存或更新分析任务记录 |
| `tasks/processing/` | 保存或更新处理任务记录 |
| `tasks/training/` | 保存或更新训练任务记录 |
| `.port_sessions/business_functions/` | 保存业务幂等、锁等运行状态 |

这些子目录名由当前代码固定，不能仅通过修改提示词改名。
`datasets/` 和 `models/` 保存 Agent 可见的 manifest/元数据，不存训练数据集实体
或模型权重；处理后视频、标签、权重和业务日志由 Business Backend 管理。

这样视频引用、manifest、Task 和 Session 不会进入 Git 仓库。正式生产或
多人共用服务器时，建议再将代码迁移到 `/opt/claw-code-agent`，并使用独立的
`claw-agent` 系统用户。

## 3. 获取代码

如果代码还不存在，首次获取：

```bash
mkdir -p /home/atis/Documents/RAY
git clone \
  https://github.com/Leixiyu/claw-code-agent.git \
  /home/atis/Documents/RAY/claw_code_agent
```

如果代码已经位于该路径，不要再次 clone。

确认版本：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
git branch --show-current
git log -1 --oneline
```

生产部署应记录当前 commit，以便回滚。

## 4. 创建虚拟环境并安装依赖

```bash
cd /home/atis/Documents/RAY/claw_code_agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install . --no-deps
```

验证入口：

```bash
/home/atis/Documents/RAY/claw_code_agent/.venv/bin/claw-code-agent --help
/home/atis/Documents/RAY/claw_code_agent/.venv/bin/claw-code-gui --help
```

`vLLM` 不应安装到这个 Harness 虚拟环境。只有在服务器自行托管模型时，才应为 vLLM
建立独立环境或容器。

## 5. 配置模型和运行目录

4090 是个人测试服务器，因此统一使用 Harness 根目录下的 `.env`：

```text
/home/atis/Documents/RAY/claw_code_agent/.env
```

进入项目并创建或编辑：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
nano .env
```

百炼 API 示例：

```dotenv
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=替换为真实百炼APIKey
OPENAI_MODEL=qwen3-coder-next

AGENT_WORKSPACE=/home/atis/Documents/RAY/claw_agent_data/workspace

VIDEO_ANALYSIS_API=http://video-analysis-host:8000
VIDEO_PROCESSING_API=http://video-processing-host:8000
MODEL_TRAINING_API=http://model-training-host:8000
```

注意：

- `.env` 中直接设置 `OPENAI_API_KEY`，不要使用
  `OPENAI_API_KEY="${DASHSCOPE_API_KEY}"`；systemd `EnvironmentFile` 不执行变量展开。
- `VIDEO_ANALYSIS_API`、`VIDEO_PROCESSING_API` 和 `MODEL_TRAINING_API`
  填写对应 Backend 的
  base URL，不要在末尾手动加具体业务 endpoint。
- 不要在终端、日志、截图或文档中输出真实 API Key。

设置权限：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
chown atis:"$(id -gn atis)" .env
chmod 600 .env
git check-ignore -v .env
```

`600` 表示只有 `atis` 可以读取和修改该文件。

### 5.1 工作目录与配置加载规则

CLI 和 GUI 的 Agent workspace 选择顺序均为：

```text
显式 --cwd > AGENT_WORKSPACE > 启动进程的当前目录
```

例如 GUI 可使用 `--cwd /srv/video-agent-data` 覆盖环境变量。服务器配置使用
绝对路径，避免相对路径随启动位置变化。本文 systemd 不传 `--cwd`，统一以
`.env` 中的 `AGENT_WORKSPACE` 为准。

`.env` 的加载方式：

- 程序只自动读取**启动时当前目录**下的 `.env`，不会搜索代码仓库或 Agent
  workspace；`--cwd` 不会改变这一步的查找位置。
- 自动加载不会覆盖已有进程环境变量。
- 第 6 节的 CLI 示例先显式 `source` 仓库配置；第 7 节由 systemd 的
  `EnvironmentFile` 读取同一文件，因此都不依赖自动查找。

以下三个路径不是同一个配置：

| 配置 | 作用 |
| --- | --- |
| systemd `WorkingDirectory` | 服务进程当前目录，也是部分相对运行状态路径的基准 |
| `AGENT_WORKSPACE` / `--cwd` | Agent 业务工作区根目录 |
| GUI `--session-dir` | GUI 会话保存目录，建议显式传绝对路径 |

CLI 默认会话目录 `.port_sessions/agent` 和默认 scratchpad 路径基于**进程当前
目录**计算，不随 `--cwd` 自动迁移。业务幂等状态则保存在 Agent workspace 的
`.port_sessions/business_functions/`。因此请保留第 6 节的 `cd` 步骤；GUI
服务使用第 7 节明确设置的 `WorkingDirectory` 和 `--session-dir`。

### 5.2 安装运行时 Agent 指令

仓库中的 `agent_operation.md` 是运行时 Agent 指令模板。将其复制到
Agent Workspace 根目录，命名为 `CLAUDE.md`，并把 Workspace 占位符替换为
实际路径：

```bash
sed \
  's|{{AGENT_WORKSPACE_PATH}}|/home/atis/Documents/RAY/claw_agent_data/workspace|g' \
  /home/atis/Documents/RAY/claw_code_agent/agent_operation.md \
  > /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
```

验证文件中已写入实际 Workspace：

```bash
grep -n 'Agent Workspace root' \
  /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
grep -n '/home/atis/Documents/RAY/claw_agent_data/workspace' \
  /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
```

Agent 从工作区中读取这份 `CLAUDE.md`。仓库根目录的 `CLAUDE.md` 用于
开发 Harness 本身，不应复制给运行时 Agent。`agent_integration.md` 是维护者的
集成清单，也不需要复制到 workspace。

`CLAUDE.md` 中的路径只向模型说明边界，不设置程序的工作目录；它必须与实际
`AGENT_WORKSPACE` / `--cwd` 一致。不要启用 `--disable-claude-md`，否则不会自动
加载这份运行指令。

## 6. 先进行命令行 Smoke Test

本节验证模型调用和工作区指令，不提交业务任务。使用服务用户在 Bash 中加载
自己维护的受信任 `.env`（`source` 会执行其中的 Shell 内容），再进行只读测试：

```bash
set -a
source /home/atis/Documents/RAY/claw_code_agent/.env
set +a

cd /home/atis/Documents/RAY/claw_agent_data/workspace
/home/atis/Documents/RAY/claw_code_agent/.venv/bin/claw-code-agent agent \
  "只检查 Agent Workspace 并简要说明可见目录，不要修改任何内容。"
```

验收：

- 能连接 Qwen API。
- Agent 能返回响应。
- Agent 能读取 Workspace 中的 `CLAUDE.md`，其中不再包含
  `{{AGENT_WORKSPACE_PATH}}` 占位符。
- 默认不开放通用文件写工具；业务 Functions 仍可按实现写入
  授权 Workspace 内的 Task JSON 和 public manifest。
- 默认不能执行 Shell。
- CLI 的 session 和幂等 POC 状态可写入
  `/home/atis/Documents/RAY/claw_agent_data/workspace/.port_sessions`。
- 输出和 session 中不应出现 API Key。

如果这一步失败，先不要创建 systemd 服务。优先检查：

```text
Python 版本
requirements 安装结果
OPENAI_BASE_URL
OPENAI_MODEL
服务器到模型 API 的网络
API Key 的区域和权限
AGENT_WORKSPACE 是否存在且服务用户可读写
Workspace/CLAUDE.md 是否存在且占位符已替换
```

业务 API 配置在下一节验收；它们缺失不应被误诊为模型连接失败。

### 6.1 验证业务 Functions

先在仓库中运行不依赖真实 Backend 的测试：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.test_video_analysis_unit \
  tests.test_video_processing \
  tests.test_model_training
```

这些单元测试不能证明真实 Backend 已接通。测试 Backend 或 SSH tunnel 可用后，
提供一个已存在的测试视频，完成 Analysis 和 Processing 的 submit/status/result
闭环；再使用 Processing 生成的 ready manifest，在取得所需训练批准后完成
Training 闭环。不要手写 dataset manifest 或模型元数据来代替业务结果。

Backend 尚未就绪时，记录本节“未验收”，仍可继续启动 GUI；在本节通过前，
只报告 Harness/模型可用。业务验收时检查：

- Agent 可见九个业务 Functions，三个模块都可完成
  submit/status/result HTTP POC。
- submit 后生成 `workspace/tasks/<module>/<task_id>.json`。
- status 调用会更新对应 Task JSON。
- `status="done"` 且 `result_ready=true` 后，同一轮调用对应 result Function；
  不只报告任务完成而遗漏结果获取。
- Processing result 完成后生成 `workspace/datasets/<dataset_id>.json`，函数只
  返回 Agent 可见的 `manifest_path`，不泄露 Backend 物理路径。
- Model Training Submit 会先校验
  `workspace/datasets/<dataset_ref>.json` 存在、身份与 scenario 匹配且
  `status="ready"`，再验证 `POST /train` 纯 JSON 请求；Status 可验证
  `GET /status/{task_id}`；Result 可验证
  `GET /result/{task_id}`、`workspace/models/<model_id>.json` 和
  `workspace/tasks/training/<task_id>.json`。

## 7. 创建 systemd 服务

创建 `/etc/systemd/system/claw-code-agent.service`：

```ini
[Unit]
Description=Claw Code Agent local GUI
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=atis
WorkingDirectory=/home/atis/Documents/RAY/claw_agent_data
EnvironmentFile=/home/atis/Documents/RAY/claw_code_agent/.env
Environment=HOME=/home/atis/Documents/RAY/claw_agent_data/runtime-home

ExecStart=/home/atis/Documents/RAY/claw_code_agent/.venv/bin/claw-code-gui \
  --host 127.0.0.1 \
  --port 8765 \
  --no-browser \
  --session-dir /home/atis/Documents/RAY/claw_agent_data/sessions

Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=/home/atis/Documents/RAY/claw_code_agent
ReadWritePaths=/home/atis/Documents/RAY/claw_agent_data

[Install]
WantedBy=multi-user.target
```

`HOME` 指向单独的运行时目录，`ReadWritePaths` 为运行数据目录提供可写范围。
若自选的 workspace、会话或运行时 HOME 放在这个范围之外，需同步调整
`ReadWritePaths` 并确认服务用户权限；仅修改 `.env` 不会改变 systemd 的文件系统
限制。`WorkingDirectory`、代码目录和可写范围应在启动前存在。

修改 unit 文件后执行 `daemon-reload` 再重启；只修改 `.env` 时重启服务即可读取
新配置。路径变化时还要重新生成包含相同实际路径的 workspace `CLAUDE.md`。

默认不添加 `--allow-shell`、`--unsafe` 或 `--allow-write`。只有在明确完成业务工具和权限
设计后，才应按最小权限原则开放能力。

检查并启动：

```bash
sudo systemd-analyze verify /etc/systemd/system/claw-code-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now claw-code-agent
sudo systemctl status claw-code-agent
```

## 8. 验证运行状态

在服务器上检查 GUI 状态接口：

```bash
curl --fail http://127.0.0.1:8765/api/state
```

检查返回的 `cwd` 是否等于预期 Agent workspace、`session_directory` 是否为
配置的会话目录。HTTP 成功只证明 GUI 状态接口可响应，不证明模型或 Backend
可用；通过第 9 节访问 GUI 后再执行第 6 节的只读请求，并按第 6.1 节验证业务。

查看日志：

```bash
sudo journalctl -u claw-code-agent -n 200 --no-pager
sudo journalctl -u claw-code-agent -f
```

查看监听端口：

```bash
ss -lntp | grep 8765
```

正常情况下只能看到 `127.0.0.1:8765`，不应监听公网地址。

## 9. 安全访问 GUI

当前 GUI 没有适合公网暴露的完整身份认证和租户授权。不要直接使用：

```text
--host 0.0.0.0
```

推荐从本地电脑建立 SSH 隧道：

```bash
ssh -L 8765:127.0.0.1:8765 your-user@your-server
```

然后在本地浏览器访问：

```text
http://127.0.0.1:8765
```

如果未来必须通过域名访问，应在反向代理层增加：

- HTTPS
- 用户认证
- IP 或 VPN 限制
- 请求大小限制
- 访问日志脱敏
- API 限流

在这些能力完成前不要公开 GUI。

## 10. 更新部署

安排维护窗口，暂停新请求并记录运行中业务的 `task_id`。等待当前 Agent 回合
结束再停止服务；停止 Harness 不会取消已经提交到 Backend 的任务。

更新前记录当前 commit 和已安装依赖：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
git rev-parse HEAD
.venv/bin/pip freeze > /home/atis/Documents/RAY/claw_agent_data/requirements.before-update.txt
```

停止服务后，备份 `sessions/`、workspace 的 `CLAUDE.md`、`tasks/`、`datasets/`、
`models/`、`.port_sessions/` 及需要保留的其他运行状态到独立备份位置；只备份已
存在的目录。保留 `.env` 的受控备份和上述版本记录，备份也应限制访问权限。
不要在有 Harness 写入的同时复制状态文件。

```bash
sudo systemctl stop claw-code-agent
```

完成备份后再继续。

拉取并重新安装（任一步失败即停止，修复或回滚后再启动服务）：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
git fetch origin
git switch main
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install . --no-deps
sed \
  's|{{AGENT_WORKSPACE_PATH}}|/home/atis/Documents/RAY/claw_agent_data/workspace|g' \
  agent_operation.md \
  > /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
sudo systemctl start claw-code-agent
```

更新后验证服务状态：

```bash
sudo systemctl status claw-code-agent
curl --fail http://127.0.0.1:8765/api/state
sudo journalctl -u claw-code-agent -n 100 --no-pager
```

随后重复模型只读检查；用原 `task_id` 查询更新前运行中的业务任务，必要时获取
结果，不要重新提交。若提交时响应丢失，应先通过已有幂等记录和 Backend 状态
核对，不要假设没有提交成功。新版本应另外完成可控业务验收。

不要在生产服务器上直接修改仓库文件。开发修改应先提交到 GitHub，再通过上述流程部署。

## 11. 回滚

暂停新请求、记录任务 ID 并停止服务，然后使用更新前记录的 commit。
以下命令中的 `<known-good-commit>` 必须替换成真实 commit：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
sudo systemctl stop claw-code-agent
git switch --detach <known-good-commit>
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install . --no-deps
sed \
  's|{{AGENT_WORKSPACE_PATH}}|/home/atis/Documents/RAY/claw_agent_data/workspace|g' \
  agent_operation.md \
  > /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/claw_agent_data/workspace/CLAUDE.md
sudo systemctl start claw-code-agent
```

按第 8 节和模型只读检查确认恢复，再用原 task ID 核对 Backend 状态。
代码回滚不等于业务任务或数据回滚：不要直接用旧状态备份覆盖更新后新增的任务
记录，否则可能丢失引用并重复提交。若新旧状态格式不兼容，应停止服务并制定
迁移/恢复方案。

`requirements.txt` 若使用版本范围，重新安装不保证还原旧依赖；需要精确恢复时，
依据更新前保存的依赖清单重建并验证虚拟环境。确认恢复后，可以保持该 commit，
或在仓库中创建正式回滚提交后重新部署 `main`。

## 12. 自托管 Qwen（可选）

如果未来不使用百炼 API，而是在独立 GPU 服务上运行 Qwen，只需要替换模型配置：

```dotenv
OPENAI_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY=local-token
OPENAI_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
```

模型服务必须支持：

- `/v1/chat/completions`
- `tools`
- `tool_choice=auto`
- 标准 `tool_calls`
- 对应模型的 tool-call parser

Harness 和模型服务应使用不同的 systemd service 或容器。不要让模型服务以 Harness 用户运行。

## 13. 当前业务能力边界

当前已完成的 POC 能力：

- Video Analysis 的 submit/status/result 三个 Functions。
- Video Processing 的 submit/status/result 三个 Functions。
- Analysis/Processing Task JSON 在 Agent Workspace 中的创建和更新。
- Processing result 对 public dataset manifest 的保存和 Agent 可见引用。
- Model Training submit/status/result HTTP POC、submit 前 ready dataset manifest
  校验、Training Task JSON 更新与 public model metadata 保存。

部署成功不表示以下能力已完成：

- Scenario Registry 和 scenario 版本管理。
- 完整的对象存储生命周期、模型部署和回滚。
- 生产级权威任务数据库、Webhook 和可靠轮询。
- 多租户授权、外部 API 认证、限流、重试和审计。

这些能力的开发进度、适配阶段和 POC 验收标准以
[undeveloped_adaptation.md](undeveloped_adaptation.md) 为准。

## 14. 部署验收清单

### Harness 与模型验收

- [ ] Python 版本不低于 3.10。
- [ ] 服务由 `atis` 非 root 用户运行。
- [ ] API Key 通过受控 `.env` 注入，未写入代码或 Agent 指令。
- [ ] `.env` 所有者是 `atis`，权限为 `600`。
- [ ] `.env` 已被 `.gitignore` 忽略，没有进入 Git。
- [ ] `AGENT_WORKSPACE` 和模型连接配置已在 `.env` 中设置。
- [ ] Workspace 根目录存在且服务用户可读写。
- [ ] `--cwd` / `AGENT_WORKSPACE` 指向预期根目录；自动生成目录无需预创建。
- [ ] `workspace/CLAUDE.md` 已从 `agent_operation.md` 生成，包含实际
      Workspace 路径且没有遗留占位符。
- [ ] systemd 的 `HOME` 指向可写的 `runtime-home`。
- [ ] GUI 只监听 `127.0.0.1`。
- [ ] 默认未启用 Shell、Unsafe 和通用文件写入权限；只允许
      业务 Functions 实现的授权持久化。
- [ ] systemd 服务可以自动启动和失败重启。
- [ ] `/api/state` 可响应，返回的工作区与会话路径正确。
- [ ] 模型只读请求成功，运行时指令已加载。

### 业务接入验收（Backend 就绪后）

- [ ] `VIDEO_ANALYSIS_API`、`VIDEO_PROCESSING_API`、`MODEL_TRAINING_API` 指向真实 Backend。
- [ ] 原始测试视频已放入授权目录。
- [ ] 九个业务 Functions 可见，三个模块均已完成至少一次
      可控的 submit/status/result 验收。
- [ ] Task JSON 会创建并更新，Processing result 会保存 public
      dataset manifest。
- [ ] Model Training Submit 会先校验 ready dataset manifest，然后只发送
      逻辑 JSON 引用；Status/Result 可查询，会更新 Training Task JSON
      并保存 public model metadata。
### 运维验收

- [ ] Journal 日志中没有 API Key。
- [ ] Agent session 中没有 API Key。
- [ ] 已记录当前部署 commit。
- [ ] 已验证更新和回滚步骤。
