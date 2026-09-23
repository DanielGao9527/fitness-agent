const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");

const baseURL = "http://127.0.0.1:8767";
const output = path.resolve(__dirname, "..", "artifacts");
const wait = async (fn) => {
  for (let i = 0; i < 60; i++) {
    if (await fn()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("State did not appear");
};

async function main() {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: "zh-CN",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("dialog", (dialog) => dialog.accept());
  const call = (url, method = "GET", body) =>
    context.request.fetch(baseURL + "/api" + url, {
      method,
      ...(body ? { data: body } : {}),
    });
  const drafts = async () => (await call("/meal-drafts")).json();
  const layout = async (label) => {
    const result = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      clipped: [...document.querySelectorAll("button,input,textarea,select")]
        .filter((el) => {
          const box = el.getBoundingClientRect();
          return (
            box.width > 0 &&
            box.height > 0 &&
            el.tagName === "BUTTON" &&
            el.scrollWidth > el.clientWidth + 2
          );
        })
        .map((el) => el.textContent.trim()),
      dialogOverflow:
        document.querySelector("#record-dialog").scrollWidth >
        document.querySelector("#record-dialog").clientWidth + 1,
    }));
    assert.equal(result.overflow, false, label);
    assert.equal(result.dialogOverflow, false, label);
    assert.deepEqual(result.clipped, [], label);
  };
  const newText = async (text) => {
    await page.getByRole("button", { name: "记录饮食", exact: true }).click();
    await page.getByRole("button", { name: "文字描述", exact: true }).click();
    await page.getByLabel("本次饮食描述（仅所选餐次）", { exact: true }).fill(text);
  };
  try {
    const registration = await call("/auth/register", "POST", {
      username: `draft_ui_${Date.now()}`,
      password: "Test-only-password-42",
    });
    assert.equal(registration.status(), 201);
    await page.goto(baseURL);
    await page.getByRole("button", { name: "记录饮食", exact: true }).waitFor();
    await newText("早餐吃了100克鸡蛋和一杯牛奶，没吃面包。测试替身样本");
    await page.getByRole("button", { name: "解析食物", exact: true }).click();
    await page
      .getByRole("group", { name: "草稿食物 2", exact: true })
      .waitFor();
    await wait(
      async () =>
        !(await page
          .getByRole("button", { name: "添加食物", exact: true })
          .isDisabled()),
    );
    assert.equal(
      await page.getByRole("button", { name: "确认入账" }).isDisabled(),
      true,
    );
    assert.equal(
      (await (await call(`/meals?day=${(await drafts())[0].day}`)).json())
        .length,
      0,
    );
    assert.ok((await drafts())[0].questions.length);
    await layout("desktop draft");
    await page.screenshot({
      path: path.join(output, "draft-desktop.png"),
      fullPage: true,
    });
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await layout(`draft ${width}`);
      await page.screenshot({
        path: path.join(output, `draft-mobile-${width}.png`),
        fullPage: true,
      });
    }
    await page.locator(".draft-actions").scrollIntoViewIfNeeded();
    await layout("draft mobile actions");
    await page.screenshot({
      path: path.join(output, "draft-mobile-actions.png"),
      fullPage: true,
    });
    await page
      .getByRole("group", { name: "草稿食物 2", exact: true })
      .getByLabel("食用份量（g）")
      .fill("250");
    await page.getByRole("button", { name: "添加食物", exact: true }).click();
    await page
      .getByRole("group", { name: "草稿食物 3", exact: true })
      .getByRole("button", { name: "移除此食物" })
      .click();
    await page.getByRole("button", { name: "暂存草稿" }).click();
    await page.getByText("草稿已暂存", { exact: true }).waitFor();
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.reload();
    await page.getByRole("button", { name: "继续", exact: true }).click();
    assert.equal(
      await page
        .getByRole("group", { name: "草稿食物 2", exact: true })
        .getByLabel("食用份量（g）")
        .inputValue(),
      "250",
    );
    await page.getByLabel("我已核对食物和份量").check();
    let lost = false;
    await page.route("**/api/meal-drafts/*/confirm", async (route) => {
      if (!lost) {
        lost = true;
        const response = await route.fetch();
        assert.equal(response.status(), 200);
        await route.abort("failed");
      } else await route.continue();
    });
    await page.getByRole("button", { name: "确认入账" }).click();
    await wait(async () =>
      Boolean(await page.locator("#draft-form .form-error").textContent()),
    );
    await page.getByRole("button", { name: "确认入账" }).click();
    await page.getByText("饮食已确认入账", { exact: true }).waitFor();
    assert.equal((await drafts()).length, 0);
    await page.locator(".record").filter({ hasText: "牛奶" }).waitFor();
    assert.equal(
      await page.locator(".record").filter({ hasText: "牛奶" }).count(),
      1,
    );
    await page.unroute("**/api/meal-drafts/*/confirm");

    // A missing key disables parsing but never blocks manual draft completion.
    const health = await (await call("/health")).json();
    await page.route("**/api/health", route => route.fulfill({
      json: { ...health, meal_text: "not_configured" },
    }));
    await page.reload();
    await newText("<img src=x onerror=alert(1)> 已吃米饭，手动核对测试");
    assert.equal(
      await page
        .getByRole("button", { name: "解析食物", exact: true })
        .isDisabled(),
      true,
    );
    await page.getByRole("button", { name: "暂存草稿" }).click();
    await page.getByText("草稿已暂存", { exact: true }).waitFor();
    await page.getByRole("button", { name: "添加食物", exact: true }).click();
    await page
      .getByLabel("食物名称", { exact: true })
      .fill("<img src=x onerror=alert(1)> 米饭");
    await page.getByLabel("食用份量（g）").fill("180");
    await page.getByRole("button", { name: "暂存草稿" }).click();
    await wait(async () => (await drafts())[0].status === "ready");
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.reload();
    assert.equal(await page.locator(".draft-list img").count(), 0);
    await page.getByRole("button", { name: "继续", exact: true }).click();

    const other = await context.newPage();
    other.on("dialog", (dialog) => dialog.accept());
    await other.goto(baseURL);
    await other.getByRole("button", { name: "继续", exact: true }).click();
    await other.getByLabel("食用份量（g）").fill("200");
    await other.getByRole("button", { name: "暂存草稿" }).click();
    await other.getByText("草稿已暂存", { exact: true }).waitFor();
    await page.getByLabel("食用份量（g）").fill("220");
    await page.getByRole("button", { name: "暂存草稿" }).click();
    await page
      .getByText("草稿已在其他页面更新，请重新打开后核对", { exact: true })
      .waitFor();
    assert.equal(await page.getByLabel("食用份量（g）").inputValue(), "220");
    await page.getByRole("button", { name: "读取最新草稿" }).click();
    await wait(
      async () =>
        (await page.getByLabel("食用份量（g）").inputValue()) === "200",
    );
    await other.close();
    await page.getByRole("button", { name: "放弃", exact: true }).click();
    await wait(async () => (await drafts()).length === 0);

    await page.unroute("**/api/health");
    await page.reload();
    await newText("timeout test: provider failure");
    await page.getByRole("button", { name: "解析食物", exact: true }).click();
    await page
      .getByText("AI 响应超时，请稍后重试。", { exact: true })
      .waitFor();
    await page.reload();
    assert.equal((await drafts()).length, 1);
    await page.getByRole("button", { name: "继续", exact: true }).click();
    assert.ok(
      (await page.getByLabel("本次饮食描述（仅所选餐次）", {exact:true}).inputValue()).includes("timeout"),
    );
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    await page.locator("#auth-screen").waitFor({ state: "visible" });
    assert.equal(await page.locator("#dialog-content").textContent(), "");
    assert.equal(await page.locator("#content").textContent(), "");
    await call("/auth/register", "POST", {
      username: `draft_other_${Date.now()}`,
      password: "Test-only-password-43",
    });
    await page.reload();
    await page.getByRole("button", { name: "记录饮食", exact: true }).waitFor();
    assert.equal(await page.locator(".draft-list").count(), 0);
    assert.deepEqual(errors, []);
    console.log(
      "Draft browser workflow passed: restore, edit, confirm retry, conflicts, no-key mode, timeout, isolation, desktop/mobile. Model is a test fixture.",
    );
  } catch(error) {
    await page.screenshot({path:path.resolve(__dirname,'../artifacts/r121-drafts-failure.png'),fullPage:true});
    console.error(await page.locator('#dialog-content').innerText());
    throw error;
  } finally {
    await page.unrouteAll({behavior: "wait"});
    await browser.close();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
