const {chromium} = require('playwright');
const assert = require('node:assert/strict'), path = require('node:path');
async function main() {
  const browser = await chromium.launch({headless:true,channel:'chrome'});
  const context = await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'America/Los_Angeles',locale:'zh-CN'});
  const page = await context.newPage(), base='http://127.0.0.1:8767', errors=[], posts=[];
  page.on('pageerror', e=>errors.push(e.message));
  page.on('request', r=>{if(r.method()==='POST')posts.push(r.url());});
  try {
    await page.goto(base);
    assert.equal(await page.evaluate(()=>window.BusinessTime.day(new Date('2026-09-20T16:00:00Z'))),'2026-09-21');
    await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`ready_${Date.now()}`);
    await page.getByLabel('密码',{exact:true}).fill('Synthetic-readiness-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('#content .loading').waitFor({state:'detached'});
    const before=await (await page.request.get(`${base}/api/usage`)).json();
    await page.getByRole('button',{name:'AI使用次数',exact:true}).click();
    await page.locator('.usage-values').waitFor();
    assert.match(await page.locator('#usage-content').textContent(),/本期已用0 次/);
    assert.match(await page.locator('#usage-content').textContent(),/北京时间.*08:00/);
    for (const [width,height] of [[1440,1000],[390,844],[320,740]]) {
      await page.setViewportSize({width,height});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      assert.equal(await page.locator('#usage-dialog').evaluate(el=>el.scrollWidth>el.clientWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r120-usage-${width}.png`)});
    }
    await page.getByRole('button',{name:'刷新额度',exact:true}).click();
    await page.locator('.usage-values').waitFor();
    assert.deepEqual(await(await page.request.get(`${base}/api/usage`)).json(),before);
    await page.route('**/api/usage', async route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'模拟读取失败'})}));
    await page.getByRole('button',{name:'刷新额度',exact:true}).click();
    await page.getByText('模拟读取失败',{exact:true}).waitFor();
    await page.unroute('**/api/usage');
    await page.getByRole('button',{name:'刷新额度',exact:true}).click();
    await page.locator('.usage-values').waitFor();
    await page.getByRole('button',{name:'关闭额度',exact:true}).click();
    await page.route('**/api/usage',async route=>{await new Promise(r=>setTimeout(r,350));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({...before,used:98})}).catch(()=>{});});
    await page.getByRole('button',{name:'AI使用次数',exact:true}).click();
    await page.getByRole('button',{name:'关闭额度',exact:true}).click();
    await page.waitForTimeout(500);
    assert.equal(await page.locator('#usage-content').textContent(),'');
    await page.unroute('**/api/usage');
    await page.getByRole('button',{name:'退出登录',exact:true}).click();
    await page.locator('#auth-screen').waitFor({state:'visible'});
    await page.route('**/api/health',async route=>{const response=await route.fetch();const data=await response.json();await route.fulfill({response,json:{...data,registration_enabled:false}});});
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('[data-auth="register"]').hidden);
    assert.deepEqual(errors,[]);
    assert.equal(posts.some(url=>/understand|recommend|nutrition|transcribe/.test(url)),false);
    console.log(JSON.stringify({passed:true,provider_calls:0,checks:['Beijing date in LA browser','quota readonly','failed read/retry','late response discarded','registration visibility','1440/390/320']}));
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
