"""Single end-user visibility/execution policy. Restart after editing these sets.

Keys are (kind, canonical name). Move an entry between the two sets to change
access. Slash/skill aliases inherit the canonical entry. Unknown entries are
denied; local administrative CLI commands remain separate from this public
user policy. HTTP keys include the verb so read access never grants writes.
"""
from __future__ import annotations

USER_AVAILABLE = {
    ('command', 'clear'),
    ('command', 'compact'),
    ('command', 'help'),
    ('http', 'DELETE /api/sessions'),
    ('http', 'DELETE /api/sessions/{session_id}'),
    ('http', 'GET /api/auth/me'),
    ('http', 'GET /api/capabilities'),
    ('http', 'GET /api/sessions'),
    ('http', 'GET /api/sessions/{session_id}'),
    ('http', 'GET /api/skills'),
    ('http', 'GET /api/slash-commands'),
    ('http', 'GET /api/state'),
    ('http', 'GET /api/usage'),
    ('http', 'GET /api/usage/requests'),
    ('http', 'GET /health'),
    ('http', 'POST /api/auth/login'),
    ('http', 'POST /api/auth/logout'),
    ('http', 'POST /api/chat'),
    ('http', 'POST /api/chat/stream'),
    ('http', 'POST /api/clear'),
    ('http', 'POST /api/sessions/clear-preview'),
    ('http', 'POST /api/uploads'),
    ('tool', 'get_model_training_result'),
    ('tool', 'get_model_training_status'),
    ('tool', 'get_video_analysis_result'),
    ('tool', 'get_video_analysis_status'),
    ('tool', 'get_video_processing_result'),
    ('tool', 'get_video_processing_status'),
    ('tool', 'glob_search'),
    ('tool', 'grep_search'),
    ('tool', 'list_dir'),
    ('tool', 'list_model_training_tasks'),
    ('tool', 'list_video_analysis_tasks'),
    ('tool', 'list_video_processing_tasks'),
    ('tool', 'read_file'),
    ('tool', 'sleep'),
    ('tool', 'submit_model_training'),
    ('tool', 'submit_video_analysis'),
    ('tool', 'submit_video_processing'),
}

