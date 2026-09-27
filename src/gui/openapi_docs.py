"""Describe the public dispatcher without registering duplicate runtime routes."""
from __future__ import annotations

from copy import deepcopy

from fastapi.openapi.utils import get_openapi

from ..user_access import user_can_http
from .api_errors import STATUS_CODES


TAGS = [
    {'name': '用户认证', 'description': '登录、当前身份与退出登录。'},
    {'name': '对话', 'description': '普通或 NDJSON 流式聊天，以及清除运行状态。'},
    {'name': '历史会话', 'description': '列表、读取、重命名和逐个删除。'},
    {'name': '文件上传', 'description': '上传到当前用户工作区，返回业务工具可用的 video_ref。'},
    {'name': 'Token 用量', 'description': '独立持久化账本，保留已删除会话的消耗；当前不计算金额。'},
    {'name': '系统信息', 'description': '当前用户的运行配置、权限、命令与技能。'},
    {'name': '服务健康', 'description': '只读依赖探测，不提交模型推理或业务任务。'},
    {'name': '其他接口', 'description': '由权限配置新开放、尚未添加专门说明的接口。'},
]

# Presentation metadata only: visibility always comes from user_can_http.
OPERATIONS = {
    ('post', '/api/auth/login'): ('用户认证', '登录',
        '用户名支持中文和内部空格；返回 Bearer 令牌，同时设置 harness_session Cookie。登录有效期为 12 小时。'),
    ('get', '/api/auth/me'): ('用户认证', '当前登录用户', '返回当前用户的 user_id 和 username。'),
    ('post', '/api/auth/logout'): ('用户认证', '退出登录', '撤销当前令牌并清除登录 Cookie。'),
    ('post', '/api/chat'): ('对话', '发送消息',
        '不传 resume_session_id 时，首条普通消息创建新会话；传入时继续已保存会话。'
        'usage 是会话累计量，run_usage 是本次执行增量，用户历史总量请查询 /api/usage。'
        'pasted_contents 用于展开粘贴文本引用。示例 /help 不调用模型。'),
    ('post', '/api/chat/stream'): ('对话', '发送消息并接收事件流',
        '请求体与 /api/chat 相同。响应为逐行 JSON（NDJSON），不是 SSE，也不是逐 Token 文本流。'
        '事件包括 heartbeat、tool_start、result（结果在 data 中）、error。'
        '流开始后的失败仍使用 HTTP 200，必须检查 error 事件的 code/message/status；'
        '认证及请求参数错误在流开始前返回普通 HTTP JSON 错误。断线不会取消已提交的执行，请勿盲目重发。'),
    ('post', '/api/clear'): ('对话', '清除运行状态',
        '退出当前对话并重置临时运行状态；返回状态快照及 action=runtime_state_cleared。'
        '保留历史会话、Token 账本、上传文件和业务任务。回复生成中返回 409。'
        'GUI「新对话」只清空本地界面，不调用此接口。'),
    ('get', '/api/sessions'): ('历史会话', '列出历史会话',
        '返回当前用户的会话数组；不传 limit 则返回全部。X-Session-Total 为总数，'
        'X-Session-Skipped 为损坏或不可读取的会话数。'),
    ('get', '/api/sessions/{session_id}'): ('历史会话', '读取会话', '读取已保存消息及会话用量，不触发模型执行。'),
    ('post', '/api/sessions/{session_id}/rename'): ('历史会话', '重命名会话',
        '名称去除首尾空白后须为 1–80 个字符，不允许控制字符。运行中的会话返回 409。'),
    ('delete', '/api/sessions/{session_id}'): ('历史会话', '删除一个会话',
        '必须显式设置 confirm=true；永久删除指定会话，运行中返回 409。'
        '保留独立 Token 账本、上传文件及业务任务。没有删除全部历史的接口。'),
    ('post', '/api/uploads'): ('文件上传', '上传视频文件',
        '请求体直接发送文件二进制，不能使用 multipart/form-data。X-Filename 为不含目录的文件名，'
        '中文文件名请先做 URL 编码，解码后最多 180 字节。空文件不接受，重名自动加前缀。'
        '大小上限由 HARNESS_MAX_UPLOAD_BYTES 配置，默认 2 GiB。返回 video_ref 可传给视频业务工具。'),
    ('get', '/api/usage'): ('Token 用量', '历史 Token 总量',
        'total_tokens = input_tokens + output_tokens + cache_read_input_tokens + cache_creation_input_tokens；'
        'reasoning_tokens 已包含在输出中，不重复累加。总量保留已删除会话的消耗。'
        'pending_requests = active_requests + unresolved_requests，大于 0 时总量可能不完整。'
        'imported_tokens 是旧会话累计补录，history_import_skipped 是暂未成功补录的旧会话数。'
        '无法恢复升级前已丢失的记录，也不能推算供应商未返回的用量。'),
    ('get', '/api/usage/requests'): ('Token 用量', '查询用量明细',
        '支持分页、Session ID 和状态筛选。in_progress=进行中，confirmed=用量已确认，'
        'unresolved=待核实；状态不代表业务任务是否成功。reason/reason_message 说明待核实或补录原因。'
        '时间字段为 Unix 秒；旧会话补录不能还原每次请求和原始发生日期。'),
    ('get', '/api/state'): ('系统信息', '当前用户运行配置',
        '返回模型、连接地址、工作目录、运行限制和 active_session_id 等状态快照。普通用户不能修改配置。'),
    ('get', '/api/capabilities'): ('系统信息', '当前可用能力',
        '返回允许的 HTTP 接口、命令及别名、技能和工具；与 GUI 展示和服务端权限检查使用同一配置。'),
    ('get', '/api/slash-commands'): ('系统信息', '可用对话命令', '只返回当前允许的命令、别名及说明。'),
    ('get', '/api/skills'): ('系统信息', '可用技能', 'include_internal 不会绕过权限过滤；没有可用技能时返回空数组。'),
    ('get', '/health'): ('服务健康', '检查服务与依赖',
        '无需登录。并行探测已配置的模型及视频分析健康端点，单项超时 5 秒。'
        'healthy/degraded 返回 200；有不可用依赖时返回 503，并保留相同健康报告结构。'
        'not_configured 表示未配置或该服务尚无健康端点，不等于故障。'),
}


