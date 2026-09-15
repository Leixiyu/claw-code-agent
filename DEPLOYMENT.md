# Claw Code Agent 统一部署指南

本文是当前多用户 Video Agent 的部署指南，合并原单用户指南的安装、验证和运维步骤，
并以当前代码行为为准。面向负责部署和维护 Harness 的developer；
业务 Agent 的运行规范见 [agent_operation.md](agent_operation.md)。

当前使用 Harness 内置 Functions 通过 HTTP 调用 Business API，不需要先部署 MCP
服务。Harness 服务器可直接调用云端 LLM，无需本地 GPU 或安装 vLLM。
系统尚未完成生产级多租户安全加固，默认采用非 root 用户、回环监听和 SSH 隧道测试。

## 阅读路线

- 首次部署：第 1–7 节，先完成 CLI/GUI 和模型验收。
- 服务器常驻运行：第 8–9 节，配置 systemd、日志和访问检查。
- 业务联调：第 10 节；后端未就绪时，单独标记为“未验收”。
- 日常发布和故障恢复：第 11–12 节。
- 从旧单用户版本迁移：先阅读第 13 节，再进行部署和数据导入。
- 可选自托管模型、能力边界和最终清单：第 14–16 节。

文中命令由部署人员按步骤执行，不是让 Agent 执行的任务。示例采用 Linux/Ubuntu；
macOS 可以运行 CLI/GUI，但 systemd 章节仅适用于 Linux。

## 1. 部署约定与前置检查

本文使用以下示例路径；换服务器时统一替换命令、配置和 systemd 中的实际路径，
不要混用历史版本的目录名。

| 项目 | 示例 | 用途 |
| --- | --- | --- |
| OS 服务用户 | `atis` | 安装、启动服务和管理账号的非 root 用户 |
| 代码目录 | `/home/atis/Documents/RAY/claw-code-agent` | Git、虚拟环境、项目 `.env` |
| `AGENT_WORKSPACE` | `/home/atis/Documents/RAY/agent_workspace` | 所有用户运行目录的容器，不是单个 Agent 的 cwd |
| `HARNESS_AUTH_DIR` | `/home/atis/Documents/RAY/.agent_workspace-auth` | Workspace 外的账号数据库和 CLI 登录凭证 |
| 单用户实际 cwd | `AGENT_WORKSPACE/users/<user_id>` | Harness 根据登录身份选择 |
| GUI | `127.0.0.1:8765` | 本机或通过 SSH 隧道访问 |

需要 Python >= 3.10、Git、可访问模型 API 的网络，以及可访问 Business API 的
网络或隧道。已有 `atis` 用户即可，不必另建系统用户；应用里的 Alice/Bob
不是 Linux 用户，也没有独立 OS 沙箱。

Ubuntu 可先安装基础依赖：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv curl ca-certificates
python3 --version
git --version
```

如果系统 Python 低于 3.10，先安装满足要求的版本，或选第 3 节的 Conda 环境。
部署分开验收：服务能访问、LLM 能响应、Business 闭环能执行，三者不能互相替代。

## 2. 获取代码与准备顶层目录

以下普通命令使用服务用户 `atis` 执行，不要用 sudo 安装 Python 包。

首次获取代码；目录已有仓库时不要再次 clone：

```bash
mkdir -p /home/atis/Documents/RAY
git clone https://github.com/Leixiyu/claw-code-agent.git /home/atis/Documents/RAY/claw-code-agent
cd /home/atis/Documents/RAY/claw-code-agent
git status --short
git log -1 --oneline
```

记录当前 commit。存在本地修改时先确认归属，不要通过 reset 或强制覆盖清理。

只需提前准备顶层数据与认证目录；用户的 uploads/sessions 不必手动创建：

```bash
mkdir -p /home/atis/Documents/RAY/agent_workspace
mkdir -p /home/atis/Documents/RAY/.agent_workspace-auth
chmod 700 /home/atis/Documents/RAY/agent_workspace
chmod 700 /home/atis/Documents/RAY/.agent_workspace-auth
```

服务用户必须拥有这两个目录的读写权限。若目录由其他账号创建，请管理员仅针对
这两个已确认的目录修正所有权；不要递归更改整个 home 或代码父目录的权限。

## 3. Python 环境与安装

venv 和 Conda 二选一。新终端重新激活所选环境，后续使用同一个环境的
`python`、`claw-code-agent` 和 `claw-code-gui`。

### 3.1 标准 venv

```bash
cd /home/atis/Documents/RAY/claw-code-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install . --no-deps
```

环境已存在时跳过创建。服务器使用普通安装，更新 Git 后必须重新安装项目包。

### 3.2 Conda（已有 Conda 时）

```bash
conda create -n claw-agent python=3.10 pip -y
conda activate claw-agent
cd /home/atis/Documents/RAY/claw-code-agent
python -m pip install -r requirements.txt
python -m pip install . --no-deps
```

环境已存在时只激活。使用 Conda 不需要再建仓库内的 `.venv`。

### 3.3 验证环境

```bash
python --version
command -v python
command -v claw-code-agent
command -v claw-code-gui
claw-code-agent --help
claw-code-gui --help
python -m pip check
```

记录 `claw-code-gui` 的绝对路径，systemd 的 ExecStart 必须指向它。开发环境可选
`python -m pip install -e . --no-deps`，但不要混淆 editable 安装与服务器普通安装。
vLLM 不属于 Harness 依赖，应在独立模型环境中安装。

## 4. 配置 .env

本项目沿用代码目录内的 `.env`，不另设 agent.env：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
nano .env
```