USER_UNAVAILABLE = {
    ('command', 'account'),
    ('command', 'add-dir'),
    ('command', 'agents'),
    ('command', 'ask'),
    ('command', 'branch'),
    ('command', 'bridge'),
    ('command', 'btw'),
    ('command', 'chrome'),
    ('command', 'commit'),
    ('command', 'config'),
    ('command', 'context'),
    ('command', 'context-raw'),
    ('command', 'copy'),
    ('command', 'cost'),
    ('command', 'deep-link'),
    ('command', 'desktop'),
    ('command', 'diff'),
    ('command', 'direct-connect'),
    ('command', 'disconnect'),
    ('command', 'doctor'),
    ('command', 'effort'),
    ('command', 'exit'),
    ('command', 'export'),
    ('command', 'extra-usage'),
    ('command', 'fast'),
    ('command', 'feedback'),
    ('command', 'files'),
    ('command', 'hooks'),
    ('command', 'ide'),
    ('command', 'init'),
    ('command', 'install-github-app'),
    ('command', 'install-slack-app'),
    ('command', 'keybindings'),
    ('command', 'login'),
    ('command', 'logout'),
    ('command', 'lsp'),
    ('command', 'mcp'),
    ('command', 'memory'),
    ('command', 'messages'),
    ('command', 'mobile'),
    ('command', 'model'),
    ('command', 'output-style'),
    ('command', 'passes'),
    ('command', 'permissions'),
    ('command', 'plan'),
    ('command', 'plugin'),
    ('command', 'pr-comments'),
    ('command', 'privacy-settings'),
    ('command', 'prompt'),
    ('command', 'rate-limit-options'),
    ('command', 'release-notes'),
    ('command', 'reload-plugins'),
    ('command', 'remote'),
    ('command', 'remote-env'),
    ('command', 'remote-setup'),
    ('command', 'remotes'),
    ('command', 'rename'),
    ('command', 'resource'),
    ('command', 'resources'),
    ('command', 'resume'),
    ('command', 'rewind'),
    ('command', 'sandbox-toggle'),
    ('command', 'search'),
    ('command', 'skills'),
    ('command', 'ssh'),
    ('command', 'stats'),
    ('command', 'status'),
    ('command', 'stickers'),
    ('command', 'tag'),
    ('command', 'task'),
    ('command', 'task-next'),
    ('command', 'tasks'),
    ('command', 'team'),
    ('command', 'teams'),
    ('command', 'teleport'),
    ('command', 'theme'),
    ('command', 'token-budget'),
    ('command', 'tools'),
    ('command', 'trigger'),
    ('command', 'triggers'),
    ('command', 'trust'),
    ('command', 'upgrade'),
    ('command', 'version'),
    ('command', 'vim'),
    ('command', 'voice'),
    ('command', 'workflow'),
    ('command', 'workflows'),
    ('command', 'worktree'),
    ('http', 'DELETE /api/ask-user/queue/{index}'),
    ('http', 'DELETE /api/memory/file'),
    ('http', 'DELETE /api/teams/{name}'),
    ('http', 'GET /api/account'),
    ('http', 'GET /api/ask-user'),
    ('http', 'GET /api/background'),
    ('http', 'GET /api/background/{background_id}'),
    ('http', 'GET /api/background/{background_id}/logs'),
    ('http', 'GET /api/diagnostics'),
    ('http', 'GET /api/diagnostics/{name}'),
    ('http', 'GET /api/file-history'),
    ('http', 'GET /api/mcp'),
    ('http', 'GET /api/memory'),
    ('http', 'GET /api/memory/file'),
    ('http', 'GET /api/plan'),
    ('http', 'GET /api/plugins'),
    ('http', 'GET /api/remote'),
    ('http', 'GET /api/remote-triggers'),
    ('http', 'GET /api/search'),
    ('http', 'GET /api/tasks'),
    ('http', 'GET /api/teams'),
    ('http', 'GET /api/workflows'),
    ('http', 'GET /api/worktree'),
    ('http', 'PATCH /api/remote-triggers/{trigger_id}'),
    ('http', 'PATCH /api/tasks/{task_id}'),
    ('http', 'POST /api/account/login'),
    ('http', 'POST /api/account/logout'),
    ('http', 'POST /api/ask-user/clear-history'),
    ('http', 'POST /api/ask-user/queue'),
    ('http', 'POST /api/background/{background_id}/kill'),
    ('http', 'POST /api/mcp/resources/read'),
    ('http', 'POST /api/mcp/tools/call'),
    ('http', 'POST /api/plan/clear'),
    ('http', 'POST /api/remote-triggers'),
    ('http', 'POST /api/remote-triggers/{trigger_id}/run'),
    ('http', 'POST /api/remote/connect'),
    ('http', 'POST /api/remote/disconnect'),
    ('http', 'POST /api/search/activate/{name}'),
    ('http', 'POST /api/search/query'),
    ('http', 'POST /api/state'),
    ('http', 'POST /api/tasks'),
    ('http', 'POST /api/tasks/{task_id}/block'),
    ('http', 'POST /api/tasks/{task_id}/cancel'),
    ('http', 'POST /api/tasks/{task_id}/complete'),
    ('http', 'POST /api/tasks/{task_id}/start'),
    ('http', 'POST /api/teams'),
    ('http', 'POST /api/teams/{name}/messages'),
    ('http', 'POST /api/workflows/{name}/run'),
    ('http', 'POST /api/worktree/enter'),
    ('http', 'POST /api/worktree/exit'),
    ('http', 'PUT /api/memory/file'),
    ('http', 'PUT /api/plan'),
    ('skill', 'debug'),
    ('skill', 'simplify'),
    ('skill', 'update-config'),
    ('skill', 'verify'),
    ('tool', 'Agent'),
    ('tool', 'EnterPlanMode'),
    ('tool', 'ExitPlanMode'),
    ('tool', 'LSP'),
    ('tool', 'Skill'),
    ('tool', 'TaskOutput'),
    ('tool', 'TaskStop'),
    ('tool', 'account_list_profiles'),
    ('tool', 'account_login'),
    ('tool', 'account_logout'),
    ('tool', 'account_status'),
    ('tool', 'ask_user_question'),
    ('tool', 'bash'),
    ('tool', 'config_get'),
    ('tool', 'config_list'),
    ('tool', 'config_set'),
    ('tool', 'delegate_agent'),
    ('tool', 'edit_file'),
    ('tool', 'mcp_call_tool'),
    ('tool', 'mcp_list_resources'),
    ('tool', 'mcp_list_tools'),
    ('tool', 'mcp_read_resource'),
    ('tool', 'notebook_edit'),
    ('tool', 'plan_clear'),
    ('tool', 'plan_get'),
    ('tool', 'remote_connect'),
    ('tool', 'remote_disconnect'),
    ('tool', 'remote_list_profiles'),
    ('tool', 'remote_status'),
    ('tool', 'remote_trigger'),
    ('tool', 'search_activate_provider'),
    ('tool', 'search_list_providers'),
    ('tool', 'search_status'),
    ('tool', 'send_message'),
    ('tool', 'task_block'),
    ('tool', 'task_cancel'),
    ('tool', 'task_complete'),
    ('tool', 'task_create'),
    ('tool', 'task_get'),
    ('tool', 'task_list'),
    ('tool', 'task_next'),
    ('tool', 'task_start'),
    ('tool', 'task_update'),
    ('tool', 'team_create'),
    ('tool', 'team_delete'),
    ('tool', 'team_get'),
    ('tool', 'team_list'),
    ('tool', 'team_messages'),
    ('tool', 'todo_write'),
    ('tool', 'tool_search'),
    ('tool', 'update_plan'),
    ('tool', 'web_fetch'),
    ('tool', 'web_search'),
    ('tool', 'workflow_get'),
    ('tool', 'workflow_list'),
    ('tool', 'workflow_run'),
    ('tool', 'worktree_enter'),
    ('tool', 'worktree_exit'),
    ('tool', 'worktree_status'),
    ('tool', 'write_file'),
}


