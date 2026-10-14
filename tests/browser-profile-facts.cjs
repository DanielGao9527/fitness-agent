// Only the disposable --fact-versions fixture on 8772; synthetic data, no cloud calls.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:8772';

async function main() {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  const first = await context.newPage(), second = await context.newPage();
  const errors = [];
  for (const page of [first, second]) page.on('pageerror', error => errors.push(error.message));
  const profile = async page => {
    await page.goto(base);
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('[data-view="profile"]').first().click();
    await page.locator('#profile-form').waitFor();
  };
  const save = async (page, equipment, status) => {
    await page.locator('[name="equipment"]').fill(equipment);
    const response = page.waitForResponse(r => r.url() === base + '/api/profile' && r.request().method() === 'PUT');
    await page.getByRole('button', {name: '保存档案', exact: true}).click();
    const result = await response;
    assert.equal(result.status(), status);
    assert.ok(result.request().headers()['x-equipment-revision']);
    return result;
  };
  try {
    const registered = await context.request.post(base + '/api/auth/register', {
      data: {username: `facts_${Date.now()}`, password: 'Synthetic-fact-2026'},
    });
    assert.equal(registered.status(), 201);
    await profile(first);
    await save(first, '在家，只有哑铃', 200);
    await first.waitForFunction(() => document.querySelector('#profile-form').dataset.dirty === 'false');
    await profile(second);
    const staleRevision = await second.locator('#profile-form').getAttribute('data-equipment-revision');
    await save(first, '在家，徒手', 200);
    await first.waitForFunction(() => document.querySelector('#profile-form').dataset.dirty === 'false');
    await save(first, '在家，只有哑铃', 200);
    await save(second, '泳池', 409);
    await second.locator('#profile-form .form-error').getByText('器械档案已在其他页面或对话中更正', {exact: false}).waitFor();
    assert.equal(await second.locator('#profile-form').getAttribute('data-equipment-revision'), staleRevision);
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '在家，只有哑铃');
    await profile(second);
    await save(second, '在家，徒手', 200);
    await second.waitForFunction(() => document.querySelector('#profile-form').dataset.dirty === 'false');
    // Also check the explicit conversation-to-profile confirmation wording.
    await second.locator('[data-view="assistant"]').first().click();
    await second.locator('#coach-message').fill('在家，只有哑铃');
    await second.getByRole('button', {name: '发送消息', exact: true}).click();
    await second.locator('.coach-equipment-scope').waitFor();
    assert.equal(await second.locator('.coach-equipment-scope').inputValue(), '');
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '在家，徒手');
    await second.locator('.coach-review-checked').check();
    await second.locator('.coach-confirm-understanding').click();
    await second.locator('.coach-error').getByText('请选择器械的作用范围', {exact: true}).waitFor();
    await second.locator('.coach-equipment-scope').selectOption('long_term');
    await second.locator('.coach-confirm-understanding').click();
    await second.waitForFunction(async () => (await (await fetch('/api/profile')).json()).equipment === '在家，只有哑铃');
    const metadata = await (await context.request.get(base + '/api/profile/facts')).json();
    assert.equal(metadata.items[0].source, 'conversation_review');
    for (const width of [390, 320]) {
      await second.setViewportSize({width, height: 900});
      assert.equal(await second.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed: true, real_provider_calls: 0,
      checks: ['two-tab ABA conflict', 'fresh save', 'explicit equipment confirmation', 'provenance', '390/320 layout']}));
  } finally { await browser.close(); }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
