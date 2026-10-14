// Disposable --fact-versions fixture only. Real UI/API, synthetic model + database.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:8772';

async function main() {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    assert.equal((await context.request.post(base + '/api/auth/register', {
      data: {username: `scopes_${Date.now()}`, password: 'Synthetic-scope-2026'},
    })).status(), 201);
    assert.equal((await context.request.put(base + '/api/profile', {
      data: {equipment: '在家，徒手', minutes_per_session: 40, experience: 'experienced'},
    })).status(), 200);
    await page.goto(base);
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('[data-view="assistant"]').first().click();
    const send = async text => {
      await page.locator('#coach-message').fill(text);
      await page.getByRole('button', {name: '发送消息', exact: true}).click();
      await page.locator('.coach-equipment-scope').waitFor();
    };
    const confirm = async scope => {
      await page.locator('.coach-equipment-scope').selectOption(scope);
      await page.locator('.coach-review-checked').check();
      const pending = page.waitForResponse(r => r.url().endsWith('/confirm-understanding'));
      await page.locator('.coach-confirm-understanding').click();
      const response = await pending;
      assert.equal(response.status(), 200);
      assert.equal(response.request().postDataJSON().equipment_scope, scope);
      await page.locator('.coach-equipment-scope').waitFor({state: 'hidden'});
      return response.json();
    };
    await page.locator('#coach-message').fill('训练的地方可能只有哑铃，还没确认');
    await page.getByRole('button', {name: '发送消息', exact: true}).click();
    await page.locator('.nutrition-question').getByText('这些器械是否实际可用还不确定。', {exact: false}).waitFor();
    assert.equal(await page.locator('.coach-confirm-understanding').count(), 0);
    assert.equal(await page.locator('.coach-equipment-scope').count(), 0);
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '在家，徒手');
    await page.reload();
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('[data-view="assistant"]').first().click();
    await page.locator('.nutrition-question').getByText('这些器械是否实际可用还不确定。', {exact: false}).waitFor();
    await send('我训练的地方只有哑铃');
    assert.equal(await page.locator('.coach-equipment-scope').inputValue(), '');
    await page.locator('.coach-review-checked').check();
    await page.locator('.coach-confirm-understanding').click();
    await page.locator('.coach-error').getByText('请选择器械的作用范围', {exact: true}).waitFor();
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '在家，徒手');
    for (const width of [390, 320]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    }
    await page.setViewportSize({width: 1280, height: 900});
    await confirm('long_term');
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '只有哑铃');
    await send('今天我训练的地方只能徒手');
    assert.equal(await page.locator('.coach-equipment-scope').inputValue(), 'day');
    const daily = await confirm('day');
    assert.equal(daily.training_context.equipment_source, 'day');
    assert.equal(daily.training_context.equipment, '只能徒手');
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '只有哑铃');
    await page.locator('.training-recommendation').last().getByText('今天已确认条件', {exact: false}).waitFor();
    await send('这次我训练的地方只有哑铃');
    assert.equal(await page.locator('.coach-equipment-scope').inputValue(), 'session');
    const local = await confirm('session');
    assert.equal(local.training_context.equipment_source, 'current_conversation');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed: true, real_provider_calls: 0,
      checks: ['uncertain input asks a question without confirm button', 'question survives refresh',
        'followup continues the same conversation', 'unknown scope blocks save', 'explicit scope confirmation', 'long-term profile write',
        'day override preserves profile', 'final advice shows day source', 'session override', '390/320 layout']}));
  } finally { await browser.close(); }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
