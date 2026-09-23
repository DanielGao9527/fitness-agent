const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/guest-session.js'), 'utf8');
const storage = new Map();
function session(type, broken = false) {
  const context = { window: { addEventListener() {} }, Uint8Array,
    crypto: require('node:crypto').webcrypto,
    performance: { getEntriesByType: () => [{ type }] },
    sessionStorage: {
      getItem: key => { if (broken) throw Error('disabled'); return storage.get(key); },
      setItem: (key, value) => { if (broken) throw Error('disabled'); storage.set(key, value); },
      removeItem: key => storage.delete(key),
    },
  };
  vm.runInNewContext(code, context);
  return context.window.GuestSession;
}
const first = session('navigate');
assert.equal(Object.keys(first.headers()).length, 0);
const key = first.start();
assert.match(key, /^[a-f0-9]{64}$/);
assert.equal(session('reload').headers()['X-Fitness-Guest'], key);
assert.equal(Object.keys(session('navigate').headers()).length, 0);
assert.equal(Object.keys(session('back_forward').headers()).length, 0);
first.clear();
assert.equal(Object.keys(session('reload').headers()).length, 0);
assert.match(session('navigate', true).start(), /^[a-f0-9]{64}$/);
assert.equal(Object.keys(session('reload', true).headers()).length, 0);
console.log('Guest visit storage tests passed');
