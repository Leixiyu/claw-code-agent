const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/gui/static/usage.js'), 'utf8');
function fixture(fetch) {
  const elements = Object.fromEntries(['usage-refresh', 'usage-total', 'usage-details', 'usage-note']
    .map(id => [id, {textContent: '', disabled: false}]));
  const context = {CustomEvent: class { constructor(type) { this.type = type; } }, window: {dispatchEvent() {}}, document: {querySelector: key => elements[key.slice(1)]}, fetch};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/gui/static/api.js'), 'utf8'), context);
  vm.runInContext(source, context);
  return {elements, window: context.window};
}
test('lifetime total, cached usage, legacy imports and uncertainty are visible', async () => {
  const {elements, window} = fixture(async url => {
    assert.equal(url, '/api/usage');
    return {ok: true, json: async () => ({total_tokens: 120, input_tokens: 40, output_tokens: 20,
      cache_read_input_tokens: 60, cache_creation_input_tokens: 0, imported_tokens: 12,
      pending_requests: 1, history_import_skipped: 2})};
  });
  await window.refreshHarnessUsage();
  assert.equal(elements['usage-total'].textContent, '120');
  assert.match(elements['usage-details'].textContent, /缓存读取 60/);
  assert.match(elements['usage-note'].textContent, /包含已删除对话/);
  assert.match(elements['usage-note'].textContent, /历史补录 12/);
  assert.match(elements['usage-note'].textContent, /待核实/);
  assert.match(elements['usage-note'].textContent, /2 个旧会话/);
});
test('failed fetch clears stale values and enables retry', async () => {
  const {elements, window} = fixture(async () => ({ok: false, status: 401, json: async () => ({})}));
  elements['usage-total'].textContent = '500';
  await window.refreshHarnessUsage();
  assert.equal(elements['usage-total'].textContent, '—');
  assert.match(elements['usage-note'].textContent, /重新登录/);
  assert.equal(elements['usage-refresh'].disabled, false);
});
