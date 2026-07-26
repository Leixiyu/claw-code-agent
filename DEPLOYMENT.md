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

当前 Harness 是 Agent runtime 和本地管理 GUI，不是完整的视频业务工作流服务。视频上传、
分析、训练、部署、Webhook 和任务状态机需要按照 README 路线图继续开发。

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
/home/atis/Documents/RAY/claw_agent_data   # Session、视频、日志和临时数据
```

以 `atis` 用户创建运行目录：

```bash
mkdir -p /home/atis/Documents/RAY/claw_agent_data/sessions
mkdir -p /home/atis/Documents/RAY/claw_agent_data/workspace
mkdir -p /home/atis/Documents/RAY/claw_agent_data/video-input
mkdir -p /home/atis/Documents/RAY/claw_agent_data/video-output
mkdir -p /home/atis/Documents/RAY/claw_agent_data/video-temp
mkdir -p /home/atis/Documents/RAY/claw_agent_data/logs
chmod 700 /home/atis/Documents/RAY/claw_agent_data
```

这样视频、日志和 Session 不会进入 Git 仓库。正式生产或多人共用服务器时，建议再将代码
迁移到 `/opt/claw-code-agent`，并使用独立的 `claw-agent` 系统用户。

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
VIDEO_INPUT_DIR=/home/atis/Documents/RAY/claw_agent_data/video-input
VIDEO_OUTPUT_DIR=/home/atis/Documents/RAY/claw_agent_data/video-output
VIDEO_TEMP_DIR=/home/atis/Documents/RAY/claw_agent_data/video-temp
TASK_LOG_DIR=/home/atis/Documents/RAY/claw_agent_data/logs

BUSINESS_API_BASE_URL=
BUSINESS_API_TOKEN=
TASK_API_BASE_URL=
TASK_API_TOKEN=
```

注意：

- `.env` 中直接设置 `OPENAI_API_KEY`，不要使用
  `OPENAI_API_KEY="${DASHSCOPE_API_KEY}"`；systemd `EnvironmentFile` 不执行变量展开。
- 不要在值的两侧加入不必要的 Shell 命令或 `export`。
- `.env` 已在 `.gitignore` 中，仍应在每次提交前检查它没有进入 Git。
- 不要在终端、日志、截图或文档中输出真实 API Key。

设置权限：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
chown atis:"$(id -gn atis)" .env
chmod 600 .env
git check-ignore -v .env
```

`600` 表示只有 `atis` 可以读取和修改该文件。

## 6. 先进行命令行 Smoke Test

使用服务用户加载受信任的配置并执行一次只读测试：

```bash
set -a
source /home/atis/Documents/RAY/claw_code_agent/.env
set +a

cd /home/atis/Documents/RAY/claw_agent_data
/home/atis/Documents/RAY/claw_code_agent/.venv/bin/claw-code-agent agent \
  "只检查当前工作目录并简要说明可见文件，不要修改任何内容。"
```

验收：

- 能连接 Qwen API。
- Agent 能返回响应。
- 默认不能写文件。
- 默认不能执行 Shell。
- `/home/atis/Documents/RAY/claw_agent_data` 下可以生成本地 session 状态。
- 输出和 session 中不应出现 API Key。

如果这一步失败，先不要创建 systemd 服务。优先检查：

```text
Python 版本
requirements 安装结果
OPENAI_BASE_URL
OPENAI_MODEL
服务器到模型 API 的网络
API Key 的区域和权限
AGENT_WORKSPACE 是否存在且可读
```

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

本机检查：

```bash
curl --fail http://127.0.0.1:8765/api/state
```

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

更新前记录当前 commit：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
git rev-parse HEAD
```

拉取并重新安装：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
git fetch origin
git switch main
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install . --no-deps
sudo systemctl restart claw-code-agent
```

更新后验证：

```bash
sudo systemctl status claw-code-agent
curl --fail http://127.0.0.1:8765/api/state
sudo journalctl -u claw-code-agent -n 100 --no-pager
```

不要在生产服务器上直接修改仓库文件。开发修改应先提交到 GitHub，再通过上述流程部署。

## 11. 回滚

使用更新前记录的 commit：

```bash
cd /home/atis/Documents/RAY/claw_code_agent
git switch --detach <known-good-commit>
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install . --no-deps
sudo systemctl restart claw-code-agent
```

确认恢复后，可以继续保持该 commit，或在仓库中创建正式回滚提交后重新部署 `main`。

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

## 13. 当前不应视为已经完成的能力

部署成功仅表示 Harness、Qwen 接入和本地 GUI 可以运行，不表示以下视频业务已经可用：

- 视频上传和对象存储
- Scenario Registry
- 视频分析 API 工具
- 模型训练 API 工具
- 模型部署和回滚工具
- 持久化业务任务状态机
- Webhook 和可靠轮询
- 多租户权限
- 生产级外部 API

这些能力的开发进度以 README 末尾的“视频处理 Agent：当前进度与开发路线”为准。

## 14. 部署验收清单

- [ ] Python 版本不低于 3.10。
- [ ] 服务由 `atis` 非 root 用户运行。
- [ ] API Key 只存在于 Harness 根目录的 `.env`。
- [ ] `.env` 所有者是 `atis`，权限为 `600`。
- [ ] `.env` 已被 `.gitignore` 忽略，没有进入 Git。
- [ ] GUI 只监听 `127.0.0.1`。
- [ ] 默认未启用 Shell、Unsafe 和文件写入权限。
- [ ] systemd 服务可以自动启动和失败重启。
- [ ] `/api/state` 健康检查通过。
- [ ] Journal 日志中没有 API Key。
- [ ] Agent session 中没有 API Key。
- [ ] 已记录当前部署 commit。
- [ ] 已验证更新和回滚步骤。
