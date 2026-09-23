// Isolated synthetic browser verification. No production records or real model calls.
const {chromium} = require('playwright');
const assert = require('node:assert/strict'), path = require('node:path');

async function main() {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'zh-CN', reducedMotion: 'reduce'});
  const page = await context.newPage(), errors = [], base = 'http://127.0.0.1:8767', day = '2026-09-15';
  page.on('pageerror', error => errors.push(error.message));
  let acceptDialog = true;
  page.on('dialog', dialog => acceptDialog ? dialog.accept() : dialog.dismiss());
  const ready = () => page.waitForFunction(() => document.querySelector('.body-period') && !document.querySelector('.body-status').textContent);
  const open = async () => {await page.locator('.nav-item[data-view="body"]').click(); await ready();};
  const records = async () => (await (await page.request.get(`${base}/api/body-measurements?day=${day}&days=90`)).json()).records;
  const save = async () => {await page.getByRole('button', {name: '保存体测', exact: true}).click(); await page.locator('.body-form').waitFor({state: 'detached'}); await ready();};
  try {
    await page.goto(base);
    await page.getByRole('button', {name: '注册', exact: true}).click();
    await page.getByLabel('用户名', {exact: true}).fill(`body_${Date.now()}`);
    await page.getByLabel('密码', {exact: true}).fill('Synthetic-body-2026');
    await page.getByRole('button', {name: '注册并开始', exact: true}).click();
    await page.locator('#workspace').waitFor({state: 'visible'});
    await page.locator('#day').fill(day); await page.locator('#day').dispatchEvent('change');
    await page.locator('.metrics').waitFor();
    await open();
    assert.match(await page.locator('.body-results').textContent(), /尚未填写身体数据/);
    assert.equal((await records()).length, 0);
    await page.getByRole('button', {name: '记录体测', exact: true}).click();
    await page.getByRole('button', {name: '保存体测', exact: true}).click();
    assert.match(await page.locator('.body-input-error').textContent(), /至少填写一项/);
    await page.getByLabel('体重（kg）', {exact: true}).fill('72.5');
    await page.getByLabel('体脂率（%）', {exact: true}).fill('23.4');
    await page.getByLabel('测量备注（可选）', {exact: true}).fill('合成样本 <img src=x onerror=alert(1)>');
    acceptDialog = false;
    await page.locator('.nav-item[data-view="meals"]').click();
    assert.equal(await page.getByLabel('体重（kg）', {exact: true}).inputValue(), '72.5');
    acceptDialog = true;
    // A failed save keeps values. A lost successful response retries the same ID.
    await page.route('**/api/body-measurements', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: '模拟保存失败'})}));
    await page.getByRole('button', {name: '保存体测', exact: true}).click();
    await page.locator('.body-error').filter({hasText: '模拟保存失败'}).waitFor();
    assert.equal(await page.getByLabel('体重（kg）', {exact: true}).inputValue(), '72.5');
    await page.unrouteAll({behavior: 'wait'});
    await page.route('**/api/body-measurements', async route => {await route.fetch(); await route.abort('failed');});
    await page.getByRole('button', {name: '保存体测', exact: true}).click();
    await page.locator('.body-error').filter({hasText: /fetch/i}).waitFor();
    await page.unrouteAll({behavior: 'wait'}); await save();
    assert.equal((await records()).length, 1);
    assert.equal(await page.locator('.body-results img').count(), 0);
    assert.match(await page.locator('.body-metrics').textContent(), /至少两次测量/);
    // Seed extra dated measurements; current profile is deliberately not imported.
    for (const [recordDay, weight, fat] of [['2026-09-14',72.8,null], ['2026-09-12',null,24], ['2026-08-01',75,25]]) {
      assert.equal((await page.request.post(`${base}/api/body-measurements`, {data: {client_id: crypto.randomUUID(), day: recordDay, weight_kg: weight, body_fat_percent: fat}})).status(),201);
    }
    assert.equal((await page.request.put(`${base}/api/profile`, {data: {weight_kg: 90}})).status(), 200);
    await page.getByRole('button', {name: '刷新体测回顾', exact: true}).click(); await ready();
    assert.equal(await page.locator('.body-table tbody tr').count(), 3);
    assert.match(await page.locator('.body-metrics').textContent(), /-0.3 kg/);
    assert.match(await page.locator('.body-metrics').textContent(), /-0.6 个百分点/);
    for (const [width,height] of [[1440,1000], [390,844], [320,740]]) {
      await page.setViewportSize({width,height});
      await page.waitForFunction(() => {
        const canvas=document.querySelector('.body-chart canvas');
        return Math.abs(canvas.width/devicePixelRatio-canvas.getBoundingClientRect().width)<2;
      });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1), false);
      assert.deepEqual(await page.locator('.body-toolbar button,.nav-item').evaluateAll(nodes => nodes.filter(n=>n.scrollWidth > n.clientWidth+2).map(n=>n.textContent)), []);
      assert.ok(await page.locator('canvas').evaluate(canvas => {
        const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
        let colored=0;for(let i=0;i<data.length;i+=4)if(data[i+3]>100 && data[i+1]>data[i]+20)colored++;
        return colored>30;
      }));
      await page.screenshot({path: path.resolve(__dirname, `../artifacts/r115-body-${width}.png`),fullPage:true});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole('button', {name:'体脂率',exact:true}).click();
    assert.match(await page.locator('canvas').getAttribute('aria-label'), /体脂率/);
    await page.getByRole('button', {name:'近90天',exact:true}).click(); await ready();
    assert.equal(await page.locator('.body-table tbody tr').count(),4);
    await page.getByRole('button', {name:`编辑${day}体测`,exact:true}).click();
    await page.getByLabel('体重（kg）', {exact:true}).fill('72'); await save();
    assert.equal((await records()).find(row=>row.day===day).weight_kg,72);
    assert.equal((await(await page.request.get(`${base}/api/profile`)).json()).weight_kg,90);
    await page.getByRole('button', {name:`删除${day}体测`,exact:true}).click(); await ready();
    await page.waitForFunction(day => ![...document.querySelectorAll('[data-body-delete]')].some(el=>el.getAttribute('aria-label')===`删除${day}体测`),day);
    assert.equal((await records()).length,3);
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('#day').fill(day);await page.locator('#day').dispatchEvent('change');await page.locator('.metrics').waitFor();await open();
    assert.equal(await page.locator('.body-table tbody tr').count(),2);
    // A delayed read cannot paint onto another screen/date/account.
    let release, started;
    const gate=new Promise(resolve=>{release=resolve;}), entered=new Promise(resolve=>{started=resolve;});
    await page.route('**/api/body-measurements?**',async route=>{const response=await route.fetch();started();await gate;await route.fulfill({response}).catch(()=>{});});
    await page.getByRole('button',{name:'刷新体测回顾',exact:true}).click();await entered;
    await page.locator('.nav-item[data-view="meals"]').click();release();await page.unrouteAll({behavior:'wait'});
    await page.locator('.meal-week').waitFor();assert.equal(await page.locator('.body-results').count(),0);
    await page.getByRole('button',{name:'退出登录',exact:true}).click();
    await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`body_other_${Date.now()}`);
    await page.getByLabel('密码',{exact:true}).fill('Synthetic-body-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});await open();
    assert.equal(await page.locator('.body-table').count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['empty','partial validation','create/edit/delete','lost response retry','unsaved guard','30/90 days','profile unchanged','gap/units','canvas pixels','1440/390/320','persistence','late response','account isolation']}));
  } finally {await page.unrouteAll({behavior:'ignoreErrors'});await context.close();await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
