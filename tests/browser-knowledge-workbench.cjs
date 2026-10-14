const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');

async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const register=async(p,prefix)=>{
    await p.goto('http://127.0.0.1:8767');await p.getByRole('button',{name:'注册',exact:true}).click();
    await p.getByLabel('用户名',{exact:true}).fill(prefix+Date.now());
    await p.getByLabel('密码',{exact:true}).fill('Synthetic-workbench-2026');
    await p.getByRole('button',{name:'注册并开始',exact:true}).click();
    await p.locator('#workspace').waitFor({state:'visible'});await p.locator('[data-view="assistant"]').click();
    await p.waitForFunction(()=>document.querySelector('.workbench-status')?.textContent.includes('已刷新'));
  };
  try{
    await register(page,'feedback_');
    await page.waitForFunction(()=>document.querySelector('.coach-status')?.textContent.includes('对话已保存')&&!document.querySelector('.coach-input')?.disabled);
    await page.locator('.coach-input').fill('力量训练一般一周做几次');await page.getByRole('button',{name:'发送消息',exact:true}).click();
    await page.locator('.coach-knowledge').waitFor();
    await page.locator('.knowledge-workbench > summary').click();
    await page.locator('.feedback-target').selectOption('1');
    const privateText='<img src=x onerror="window.workbenchInjected=true"> 私人测试反馈';
    await page.locator('.feedback-comment').fill(privateText);
    await page.getByRole('button',{name:'保存反馈',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.feedback-list .workbench-item').length===1);
    assert.equal(await page.evaluate(()=>Boolean(window.workbenchInjected)),false);
    assert.equal(await page.locator('.feedback-list img').count(),0);
    await page.getByRole('button',{name:'准备摘录候选',exact:true}).click();
    await page.locator('.candidate-source').selectOption('cdc-adults:strength');
    assert.equal(await page.locator('.candidate-reviewed').isChecked(),false);
    await page.getByRole('button',{name:'保存候选',exact:true}).click();
    assert.equal(await page.locator('.candidate-list .workbench-item').count(),0);
    await page.locator('.candidate-reviewed').check();
    await page.getByRole('button',{name:'保存候选',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.candidate-list')?.textContent.includes('版本 1'));
    assert.doesNotMatch(await page.locator('.candidate-list').textContent(),/私人测试反馈/);
    await page.getByRole('button',{name:'修订',exact:true}).click();
    const excerpt=await page.locator('.candidate-content').inputValue();
    await page.locator('.candidate-content').fill(Array.from(excerpt).slice(0,8).join(''));
    assert.equal(await page.locator('.candidate-reviewed').isChecked(),false);
    await page.locator('.candidate-reviewed').check();await page.getByRole('button',{name:'保存候选',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.candidate-list')?.textContent.includes('版本 2'));
    await page.locator('.candidate-note').fill('搁置测试 <script>window.workbenchInjected=true</script>');
    await page.getByRole('button',{name:'搁置这版候选',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.candidate-list')?.textContent.includes('已搁置'));
    assert.equal(await page.locator('.candidate-list script').count(),0);
    const other=await browser.newPage({locale:'zh-CN'});
    await register(other,'feedback_other_');await other.locator('.knowledge-workbench > summary').click();
    assert.equal(await other.locator('.feedback-list .workbench-item').count(),0);
    assert.equal(await other.locator('.candidate-list .workbench-item').count(),0);await other.close();
    await page.reload();await page.locator('[data-view="assistant"]').click();
    await page.waitForFunction(()=>document.querySelector('.candidate-list')?.textContent.includes('版本 3'));
    await page.locator('.knowledge-workbench > summary').click();
    for(const [width,height] of [[1440,1000],[390,844],[320,740]]){
      await page.setViewportSize({width,height});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/knowledge-workbench-${width}.png`),fullPage:true});
    }
    page.once('dialog',dialog=>dialog.accept());
    await page.getByRole('button',{name:'删除反馈及关联候选',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.feedback-list .workbench-item')&&!document.querySelector('.candidate-list .workbench-item'));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['feedback origin','explicit quote review','private/public separation','revision and rejection','XSS escaping','account isolation','reload persistence','cascade deletion','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/knowledge-workbench-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
