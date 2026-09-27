const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ctx = vm.createContext({window: {}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/gui/static/auth.js'), 'utf8'), ctx);

test('avatar text follows Chinese, single-word and multi-word username rules', () => {
  for (const [name, expected] of [
    ['雷晞宇', '晞宇'], ['晞宇', '晞宇'], ['雷', '雷'], ['𠮷野家', '野家'],
    ['ray', 'R'], ['ray_chen-123', 'R'], ['ray chen', 'RC'],
    ['ray chen lee', 'RC'], ['ray   chen', 'RC'], ['雷 晞宇', '雷晞'],
    [' ray chen ', 'RC'], ['123ray', '1'], ['', '?'],
  ]) {
    assert.equal(ctx.userAvatarText(name), expected, name);
  }
});
