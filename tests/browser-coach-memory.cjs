// Runs only against an isolated synthetic fixture server, never production.
const {chromium}=require("playwright");
const assert=require("node:assert/strict");

async function main() {
  const browser=await chromium.launch({headless:true,channel:"chrome"});
  const page=await browser.newPage({viewport:{width:1280,height:850},locale:"zh-CN"});
  const errors=[];page.on("pageerror",error=>errors.push(error.message));
  try {
    await page.goto("http://127.0.0.1:8771/");
    await page.getByRole("button",{name:"注册",exact:true}).click();
    await page.getByLabel("用户名",{exact:true}).fill(`memory_${Date.now()}`);
    await page.getByLabel("密码",{exact:true}).fill("Synthetic-memory-2026");
    await page.getByRole("button",{name:"注册并开始",exact:true}).click();
    await page.locator("#workspace").waitFor({state:"visible"});
    await page.locator('[data-view="assistant"]').click();
    await page.locator(".coach-memory summary").click();
    await page.locator(".coach-memory-list").getByText("还没有保存个人记忆").waitFor();
    await page.locator(".coach-memory-content").fill("请用简明中文解释");
    await page.getByRole("button",{name:"保存记忆",exact:true}).click();
    await page.locator(".coach-memory-list").getByText("请用简明中文解释").waitFor();
    await page.locator(".coach-memory-list").getByRole("button",{name:"修改"}).click();
    await page.locator(".coach-memory-content").fill("请分步骤讲解");
    await page.getByRole("button",{name:"保存修改"}).click();
    await page.locator(".coach-memory-list").getByText("请分步骤讲解").waitFor();
    page.once("dialog",dialog=>dialog.accept());
    await page.locator(".coach-memory-list").getByRole("button",{name:"删除"}).click();
    await page.locator(".coach-memory-list").getByText("还没有保存个人记忆").waitFor();
    await page.setViewportSize({width:390,height:780});
    assert.ok(await page.locator(".coach-memory-form").isVisible());
    assert.deepEqual(errors,[]);
    console.log("Coach memory browser: save, edit, delete and narrow layout passed on synthetic fixture.");
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
