import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";
import {
  mkdtempSync,
  rmSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  existsSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import assert from "node:assert/strict";
import { testDatabase } from "./database.mjs";
const testDb = await testDatabase();
const directory = mkdtempSync(join(tmpdir(), "involta-e2e-"));
process.env.DATA_DIR = directory;
const { closeDb, one, all } = await import("../lib/db.mjs");
const { migrate, seedCities } = await import("../lib/migrate.mjs");
await migrate();
await seedCities();
const city = await one("SELECT * FROM cities WHERE slug='ivanovo'");
const password = randomBytes(24).toString("hex");
const apiCanary = `test-only-${randomBytes(32).toString("hex")}`;
const server = spawn(
  process.execPath,
  [
    "node_modules/next/dist/bin/next",
    "start",
    "--hostname",
    "127.0.0.1",
    "--port",
    "3107",
  ],
  {
    env: {
      ...process.env,
      DATA_DIR: directory,
      ADMIN_PASSWORD: password,
      SESSION_SECRET: randomBytes(48).toString("hex"),
      OPENAI_API_KEY: apiCanary,
      COOKIE_SECURE: "false",
      SITE_URL: "http://127.0.0.1:3107",
    },
    stdio: ["ignore", "pipe", "pipe"],
  },
);
let logs = "";
server.stdout.on("data", (d) => (logs += d));
server.stderr.on("data", (d) => (logs += d));
let browser;
try {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch("http://127.0.0.1:3107/guide");
      if (r.ok) break;
    } catch {}
    if (i === 79) throw new Error("Сервер не запустился: " + logs);
    await new Promise((r) => setTimeout(r, 250));
  }
  browser = await chromium.launch({
    executablePath:
      process.env.CHROMIUM_PATH ||
      (existsSync("/usr/bin/chromium")
        ? "/usr/bin/chromium"
        : chromium.executablePath()),
    headless: true,
    args: [
      "--no-sandbox",
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
    ],
  });
  const context = await browser.newContext({ permissions: ["microphone"] });
  const page = await context.newPage();
  const errors = [];
  const directOpenAIRequests = [];
  page.on("request", (request) => {
    if (new URL(request.url()).hostname === "api.openai.com")
      directOpenAIRequests.push(request.url());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("http://127.0.0.1:3107/guide/ivanovo");
  await page.getByRole("heading", { name: "Подборка в работе" }).waitFor();
  assert.match(
    await page.locator("meta[name=robots]").getAttribute("content"),
    /noindex/,
  );
  const unauthorized = await context.request.get(
    "http://127.0.0.1:3107/api/admin/audio/missing",
  );
  assert.equal(unauthorized.status(), 401);
  const forged = await context.request.post(
    "http://127.0.0.1:3107/api/reviews",
    { headers: { origin: "https://evil.example" } },
  );
  assert.equal(forged.status(), 400);
  await page.goto("http://127.0.0.1:3107/admin");
  await page.getByLabel("Пароль редакции").fill(password);
  await page.getByRole("button", { name: "Войти ↗" }).click();
  await page.getByRole("heading", { name: "Рабочий стол." }).waitFor();
  assert.ok(
    !(await page.content()).includes(apiCanary),
    "Секрет не передаётся в HTML админки",
  );
  await page.goto(`http://127.0.0.1:3107/admin?tab=cities&city=${city.id}`);
  await page
    .getByLabel("Вступление")
    .fill("Короткая тестовая подборка Иванова.");
  await page.getByRole("button", { name: "+ Ресторан" }).click();
  const fieldset = page.locator("fieldset");
  await fieldset
    .getByLabel("Название", { exact: true })
    .fill("Тестовый ресторан");
  await fieldset.getByLabel("Кухня", { exact: true }).fill("Русская");
  await fieldset
    .getByLabel("Почему стоит зайти")
    .fill("Тестовая карточка для проверки публикации.");
  await fieldset
    .getByLabel("Яндекс Карты")
    .fill("https://yandex.ru/maps/?text=Иваново");
  await page.getByRole("button", { name: "Сохранить новую версию" }).click();
  await page.getByText("Черновик сохранён.", { exact: false }).waitFor();
  const raw = await fetch("http://127.0.0.1:3107/guide/ivanovo").then((r) =>
    r.text(),
  );
  assert.ok(
    !raw.includes("Тестовый ресторан"),
    "Черновик не должен попасть на сайт",
  );
  await page
    .getByRole("button", { name: "Опубликовать текущий черновик ↗" })
    .click();
  await page.getByText("Версия опубликована", { exact: true }).waitFor();
  await page.goto("http://127.0.0.1:3107/guide/ivanovo");
  await page.getByRole("heading", { name: "Тестовый ресторан" }).waitFor();
  assert.match(
    await page.locator("meta[name=robots]").getAttribute("content"),
    /index/,
  );
  const html = await fetch(page.url()).then((r) => r.text());
  assert.ok(
    html.includes("Тестовый ресторан") && html.includes("application/ld+json"),
  );
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Записать отзыв" }).click();
  await page.getByRole("button", { name: /Остановить/ }).waitFor();
  await page.waitForTimeout(2200);
  await page.getByRole("button", { name: /Остановить/ }).click();
  const uploadResponse = page.waitForResponse(
    (r) => r.url().endsWith("/api/reviews") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Отправить ↗" }).click();
  const uploaded = await uploadResponse;
  assert.equal(uploaded.status(), 201, await uploaded.text());
  await page.getByRole("heading", { name: "Ваш голос в работе" }).waitFor();
  const review = await one(
    "SELECT * FROM reviews ORDER BY created_at DESC LIMIT 1",
  );
  assert.ok(review);
  assert.equal(review.city_id, city.id);
  assert.ok(review.hash);
  const anonymous = await browser.newContext();
  const other = await anonymous.request.get(
    `http://127.0.0.1:3107/api/my-reviews?city=${city.id}`,
  );
  assert.deepEqual(await other.json(), []);
  await anonymous.close();
  await page.goto(
    "http://127.0.0.1:3107/admin?tab=reviews&review=" + review.id,
  );
  await page.getByRole("button", { name: "Не одобрять", exact: true }).click();
  await page.getByText("Отклонено", { exact: false }).first().waitFor();
  assert.equal(
    (await one("SELECT moderation FROM reviews WHERE id=$1", review.id))
      .moderation,
    "rejected",
  );
  assert.equal((await all("SELECT id FROM reviews")).length, 1);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:3107/guide/ivanovo");
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "Нет горизонтального переполнения на телефоне",
  );
  mkdirSync("var/screenshots", { recursive: true });
  await page.screenshot({
    path: "var/screenshots/city-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("http://127.0.0.1:3107/guide");
  await page.screenshot({ path: "var/screenshots/home.png" });
  assert.deepEqual(errors, []);
  assert.deepEqual(
    directOpenAIRequests,
    [],
    "Браузер не вызывает OpenAI напрямую",
  );
  for (const file of readdirSync(".next/static", { recursive: true })) {
    if (!String(file).endsWith(".js")) continue;
    const js = readFileSync(join(".next/static", String(file)), "utf8");
    assert.ok(
      !js.includes(apiCanary),
      "Секрет не попадает в клиентскую сборку",
    );
    assert.ok(
      !js.includes("api.openai.com/v1/"),
      "Клиентская сборка не содержит вызовы OpenAI",
    );
  }
  console.log(
    "E2E PASS: вход, защита API, черновик, публикация, HTML SEO, микрофон, cookie-история, модерация, мобильная вёрстка.",
  );
} catch (e) {
  console.error(e);
  console.error(logs.slice(-4000));
  process.exitCode = 1;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await new Promise((r) => server.once("exit", r));
  await closeDb();
  await testDb.cleanup();
  rmSync(directory, { recursive: true, force: true });
}
