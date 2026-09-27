# Claw Code Agent 部署指南

面向 Linux 测试服务器的安装与维护。Harness 使用内置 Functions 调用 Business HTTP API；
使用云端模型时不需要 GPU、vLLM 或 MCP 服务。当前是多用户 prototype，不是生产级隔离环境。

## 1. 安装

示例统一使用以下路径；换服务器时同步修改命令、`.env` 和 systemd 配置。

| 项目 | 路径 / 值 |
| --- | --- |
| 系统用户 | `atis`（非 root） |
| 代码 | `/home/atis/Documents/RAY/claw-code-agent` |
| 总 Workspace | `/home/atis/Documents/RAY/agent_workspace` |
| 认证数据 | `/home/atis/Documents/RAY/.agent_workspace-auth`（必须在 Workspace 外） |
| GUI 端口 | `8765` |

需要 Git、Python ≥ 3.10，以及服务器到模型和 Business API 的网络连接。
已有仓库/环境时跳过对应创建命令，不要覆盖本地修改。

```bash
mkdir -p /home/atis/Documents/RAY
git clone https://github.com/Leixiyu/claw-code-agent.git /home/atis/Documents/RAY/claw-code-agent
cd /home/atis/Documents/RAY/claw-code-agent

conda create -n claw-agent python=3.10 pip -y
conda activate claw-agent
python -m pip install -r requirements.txt
python -m pip install . --no-deps
python -m pip check
command -v claw-code-gui

mkdir -p /home/atis/Documents/RAY/agent_workspace
mkdir -p /home/atis/Documents/RAY/.agent_workspace-auth
chmod 700 /home/atis/Documents/RAY/agent_workspace /home/atis/Documents/RAY/.agent_workspace-auth
```

使用 venv 时，以 `python3 -m venv .venv` 和 `source .venv/bin/activate` 替代 Conda
创建/激活步骤。不要用 sudo 安装 Python 包；代码、数据及认证目录需由服务用户读写。

## 2. 配置 .env 和 Agent 规范

在代码目录新建/修改 `.env`，替换下列示例地址和密钥：

```dotenv
AGENT_WORKSPACE=/home/atis/Documents/RAY/agent_workspace
HARNESS_AUTH_DIR=/home/atis/Documents/RAY/.agent_workspace-auth
HARNESS_MAX_UPLOAD_BYTES=2147483648

OPENAI_BASE_URL=https://your-llm-api.example/v1
OPENAI_MODEL=your-tool-calling-model
OPENAI_API_KEY=replace-with-real-key

# 可选：完整、无需认证的模型健康 URL；没有则留空
MODEL_API_HEALTH_URL=""

# Business 服务根地址，不附加 /predict、/status、/result 等路径
VIDEO_ANALYSIS_API=http://video-analysis-host:8000
VIDEO_PROCESSING_API=http://video-processing-host:8000
MODEL_TRAINING_API=http://model-training-host:8000
```

- `AGENT_WORKSPACE` 是所有用户目录的容器，不是某个用户的 cwd。
- `HARNESS_MAX_UPLOAD_BYTES` 是单次 HTTP 上传上限，默认 2 GiB。
- 从代码目录启动会自动加载 `.env`；已有环境变量优先。systemd 使用同一份文件。
- 填写完整值，不使用 `export`、Shell 命令或变量引用。不要将登录 Token 固定在公共配置中。
- `.env` 不随 Git 同步；服务器需单独维护。不要输出或分享整份配置。

```bash
chmod 600 .env
git check-ignore -v .env
git ls-files -- .env
```

最后一条应无输出；若密钥文件已被跟踪，需要另行处理泄露及 Git 索引问题。

安装给业务 Agent 的公共规范（已有自定义版本时先备份、比较再覆盖）：

```bash
cp agent_operation.md /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
```

保留 `{{AGENT_WORKSPACE_PATH}}` 占位符，Harness 会按登录用户替换。
不要复制仓库根目录的 `CLAUDE.md`，它是开发 Harness 本身的规范。

## 3. 创建账号与 CLI 验证

在已激活环境的代码目录执行；账号已存在时不必重建：

```bash
claw-code-agent users-create ray
claw-code-agent login ray
claw-code-agent whoami
claw-code-agent agent "只检查 Agent Workspace 并简要说明可见目录，不要修改任何内容。"
claw-code-agent agent-chat --show-transcript
```

