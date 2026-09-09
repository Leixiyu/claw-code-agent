# Claw Code Agent 服务器部署指南

本文记录当前 Harness 的服务器部署方法。默认部署方案为：

- Linux 服务器（示例使用 Ubuntu）
- Python 3.10 或更高版本
- Harness 和 GUI 部署在本机
- LLM 通过阿里云百炼 OpenAI-compatible API 调用
- systemd 负责进程守护
- 4090 POC 使用现有非 root 用户 `atis`
- Harness 路径为 `/home/atis/Documents/RAY/claw-code-agent`
- 运行数据存放在相邻的 `/home/atis/Documents/RAY/agent_workspace`
- Demo GUI 监听 `0.0.0.0:8765`，通过公网 `http://183.11.226.132:8765` 访问

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
| 代码目录 | `/home/atis/Documents/RAY/claw-code-agent` | Git 仓库、虚拟环境和 `.env` |
| 运行数据目录 | `/home/atis/Documents/RAY/agent_workspace` | 直接容纳业务文件、会话和运行时 HOME |
| Agent workspace | `/home/atis/Documents/RAY/agent_workspace`（与运行数据根目录相同） | Agent 的业务文件操作根目录，名称和位置可自选 |

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
/home/atis/Documents/RAY/claw-code-agent   # Git 代码和 Python 虚拟环境
/home/atis/Documents/RAY/agent_workspace  # Session、Agent Workspace 和运行时 HOME
```

### 2.1 部署时需要准备的内容

以服务用户 `atis` 执行：

```bash
mkdir -p /home/atis/Documents/RAY/agent_workspace/uploads
mkdir -p /home/atis/Documents/RAY/agent_workspace/runtime-home/.claude
mkdir -p /home/atis/Documents/RAY/agent_workspace/sessions
chmod 700 /home/atis/Documents/RAY/agent_workspace
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
agent_workspace/            # 本文实际使用的根目录，没有额外的 workspace/ 层
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
  /home/atis/Documents/RAY/claw-code-agent
```

如果代码已经位于该路径，不要再次 clone。

确认版本：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
git branch --show-current
git log -1 --oneline
```

生产部署应记录当前 commit，以便回滚。

## 4. 创建 Python 环境并安装依赖

下面两种方式**二选一**。后续终端命令统一使用已激活环境中的 `python` 和
`claw-code-agent` / `claw-code-gui`，不要混用两套环境。新开终端时需重新激活。

### 4.1 标准 venv

```bash
cd /home/atis/Documents/RAY/claw-code-agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install . --no-deps
```

安装后激活：

```bash
source /home/atis/Documents/RAY/claw-code-agent/.venv/bin/activate
```

### 4.2 Conda（已安装 Conda 时）

`claw-agent` 是示例环境名；环境已存在时跳过创建步骤。

```bash
conda create -n claw-agent python=3.10 pip -y
conda activate claw-agent
cd /home/atis/Documents/RAY/claw-code-agent
python -m pip install -r requirements.txt
python -m pip install . --no-deps
```

### 4.3 验证当前环境

```bash
python --version
which python
which claw-code-agent
which claw-code-gui
claw-code-agent --help
claw-code-gui --help
```

确认上述入口来自同一个所选环境。记录 `which claw-code-gui` 的绝对路径，
第 7 节配置 systemd 时使用。Conda 环境不需要再创建仓库中的 `.venv`。

`vLLM` 不应安装到这个 Harness 虚拟环境。只有在服务器自行托管模型时，才应为 vLLM
建立独立环境或容器。

## 5. 配置模型和运行目录

4090 是个人测试服务器，因此统一使用 Harness 根目录下的 `.env`：

```text
/home/atis/Documents/RAY/claw-code-agent/.env
```

进入项目并创建或编辑：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
nano .env
```

百炼 API 示例：

```dotenv
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=替换为真实百炼APIKey
OPENAI_MODEL=qwen3-coder-next

