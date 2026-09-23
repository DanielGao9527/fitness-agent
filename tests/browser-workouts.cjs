// Run only against ui_fixture_server.py. No real provider or microphone requests.
const {chromium} = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");

async function main() {
  const base = "http://127.0.0.1:8767";
  const output = path.resolve(__dirname, "../artifacts");
  await fs.mkdir(output, {recursive:true});
  const browser = await chromium.launch({headless:true, channel:"chrome"});
  const context = await browser.newContext({viewport:{width:1440,height:1000},locale:"zh-CN",reducedMotion:"reduce"});
  const page = await context.newPage();
  const errors=[], requests=[];
  const user=`workout_${Date.now()}`;
  page.on("pageerror", error=>errors.push(error.message));
  page.on("request", request=>{
    if(request.url().endsWith("/workouts/parse-text") || request.url().endsWith("/workouts/calories/preview-batch"))
      requests.push({url:request.url(),body:request.postDataJSON()});
  });
  const until=async(fn)=>{
    for(let n=0;n<100;n++){if(await fn()) return;await page.waitForTimeout(50);}
    throw new Error("Expected UI state did not appear");
  };
  const summary=()=>page.evaluate(async()=> (await fetch(`/api/summary?day=${document.querySelector('#day').value}`)).json());
  const estimates=()=>requests.filter(r=>r.url.endsWith("preview-batch"));
  const parses=()=>requests.filter(r=>r.url.endsWith("parse-text"));
  const submit=()=>page.locator('#workout-form [type="submit"]');
  const fill=async(index,name,minutes)=>{
    const row=page.locator(".workout-item").nth(index);
    await row.getByLabel("训练项目或部位",{exact:true}).fill(name);
    await row.getByLabel("总时长（分钟）",{exact:true}).fill(String(minutes));
  };
  const review=async()=>{
    await page.getByRole("button",{name:"核对训练",exact:true}).click();
    await until(async()=>await submit().isEnabled() && (await submit().textContent()).includes("保存训练"));
  };
  const save=async()=>{
    await page.getByRole("button",{name:"保存训练",exact:true}).click();
    await page.getByRole("dialog").waitFor({state:"hidden"});
  };
  const add=()=>page.getByRole("button",{name:"记录训练",exact:true}).click();
  const layout=async(label)=>{
    await page.locator('#record-dialog').evaluate(el=>{el.scrollTop=0;});
    const result=await page.evaluate(()=>({
      overflow:document.documentElement.scrollWidth>innerWidth+1,
      clipped:[...document.querySelectorAll('#workout-form button,#workout-form input,#workout-form select')]
        .filter(el=>el.getBoundingClientRect().width>0 && el.scrollWidth>el.clientWidth+3).map(el=>el.name || el.textContent.trim()),
      dialogOverflow:document.querySelector('#record-dialog').scrollWidth>document.querySelector('#record-dialog').clientWidth+1,
    }));
    assert.equal(result.overflow,false,label);assert.equal(result.dialogOverflow,false,label);
    assert.deepEqual(result.clipped,[],label);
    await page.screenshot({path:path.join(output,`r17-${label}.png`),fullPage:true});
    await page.locator('#workout-form [type="submit"]').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,`r17-${label}-actions.png`),fullPage:true});
  };
  try {
    await page.goto(base);
    await page.getByRole("button",{name:"注册",exact:true}).click();
    await page.getByLabel("用户名",{exact:true}).fill(user);
    await page.getByLabel("密码",{exact:true}).fill("Synthetic-workout-test-2026");
    await page.getByRole("button",{name:"注册并开始"}).click();
    await page.locator("#workspace").waitFor({state:"visible"});
    await page.locator('.nav-item[data-view="profile"]').click();
    await page.getByLabel("体重（kg，可选）").fill("70");
    await page.getByRole("button",{name:"保存档案"}).click();
    await page.getByText("档案已保存",{exact:true}).waitFor();
    await page.locator('[data-view="workouts"]').click();
    await add();await fill(0,"游泳",30);
    await page.getByRole("button",{name:"再加一个项目"}).click();
    await fill(1,"胸和三头力量训练",40);
    assert.equal(estimates().length,0);
    assert.equal(await page.locator('.workout-options[open]').count(),0);
    await review();assert.equal(estimates().length,1);assert.equal(estimates()[0].body.items.length,2);
    assert.equal((await summary()).workout_count,0,"preview must not save records");
    for(const [width,height] of [[1440,1000],[390,844],[320,740]]) {
      await page.setViewportSize({width,height});await layout(`manual-${width}`);
    }
    await save();await until(async()=> (await summary()).completed_minutes===70);
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole("button",{name:"编辑训练",exact:true}).last().click();
    await fill(0,"胸和三头力量训练",50);await review();
    assert.equal(estimates().at(-1).body.items.length,1);await save();
    await until(async()=> (await summary()).completed_minutes===80);

    await add();await fill(0,"背部力量训练",20);
    await page.getByLabel("本次体重（kg）").fill("");
    const before=estimates().length;await review();assert.equal(estimates().length,before);
    await save();await until(async()=> (await summary()).workout_count===3);
    assert.equal((await summary()).estimated_workout_calories.unknown_count,1);

    await add();await fill(0,"timeout 训练",15);await review();
    assert.match(await page.locator('#workout-form .form-error').textContent(),/消耗未知保存/);
    await layout("estimate-failure");await save();
    await until(async()=> (await summary()).workout_count===4);

    await add();await page.getByRole("button",{name:"文字描述",exact:true}).click();
    await page.getByLabel("训练描述",{exact:true}).fill("游泳游了30分钟，练三头胸练了40分钟，完成到力竭，强度就正常强度。");
    assert.equal(parses().length,0);
    await page.getByRole("button",{name:"解析训练",exact:true}).click();
    await until(async()=>await submit().isEnabled() && (await submit().textContent()).includes("保存训练"));
    assert.equal(parses().length,1);assert.equal(await page.locator('.workout-item').count(),2);
    assert.equal(estimates().at(-1).body.items.length,2);
    await layout("text-preview");
    await fill(1,"胸和三头力量训练",45);await review();
    assert.equal(parses().length,1);assert.equal(estimates().at(-1).body.items.length,1);
    await save();await until(async()=> (await summary()).workout_count===6);
    assert.equal((await summary()).completed_minutes,190);
    await add();await page.getByRole("button",{name:"文字描述",exact:true}).click();
    await page.getByLabel("训练描述",{exact:true}).fill("我今天上午练了胸30分钟，我练了肩20分钟，晚上练了二头和三头50分钟");
    await page.getByRole("button",{name:"解析训练",exact:true}).click();
    await until(async()=>await submit().isEnabled() && (await submit().textContent()).includes("保存训练"));
    assert.equal(parses().length,2);assert.equal(await page.locator('.workout-item').count(),3);
    for(const [index,minutes] of [30,20,50].entries()) {
      assert.equal(await page.locator('.workout-item').nth(index).getByLabel("总时长（分钟）",{exact:true}).inputValue(),String(minutes));
    }
    assert.equal((await summary()).workout_count,6,"parse must not save records");
    assert.equal(estimates().at(-1).body.items.length,3);
    await layout("separate-durations");await save();
    await until(async()=> (await summary()).workout_count===9);
    const stored=await summary();assert.equal(stored.completed_minutes,290);
    await page.reload();await page.locator('#workspace').waitFor({state:"visible"});
    assert.deepEqual(await summary(),stored);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,record_count:stored.workout_count,completed_minutes:stored.completed_minutes,
      checks:["manual automatic preview","explicit save","shared duration","separate durations 30/20/50","changed row only","single edit","no weight","estimate failure nonblocking","text auto preview","reload persistence","1440/390/320 layout"],
      provider_calls:0,screenshots:["r17-manual-1440.png","r17-manual-390.png","r17-manual-320.png","r17-estimate-failure.png","r17-text-preview.png"]}));
  } finally {
    await context.close();await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