创建账号时交互设置密码（至少 8 位），没有默认密码。Token 有效期 12 小时；
同一认证目录的 CLI 共享登录凭证，`HARNESS_AUTH_TOKEN` 环境变量会优先覆盖它。

用户名支持 1–64 个字符，可包含中文、字母、数字、内部空格及 `_ . -`，首尾不能有空格。
包含空格时请加引号，例如 `claw-code-agent users-create "ray chen"`；中文示例：`claw-code-agent users-create 雷晞宇`。
GUI 圆形头像中，纯汉字用户名取最后两个汉字（单字取该字），其他用户名取前两个空格分隔部分的首字符并大写；只有一部分时取其大写首字符。

Harness 自动创建 `users/<user_id>/` 下的 uploads、sessions、三个任务索引及运行目录；
无需手工创建每个用户的目录。视频通过 GUI 上传，业务结果保存在 Business 后端，
聊天记录可包含结果内容。

常用会话命令：

```bash
claw-code-agent sessions
claw-code-agent session-info <session_id>
claw-code-agent agent-chat --resume-session-id <session_id>
claw-code-agent session-delete <session_id>
claw-code-agent logout
```

聊天中 `/exit` 退出，`/compact` 压缩上下文，`/clear` 重置当前会话但不删除历史。
删除命令默认确认（可加 `--yes`），每次仅永久删除当前用户指定的一条会话 JSON，运行中会话拒绝删除；
保留视频、业务索引及 scratchpad。不要手动清除 `sessions/.lifecycle/` 删除标记。

## 4. GUI 与 systemd

### 先前台验证

```bash
claw-code-gui --host 127.0.0.1 --port 8765 --no-browser
```

在服务器浏览器打开 `http://127.0.0.1:8765`。从自己电脑访问时，在本地建立 SSH 隧道：

```bash
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:8765:127.0.0.1:8765 <harness-server-ssh-alias>
```

保持隧道运行，在本地打开同一 URL。登录后测试聊天与上传。
若已有部署监听 `0.0.0.0`，可在网络允许时访问 `http://183.11.226.132:8765`；
公网使用应配置 HTTPS 和访问限制，不建议直接暴露 HTTP 登录。

### 在线 API 文档

GUI 服务启动后，同一端口自动提供 `/docs`（Swagger UI）和 `/openapi.json`，不需要额外启动文档服务。
例如通过上述隧道访问 `http://127.0.0.1:8765/docs`；现有公网部署对应 `http://183.11.226.132:8765/docs`。

- 按「用户认证、对话、历史会话、文件上传、Token 用量、系统信息、服务健康」分组，仅展示允许用户调用的接口。
- 文档页面无需登录；除登录和健康检查外，执行接口需要认证。先在同站点 GUI 或文档登录接口登录，浏览器自动携带 Cookie。
  也可把登录结果中的 `access_token` 填入 **Authorize → BearerAuth**，不加 `Bearer ` 前缀。
- 查询 `/api/state`、`/api/capabilities`、`/health` 和 `/api/usage` 可查看当前用户运行配置、可用能力、依赖状态和历史 Token 用量。
  **Execute** 会发送真实请求；示例不是服务器实时数据。
- 上传使用 `X-Filename` 和二进制请求体；聊天流使用 NDJSON。文档说明了这两类请求及统一错误结构。
- 路由和权限由代码生成并缓存；更新代码及两组能力配置后，按下文发布步骤重装并重启服务即可刷新文档。
  Swagger UI 的 JS/CSS 沿用 FastAPI 默认 CDN，浏览器需要能访问该 CDN；`/openapi.json` 不依赖它。

### 配置常驻服务

先停止前台 GUI，避免端口冲突。编辑：

```bash
sudo nano /etc/systemd/system/claw-code-agent.service
```

以下 `ExecStart` 使用当前 Conda 路径，必须与 `command -v claw-code-gui` 的结果一致：

