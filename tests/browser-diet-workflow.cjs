// Current standalone food-photo workflow; meal suggestions are covered by browser-quick-meals.
// Synthetic fixture only. No real provider calls and no production records.
const {chromium}=require('playwright'),assert=require('node:assert/strict'),path=require('node:path');
async function main(){
  const browser=await chromium.launch({headless:true,channel:'chrome'}),page=await browser.newPage({viewport:{width:1440,height:1000},locale:'zh-CN'});
  let allowLeave=true;
  const errors=[],photos=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>allowLeave?d.accept():d.dismiss());
  page.on('request',r=>{if(r.method()==='POST'&&r.url().includes('/photo?'))photos.push(r.url());});
  const api=async(method,url,data)=>{const r=await page.request.fetch('http://127.0.0.1:8767/api'+url,{method,...(data?{data}:{})});assert.ok(r.ok(),await r.text());return r.json();};
  const layout=async label=>{
    for(const width of [1440,390,320]){await page.setViewportSize({width,height:width===1440?1000:844});
      const result=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,dialog:document.querySelector('#record-dialog').open&&document.querySelector('#record-dialog').scrollWidth>document.querySelector('#record-dialog').clientWidth+1,
        clipped:[...document.querySelectorAll('button')].filter(e=>e.getBoundingClientRect().width&&e.scrollWidth>e.clientWidth+3).map(e=>e.textContent.trim())}));
      assert.equal(result.overflow,false,label);assert.equal(result.dialog,false,label);assert.deepEqual(result.clipped,[],label);
      await page.screenshot({path:path.resolve(__dirname,`../artifacts/r19-${label}-${width}.png`),fullPage:true});
    }await page.setViewportSize({width:1440,height:1000});
  };
  try{
    await api('POST','/auth/register',{username:'diet_'+Date.now(),password:'Synthetic-diet-test-42'});
    await page.goto('http://127.0.0.1:8767');await page.locator('#workspace').waitFor({state:'visible'});
    const day=await page.locator('#day').inputValue();
    assert.deepEqual(await api('GET','/meals?day='+day),[]);
    await page.locator('button[data-view="today"]').click();await page.getByRole('button',{name:'记录饮食',exact:true}).click();await page.locator('[data-action="photo-meal"]').click();
    const image=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=c.height=100;c.getContext('2d').fillRect(0,0,100,100);return c.toDataURL('image/png').split(',')[1];});
    await page.locator('.photo-file').setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:Buffer.from(image,'base64')});assert.equal(photos.length,0);
    await page.locator('.photo-preview').waitFor({state:'visible'});await layout('photo-preview');
    await page.locator('#draft-form [name="text"]').fill('timeout');await page.locator('[data-action="parse-photo"]').click();
    await page.getByText('AI 响应超时，请稍后重试。',{exact:true}).waitFor();
    assert.equal(await page.locator('.photo-preview').isVisible(),true);assert.equal(photos.length,1);
    await page.locator('#draft-form [name="text"]').fill('合成照片样例');
    await page.locator('[data-action="parse-photo"]').click();await page.locator('.draft-item').nth(1).waitFor();
    await page.waitForFunction(()=>!document.querySelector('[data-action="add-draft-item"]').disabled);
    assert.equal(photos.length,2);assert.equal(await page.locator('.draft-item [name="grams"]').first().inputValue(),'');
    await page.locator('.draft-item [name="amount_description"]').first().fill('2个');await page.locator('.draft-item [name="grams"]').nth(1).fill('100');
    await page.locator('[data-action="estimate-nutrition"]').click();await page.locator('.nutrition-result').nth(1).waitFor();await layout('photo-reviewed');
    await page.locator('#draft-form [name="reviewed"]').check();await page.locator('[data-action="confirm-draft"]').click();await page.locator('#record-dialog').waitFor({state:'hidden'});
    const recorded=await api('GET','/meals?day='+day);assert.equal(recorded.length,2);
    await page.reload();await page.locator('#workspace').waitFor({state:'visible'});assert.equal((await api('GET','/meals?day='+day)).length,2);assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,real_calls:0,checks:['photo explicit upload','photo review and nutrition','refresh persistence','1440/390/320 layout']}));
  }catch(error){await page.screenshot({path:path.resolve(__dirname,'../artifacts/r19-failure.png'),fullPage:true});throw error;}
  finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