配置模板（示例值必须换成当前实际服务配置）：

```dotenv
AGENT_WORKSPACE=/home/atis/Documents/RAY/agent_workspace
HARNESS_AUTH_DIR=/home/atis/Documents/RAY/.agent_workspace-auth
HARNESS_MAX_UPLOAD_BYTES=2147483648

# OpenAI-compatible LLM；填写你已开通的服务地址、模型/接入点名称和密钥
OPENAI_BASE_URL=https://your-llm-api.example/v1
OPENAI_MODEL=your-tool-calling-model
OPENAI_API_KEY=replace-with-real-key

# Business 服务根地址，不要附加 /predict、/process、/train 或 /status
VIDEO_ANALYSIS_API=http://video-analysis-host:8000
VIDEO_PROCESSING_API=http://video-processing-host:8000
MODEL_TRAINING_API=http://model-training-host:8000
```

模型可以继续使用已验证的豆包/千问 OpenAI-compatible 配置；这里不固定供应商、
地区或模型名称。仅测试聊天时可暂不配置 Business API，但对应业务不能验收。

| 变量 | 说明 |
| --- | --- |
| `HARNESS_AUTH_DIR` | 保存 `auth.sqlite3`。必须在 Workspace 外；省略时为同级 `.<workspace目录名>-auth` |
| `HARNESS_MAX_UPLOAD_BYTES` | Harness HTTP 上传接口的单次大小限制，单位字节；默认 2147483648（2 GiB），不是用户总配额 |
| `HARNESS_AUTH_TOKEN` | 可选的有效登录 Token，用于 CLI 或 demo 脚本；不是账号密码，也不是模型 API Key |
| `HARNESS_GUI_URL` | demo 脚本访问的 Harness URL；默认 `http://127.0.0.1:8765` |

不要长期把个人登录 Token 放进公共服务 `.env`，否则多个管理终端可能误用同一身份。
CLI 优先读取环境中的 `HARNESS_AUTH_TOKEN`，其次才是本地登录凭证。

### 4.1 配置权限与加载规则

```bash
cd /home/atis/Documents/RAY/claw-code-agent
chmod 600 .env
git check-ignore -v .env
git ls-files -- .env
```

确认服务用户拥有 `.env`，且最后一条命令没有输出（未被 Git 跟踪）。
若已经跟踪，先处理凭证泄露和索引问题；仅加入 .gitignore 不能取消既有跟踪。

- 程序只自动读取启动时当前目录中的 `.env`，不会递归查找仓库或 Workspace。
- 已存在的进程环境变量优先，不会被自动加载覆盖；改文件后要重启服务/CLI。
- systemd 使用同一份 `EnvironmentFile`。不要在文件里使用 Shell 命令、
  `export` 或 `${OTHER_VARIABLE}` 引用；直接填写完整值，保证两种加载方式一致。
- 从其他目录启动时，可在 Bash 中显式加载自己维护的可信配置：

```bash
set -a
source /home/atis/Documents/RAY/claw-code-agent/.env
set +a
```

`source` 会执行 Shell 内容，只用于可信文件。不要打印 API Key、Token 或整份环境。

### 4.2 不同“目录”配置的含义

