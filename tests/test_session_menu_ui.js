const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/gui/static/app.js'), 'utf8');
const actions = source.slice(source.indexOf('async function copySessionId('), source.indexOf('async function deleteSession('));

function fixture(input = '  新名称  ', fail = false) {
  const calls = [];
  const ctx = {
    State: {isBusy: false},
    async requestSessionName(session) { calls.push(['name', session]); return input; },
    window: {prompt(...args) { calls.push(['prompt', ...args]); return input; }},
    navigator: {clipboard: {async writeText(id) { if (fail) throw Error('denied'); calls.push(['copy', id]); }}},
    setBusy(busy) { ctx.State.isBusy = busy; },
    setStatus(...args) { calls.push(['status', ...args]); },
    async apiPost(...args) { calls.push(['post', ...args]); if (fail) throw Error('Session is running'); },
    async loadSessions() { calls.push(['reload']); },
  };
  vm.createContext(ctx);
  vm.runInContext(actions, ctx);
  return {ctx, calls};
}

test('rename cancellation, empty names and busy state never submit', async () => {
  for (const input of [null, '', ' ', 'a'.repeat(81)]) {
    const {ctx, calls} = fixture(input);
    await ctx.renameSession({session_id: 'one'});
    assert.equal(calls.some(c => c[0] === 'post'), false);
  }
  const {ctx, calls} = fixture();
  ctx.State.isBusy = true;
  await ctx.renameSession({session_id: 'one'});
  assert.equal(calls.some(c => c[0] === 'name'), false);
});

test('successful rename trims input, targets selected session and reloads saved names', async () => {
  const {ctx, calls} = fixture();
  await ctx.renameSession({session_id: 'saved-id', name: '旧名称'});
  const post = calls.find(c => c[0] === 'post');
  assert.equal(post[1], '/api/sessions/saved-id/rename');
  assert.equal(post[2].name, '新名称');
  assert.equal(calls.filter(c => c[0] === 'reload').length, 1);
  assert.equal(ctx.State.isBusy, false);
});

test('rename conflict leaves list unchanged and exposes the error', async () => {
  const {ctx, calls} = fixture('new', true);
  await ctx.renameSession({session_id: 'one'});
  assert.equal(calls.some(c => c[0] === 'reload'), false);
  assert.match(calls.at(-1)[2], /Session is running/);
  assert.equal(ctx.State.isBusy, false);
});

test('copy uses the complete ID and falls back to a copyable dialog on HTTP', async () => {
  for (const fail of [false, true]) {
    const {ctx, calls} = fixture(null, fail);
    await ctx.copySessionId('full-session-id');
    assert.equal(calls[0][0], fail ? 'prompt' : 'copy');
    assert.equal(calls[0].at(-1), 'full-session-id');
  }
});
