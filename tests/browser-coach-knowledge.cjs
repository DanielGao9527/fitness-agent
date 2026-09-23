// Main UI on the isolated provider-double server only.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');

async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'});
  const base='http://127.0.0.1:8767',posts=[],errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
  const ready=()=>page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.includes('对话已保存')&&!document.querySelector('.coach-input')?.disabled);
  const send=async message=>{
    await page.locator('.coach-input').fill(message);
    await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.coach-input')?.value==='');await ready();
  };
  const latest=()=>page.locator('.coach-turn').last();
  try{
    await page.goto(base);await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(`knowledge_${Date.now()}`);
    await page.getByLabel('密码',{exact:true}).fill('Synthetic-knowledge-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('[data-view="assistant"]').click();await ready();
    assert.equal(await page.locator('.coach-consent input').count(),0);
    for(const [message,source] of [['力量训练一般一周做几次','CDC'],['哪些食物含蛋白质','Public Health England']]){
      await send(message);
      await latest().locator('.coach-knowledge').waitFor();
      assert.match(await latest().textContent(),new RegExp(source));
      assert.doesNotMatch(await latest().textContent(),/请明确本次可用场地|补充已保存在/);
      assert.equal(await latest().locator('.training-recommendation').count(),0);
    }
    assert.equal(posts.filter(url=>/recommend-|understand/.test(url)).length,0);
    assert.equal(posts.filter(url=>url.endsWith('/answer-knowledge')).length,2);
    const calls=posts.length;
    await page.locator('.coach-refresh').click();await ready();assert.equal(posts.length,calls);
    await latest().locator('summary').click();
    assert.match(await latest().locator('a').first().getAttribute('href'),/^https:\/\//);
    for(const [width,height] of [[1440,1000],[390,844],[320,740]]){
      await page.setViewportSize({width,height});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/coach-knowledge-${width}.png`),fullPage:true});
    }
    await page.setViewportSize({width:1440,height:1000});
    await send('今天练什么');
    assert.equal(posts.filter(url=>url.endsWith('/recommend-training')).length,1);
    const training=posts.filter(url=>url.endsWith('/recommend-training')).length;
    await send('力量训练通常一星期练几天');
    assert.equal(posts.filter(url=>url.endsWith('/recommend-training')).length,training);
    assert.equal(await latest().locator('.coach-recommend-training').count(),0);
    await send('安排晚餐');
    await page.locator('.coach-consent .quick-consent').waitFor();
    assert.equal(await page.locator('.coach-input').isEnabled(),true);
    assert.equal(posts.filter(url=>url.endsWith('/recommend-meals')).length,0);
    await send('蛋白质主要来自哪些食物');
    assert.equal(await page.locator('.coach-consent input').count(),0);
    assert.equal(posts.filter(url=>url.endsWith('/recommend-meals')).length,0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['main chat answers and sources','no meal consent for knowledge','no repeated plans','reload without calls','meal consent still required','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/coach-knowledge-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