| 配置 | 当前多用户行为 |
| --- | --- |
| `WorkingDirectory` | 服务进程启动目录；本文设为代码目录 |
| `AGENT_WORKSPACE` | 总 Workspace，登录后才选择其中的用户目录 |
| Agent/GUI `--cwd` | 可覆盖总 Workspace；本文由 .env 提供，不显式传递 |
| 账号命令 `--workspace-root` | users-create/login 等命令的总 Workspace；默认读取 AGENT_WORKSPACE |
| 用户实际 cwd | Harness 固定为 `users/<user_id>`，不是 LLM 指定的路径 |
| session/scratchpad | 固定到当前用户的 sessions/ 和 .port_sessions/scratchpad/ |

不要把单用户路径写入 `AGENT_WORKSPACE`，也不要再配置一个所有人共享的
`--session-dir`。登录版会按用户覆盖 session/scratchpad 路径。
修改 Workspace 或认证目录可能连接到另一套账号库，并不会自动迁移旧数据。

## 5. 公共规范与账号初始化

### 5.1 安装给 Agent 的指令

```bash
cd /home/atis/Documents/RAY/claw-code-agent
cp agent_operation.md /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
```

这是部署更新公共规范的操作。已有自定义规范时，先备份并审核差异再覆盖。

保留文件里的 `{{AGENT_WORKSPACE_PATH}}`，不要像旧版一样用 sed 替换成总 Workspace。
Harness 读取公共规范后，按登录用户替换为 `users/<user_id>`，再注入提示词；
磁盘上的公共文件不因某个用户登录而改写。

仓库根目录的 `CLAUDE.md` 用于开发 Harness 本身，不能代替 `agent_operation.md`。
登录版禁用通用 CLAUDE.md 目录发现，使用专门的公共规范加载路径。公共文件缺失时
代码会使用简短兜底提示，因此“聊天能响应”不证明业务规范已安装，需手动验收。
更新模板后重启 GUI；CLI 重新启动，并以新会话确认新规范。

### 5.2 创建用户

从代码目录、已激活环境中执行：

```bash
claw-code-agent users-create ray
```

命令交互输入两次密码（至少 8 个字符）；账号名允许 1–64 个字母、数字、点、
下划线或连字符。不存在默认密码或在线注册，创建输出中可看到稳定的随机 user_id。
账号管理依赖服务器文件访问权限，不是面向普通用户的远程管理员接口。

认证目录保存用户名/user_id、密码 salt/哈希、Token 哈希和到期时间。
密码采用 PBKDF2-SHA256，服务端 Token 有效期 12 小时。
`cli-token.json` 保存可直接使用的 CLI Token，权限 600；仍须视为敏感凭证。

### 5.3 哪些目录由 Harness 自动创建

创建账号时立即初始化；CLI 准备用户运行环境、GUI 初始化用户应用或上传时也会
检查并补建缺失目录，不会清空已有文件。空目录不会自动产生测试视频。

```text
AGENT_WORKSPACE/
├── CLAUDE.md                         # 部署人员复制的公共规范
└── users/
    └── <user_id>/
        ├── uploads/                  # 上传的 raw videos
        ├── sessions/                 # CLI/GUI 共用聊天记录
        ├── video_processing_task_id.json
        ├── model_training_task_id.json
        ├── video_analysis_task_id.json
        ├── .port_sessions/
        │   ├── scratchpad/           # 会话 scratchpad
        │   ├── task_indexes.lock     # 任务索引锁
        │   ├── business_functions/   # 按需：提交幂等控制信息、锁
        │   └── background/           # 按需：后台执行日志和进程记录
        └── runtime-home/.claude/      # 仅预留，不切换进程全局 HOME
```

认证目录是上述树的外部同级目录，不要放进任何用户目录。
当前不再单独生成业务 `tasks/`、`datasets/`、`models/`；Business 保存真正的
处理结果、processed datasets、模型、标签和业务日志。聊天记录仍允许包含
Result、manifest 和 metadata，因此 sessions 同样需要受控访问和备份。

三个任务索引初始为 `{"tasks": []}`，有任务后例如：

```json
{
  "tasks": [
    {"task_id": "task_001", "status": "done"},
    {"task_id": "task_002", "status": "running"}
  ]
}
```

- Submit 新增 task_id，初始 pending；幂等重放不重置已有索引状态。
- Status 查询成功后更新 pending/running/done/failed；失败不覆盖已保存状态。
- List 返回本地已知状态，只对 done 的任务实时请求 Result，不查询 Status API。
  待完成任务必须调用对应 Status 才能发现完成；List 不是自动轮询器。
