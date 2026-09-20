// Pure front-end unit tests: mock dialogs and requests, never operate a browser
// or delete real data. Run: node --test tests/test_session_deletion_ui.js
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/gui/static/app.js'), 'utf8');
const actions = source.slice(source.indexOf('async function deleteSession('), source.indexOf('async function openSession('));

function fixture({ confirm = true, report = { deleted: ['one'] }, status = 200 } = {}) {
  const calls = [];
  const state = { isBusy: false, activeSessionId: 'one' };
  const context = {
    State: state,
    window: { confirm(message) { calls.push(['confirm', message]); return confirm; } },
    setStatus(...args) { calls.push(['status', ...args]); },
    setBusy(busy) { state.isBusy = busy; calls.push(['status', busy ? 'busy' : 'ready', busy ? 'Working…' : 'Ready']); },
    newSession() { state.activeSessionId = null; calls.push(['new']); },
    async loadSessions() { calls.push(['reload']); },
    async apiPost(url) { calls.push(['preview', url]); return { session_ids: ['one', 'two'] }; },
    async fetch(url, options) {
      calls.push(['delete', url, options]);
      return { ok: status === 200, status, json: async () => report };
    },
  };
  vm.createContext(context);
  vm.runInContext(actions, context);
  return { context, calls, state };
}

test('cancel single or bulk deletion sends no DELETE request', async () => {
  const { context, calls } = fixture({ confirm: false });
  await context.deleteSession({ session_id: 'one', preview: 'query' });
  await context.clearSessions();
  assert.equal(calls.filter(c => c[0] === 'delete').length, 0);
  assert.equal(calls.filter(c => c[0] === 'confirm').length, 2);
  assert.match(calls[0][1], /永久删除.*无法恢复/);
});

test('deleting active session resets chat and preserves success status', async () => {
  const { context, calls, state } = fixture();
  await context.deleteSession({ session_id: 'one' });
  assert.equal(calls.find(c => c[0] === 'delete')[1], '/api/sessions/one?confirm=true');
  assert.equal(state.activeSessionId, null);
  assert.equal(state.isBusy, false);
  assert.equal(calls.filter(c => c[0] === 'reload').length, 1);
  assert.match(calls.at(-1)[2], /已永久删除 1 个会话/);
});

test('bulk deletion submits confirmed snapshot and reports skipped/error counts', async () => {
  const { context, calls, state } = fixture({ report: {
    deleted: ['two'], skipped_running: ['one'], not_found: ['missing'], errors: [{ session_id: 'bad' }],
  } });
  await context.clearSessions();
  const request = calls.find(c => c[0] === 'delete');
  assert.deepEqual(JSON.parse(request[2].body), { session_ids: ['one', 'two'], confirm: true });
  assert.equal(state.activeSessionId, 'one');
  assert.equal(calls.at(-1)[1], 'error');
  assert.match(calls.at(-1)[2], /跳过 1 个运行中会话/);
  assert.match(calls.at(-1)[2], /1 个删除失败/);
});

test('busy HTTP response preserves chat and error message', async () => {
  const { context, calls, state } = fixture({ status: 409, report: { detail: 'Session is running' } });
  await context.deleteSession({ session_id: 'one' });
  assert.equal(state.activeSessionId, 'one');
  assert.equal(state.isBusy, false);
  assert.equal(calls.at(-1)[1], 'error');
  assert.match(calls.at(-1)[2], /Session is running/);
});

test('current browser turn disables deletion and confirmation', async () => {
  const { context, calls, state } = fixture();
  state.isBusy = true;
  await context.deleteSession({ session_id: 'one' });
  await context.clearSessions();
  assert.equal(calls.filter(c => c[0] !== 'status').length, 0);
});