AGENT_WORKSPACE=/home/atis/Documents/RAY/agent_workspace

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
cd /home/atis/Documents/RAY/claw-code-agent
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
  's|{{AGENT_WORKSPACE_PATH}}|/home/atis/Documents/RAY/agent_workspace|g' \
  /home/atis/Documents/RAY/claw-code-agent/agent_operation.md \
  > /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
```

验证文件中已写入实际 Workspace：

```bash
grep -n 'Agent Workspace root' \
  /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
grep -n '/home/atis/Documents/RAY/agent_workspace' \
  /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
```

Agent 从工作区中读取这份 `CLAUDE.md`。仓库根目录的 `CLAUDE.md` 用于
开发 Harness 本身，不应复制给运行时 Agent。`agent_integration.md` 是维护者的
集成清单，也不需要复制到 workspace。

`CLAUDE.md` 中的路径只向模型说明边界，不设置程序的工作目录；它必须与实际
`AGENT_WORKSPACE` / `--cwd` 一致。不要启用 `--disable-claude-md`，否则不会自动
加载这份运行指令。

## 6. 先进行命令行 Smoke Test

本节验证模型调用和工作区指令，不提交业务任务。先按第 4 节激活所选环境，
再使用服务用户在 Bash 中加载
自己维护的受信任 `.env`（`source` 会执行其中的 Shell 内容），再进行只读测试：

```bash
set -a
source /home/atis/Documents/RAY/claw-code-agent/.env
set +a

cd /home/atis/Documents/RAY/agent_workspace
claw-code-agent agent \
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
  `/home/atis/Documents/RAY/agent_workspace/.port_sessions`。
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

先激活第 4 节所选环境，再在仓库中运行不依赖真实 Backend 的测试：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
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
- submit 后生成 `agent_workspace/tasks/<module>/<task_id>.json`。
- status 调用会更新对应 Task JSON。
- `status="done"` 且 `result_ready=true` 后，同一轮调用对应 result Function；
  不只报告任务完成而遗漏结果获取。
- Processing result 完成后生成 `agent_workspace/datasets/<dataset_id>.json`，函数只
  返回 Agent 可见的 `manifest_path`，不泄露 Backend 物理路径。
- Model Training Submit 会先校验
  `agent_workspace/datasets/<dataset_ref>.json` 存在、身份与 scenario 匹配且
  `status="ready"`，再验证 `POST /train` 纯 JSON 请求；Status 可验证
  `GET /status/{task_id}`；Result 可验证
  `GET /result/{task_id}`、`agent_workspace/models/<model_id>.json` 和
  `agent_workspace/tasks/training/<task_id>.json`。

## 7. 创建 systemd 服务

先激活所选 Python 环境，执行 `which claw-code-gui` 获取入口的绝对路径。
下面 unit 的 `ExecStart` 使用标准 venv 示例：

- **标准 venv：**保留示例路径，前提是第 4.1 节已完成。
- **Conda：**将 `ExecStart=` 后的可执行文件路径替换为上述命令的实际输出，
  保留后面的启动参数。不要猜测 Conda 安装位置，也不要在 unit 中写
  `conda activate`；systemd 通过绝对路径启动入口，不依赖终端已激活的环境。

创建 `/etc/systemd/system/claw-code-agent.service`：