- 旧 `{"task_ids": [...]}` 可兼容读取为 status=null，写入时升级格式；
  调用 Status 后填充状态。null 不是 Business 新增的业务状态。
- 结果不单独落盘；单个 done 任务结果查询失败，在 List 中单独报告，不阻断其他任务。

## 6. CLI 登录与模型 Smoke Test

```bash
cd /home/atis/Documents/RAY/claw-code-agent
claw-code-agent login alice
claw-code-agent whoami
claw-code-agent agent "只检查 Agent Workspace 并简要说明可见目录，不要修改任何内容。"
claw-code-agent agent-chat --show-transcript
```

退出交互聊天使用 `/exit`。其他原型内命令为 `/help`、`/clear`、`/compact`；
`/clear` 开始新对话，不删除磁盘上的历史会话。需要退出登录时执行：

```bash
claw-code-agent logout
```

agent、agent-chat、agent-resume 和后台 Agent 入口都需要有效登录。
同一认证目录的 CLI 使用同一份 cli-token.json，后一次 login 会替换终端后续使用的
默认凭证；同一 OS 账号上的多个测试终端不要假设彼此身份独立。
普通多用户测试优先使用不同浏览器会话；脚本需要各自管理 Token。

检查 Agent 实际 cwd 为当前用户目录，CLI 会话进入该用户 sessions/；
只读请求应有最终回答，工具调用时显示进度提示。仅启用只读文件工具、sleep 和
12 个 Business Functions，不向业务用户开放 Shell、通用写文件、配置修改或委派。
Business Functions 自身仍能上传文件、维护索引并调用后端。

HTTP/模型错误、工具失败或 stop_reason=backend_error 都不能算验收通过。
模型成功也不代表 Business API 已连接。退出登录或停止 Harness 不会取消后端已提交任务。

## 7. GUI、上传与 HTTP 客户端

### 7.1 前台启动与 SSH 访问

```bash
cd /home/atis/Documents/RAY/claw-code-agent
claw-code-gui --host 127.0.0.1 --port 8765 --no-browser
```

先保持前台运行排查问题。同服务器浏览器访问 `http://127.0.0.1:8765`，
登录后聊天、上传视频；同名文件自动改名，不覆盖已有视频。
上传返回的 `uploads/...` 相对路径可交给 Agent，仅属于当前用户。

从自己电脑访问远程 Harness，在本地执行（替换 SSH 别名）：

```bash
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:8765:127.0.0.1:8765 <harness-server-ssh-alias>
```

保持隧道运行，本地打开同一 URL。若本地 8765 被占用，可把 `-L` 的本地端口改为
18765，再访问 `http://127.0.0.1:18765`。

不要沿用旧版直接开放公网 HTTP 8765 的步骤。若必须跨网络直接部署，需另行设计
HTTPS、反向代理可信转发、访问限制和限流；仅有登录页不代表生产安全。
原开发者 GUI 的配置/MCP/管理面板在登录版中不可用。

### 7.2 认证接口与脚本测试

| 接口 | 当前契约 |
| --- | --- |
| POST /api/auth/login | JSON username/password；返回 access_token/user，并设置 HttpOnly、SameSite=Strict Cookie |
| GET /api/auth/me | 当前登录用户 |
| POST /api/auth/logout | 撤销当前 Token |
| POST /api/uploads | 原始文件字节请求体；X-Filename 为 URL 编码的文件名，不是 multipart |
| GET /api/state | 当前用户状态，只读；可检查 cwd/session_directory |
| GET /api/sessions | 当前用户会话 |
| POST /api/chat | 等待本轮完成，返回 JSON，包括 final_output/stop_reason |
| POST /api/chat/stream | NDJSON 进度事件与最终结果，不是 SSE |

除登录外，上述 API 需要 Cookie 或 `Authorization: Bearer <token>`。
不接受 LLM 提供 user_id 来切换身份。未登录为 401；流开始后的错误可能通过 error
事件返回，不能只看最初的 HTTP 200。跨 Origin 请求可能返回 403。

先验证未登录保护（预期 401）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/auth/me
```

再使用项目自带脚本，交互输入账号密码，测试登录和流式只读请求，不在命令中写密码：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
python demo_script.py
```

脚本会调用 LLM，消耗模型额度并保存会话。它直接读取进程环境，不自动加载 .env；
如需覆盖地址，在启动前设置 `HARNESS_GUI_URL`。本地转发为 18765 时可执行：

```bash
HARNESS_GUI_URL=http://127.0.0.1:18765 python demo_script.py
```

