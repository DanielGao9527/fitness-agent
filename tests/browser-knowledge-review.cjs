const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

async function main(){
  const fixture=path.resolve(__dirname,'../artifacts/ui-coach-review-test.sqlite3');
  assert.ok(fs.existsSync(fixture),'Start tests/ui_fixture_server.py --coach on 8767 first');
  const python=process.env.FITNESS_TEST_PYTHON||path.resolve(__dirname,'../../../../.venv/Scripts/python.exe');
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const ownerContext=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
  const reviewerContext=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
  const owner=await ownerContext.newPage(),reviewer=await reviewerContext.newPage(),errors=[];
  [owner,reviewer].forEach(page=>page.on('pageerror',error=>errors.push(error.message)));
  const register=async(page,prefix)=>{
    const username=prefix+Date.now();
    await page.goto('http://127.0.0.1:8767');await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(username);await page.getByLabel('密码',{exact:true}).fill('Synthetic-review-only-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('[data-view="assistant"]').click();
    await page.waitForFunction(()=>document.querySelector('.workbench-status')?.textContent.includes('已刷新'));
    await page.waitForFunction(()=>document.querySelector('.review-permission')?.textContent.includes('当前账号'));
    await page.locator('.knowledge-workbench > summary').click();
    await page.locator('.knowledge-review > summary').click();return username;
  };
  try{
    await register(owner,'review_owner_');
    await owner.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.includes('对话已保存')&&!document.querySelector('.coach-input')?.disabled);
    await owner.locator('.coach-input').fill('力量训练一般一周做几次');await owner.getByRole('button',{name:'发送消息',exact:true}).click();
    await owner.locator('.coach-knowledge').waitFor();await owner.locator('.feedback-target').selectOption('1');
    await owner.locator('.feedback-comment').fill('private-review-marker <script>window.reviewInjected=true</script>');
    await owner.getByRole('button',{name:'保存反馈',exact:true}).click();await owner.getByRole('button',{name:'准备摘录候选',exact:true}).click();
    await owner.locator('.candidate-source').selectOption('cdc-adults:strength');await owner.locator('.candidate-reviewed').check();
    await owner.getByRole('button',{name:'保存候选',exact:true}).click();
    const submit=owner.getByRole('button',{name:'提交公共摘录供预审',exact:true});await submit.waitFor();
    await owner.waitForFunction(()=>!document.querySelector('.review-candidates button')?.disabled);
    await submit.click();assert.equal(await owner.locator('.review-own .workbench-item').count(),0);
    await owner.locator('[data-review-share]').check();await submit.click();
    await owner.waitForFunction(()=>document.querySelector('.review-own')?.textContent.includes('待检查'));
    const id=await owner.locator('.review-own [data-submission]').getAttribute('data-submission');
    const username=await register(reviewer,'review_staff_');
    assert.match(await reviewer.locator('.review-permission').textContent(),/没有公共审核权限/);
    execFileSync(python,[path.resolve(__dirname,'../scripts/knowledge_reviewer.py'),'grant','--database',fixture,'--username',username],{stdio:'pipe'});
    await reviewer.getByRole('button',{name:'刷新预审状态',exact:true}).click();
    await reviewer.waitForFunction(()=>document.querySelector('.review-permission')?.textContent.includes('具有公共摘录审核权限'));
    const card=reviewer.locator(`.review-queue [data-submission="${id}"]`);
    assert.doesNotMatch(await reviewer.locator('.review-queue').textContent(),/private-review-marker/);
    assert.equal(await reviewer.locator('.feedback-list .workbench-item').count(),0);
    assert.equal(await card.locator('[data-review-approve]').count(),0);
    await card.getByRole('button',{name:'运行离线检查',exact:true}).click();
    await card.getByRole('button',{name:'通过人工预审',exact:true}).waitFor();
    await reviewer.waitForFunction(()=>!document.querySelector('[data-review-approve]')?.disabled);
    await card.getByRole('button',{name:'通过人工预审',exact:true}).click();
    assert.match(await reviewer.locator('.review-status').textContent(),/请先人工核对/);
    await card.locator('[data-review-confirm]').check();await card.getByRole('button',{name:'通过人工预审',exact:true}).click();
    await card.getByText('人工预审通过，未发布',{exact:false}).first().waitFor();
    await owner.getByRole('button',{name:'刷新预审状态',exact:true}).click();
    await owner.waitForFunction(()=>document.querySelector('.review-own')?.textContent.includes('人工预审通过，未发布'));
    await owner.getByRole('button',{name:'修订',exact:true}).click();
    const content=await owner.locator('.candidate-content').inputValue();await owner.locator('.candidate-content').fill(Array.from(content).slice(0,12).join(''));
    await owner.locator('.candidate-reviewed').check();await owner.getByRole('button',{name:'保存候选',exact:true}).click();
    await owner.waitForFunction(()=>document.querySelector('.review-own')?.textContent.includes('已失效'));
    await reviewer.getByRole('button',{name:'刷新预审状态',exact:true}).click();await card.getByText('已失效，需重新提交',{exact:false}).first().waitFor();
    assert.equal(await card.locator('[data-review-approve]').count(),0);
    for(const [width,height] of [[1440,1000],[390,844],[320,740]]){
      await owner.setViewportSize({width,height});assert.equal(await owner.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await owner.screenshot({path:path.resolve(__dirname,`../artifacts/knowledge-review-${width}.png`),fullPage:true});
    }
    await owner.locator('[data-review-share]').check();await submit.click();
    await owner.waitForFunction(()=>document.querySelectorAll('.review-own [data-submission]').length===2);
    await owner.getByRole('button',{name:'撤回提交',exact:true}).click();
    await owner.waitForFunction(()=>document.querySelector('.review-own')?.textContent.includes('已撤回'));
    await owner.locator('[data-review-share]').check();await submit.click();
    await owner.waitForFunction(()=>document.querySelectorAll('.review-own [data-submission]').length===3);
    await owner.getByRole('button',{name:'撤回提交',exact:true}).click();
    await owner.waitForFunction(()=>!document.querySelector('[data-review-withdraw]'));
    execFileSync(python,[path.resolve(__dirname,'../scripts/knowledge_reviewer.py'),'revoke','--database',fixture,'--username',username],{stdio:'pipe'});
    await reviewer.getByRole('button',{name:'刷新预审状态',exact:true}).click();
    await reviewer.waitForFunction(()=>document.querySelector('.review-permission')?.textContent.includes('没有公共审核权限'));
    assert.equal(await reviewer.locator('.review-queue-section').isVisible(),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['explicit public sharing','private field isolation','server reviewer role','local check','manual pre-review','edit invalidation','withdraw','permission revocation','1440/390/320']}));
  }catch(error){await owner.screenshot({path:path.resolve(__dirname,'../artifacts/knowledge-review-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