```ini
[Unit]
Description=Claw Code Agent local GUI
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=atis
WorkingDirectory=/home/atis/Documents/RAY/agent_workspace
EnvironmentFile=/home/atis/Documents/RAY/claw-code-agent/.env
Environment=HOME=/home/atis/Documents/RAY/agent_workspace/runtime-home

ExecStart=/home/atis/Documents/RAY/claw-code-agent/.venv/bin/claw-code-gui \
  --host 0.0.0.0 \
  --port 8765 \
  --no-browser \
  --session-dir /home/atis/Documents/RAY/agent_workspace/sessions

Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=/home/atis/Documents/RAY/claw-code-agent
ReadWritePaths=/home/atis/Documents/RAY/agent_workspace

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

正常情况下应看到 `0.0.0.0:8765`。如果仍为 `127.0.0.1:8765`，按第 9.1 节修改并重启服务。

## 9. Demo 公网访问与本地 curl

本文采用公网直连的 Demo 部署：服务器为 `183.11.226.132`，端口为 TCP 8765，
无需 SSH 隧道。当前 GUI 尚无完整身份认证，开放的是包含状态、聊天和配置接口
的整个 GUI 服务；此方案用于临时测试。

### 9.1 在服务器上修改监听地址

编辑已有服务文件：

```bash
sudo nano /etc/systemd/system/claw-code-agent.service
```

保留当前已验证可用的 Conda/venv `ExecStart` 可执行文件路径，只将其参数
`--host 127.0.0.1` 改成 `--host 0.0.0.0`，端口继续使用 `--port 8765`。
不要把已有的 Conda 路径改回文档中的 `.venv` 示例路径。

保存后在服务器执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart claw-code-agent
sudo systemctl status claw-code-agent --no-pager -l
ss -lntp | grep 8765
curl --fail --show-error --max-time 10 http://127.0.0.1:8765/api/state
```

先确认监听 `0.0.0.0:8765`，且服务器本机 GET 请求能返回 JSON。

### 9.2 放行公网 TCP 8765

根据服务器实际网络配置处理，不需要重复启用或安装防火墙：

- 如果使用 UFW，执行 `sudo ufw status` 检查；处于 active 状态时执行：

  ```bash
  sudo ufw allow 8765/tcp
  ```

- 如果服务器有云安全组或上游防火墙，增加 TCP 8765 的入站放行规则。
- 如果 `183.11.226.132` 是路由器/NAT 的公网地址，在网关配置 TCP 8765
  转发到这台服务器的内网 IP 的 8765 端口。SSH 能连接并不代表 8765 已转发。

### 9.3 在本地电脑 curl 状态接口

在**本地电脑**终端直接执行，无需 Conda、SSH 隧道或模型 API Key：

```bash
curl --fail --show-error --max-time 10 http://183.11.226.132:8765/api/state
```

检查 JSON 中的 `cwd` 是否为 `/home/atis/Documents/RAY/agent_workspace`，
`session_directory` 是否为该目录下的 `sessions`，以及 `model`、`base_url`
是否符合服务器配置。这个 GET 请求只读取 GUI 状态，不调用模型。