它使用环境中的有效 HARNESS_AUTH_TOKEN，或交互登录；不会自动使用 CLI 的
cli-token.json。不要把 Token 粘到截图、共享命令、日志或 Git。

## 8. systemd 常驻服务（Linux）

前台 CLI/GUI 验证通过后再创建服务，先停止前台 GUI，避免端口冲突。
以下使用 venv 示例；Conda 需把 ExecStart 改成第 3 节查到的真实可执行路径，
不在 unit 里执行 conda activate。

编辑服务文件：

```bash
sudo nano /etc/systemd/system/claw-code-agent.service
```

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
ExecStart=/home/atis/Documents/RAY/claw-code-agent/.venv/bin/claw-code-gui --host 127.0.0.1 --port 8765 --no-browser
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Workspace 仍从 .env 提供，不额外传 --cwd 或共享 --session-dir。不设置旧版的
`HOME=.../runtime-home`，更不能按用户请求修改全局 HOME。
这里的运行限制不等于用户间沙箱。

可选的文件系统加固（先确认当前服务的写入需求，再加入 [Service]）：

```ini
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=/home/atis/Documents/RAY/claw-code-agent
ReadWritePaths=/home/atis/Documents/RAY/agent_workspace /home/atis/Documents/RAY/.agent_workspace-auth
```

两个可写目录必须预先存在；修改路径时同步修改这些规则。.env 不能自动改变
systemd 的写入白名单。PrivateTmp 也意味着服务的 /tmp 与普通终端不共享，
测试视频应通过 uploads 输入，而不是依赖临时目录。

检查并启动：

```bash
sudo systemd-analyze verify /etc/systemd/system/claw-code-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now claw-code-agent
sudo systemctl status claw-code-agent --no-pager -l
```

只修改 .env 时重启即可；修改 unit 后需先 daemon-reload。更新公共规范或模型配置
后同样需要重启 GUI，重新测试会话。运行单个服务 Worker，不开启多 Worker。

## 9. 运行检查与排障

```bash
sudo systemctl status claw-code-agent --no-pager -l
sudo journalctl -u claw-code-agent -n 100 --no-pager
sudo journalctl -u claw-code-agent -f
ss -lntp | grep 8765
```

`journalctl -f` 持续跟随日志，Ctrl+C 退出后再执行下一条。
预期只监听 127.0.0.1:8765。打开登录页并登录，在浏览器同一站点访问
`/api/auth/me` 和 `/api/state`，确认用户身份、用户 cwd 和 sessions 路径。
未认证 curl /api/state 返回 401 是正常保护，不是服务故障。

| 现象 | 检查 |
| --- | --- |
| command not found / ExecStart 失败 | Python 环境、入口绝对路径、服务用户可读/执行权限 |
| GUI 无法启动 | 端口冲突、Journal、依赖、Workspace/认证目录权限 |
| 登录报错或账号不存在 | 是否使用相同总 Workspace、HARNESS_AUTH_DIR；账号是否预创建 |
| CLI 登录了但仍报 Token 无效 | 是否有旧 HARNESS_AUTH_TOKEN 覆盖本地凭证；Token 是否已过期/撤销 |
| 本机可访问、本地电脑不能访问 | SSH 隧道目标、别名、端口占用；业务 API 隧道不是 GUI 隧道 |
| 上传返回 413 | HARNESS_MAX_UPLOAD_BYTES；如有代理，还要检查代理的请求体限制 |
| 上传返回 400 | 空文件或 X-Filename 非法；该接口不接收 multipart |
| 修改 .env 不生效 | 启动目录、已有环境变量优先级、EnvironmentFile 路径和是否重启 |
| HTTP 成功但 Agent 执行失败 | final_output/stop_reason、流 error 事件、LLM 配置和工具错误 |
| List 仍显示 pending/running/null | 先调 Status 更新索引；List 不主动刷新状态 |
| Status done 但 Result 失败 | 保留 task_id，对账/稍后查询；不要重新 Submit |
| 加固后出现只读错误 | ReadWritePaths 是否同时包含 Workspace 和认证目录 |

日志可能包含业务信息，分享前脱敏。后台 Agent 的日志和进程记录在当前用户
`.port_sessions/background/`，与 systemd Journal 是两套记录。
后台启动使用当前用户凭证；过期/退出后不能再用该凭证启动新的操作。

## 10. 开发回归与真实业务联调

### 10.1 不调用真实接口的测试