def user_can(kind: str, name: str) -> bool:
    key = (kind, name)
    return key in USER_AVAILABLE and key not in USER_UNAVAILABLE


def _path_matches(template: str, path: str) -> bool:
    expected, actual = template.split('/'), path.split('/')
    return len(expected) == len(actual) and all(
        left == right or (left.startswith('{') and left.endswith('}') and bool(right))
        for left, right in zip(expected, actual))


def user_can_http(method: str, path: str) -> bool:
    key = f'{method.upper()} {path}'
    if ('http', key) in USER_UNAVAILABLE:
        return False
    for kind, entry in USER_AVAILABLE:
        if kind != 'http' or not user_can(kind, entry):
            continue
        verb, template = entry.split(' ', 1)
        if verb == method.upper() and _path_matches(template, path):
            return True
    return False


def user_can_command(name: str) -> bool:
    from .agent_slash_commands import find_slash_command
    from .bundled_skills import find_bundled_skill
    spec = find_slash_command(name)
    if spec is not None:
        return user_can('command', spec.names[0])
    skill = find_bundled_skill(name)
    return skill is not None and skill.user_invocable and user_can('skill', skill.name)


def user_commands():
    from .agent_slash_commands import get_slash_command_specs
    return tuple(spec for spec in get_slash_command_specs() if user_can('command', spec.names[0]))


def user_skills():
    from .bundled_skills import get_bundled_skills
    return tuple(skill for skill in get_bundled_skills()
                 if skill.user_invocable and user_can('skill', skill.name))


def user_capabilities():
    return {
        'http': sorted(name for kind, name in USER_AVAILABLE if kind == 'http' and user_can(kind, name)),
        'commands': [name for spec in user_commands() for name in spec.names],
        'skills': [skill.name for skill in user_skills()],
        'tools': sorted(name for kind, name in USER_AVAILABLE if kind == 'tool' and user_can(kind, name)),
    }
