const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

async function main(){
  const fixture=path.resolve(__dirname,'../artifacts/ui-coach-review-test.sqlite3');
  assert.ok(fs.existsSync(fixture),'Start ui_fixture_server.py --coach --knowledge-build on 8767');
  const python=process.env.FITNESS_TEST_PYTHON||path.resolve(__dirname,'../../../../.venv/Scripts/python.exe');
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const username='build_fixture_'+Date.now();
  try{
    await page.goto('http://127.0.0.1:8767');await page.getByRole('button',{name:'注册',exact:true}).click();
    await page.getByLabel('用户名',{exact:true}).fill(username);await page.getByLabel('密码',{exact:true}).fill('Synthetic-build-fixture-2026');
    await page.getByRole('button',{name:'注册并开始',exact:true}).click();await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('[data-view="assistant"]').click();await page.waitForFunction(()=>document.querySelector('.review-permission')?.textContent.includes('当前账号'));
    execFileSync(python,[path.resolve(__dirname,'../scripts/knowledge_reviewer.py'),'grant','--database',fixture,'--username',username],{stdio:'pipe'});
    // Existing feedback/pre-review contracts are seeded through authenticated APIs.
    const seeded=await page.evaluate(async()=>{
      const post=(url,body)=>api(url,{method:'POST',body:JSON.stringify(body)});
      let data=await post('/coach/conversations',{client_id:crypto.randomUUID(),day:state.day});
      data=await post('/coach/conversations/'+data.id+'/messages',{version:data.version,client_id:crypto.randomUUID(),message:'力量训练一般一周做几次'});
      data=await post('/coach/conversations/'+data.id+'/answer-knowledge',{version:data.version,client_id:crypto.randomUUID()});
      const turn=data.turns.at(-1),work='/knowledge-workbench';
      const feedback=await post(work+'/feedback',{client_id:crypto.randomUUID(),conversation_id:data.id,turn_version:turn.version,answer_hash:turn.feedback_target.answer_hash,kind:'knowledge_issue',comment:'private-build-marker'});
      const source=(await api(work+'/sources')).items.find(item=>item.source.chunk_id==='cdc-adults:strength');
      const candidate=await post(work+'/candidates',{client_id:crypto.randomUUID(),feedback_id:feedback.id,content:source.source.excerpt,source_chunk_id:source.source.chunk_id,source_hash:source.source_hash,reviewed:true});
      const submitted=await post(work+'/candidates/'+candidate.id+'/submit',{client_id:crypto.randomUUID(),version:candidate.version,share_public_excerpt:true});
      const checked=await post(work+'/review-queue/'+submitted.id+'/check',{version:submitted.version});
      const approved=await post(work+'/review-queue/'+submitted.id+'/approve',{version:checked.version,gate_id:checked.gate.id,binding_hash:checked.gate.binding_hash,reviewed:true});
      return {candidate,source,approved};
    });
    await page.locator('.knowledge-workbench > summary').click();await page.locator('.knowledge-review > summary').click();
    await page.getByRole('button',{name:'刷新工作台',exact:true}).click();
    await page.locator(`[data-build-select="${seeded.approved.id}"]`).waitFor();
    const build=page.getByRole('button',{name:'构建候选知识版本',exact:true});
    await page.waitForFunction(()=>!document.querySelector('.candidate-build-button')?.disabled);
    await build.click();assert.match(await page.locator('.review-status').textContent(),/请先选择/);
    await page.locator(`[data-build-select="${seeded.approved.id}"]`).check();await build.click();
    assert.match(await page.locator('.review-status').textContent(),/勾选构建确认/);
    await page.locator('.candidate-build-confirmed').check();await build.click();
    await page.waitForFunction(()=>document.querySelector('.review-status')?.textContent.includes('候选知识版本已构建'));
    const built=await page.evaluate(async()=> (await api('/knowledge-workbench/builds')).items.filter(item=>!item.currently_stale).at(-1));
    assert.ok(built&&built.status==='built_not_published');assert.equal(built.publication_allowed,false);
    assert.equal(built.new_chunks,0);assert.equal(built.deduplicated,1);
    assert.doesNotMatch(await page.locator('.knowledge-build-list').textContent(),/private-build-marker/);
    const directory=path.resolve(__dirname,'../artifacts/knowledge-builds',built.id),manifest=fs.readFileSync(path.join(directory,'manifest.json'));
    for(const [width,height] of [[1440,1000],[390,844],[320,740]]){
      await page.setViewportSize({width,height});await page.locator('.knowledge-build-section').scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/knowledge-builds-${width}.png`)});
    }
    const short=await page.evaluate(async({candidate,source})=>{
      const work='/knowledge-workbench',post=(url,body)=>api(url,{method:'POST',body:JSON.stringify(body)});
      const updated=await api(work+'/candidates/'+candidate.id,{method:'PUT',body:JSON.stringify({version:candidate.version,content:Array.from(candidate.content).slice(0,20).join(''),source_chunk_id:source.source.chunk_id,source_hash:source.source_hash,reviewed:true})});
      const submitted=await post(work+'/candidates/'+candidate.id+'/submit',{client_id:crypto.randomUUID(),version:updated.version,share_public_excerpt:true});
      const checked=await post(work+'/review-queue/'+submitted.id+'/check',{version:submitted.version});
      return await post(work+'/review-queue/'+submitted.id+'/approve',{version:checked.version,gate_id:checked.gate.id,binding_hash:checked.gate.binding_hash,reviewed:true});
    },seeded);
    await page.getByRole('button',{name:'刷新工作台',exact:true}).click();await page.locator(`[data-build-select="${short.id}"]`).waitFor();
    await page.waitForFunction(()=>!document.querySelector('.candidate-build-button')?.disabled);
    const before=fs.readdirSync(path.dirname(directory),{withFileTypes:true}).filter(item=>item.isDirectory()).map(item=>item.name).sort();
    await page.locator(`[data-build-select="${short.id}"]`).check();await page.locator('.candidate-build-confirmed').check();await build.click();
    await page.waitForFunction(()=>document.querySelector('.review-status')?.textContent.includes('缺少完整且匹配的已有向量缓存'));
    assert.deepEqual(fs.readdirSync(path.dirname(directory),{withFileTypes:true}).filter(item=>item.isDirectory()).map(item=>item.name).sort(),before);
    assert.deepEqual(fs.readFileSync(path.join(directory,'manifest.json')),manifest);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_provider_calls:0,checks:['explicit selection and confirmation','private field separation','isolated complete package','identical-text dedup','no online activation','shorter excerpt blocks missing encoding','prior package preserved','1440/390/320']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/knowledge-builds-failure.png'),fullPage:true});throw error;}
  finally{
    execFileSync(python,[path.resolve(__dirname,'../scripts/knowledge_reviewer.py'),'revoke','--database',fixture,'--username',username],{stdio:'pipe'});
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