从仓库和已激活的环境运行；pytest 是开发测试依赖：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
python -m pip install 'pytest>=7'
RUN_VIDEO_ANALYSIS_INTEGRATION=0 python -m pytest \
  tests/test_user_prototype.py tests/test_task_indexes.py \
  tests/test_video_analysis_unit.py tests/test_video_processing.py \
  tests/test_model_training.py tests/test_business_idempotency.py -q
RUN_VIDEO_ANALYSIS_INTEGRATION=0 python -m pytest tests -q
```

请在开发/预发布环境运行；旧 runtime 测试可能创建本地运行目录。
macOS 的 /var 与 /private/var 路径比较问题可用 `TMPDIR=/private/tmp` 运行测试。
这些测试使用模拟后端，不能证明真实视频服务已接通。

### 10.2 网络与真实接口

Business URL 从 Harness 所在服务器访问，不是从用户浏览器访问。
如果后端仅能经 SSH 跳板机到达，应在运行 Harness 的那台机器上建立隧道。例如，
已配置可用的 SSH 别名后：

```bash
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18000:127.0.0.1:8000 <business-server-ssh-alias>
```

对应服务的 .env URL 配为 `http://127.0.0.1:18000`。不同后端端口需分别转发，
不要假设三个 API 共用地址。隧道需持续运行；上面的 Harness systemd 服务不会
自动创建或维护它，重启服务器后必须单独恢复隧道。

### 10.3 业务验收顺序

经业务负责人批准，用小型测试视频和可控训练任务验收，避免重复触发昂贵任务：

1. 登录并上传 raw video，确认路径进入当前用户 uploads/。
2. Analysis：支持业务服务器 local_file、Harness 用户目录 upload_file、后端解析的
   cos_file；执行 submit → status → result。
3. Processing：raw_video_refs 是要从当前用户目录上传的文件；执行 submit → status
   → result，获得实时 manifest/dataset_id，不生成本地 datasets 文件。
4. Training：传入 Processing 返回的 dataset_id（参数名 dataset_ref），不上传数据。
   Harness 从当前用户处理任务的实时 Result 验证 dataset ID、scenario 和 ready，
   然后提交训练；完成后获取 model metadata，不生成本地 models 文件。
5. 三个模块 Submit 各自写入 task_id/pending，Status 更新索引。done 后应立即调用
   对应 Result；List 只重新获取本地已标 done 的结果。
6. 用 Bob 登录，确认不能查看 Alice 的视频、会话和任务。Alice 在 CLI/GUI 间恢复
   同一会话时路径应一致。允许上下文回答历史结果，但不能伪称刚查过最新状态。

Backend 未就绪时标记本节未验收；可以完成服务和模型层测试，但不报告业务已跑通。
`tests/test_video_analysis.py` 是需显式开启的真实接口开发测试，不是登录客户端；
具体开关和输入见 [TESTING_GUIDE.md](TESTING_GUIDE.md)，不要批量回归时开启。

## 11. 更新部署与备份

按顺序执行，任一步失败就停止，不要继续启动失败版本。

### 11.1 停止写入并备份

通知用户、暂停新请求，记录运行中 task_id，等 Agent 当前回合结束。
停止服务，并确认其他 CLI/后台 Harness 也不再写入：

```bash
sudo systemctl stop claw-code-agent
```

创建独立且受限的新备份目录（不放在 Agent Workspace 内）。以下 BACKUP_DIR 是
本轮 shell 变量，不是 .env 配置；后续备份命令在同一终端执行：

```bash
umask 077
mkdir -p /home/atis/Documents/RAY/harness-backups
BACKUP_DIR=$(mktemp -d /home/atis/Documents/RAY/harness-backups/release-XXXXXXXX)
cd /home/atis/Documents/RAY/claw-code-agent
git rev-parse HEAD > "$BACKUP_DIR/commit.txt"
python -m pip freeze > "$BACKUP_DIR/requirements.txt"
cp .env "$BACKUP_DIR/agent.env.backup"
cp /etc/systemd/system/claw-code-agent.service "$BACKUP_DIR/claw-code-agent.service"
tar -czf "$BACKUP_DIR/runtime.tar.gz" \
  -C /home/atis/Documents/RAY agent_workspace .agent_workspace-auth
```

这份备份包含视频，需预留容量；自选路径时同步调整命令。备份既包含所有用户数据，
也包含敏感认证库/Token/.env，不可放进 Git、用户目录或公开下载路径。
用户目录与认证库的 user_id 必须匹配，只备份 sessions 不足以恢复用户。
受控验证压缩包可读，并按组织策略保存异地/加密副本。