```ini
[Unit]
Description=Claw Code Agent multi-user video prototype
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=atis
WorkingDirectory=/home/atis/Documents/RAY/claw-code-agent
EnvironmentFile=/home/atis/Documents/RAY/claw-code-agent/.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/home/atis/anaconda3/envs/claw-agent/bin/claw-code-gui --host 127.0.0.1 --port 8765 --no-browser
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Workspace 直接由 `.env` 提供，不额外传 `--cwd` 或共享 `--session-dir`，不设置旧版
`HOME=.../runtime-home`。使用单 Worker；这些配置不等于用户间的 OS 沙箱。

```bash
sudo systemd-analyze verify /etc/systemd/system/claw-code-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now claw-code-agent
sudo systemctl status claw-code-agent --no-pager -l
```

若使用文件系统加固，在 `[Service]` 中添加以下配置；两个可写目录须预先存在：

```ini
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=/home/atis/Documents/RAY/claw-code-agent
ReadWritePaths=/home/atis/Documents/RAY/agent_workspace /home/atis/Documents/RAY/.agent_workspace-auth
```

## 5. 健康检查与验收

```bash
curl -sS --max-time 10 http://127.0.0.1:8765/health | python -m json.tool
```

`GET /health` 仅在最外层应用注册一次，不在每个用户的内部应用重复注册；无需登录，每次并发探测，单项最多 5 秒，不发送认证密钥、不调用聊天或业务执行接口。
返回 `status`、`checked_at`、`summary`（各状态数量）和 `services`（逐项状态、耗时及失败原因）。

| 服务 | 探测方式 |
| --- | --- |
| 模型 | `MODEL_API_HEALTH_URL`；留空为 `not_configured` |
| 视频分析 | `VIDEO_ANALYSIS_API` + `/health` |
| 视频预处理、模型训练 | 暂无健康接口，固定为 `not_configured` |

模型接口允许 2xx 空响应/普通文本；JSON 若含 `status`，需为 healthy/ok/ready/up。
视频分析需返回 2xx 和含该状态的 JSON。故障返回 HTTP 503、`unhealthy`；
仅有未配置项则返回 HTTP 200、`degraded`。目前即使模型与分析均健康，总体仍为 `degraded`。
接口没有 GUI 面板、CLI 命令或自动轮询；监控方应控制频率，不要因依赖故障反复重启 Harness。

上线前分别确认：

- 服务：systemd 为 `active (running)`，GUI 能登录、上传，未登录访问 `/api/auth/me` 返回 401。
- 模型：只读测试有正常最终回答，无 `backend_error`；健康探针通过不保证生成请求不会超时/限流。
- 业务：用经批准的小型测试任务完成 submit → status → result，确认用户间数据不混用。
  List 只为本地标记 done 的任务查询结果；待完成任务需调用 Status 更新。

业务 API 必须能从 Harness 服务器访问。需要跳板机时，在服务器上另建业务隧道：

```bash
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18000:127.0.0.1:8000 <business-server-ssh-alias>
```

对应 Business URL 填 `http://127.0.0.1:18000`。隧道须持续运行，不由 Harness 自动维护。
完整回归、真实业务测试及脚本调用见 [TESTING_GUIDE.md](TESTING_GUIDE.md)；
测试会创建临时运行数据，请在开发/预发布环境运行。

## 6. 重启、更新与日志

操作前暂停新请求，等待当前 Agent 回合结束；已提交的 Business 任务不会随 Harness 停止而取消。

| 变更 | 操作 |
| --- | --- |
| 只修改 `.env` | `sudo systemctl restart claw-code-agent` |
| 修改 unit / drop-in | 先 `sudo systemctl daemon-reload`，再重启 |
| 更新普通安装的代码 | 在服务 Python 环境重新安装包，再重启 |
| 修改公共规范 | 审核并更新 Workspace/CLAUDE.md，重启并新建会话验证 |

代码更新示例（先按下一节备份；有本地修改时先处理，不强制覆盖）：

```bash
conda activate claw-agent
cd /home/atis/Documents/RAY/claw-code-agent
git status --short
sudo systemctl stop claw-code-agent
git pull --ff-only origin main
python -m pip install -r requirements.txt
python -m pip install . --no-deps
python -m pip check
# agent_operation.md 有变更时，审核后重新复制到 Workspace/CLAUDE.md
sudo systemctl restart claw-code-agent
```

