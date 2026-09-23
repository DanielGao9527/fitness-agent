const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const base = 'http://127.0.0.1:8767';

async function main() {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' });
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto(base);
    await page.locator('[data-action="guest-start"]').waitFor({ state: 'visible' });
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.resolve(__dirname, `../artifacts/guest-login-${width}.png`), fullPage: true });
    }
    await page.getByRole('button', { name: '游客体验', exact: true }).click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    const id = await page.evaluate(() => state.user.id);
    assert.ok(id < 0);
    await page.locator('.nav-item[data-view="profile"]').click();
    await page.locator('#profile-form [name="display_name"]').fill('临时体验');
    await page.getByRole('button', { name: '保存档案', exact: true }).click();
    await page.waitForFunction(() => state.profile.display_name === '临时体验');
    const day = await page.locator('#day').inputValue();
    await page.evaluate(async day => {
      await api('/meals', { method: 'POST', body: JSON.stringify({ client_id: crypto.randomUUID(), day,
        meal_type: 'breakfast', name: '测试鸡蛋', grams: 100 }) });
    }, day);
    await page.reload();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.waitForFunction(() => state.summary !== null && state.profile.display_name === '临时体验');
    assert.equal(await page.evaluate(() => state.user.id), id);
    assert.equal(await page.evaluate(() => state.profile.display_name), '临时体验');
    assert.equal(await page.evaluate(() => state.meals.length), 1);
    await page.getByRole('button', { name: 'AI使用次数', exact: true }).click();
    await page.getByText('游客额度由同一网络共享，重新进入体验不会重置。').waitFor();
    await page.locator('[data-usage="close"]').click();
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/guest-workspace-320.png'), fullPage: true });
    // A new visit has no tab proof, even though the browser still holds the cookie.
    const newPage = await context.newPage();
    await newPage.goto(base);
    await newPage.locator('[data-action="guest-start"]').waitFor({ state: 'visible' });
    assert.equal(await newPage.locator('#workspace').isVisible(), false);
    await newPage.close();
    await page.getByRole('button', { name: '退出体验', exact: true }).click();
    await page.locator('#auth-screen').waitFor({ state: 'visible' });
    await page.getByRole('button', { name: '游客体验', exact: true }).click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.waitForFunction(() => state.summary !== null);
    assert.notEqual(await page.evaluate(() => state.user.id), id);
    assert.equal(await page.evaluate(() => state.meals.length), 0);
    await page.getByRole('button', { name: '退出体验', exact: true }).click();
    await page.locator('[data-auth="register"]').click();
    const username = `guest_signup_${Date.now()}`;
    await page.locator('#auth-form [name="username"]').fill(username);
    await page.locator('#auth-form [name="password"]').fill('Synthetic-Only-2026');
    await page.getByRole('button', { name: '注册并开始', exact: true }).click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#guest-banner').isVisible(), false);
    await page.reload();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    assert.equal(await page.evaluate(() => state.user.username), username);
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.locator('[data-auth="login"]').click();
    await page.locator('#auth-form [name="username"]').fill(username);
    await page.locator('#auth-form [name="password"]').fill('Synthetic-Only-2026');
    await page.getByRole('button', { name: '登录', exact: true }).last().click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, real_provider_calls: 0,
      checks: ['login/register/guest', 'temporary profile and food', 'refresh', 'fresh visit', 'logout cleanup',
        'new guest empty', 'quota notice', 'regular persistence', '1440/390/320'] }));
  } finally {
    await context.close();
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
