// Run only against tests/ui_fixture_server.py --fact-versions (disposable DB/model).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const base = 'http://127.0.0.1:8772';

async function main() {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  const context = await browser.newContext({viewport: {width: 1280, height: 950}, locale: 'zh-CN'});
  const page = await context.newPage(), errors = [], sent = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (request.method() === 'POST') sent.push(request.url());});
  const ready = () => page.waitForFunction(() => document.querySelector('.coach-status')?.textContent.match(/保存在本机|对话已保存/));
  const send = async text => {
    await page.locator('#coach-message').fill(text);
    await page.getByRole('button', {name: '发送消息', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#coach-message')?.value === '');
    await ready();
  };
  const refresh = async () => {await page.locator('.coach-refresh').click(); await ready();};
  const newChat = async () => {await page.getByRole('button', {name: '新对话', exact: true}).click(); await ready();};
  const latest = () => page.locator('.coach-turn').last();
  try {
    assert.equal((await context.request.post(base + '/api/auth/register', {
      data: {username: `chain_${Date.now()}`, password: 'Synthetic-chain-2026'},
    })).status(), 201);
    assert.equal((await context.request.put(base + '/api/profile', {
      data: {equipment: '在家，徒手', minutes_per_session: 40, experience: 'experienced'},
    })).status(), 200);
    await page.goto(base);
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('[data-view="assistant"]').first().click();
    await ready();
    const day = await page.locator('.coach-day').inputValue();
    await send('以后我训练的地方只有哑铃');
    await page.locator('.coach-equipment-scope').waitFor();
    await page.locator('.coach-equipment-scope').selectOption('long_term');
    await page.locator('.coach-review-checked').check();
    const confirmation = page.waitForResponse(r => r.url().endsWith('/confirm-understanding'));
    await page.locator('.coach-confirm-understanding').click();
    assert.equal((await confirmation).status(), 200);
    await page.locator('.coach-equipment-scope').waitFor({state: 'hidden'});
    assert.equal((await (await context.request.get(base + '/api/profile')).json()).equipment, '只有哑铃');
    await newChat();
    await send('今天练什么');
    await latest().getByText('本次建议：推：', {exact: false}).waitFor();
    assert.match(await latest().textContent(), /个人档案.*只有哑铃/);
    assert.deepEqual(await (await context.request.get(base + `/api/workouts?day=${day}`)).json(), []);
    const recordBody = {client_id: crypto.randomUUID(), day, name: '胸背肩手臂', minutes: 30, status: 'completed'};
    const record = await (await context.request.post(base + '/api/workouts', {data: recordBody})).json();
    const legacy = await (await context.request.post(base + '/api/workouts', {
      data: {...recordBody, client_id: crypto.randomUUID(), name: '腿臀', status: 'planned'},
    })).json();
    await refresh();
    await latest().locator('.training-stale').waitFor();
    await page.getByRole('button', {name: '按最新记录重新建议', exact: true}).click();
    await latest().getByText('本次建议：下肢与核心', {exact: false}).waitFor();
    assert.match(await latest().textContent(), /近七天实际记录 · 1项/);
    // Two edits with no intervening read must still invalidate the saved suggestion.
    assert.equal((await context.request.put(base + `/api/workouts/${record.id}`, {
      data: {day, name: '腿臀', minutes: 30, status: 'completed'},
    })).status(), 200);
    assert.equal((await context.request.put(base + `/api/workouts/${record.id}`, {
      data: {day, name: '胸背肩手臂', minutes: 30, status: 'completed'},
    })).status(), 200);
    await refresh();
    await latest().locator('.training-stale').waitFor();
    await page.getByRole('button', {name: '按最新记录重新建议', exact: true}).click();
    await latest().getByText('本次建议：下肢与核心', {exact: false}).waitFor();
    assert.equal((await context.request.put(base + '/api/profile', {
      data: {equipment: '在家，徒手', minutes_per_session: 40, experience: 'experienced'},
    })).status(), 200);
    await refresh();
    await latest().locator('.training-stale').waitFor();
    await page.getByRole('button', {name: '按最新记录重新建议', exact: true}).click();
    await latest().getByText('本次按个人档案的器械：在家，徒手', {exact: false}).waitFor();
    assert.equal(await latest().getByText('哑铃高脚杯深蹲', {exact: false}).count(), 0);
    assert.equal((await context.request.delete(base + `/api/workouts/${record.id}`)).status(), 204);
    await refresh();
    await latest().locator('.training-stale').waitFor();
    await page.getByRole('button', {name: '按最新记录重新建议', exact: true}).click();
    await latest().getByText('本次建议：推：', {exact: false}).waitFor();
    const generations = sent.filter(url => url.endsWith('/recommend-training')).length;
    await page.reload();
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('[data-view="assistant"]').first().click(); await ready();
    await latest().getByText('本次建议：推：', {exact: false}).waitFor();
    assert.equal(sent.filter(url => url.endsWith('/recommend-training')).length, generations);
    const remaining = await (await context.request.get(base + `/api/workouts?day=${day}`)).json();
    assert.equal(remaining.length, 1); assert.equal(remaining[0].id, legacy.id);
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({width, height: 950});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      if (width === 390) await page.screenshot({path: path.resolve(__dirname, '../artifacts/memory-training-chain-390.png'), fullPage: true});
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed: true, real_provider_calls: 0,
      checks: ['explicit long term confirmation', 'new conversation uses profile', 'completed history changes final actions',
        'planned excluded from actual history', 'record ABA stays stale', 'profile correction invalidates and removes dumbbell actions',
        'deletion invalidates and reselects', 'reload does not regenerate', 'no implicit workout writes', '1280/390/320']}));
  } catch (error) {
    console.error(JSON.stringify({synthetic_latest_turn: await latest().textContent().catch(() => '')}));
    await page.screenshot({path: path.resolve(__dirname, '../artifacts/memory-training-chain-failure.png'), fullPage: true});
    throw error;
  } finally {await context.close(); await browser.close();}
}
main().catch(error => {console.error(error); process.exitCode = 1;});