浏览器也可以直接访问 [http://183.11.226.132:8765](http://183.11.226.132:8765)。
`0.0.0.0` 是服务端监听地址；本地 curl 和浏览器应使用上述公网 IP。

### 9.4 在本地电脑提交 Agent 请求

```bash
curl --fail --show-error http://183.11.226.132:8765/api/chat \
  -H 'Content-Type: application/json' \
  --data-binary '{"prompt":"只检查 Agent Workspace 并简要说明可见目录，不要修改任何内容。"}'
```

该 POST 会调用服务器配置的模型、消耗模型额度，并保存会话。接口等待本轮执行
结束后返回 JSON，不是 SSE 流。检查 `final_output` 和 `stop_reason`；即使
HTTP 成功，`stop_reason="backend_error"` 也表示 Agent 执行失败。

### 9.5 连接问题排查

| 现象 | 检查位置 |
| --- | --- |
| 服务器本机 curl 失败 | 服务状态、监听端口和 Journal 日志 |
| 本机可访问，公网请求超时 | 防火墙、安全组和 NAT 端口转发 |
| 仍只监听 `127.0.0.1` | unit 中的 `--host`，是否已 daemon-reload 并重启 |
| 公网 GET 正常，POST 返回 Agent 错误 | 模型/API 配置、工具调用结果和服务日志 |

在服务器查看日志：

```bash
sudo journalctl -u claw-code-agent -n 100 --no-pager
```

测试结束后可执行 `sudo systemctl stop claw-code-agent` 停止服务；若需继续保留
服务但结束公网访问，将监听地址改回 `127.0.0.1`，重新加载并重启，同时撤销
本次添加的端口放行/转发规则。

## 10. 更新部署

安排维护窗口，暂停新请求并记录运行中业务的 `task_id`。等待当前 Agent 回合
结束再停止服务；停止 Harness 不会取消已经提交到 Backend 的任务。

更新前先激活服务实际使用的 Python 环境（第 4 节），记录当前 commit 和已安装依赖：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
git rev-parse HEAD
python -m pip freeze > /home/atis/Documents/RAY/agent_workspace/requirements.before-update.txt
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
cd /home/atis/Documents/RAY/claw-code-agent
git fetch origin
git switch main
git pull --ff-only origin main
python -m pip install -r requirements.txt
python -m pip install . --no-deps
sed \
  's|{{AGENT_WORKSPACE_PATH}}|/home/atis/Documents/RAY/agent_workspace|g' \
  agent_operation.md \
  > /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
sudo systemctl daemon-reload
sudo systemctl restart claw-code-agent
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

### 业务提交的自动去重

`idempotency_key` 仅用于 Harness 内部防重复，用户无需填写。
视频处理、模型训练、视频分析的所有 Business API 请求均不携带该参数。
训练请求 JSON 只包含 `scenario` 和 `dataset_ref`。

Harness 在 `.port_sessions/business_functions/` 中持久化操作记录：

- 同一会话、同一接口和相同输入重复提交时，复用已有任务；恢复会话请使用原 session ID。
- 用户明确要求重做时，Agent 用上一次的任务 ID 作为 `repeat_of_task_id`，由 Harness 创建新操作。
  重复调用同一重做请求仍复用新任务；该参数也不会传给 Business API。
- 请求发出前先保存记录。如果提交结果不确定，停止自动重试，先核查后端是否已创建任务。
  不要通过新会话绕过此保护。本地去重无法保证后端恰好执行一次。

更新后同步 Workspace `CLAUDE.md` 中的幂等规则，重启 Harness，并新建会话验证。

## 11. 回滚

暂停新请求、记录任务 ID 并停止服务，然后使用更新前记录的 commit。
执行前激活服务实际使用的 Python 环境；以下命令中的 `<known-good-commit>`
必须替换成真实 commit。任一步失败即停止，不要继续启动服务：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
sudo systemctl stop claw-code-agent
git switch --detach <known-good-commit>
python -m pip install -r requirements.txt
python -m pip install . --no-deps
sed \
  's|{{AGENT_WORKSPACE_PATH}}|/home/atis/Documents/RAY/agent_workspace|g' \
  agent_operation.md \
  > /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
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

- [ ] Python 版本不低于 3.10，已选择并安装 venv 或 Conda 环境。
- [ ] systemd `ExecStart` 指向该环境中实际存在的 `claw-code-gui` 绝对路径。
- [ ] 服务由 `atis` 非 root 用户运行。
- [ ] API Key 通过受控 `.env` 注入，未写入代码或 Agent 指令。
- [ ] `.env` 所有者是 `atis`，权限为 `600`。
- [ ] `.env` 已被 `.gitignore` 忽略，没有进入 Git。
- [ ] `AGENT_WORKSPACE` 和模型连接配置已在 `.env` 中设置。
- [ ] Workspace 根目录存在且服务用户可读写。
- [ ] `--cwd` / `AGENT_WORKSPACE` 指向预期根目录；自动生成目录无需预创建。
- [ ] `agent_workspace/CLAUDE.md` 已从 `agent_operation.md` 生成，包含实际
      Workspace 路径且没有遗留占位符。
- [ ] systemd 的 `HOME` 指向可写的 `runtime-home`。
- [ ] Demo GUI 监听 `0.0.0.0:8765`，本地电脑可通过公网 IP 访问 `/api/state`。
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
