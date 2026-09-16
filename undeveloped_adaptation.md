# 视频 Agent：多用户 Prototype 进度与待开发需求

最近更新：2026-09-16。当前以本地预置账号 + 用户目录 + Business HTTP Functions
构建 prototype；不代表生产级多租户隔离。部署以 DEPLOYMENT.md 为准。

## 已实现

- [x] 本地账号预创建、密码哈希、12 小时 Token、登录/退出/当前用户。
- [x] GUI 和 CLI Agent 入口要求认证；账号及凭证位于 Workspace 外。
- [x] users/<user_id>/uploads、sessions、.port_sessions、runtime-home 目录初始化。
- [x] CLI/GUI 会话目录统一；用户自己的会话查看、恢复和工具 task_id 归属检查。
- [x] 网页上传视频、同名不覆盖、上传路径归入当前用户。
- [x] 原九个 Business Functions 使用实时 API；只缓存索引状态，不独立保存业务结果。
- [x] 三个任务索引保存 {"tasks": [{"task_id": "...", "status": "pending"}]}，去重、文件锁、原子写入。
- [x] Submit 初始化 pending；Status 查询成功后更新索引；幂等重放不覆盖已查询状态。
- [x] 兼容旧 task_ids 索引，未知状态为 null；下次写入自动升级格式，查询状态后填充。
- [x] 新增 list_video_analysis_tasks、list_video_processing_tasks、list_model_training_tasks。
- [x] List 读取索引状态，仅对 done 任务实时查询 Result；单个结果查询失败单独报告。
  List 不刷新状态；需要最新进度时由 Agent 调用对应 Status Function。
- [x] Processing 返回 manifest 对象，Training 返回 metadata 对象，不声称生成本地路径。
- [x] Training 提交前根据当前用户 processing IDs 实时验证 dataset ID、scenario、ready。
- [x] 幂等记录只保留提交控制信息/请求指纹/task_id，不保存独立业务 Result。
- [x] 聊天 transcript 可保存工具结果；Agent 可利用上下文，最新状态需重新查询。
- [x] 保留实时工具提示、最终回答和旧 JSON 聊天结果格式；接口新增认证要求。
- [x] 旧数据显式迁移入口：默认 dry-run，--apply 复制视频/聊天/ID；保留原件。
- [x] 原型默认禁用用户 Shell、写文件、配置管理和委派；公共 CLAUDE.md 只读加载。
- [x] 业务上下文压缩：原始 System Prompt 原样保留，旧工具结果按结构保留引用，
  用户约束/选择/授权不经固定字符裁剪；使用业务摘要并在现有会话中保留引用记录。
- [x] 手动 /compact 成功后保存会话；禁用业务会话的共享 HOME session-memory；
  摘要过长时不丢弃旧轮次强行重试，摘要截断/失败时不替换历史。
- [x] 明确压缩后 List/Status/Result 恢复规则，禁止因上下文丢失而重新 Submit；
  增加压缩、保存恢复及 GUI → CLI 业务查询回归测试。

## 当前业务工具（12 个）

| 模块 | Submit | Status | Result | List |
| --- | --- | --- | --- | --- |
| Video Analysis | submit_video_analysis | get_video_analysis_status | get_video_analysis_result | list_video_analysis_tasks |
| Video Processing | submit_video_processing | get_video_processing_status | get_video_processing_result | list_video_processing_tasks |
| Model Training | submit_model_training | get_model_training_status | get_model_training_result | list_model_training_tasks |

Analysis 继续支持 local_file（业务服务器路径）、upload_file（当前用户工作区文件）、
cos_file（业务后端解析）；Processing 上传 raw_video_refs；Training 只发送 scenario、
dataset_ref，不上传数据。Business 服务保存 processed datasets、labels、权重、结果。

## 当前数据结构

```text
AGENT_WORKSPACE/
├── CLAUDE.md
└── users/<user_id>/
    ├── uploads/
    ├── video_processing_task_id.json
    ├── model_training_task_id.json
    ├── video_analysis_task_id.json
    ├── sessions/
    ├── .port_sessions/
    └── runtime-home/.claude/
```

Result 的唯一实时权威来源是 Business API。聊天中允许保留历史快照，
但不再生成 tasks/、datasets/、models/。runtime-home 暂时只预留，
不能通过改变单个多用户服务器进程的 HOME 实现用户隔离。

## 待完成：本轮联调和旧数据处理

- [ ] 管理员确认旧数据归属账号，备份并执行迁移。
- [ ] 在实际服务器创建测试账号，验证真实后端的完整 processing → training → analysis 链路。
- [ ] 与 Business 确认 dataset/model 元数据字段和最终访问权限、ready 检查。
- [ ] 备份与迁移验收后，再由管理员清理旧独立结果文件；程序不会启动即删除。

## 后续优化

- [ ] 真正的文件/Shell/进程或容器隔离，隔离插件、HOME、环境和凭证。
- [ ] 登录限流、审计、账号禁用、密码重置、SSO、复杂权限与生产级 HTTPS。
- [ ] 多 Worker 服务的用户状态/锁共享、资源配额和会话生命周期清理。
- [ ] List 分页、并发查询、超时总预算与上下文大小控制。
- [ ] 业务后端原生幂等、提交不确定时对账、跨会话重复操作确认。
- [ ] 多 scenario 路由、更丰富的视频 preprocessing、训练/分析模型选择。
- [ ] 后端训练完成后的验证、部署、切换、回滚（当前没有这些工具）。
- [ ] 随真实业务工具扩展继续更新 agent_operation.md 和第三方 video_agent_tools 契约。
- [ ] 自动部署脚本：拉取代码、检查/创建运行目录、同步 agent_operation.md 为公共
  CLAUDE.md、安装依赖、执行测试、重启服务。具体服务器与权限明确后实现。

## 验证方式

参见 TESTING_GUIDE.md 的多用户 Prototype 章节。开发回归使用模拟 Business API，
不应将单元测试通过解释为真实视频业务后端已完成验收。