任一步失败就停止，不要继续启动失败版本。普通 `pip install .` 下，只 git pull 或重启不会
更新 site-packages 中的代码；`.env` 不随 Git 更新，新增配置需手动同步。
systemd 重启会重新读取 EnvironmentFile，无需在终端 source。

每次重启后验证：

```bash
sudo systemctl status claw-code-agent --no-pager -l
sudo journalctl -u claw-code-agent -n 50 --no-pager
sudo systemctl show claw-code-agent \
  -p ExecStart -p MainPID -p ExecMainStartTimestamp --no-pager
curl -sS --max-time 10 http://127.0.0.1:8765/health | python -m json.tool
```

确认 MainPID 非 0、启动时间更新且 ExecStart 正确。跟随日志用
`sudo journalctl -u claw-code-agent -f`，Ctrl+C 退出；分享日志前脱敏。

systemd 只重启 GUI 服务。已打开的 CLI 需 `/exit` 后重新启动；
独立后台 Agent 也需单独管理。不要为验证更新而重复 Submit 已有任务。

## 7. 备份、回滚与迁移

发布前停止 GUI，并确认其他 CLI/后台进程不再写入。至少备份：
**完整 Workspace、认证目录、.env、systemd unit/drop-in、commit 和依赖版本**。
备份含视频和凭证，必须保存在 Workspace 外的受限目录，不能进入 Git。

例如，在已激活服务环境的终端中执行：

```bash
umask 077
mkdir -p /home/atis/Documents/RAY/harness-backups
TASK_BACKUP_DIR=$(mktemp -d /home/atis/Documents/RAY/harness-backups/release-XXXXXXXX)
cd /home/atis/Documents/RAY/claw-code-agent
git rev-parse HEAD > "$TASK_BACKUP_DIR/commit.txt"
python -m pip freeze > "$TASK_BACKUP_DIR/requirements.txt"
cp .env "$TASK_BACKUP_DIR/agent.env.backup"
sudo systemctl cat claw-code-agent > "$TASK_BACKUP_DIR/service-config.txt"
tar -czf "$TASK_BACKUP_DIR/runtime.tar.gz" \
  -C /home/atis/Documents/RAY agent_workspace .agent_workspace-auth
```

- 回滚：先停止写入，确认目标 commit 与当前账号/索引格式兼容，再切换代码、重装、验证配置并启动。
  代码回滚不等于数据回滚，不要用旧备份直接覆盖新增账号、会话和任务。
- 不要删除任务索引或 `.port_sessions/business_functions/` 来解决报错。
  Submit 超时可能已被后端受理，应按已有 task_id 对账，不换 key/会话强行重提。
- 旧单用户数据迁移：先备份、创建目标账号，再预览；确认归属后才加 `--apply`：

```bash
claw-code-agent migrate-user-data ray --source /path/to/legacy-workspace
claw-code-agent migrate-user-data ray --source /path/to/legacy-workspace --apply
```

迁移保留源文件，不复制独立业务结果；导入任务需重新查询 Status。此命令不是多用户目录搬家工具。

## 8. 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 服务启动失败 / command not found | ExecStart 的 Python 环境、依赖和文件权限 |
| 只读数据库 / 写入失败 | 服务用户权限；ReadWritePaths 必须包含 Workspace 和认证目录 |
| 本机能访问，远端不能 | 监听地址、SSH 隧道、端口冲突、防火墙；公网需 HTTPS |
| 修改 .env 未生效 | 代码目录、EnvironmentFile、已有环境变量优先级及是否重启 |
| 登录失效 / 用户不存在 | 是否用了同一认证目录、旧 HARNESS_AUTH_TOKEN 是否覆盖、Token 是否过期 |
| 上传 413 | HARNESS_MAX_UPLOAD_BYTES 及反向代理请求体限制 |
| 模型超时 / 429 | 模型服务延迟、网络、上下文大小或服务端限流；健康响应不保证聊天成功 |
| List 仍显示 pending/running | 先调用对应 Status；List 不自动刷新状态 |
| Status done，但 Result 报错 | 保留 task_id 查询/对账，不重新 Submit |

能力边界和待开发项见 [undeveloped_adaptation.md](undeveloped_adaptation.md)；
业务 Agent 规范见 [agent_operation.md](agent_operation.md)，命令参考见 [README.md](README.md)。