### 11.2 更新代码、依赖和公共规范

确认工作树干净；有本地修改先由维护者处理，不能强制覆盖。

```bash
cd /home/atis/Documents/RAY/claw-code-agent
git status --short
git fetch origin
git switch main
git pull --ff-only origin main
python -m pip install -r requirements.txt
python -m pip install . --no-deps
python -m pip check
cp agent_operation.md /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
chmod 600 /home/atis/Documents/RAY/agent_workspace/CLAUDE.md
```

激活的是服务实际使用的 Python 环境。公共模板仍保留路径占位符。
审核新版本配置/索引格式和迁移要求，在测试环境完成第 10.1 节回归后再启动。
自动拉取、安装、复制规范、重启的 auto-deploy 脚本尚未实现。

```bash
sudo systemctl daemon-reload
sudo systemctl start claw-code-agent
sudo systemctl status claw-code-agent --no-pager -l
sudo journalctl -u claw-code-agent -n 100 --no-pager
```

重新登录验证 GUI、模型和原 task_id；不要通过重新 Submit 验证更新前的在途任务。
更新公共规范后新建会话确认，旧会话文本可能仍包含历史指令或路径。

### 11.3 幂等记录不能随意清空

当前 idempotency_key 是 Harness 内部提交控制参数，不传入 Business HTTP 请求。
同一会话、接口和输入重复提交可复用任务；用户明确重做时使用 repeat_of_task_id
建立新操作，重复调用同一个重做请求仍应复用新任务。

每用户的 `.port_sessions/business_functions/` 保存请求指纹、task_id 和控制记录，
不是结果缓存。提交响应丢失或超时时，不确定后端是否已受理，需要人工对账；
不要删除索引/幂等记录、切新会话或换 key 来绕过保护。
本地幂等不保证 Business 恰好执行一次，旧数据迁移也不会恢复全部历史幂等控制状态。

## 12. 回滚

先暂停新请求并停止所有写入，确认目标 commit 支持当前账号库和索引格式。
不要把当前多用户数据直接交给不支持认证的旧单用户版本，更不能因此恢复公网无认证访问。

仅在确认数据/配置兼容后，激活服务环境并执行（替换占位 commit）：

```bash
cd /home/atis/Documents/RAY/claw-code-agent
sudo systemctl stop claw-code-agent
git status --short
git switch --detach <known-good-commit>
python -m pip install -r requirements.txt
python -m pip install . --no-deps
python -m pip check
```

审核目标版本的 .env、unit、公共规范和索引契约，再从目标模板或已审核备份恢复公共
CLAUDE.md。不要盲目覆盖自定义规则；当前多用户版本仍需保留用户路径占位符。
若修改 unit，daemon-reload 后再启动；重复第 6–10 节相关验收。

代码回滚不等于数据或后端任务回滚。不要直接用旧备份覆盖更新后新增的账号、
会话和任务索引。格式不兼容时保持停机，先制定迁移/恢复方案。
依赖使用版本范围，重新 pip install 不保证还原旧依赖；必要时依据备份的 freeze
清单重建并验证环境。恢复认证库可能恢复旧 Token 状态，需评估并处理登录凭证。
确认后可固定已验证 commit，或通过正常代码发布流程发布回滚版本。

## 13. 旧单用户数据迁移

这是显式操作，程序不会启动后自动搬运或删除旧数据。先停旧服务与 CLI 并备份源目录；
新部署的目标 Workspace/认证目录和用户账号需已按前文准备。

从新代码目录执行（替换源路径，默认仅预览）：

```bash
claw-code-agent migrate-user-data alice --source /path/to/legacy-workspace
```

检查报告中的 source、target、copy_count、session_count 和 task_ids；
确认归属、容量和无冲突后才执行：

```bash
claw-code-agent migrate-user-data alice --source /path/to/legacy-workspace --apply
```

| 内容 | 处理方式 |
| --- | --- |
| uploads/ | 复制到指定用户，已有不同内容时拒绝覆盖 |
| sessions/、.port_sessions/agent/ | 复制聊天并绑定新用户运行路径 |
| 被会话引用的 scratchpad | 复制到该用户 scratchpad |
| 旧 tasks 与业务幂等文件 | 提取 task_id；不复制完整旧结果或幂等状态 |
| 新导入任务状态 | null，需 Status 查询确认，不根据旧结果假定 done |
| 独立 Result/manifest/metadata | 不复制；结果从 Business API 查询 |
| 源目录 | 保留，不自动删除 |

