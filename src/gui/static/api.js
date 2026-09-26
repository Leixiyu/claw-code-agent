"use strict";

// HTTP and NDJSON errors share code/message/status. Legacy strings are accepted.
window.harnessError = function (payload = {}, status = 0) {
  if (!payload || typeof payload !== 'object') payload = {};
  const message = payload.message || (typeof payload.error === 'string' ? payload.error : '') ||
    (typeof payload.detail === 'string' ? payload.detail : '') ||
    (status === 401 ? '登录已失效，请重新登录。' : `请求失败（${status || '网络连接'}），请稍后重试。`);
  const error = new Error(message);
  error.code = payload.code || (status === 401 ? 'authentication_required' : 'request_failed');
  error.status = payload.status || status;
  error.sessionId = payload.session_id;
  if (error.status === 401) window.dispatchEvent(new CustomEvent('harness-auth-expired'));
  return error;
};
window.harnessReadResponse = async function (response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload?.error) throw window.harnessError(payload, response.status);
  return payload;
};
window.harnessFetch = async function (url, options) {
  try { return await fetch(url, options); }
  catch (_) { throw window.harnessError({code: 'connection_lost', message: '连接中断，请检查网络。已提交的任务可能仍在运行，请勿直接重复提交。'}); }
};
