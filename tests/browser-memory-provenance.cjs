// Isolated --fact-versions fixture: explicit source review, lifecycle, and layout.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {randomUUID}=require('node:crypto');
const base='http://127.0.0.1:8772';

async function main() {
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  try {
    assert.equal((await context.request.post(base+'/api/auth/register',{data:{username:`provenance_${Date.now()}`,password:'Synthetic-memory-2026'}})).status(),201);
    const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    const conversation=await (await context.request.post(base+'/api/coach/conversations',{data:{client_id:randomUUID(),day}})).json();
    const quote='我把术语A写成 <img src=x onerror="window.__memoryInjected=1">，\n只是在举例。';
    const sent=await context.request.post(base+`/api/coach/conversations/${conversation.id}/messages`,{data:{client_id:randomUUID(),version:0,message:quote}});
    assert.equal(sent.status(),200);
    const sourceData=await sent.json();
    const profile=await (await context.request.get(base+'/api/profile')).json();
    const open=async()=>{
      await page.goto(base);await page.locator('#workspace').waitFor({state:'visible'});
      await page.locator('[data-view="assistant"]').first().click();
      await page.locator('.coach-memory summary').click();
      await page.locator('.coach-memory-source').waitFor();
    };
    await open();
    await page.locator('.coach-memory-kind').selectOption('assistant_note');
    await page.locator('.coach-memory-source').selectOption('1');
    assert.equal(await page.locator('.coach-memory-content').inputValue(),quote);
    for(const width of [390,320]){
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    }
    await page.setViewportSize({width:1280,height:900});
    assert.equal(await page.locator('.coach-memory-form').evaluate(form=>form.checkValidity()),false);
    await page.getByRole('button',{name:'保存记忆',exact:true}).click();
    assert.deepEqual((await (await context.request.get(base+'/api/coach/conversations/memory/entries')).json()).items,[]);
    await page.locator('.coach-memory-reviewed').check();
    let responsePromise=page.waitForResponse(response=>response.url().endsWith('/memory/entries/from-turn') && response.request().method()==='POST');
    await page.getByRole('button',{name:'保存记忆',exact:true}).click();
    let response=await responsePromise;assert.equal(response.status(),201);
    assert.equal(response.request().postDataJSON().reviewed,true);
    const note=await response.json();
    await page.locator('.coach-memory-list').getByText(quote,{exact:false}).waitFor();
    assert.equal(await page.locator('.coach-memory-list img').count(),0);
    assert.equal(await page.evaluate(()=>window.__memoryInjected || 0),0);
    const row=page.locator(`.coach-memory-list li[data-id="${note.id}"]`);
    await row.getByRole('button',{name:'修改',exact:true}).click();
    assert.equal(await page.locator('.coach-memory-source').isDisabled(),true);
    await page.locator('.coach-memory-content').fill('术语A是测试简称');
    responsePromise=page.waitForResponse(response=>response.url().endsWith(`/memory/entries/${note.id}`) && response.request().method()==='PUT');
    await page.getByRole('button',{name:'保存修改',exact:true}).click();
    response=await responsePromise;assert.equal(response.status(),200);
    assert.equal((await response.json()).source_mode,'manual');
    await page.locator('.coach-memory-list').getByText('术语A是测试简称',{exact:false}).waitFor();
    await page.locator('.coach-memory-kind').selectOption('assistant_note');
    await page.locator('.coach-memory-source').selectOption('1');
    await page.locator('.coach-memory-reviewed').check();
    responsePromise=page.waitForResponse(response=>response.url().endsWith('/memory/entries/from-turn') && response.request().method()==='POST');
    await page.getByRole('button',{name:'保存记忆',exact:true}).click();
    response=await responsePromise;assert.equal(response.status(),201);
    assert.equal((await context.request.delete(base+`/api/coach/conversations/${conversation.id}`,{data:{version:sourceData.version}})).status(),204);
    await open();
    await page.locator('.coach-memory-list').getByText('来源已变化或删除，暂不使用',{exact:false}).waitFor();
    const memories=await (await context.request.get(base+'/api/coach/conversations/memory/entries')).json();
    assert.equal(memories.items.filter(item=>item.stale).length,1);
    assert.equal(memories.items.filter(item=>item.source_mode==='manual').length,1);
    assert.deepEqual(await (await context.request.get(base+'/api/profile')).json(),profile);
    for(const width of [390,320]){
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    }
    const stale=memories.items.find(item=>item.stale);
    page.once('dialog',dialog=>dialog.accept());
    responsePromise=page.waitForResponse(response=>response.url().endsWith(`/memory/entries/${stale.id}`) && response.request().method()==='DELETE');
    await page.locator(`.coach-memory-list li[data-id="${stale.id}"]`).getByRole('button',{name:'删除',exact:true}).click();
    assert.equal((await responsePromise).status(),204);
    assert.equal((await (await context.request.get(base+'/api/coach/conversations/memory/entries')).json()).items.length,1);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['source selection','explicit review','escaped source','edit detaches source','source delete remains stale after reload','stale note explicit delete','profile unchanged','390/320 review and list layout']}));
  } finally {await context.close();await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