聊天历史原文可能引用旧路径，不能把旧路径当作新用户可访问目录。
普通“旧纯 ID 索引升级为 ID/status 索引”由读写兼容层处理，不需要使用这条
单用户目录迁移命令；迁移命令也不是任意多用户目录搬家工具。

迁移后登录验收数据归属，按原 task_id 查询状态/结果，不重复提交历史任务。
新账号目录不能作为 --source。确认备份和验收后，旧目录的清理由管理员另行安排；
不要将 root 下遗留的 uploads/sessions 当成所有新用户共享数据。

## 14. 可选：自托管 LLM

未来换成本地模型服务时，Harness 仍通过 OpenAI-compatible API 调用，
在 .env 更新以下配置并重启即可，不把模型权重放进 Agent Workspace：

```dotenv
OPENAI_BASE_URL=http://your-model-server:8000/v1
OPENAI_MODEL=your-served-model-name
OPENAI_API_KEY=your-model-server-key
```

服务端需适配 Harness 的 Chat Completions、tools/tool_choice 和标准 tool_calls
协议，实际模型需通过工具调用验收。模型服务器的硬件、量化、并行和 parser
配置不属于本指南；不能仅凭模型名称断言可部署。vLLM 或其他推理引擎使用独立环境、
服务用户或容器，不安装到 Harness 的运行环境。

## 15. 当前能力边界

- 三个模块各有 Submit/Status/Result/List，共 12 个业务工具；Training 使用实时
  Processing 结果校验数据集，处理结果与模型元数据不单独落盘。
- 本地预置账号、密码登录、会话/路径/task_id 归属检查已实现，但没有独立 OS/容器
  用户沙箱；共享进程和文件访问权限仍属 prototype。
- 没有在线注册、密码找回、SSO、复杂角色、登录限流、生产级审计。
- GUI 使用单 Worker 内存用户状态；不支持直接扩成多 Worker。
- List 全量读取索引并串行查询 done 任务结果，无分页/并发/总预算优化。
- 无可靠的后台自动轮询、Webhook 工作流或训练完成后的自动部署/切换/回滚工具。
- Business 必须继续校验数据权限和 ready；Harness 提前检查不能消除状态竞争。
- 当前是内置 HTTP Functions 路径；第三方测试包和未来 MCP 接入另行适配。

后续需求见 [undeveloped_adaptation.md](undeveloped_adaptation.md)，
工具/业务设计见 [agent_operation.md](agent_operation.md) 和代码契约。
模板表达的边界不等同于系统级安全隔离。

## 16. 部署验收清单

### 安装与配置

- [ ] Python >= 3.10，CLI/GUI/systemd 使用相同已验证环境。
- [ ] 非 root 服务用户可读代码/.env，可写 Workspace 和认证目录。
- [ ] .env 权限 600、未被 Git 跟踪，凭证未进入模板/日志。
- [ ] AGENT_WORKSPACE 是总目录，HARNESS_AUTH_DIR 在其外部。
- [ ] 公共 CLAUDE.md 从 agent_operation.md 安装，磁盘保留用户路径占位符。
- [ ] systemd 不设置共享 session-dir 或按用户切换 HOME。

### 身份、会话与访问

- [ ] 账号预创建后自动生成用户目录和三个空索引，无需手工 mkdir uploads/sessions。
- [ ] 未认证 API 返回 401；正确密码可以登录；退出/过期后凭证不可继续使用。
- [ ] Agent cwd、session、上传路径均属于登录用户，CLI/GUI 可共用其会话。
- [ ] Bob 无法访问 Alice 的视频/任务/会话；应用级检查不宣称为生产隔离。
- [ ] GUI 回环监听，SSH 访问可用；只读模型请求有正确的最终回答和进度提示。

### Business 与运行维护

- [ ] Backend URL/隧道可用；三个模块完成已批准的真实业务验收。
- [ ] Submit 写 pending，Status 更新索引，List 仅获取本地 done 任务的 Result。
- [ ] done 后获取对应结果；不生成独立 tasks/datasets/models 业务结果目录。
- [ ] 日志已检查并脱敏，systemd 启动/重启可用，模板更新后已重启验收。
- [ ] 记录发布 commit 和依赖，备份包含所有用户数据、认证库、公共规范、.env 和 unit。
- [ ] 已验证恢复方案；未用旧备份覆盖新增任务；旧数据迁移先预览且保留原件。