def _json_response(description, schema):
    return {'description': description, 'content': {'application/json': {'schema': schema}}}


def _ref(name):
    return {'$ref': f'#/components/schemas/{name}'}


def _response_schemas():
    token_properties = {name: {'type': 'integer', 'minimum': 0} for name in (
        'input_tokens', 'output_tokens', 'cache_read_input_tokens',
        'cache_creation_input_tokens', 'reasoning_tokens', 'total_tokens')}
    summary_properties = {**token_properties, 'user_id': {'type': 'string'}}
    for name in ('record_count', 'request_count', 'active_requests', 'unresolved_requests',
                 'pending_requests', 'imported_tokens', 'history_import_skipped'):
        summary_properties[name] = {'type': 'integer', 'minimum': 0}
    summary_properties['total_tokens'] = {**token_properties['total_tokens'], 'description': '已记录的用户历史总量，包含已删除会话。'}
    request_properties = {**token_properties,
        **{name: {'type': 'string'} for name in ('request_id', 'session_id', 'run_id', 'model', 'purpose', 'reason_message')},
        'status': {'type': 'string', 'enum': ['in_progress', 'confirmed', 'unresolved']},
        'reason': {'type': ['string', 'null']},
        'created_at': {'type': 'number', 'description': 'Unix 秒'},
        'updated_at': {'type': ['number', 'null'], 'description': 'Unix 秒'},
        'finished_at': {'type': ['number', 'null'], 'description': 'Unix 秒'},
    }
    return {
        'HarnessError': {'type': 'object', 'required': ['code', 'message', 'status', 'error', 'detail', 'error_type'],
            'properties': {**{name: {'type': 'string'} for name in ('code', 'message', 'error', 'detail', 'error_type')},
                'status': {'type': 'integer'},
                'fields': {'type': 'array', 'items': {'type': 'object', 'properties': {
                    'path': {'type': 'string'}, 'type': {'type': 'string'}}}}}},
        'HarnessUser': {'type': 'object', 'required': ['user_id', 'username'],
            'properties': {'user_id': {'type': 'string'}, 'username': {'type': 'string'}}},
        'HarnessLogin': {'type': 'object', 'properties': {
            'access_token': {'type': 'string'}, 'token_type': {'type': 'string', 'enum': ['bearer']},
            'expires_at': {'type': 'number', 'description': 'Unix 秒'}, 'user': _ref('HarnessUser')}},
        'HarnessUsage': {'type': 'object', 'properties': summary_properties},
        'HarnessUsageRequest': {'type': 'object', 'properties': request_properties},
        'HarnessUsagePage': {'type': 'object', 'properties': {
            'items': {'type': 'array', 'items': _ref('HarnessUsageRequest')},
            'total': {'type': 'integer'}, 'limit': {'type': 'integer'},
            'offset': {'type': 'integer'}, 'has_more': {'type': 'boolean'}}},
        'HarnessHealth': {'type': 'object', 'properties': {
            'status': {'type': 'string', 'enum': ['healthy', 'degraded', 'unhealthy']},
            'checked_at': {'type': 'string', 'format': 'date-time'},
            'summary': {'type': 'object', 'additionalProperties': {'type': 'integer'}},
            'services': {'type': 'object', 'additionalProperties': {'type': 'object', 'properties': {
                'status': {'type': 'string', 'enum': ['healthy', 'unhealthy', 'not_configured']},
                'reason': {'type': 'string'}, 'http_status': {'type': 'integer'},
                'response_time_ms': {'type': 'number'}}}}}},
    }


def _describe_operation(method, path, operation):
    tag, summary, description = OPERATIONS.get((method, path),
        ('其他接口', operation.get('summary', path), operation.get('description', '')))
    operation.update(tags=[tag], summary=summary, description=description)
    public = path in {'/api/auth/login', '/health'}
    operation['security'] = [] if public else [{'BearerAuth': []}, {'SessionCookie': []}]
    responses = operation.setdefault('responses', {})
    codes = {403, 500}  # Policy/origin checks and the shared error boundary.
    if path != '/health':
        codes.add(401)
    if '422' in responses:
        codes.add(422)
    if path in {'/api/chat', '/api/chat/stream'}:
        codes.add(400)
    if path == '/api/clear':
        codes.add(409)
    if '{session_id}' in path:
        codes.add(404)
        if method != 'get':
            codes.update({400, 409})
    if path in {'/api/chat', '/api/usage', '/api/usage/requests'} or method == 'delete':
        codes.add(503)
    if path == '/api/chat':
        codes.update({404, 409, 413, 502})
    if path == '/api/uploads':
        codes.update({400, 413})
    for code in codes:
        responses[str(code)] = _json_response(STATUS_CODES[code], _ref('HarnessError'))
    response_name = {
        '/api/auth/login': 'HarnessLogin', '/api/auth/me': 'HarnessUser',
        '/api/usage': 'HarnessUsage', '/api/usage/requests': 'HarnessUsagePage',
        '/health': 'HarnessHealth',
    }.get(path)
    if response_name:
        responses['200'] = _json_response('成功响应', _ref(response_name))
    if path == '/health':
        responses['503'] = _json_response('依赖不可用；仍返回健康报告', _ref('HarnessHealth'))
    if path == '/api/sessions' and method == 'get':
        responses['200']['headers'] = {name: {'schema': {'type': 'integer'}, 'description': label}
            for name, label in [('X-Session-Total', '会话总数'), ('X-Session-Skipped', '跳过的会话数')]}
    examples = {
        '/api/auth/login': {'username': 'ray', 'password': 'your-password'},
        '/api/chat': {'prompt': '/help'}, '/api/chat/stream': {'prompt': '/help'},
        '/api/sessions/{session_id}/rename': {'name': '矿山视频分析'},
    }
    if path in examples:
        operation['requestBody']['content']['application/json']['example'] = examples[path]
    if path == '/api/uploads':
        operation['parameters'] = [{'name': 'X-Filename', 'in': 'header', 'required': True,
            'description': 'URL 编码的文件名，不含目录', 'schema': {'type': 'string'}, 'example': 'video.mp4'}]
        operation['requestBody'] = {'required': True, 'content': {
            'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}
        responses['200'] = _json_response('上传完成', {'type': 'object', 'properties': {
            'video_ref': {'type': 'object', 'properties': {
                'type': {'type': 'string', 'enum': ['upload_file']}, 'path': {'type': 'string'}}},
            'size_bytes': {'type': 'integer'}}})
    if path == '/api/chat/stream':
        responses['200'] = {'description': '逐行解析；遇到 result 或 error 结束', 'content': {
            'application/x-ndjson': {'schema': {'type': 'string'},
                'example': '{"type":"heartbeat"}\n{"type":"error","code":"not_found","message":"Session not found",'
                           '"status":404,"error":"Session not found","detail":"Session not found","error_type":"HTTPException"}\n'}}}


def _prune_schemas(schema):
    """Keep only models reachable from public operations, including nested refs."""
    models = schema['components']['schemas']
    used = set()

    def visit(value):
        if isinstance(value, dict):
            ref = value.get('$ref', '')
            if ref.startswith('#/components/schemas/'):
                name = ref.rsplit('/', 1)[-1]
                if name not in used:
                    used.add(name)
                    visit(models[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema['paths'])
    schema['components']['schemas'] = {name: model for name, model in models.items() if name in used}


def install_public_openapi(app, schema_app_factory):
    def public_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        # Build route declarations only. Do not create a user AgentState, access
        # per-user data, or dispatch any endpoint just to serve documentation.
        inner = schema_app_factory()
        schema = get_openapi(title=app.title, version=app.version,
            openapi_version=app.openapi_version, tags=deepcopy(TAGS),
            routes=[*app.routes, *inner.routes])
        schema['paths'] = {path: {method: operation for method, operation in methods.items()
                                 if user_can_http(method.upper(), path)}
                           for path, methods in schema['paths'].items()}
        schema['paths'] = {path: methods for path, methods in schema['paths'].items() if methods}
        components = schema.setdefault('components', {})
        components.setdefault('schemas', {}).update(_response_schemas())
        components['securitySchemes'] = {
            'BearerAuth': {'type': 'http', 'scheme': 'bearer',
                'description': '粘贴登录返回的 access_token，无需 Bearer 前缀。'},
            'SessionCookie': {'type': 'apiKey', 'in': 'cookie', 'name': 'harness_session',
                'description': '通过本页登录接口或同站点 GUI 自动设置；无需在 Authorize 手动填写 Cookie。'},
        }
        for path, methods in schema['paths'].items():
            for method, operation in methods.items():
                _describe_operation(method, path, operation)
        _prune_schemas(schema)
        used_tags = {tag for methods in schema['paths'].values() for operation in methods.values() for tag in operation['tags']}
        schema['tags'] = [tag for tag in schema['tags'] if tag['name'] in used_tags]
        app.openapi_schema = schema
        return schema

    app.openapi = public_openapi
