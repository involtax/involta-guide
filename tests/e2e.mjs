import { chromium, expect } from "@playwright/test";
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
const { closeDb, one, all, run, saveDraft, publish } =
  await import("../lib/db.mjs");
const { migrate, seedLocations } = await import("../lib/migrate.mjs");
await migrate();
await seedLocations();
const city = await one("SELECT * FROM locations WHERE slug='ivanovo'");
const password = randomBytes(24).toString("hex");
const alexToken = randomBytes(32).toString("hex");
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
      ALEX_SECRET: alexToken,
      COOKIE_SECURE: "false",
      SITE_URL: "http://127.0.0.1:3107/guide",
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
  const outsideMountRequests = [];
  page.on("request", (request) => {
    const target = new URL(request.url());
    if (
      target.protocol === "http:" &&
      target.origin === "http://127.0.0.1:3107" &&
      !target.pathname.startsWith("/guide")
    )
      outsideMountRequests.push(target.pathname);
    if (target.hostname === "api.openai.com")
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
    "http://127.0.0.1:3107/guide/api/admin/audio/missing",
  );
  assert.equal(unauthorized.status(), 401);
  assert.equal(
    (
      await context.request.get(
        "http://127.0.0.1:3107/guide/api/admin/locations-export",
      )
    ).status(),
    401,
  );
  const forged = await context.request.post(
    "http://127.0.0.1:3107/guide/api/reviews",
    { headers: { origin: "https://evil.example" } },
  );
  assert.equal(forged.status(), 400);
  await page.goto("http://127.0.0.1:3107/guide/admin");
  await page.getByLabel("Пароль редакции").fill(password);
  await page.getByRole("button", { name: "Войти ↗" }).click();
  await page.getByRole("heading", { name: "Рабочий стол." }).waitFor();
  const exported = await context.request.get(
    "http://127.0.0.1:3107/guide/api/admin/locations-export",
  );
  assert.equal(exported.status(), 200);
  const catalogue = await exported.json();
  assert.ok(catalogue.length > 1400);
  assert.equal(catalogue.filter((l) => l.name === "Газпром Поляна").length, 1);
  assert.ok(!JSON.stringify(catalogue).includes(apiCanary));
  assert.ok(
    !(await page.content()).includes(apiCanary),
    "Секрет не передаётся в HTML админки",
  );
  await page.goto(
    `http://127.0.0.1:3107/guide/admin?tab=cities&city=${city.id}`,
  );
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
    .getByLabel("Телефон (обязателен для публикации)")
    .fill("+74951234567");
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
  assert.equal(review.location_id, city.id);
  assert.ok(review.hash);
  const anonymous = await browser.newContext();
  const other = await anonymous.request.get(
    `http://127.0.0.1:3107/guide/api/my-reviews?city=${city.id}`,
  );
  assert.deepEqual(await other.json(), []);
  await anonymous.close();
  await page.goto(
    "http://127.0.0.1:3107/guide/admin?tab=reviews&review=" + review.id,
  );
  await page.getByRole("button", { name: "Не одобрять", exact: true }).click();
  await page.getByText("Отклонено", { exact: false }).first().waitFor();
  assert.equal(
    (await one("SELECT moderation FROM reviews WHERE id=$1", review.id))
      .moderation,
    "rejected",
  );
  assert.equal((await all("SELECT id FROM reviews")).length, 1);
  // Public location hierarchy and aliases.
  await page.goto("http://127.0.0.1:3107/guide/sirius");
  await page.getByRole("heading", { level: 1, name: /в Сириусе/ }).waitFor();
  await page
    .getByRole("navigation", { name: "Навигационная цепочка" })
    .getByRole("link", { name: "Сочи", exact: true })
    .waitFor();
  assert.equal(
    await page
      .getByRole("heading", { name: "Тестовый ресторан", exact: true })
      .count(),
    0,
    "Нет подмены подборкой другого города",
  );
  const aliasResponse = await context.request.get(
    "http://127.0.0.1:3107/guide/gazprom-laura",
    { maxRedirects: 0 },
  );
  assert.equal(aliasResponse.status(), 308);
  assert.ok(
    aliasResponse.headers().location.endsWith("/guide/gazprom-polyana"),
  );
  await page.goto("http://127.0.0.1:3107/guide/moskva");
  assert.equal(await page.locator(".location-children .city-link").count(), 20);
  assert.ok(!(await page.locator("main").innerText()).includes("Капотня"));
  await page.goto(
    "http://127.0.0.1:3107/guide?q=" + encodeURIComponent("Лаура"),
  );
  await page.getByRole("link", { name: /Газпром Поляна/ }).waitFor();
  assert.equal(await page.locator(".city-grid .city-link").count(), 1);
  // Create/edit a child location through the actual admin form.
  const sochi = await one("SELECT id FROM locations WHERE slug='sochi'");
  await page.goto(
    "http://127.0.0.1:3107/guide/admin?tab=locations&location=new",
  );
  const locationForm = page
    .locator("form")
    .filter({ has: page.getByLabel("Форма для заголовка") });
  await locationForm
    .getByLabel("Название", { exact: true })
    .fill("Тестовая бухта");
  await locationForm
    .getByRole("textbox", { name: "Административный регион", exact: true })
    .fill("Краснодарский край");
  await locationForm.getByLabel("URL локации").fill("test-bay");
  await locationForm.getByLabel("Форма для заголовка").fill("в Тестовой бухте");
  await locationForm.getByLabel("Альтернативные названия").fill("Моя бухта");
  await locationForm.getByLabel("Надлокация: найти").fill("Сочи");
  await locationForm
    .getByRole("combobox", { name: /Родительская локация/ })
    .selectOption(sochi.id);
  await locationForm.getByRole("button", { name: "Сохранить локацию" }).click();
  await page
    .getByRole("heading", { name: "Тестовая бухта", exact: true })
    .waitFor();
  const bay = await one("SELECT * FROM locations WHERE slug='test-bay'");
  assert.equal(bay.parent_id, sochi.id);
  await page.getByLabel("URL локации").fill("test-bay-new");
  await page
    .getByRole("button", { name: "Сохранить локацию", exact: true })
    .click();
  await expect
    .poll(
      async () =>
        (await one("SELECT slug FROM locations WHERE id=$1", bay.id)).slug,
    )
    .toBe("test-bay-new");
  const oldAddress = await context.request.get(
    "http://127.0.0.1:3107/guide/test-bay",
    { maxRedirects: 0 },
  );
  assert.equal(oldAddress.status(), 308);
  await page.goto("http://127.0.0.1:3107/guide/test-bay-new");
  await page
    .getByRole("heading", { level: 1, name: /в Тестовой бухте/ })
    .waitFor();
  // A published child restaurant can be selected for a hub without a second DB row.
  await page.goto(
    "http://127.0.0.1:3107/guide/admin?tab=locations&location=" + bay.id,
  );
  await page.getByRole("button", { name: "+ Ресторан" }).click();
  const bayCard = page.locator("fieldset");
  await bayCard.getByLabel("Название", { exact: true }).fill("Прибрежный тест");
  await bayCard.getByLabel("Кухня", { exact: true }).fill("Европейская");
  await bayCard
    .getByLabel("Почему стоит зайти")
    .fill("Тестовая карточка дочерней локации.");
  await bayCard
    .getByLabel("Телефон (обязателен для публикации)")
    .fill("+74951234567");
  await bayCard
    .getByLabel("Яндекс Карты")
    .fill("https://yandex.ru/maps/?text=Sochi");
  await page.getByRole("button", { name: "Сохранить новую версию" }).click();
  await page.getByText("Черновик сохранён.", { exact: false }).waitFor();
  await page
    .getByRole("button", { name: "Опубликовать текущий черновик ↗" })
    .click();
  await page.getByText("Версия опубликована", { exact: true }).waitFor();
  const shared = await one(
    "SELECT id FROM restaurants WHERE location_id=$1",
    bay.id,
  );
  await page.goto(
    "http://127.0.0.1:3107/guide/admin?tab=locations&location=" + sochi.id,
  );
  await page.getByLabel("Ресторан из дочерних локаций").selectOption(shared.id);
  await page
    .getByRole("button", { name: "Добавить в черновик", exact: true })
    .click();
  await page.getByRole("button", { name: "Сохранить новую версию" }).click();
  await page.getByText("Черновик сохранён.", { exact: false }).waitFor();
  assert.equal(
    (await one("SELECT count(*) n FROM restaurants WHERE id=$1", shared.id)).n,
    1,
  );
  assert.equal(
    (await one("SELECT location_id FROM restaurants WHERE id=$1", shared.id))
      .location_id,
    bay.id,
  );
  assert.equal(
    (await one("SELECT published_version FROM locations WHERE id=$1", sochi.id))
      .published_version,
    null,
  );
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
  // New contact fields, SSR-expanded reserve and separate Alex credentials.
  await run("UPDATE locations SET top_limit=1 WHERE id=$1", city.id);
  const sample = {
    name: "Основной тест",
    cuisine: "Русская",
    description: "Первый ресторан",
    mapsUrl: "https://yandex.ru/maps/org/test/123/",
    phone: "+74951234567",
    sources: [],
    openingHours: {
      timezone: "Europe/Moscow",
      week: Array.from({ length: 7 }, () => [["00:00", "00:00"]]),
    },
  };
  const updated = await saveDraft(city.id, {
    intro: "Тест расширенного списка",
    restaurants: [sample, { ...sample, name: "Резервный тест" }],
  });
  await publish(city.id, updated);
  await page.goto("http://127.0.0.1:3107/guide/ivanovo");
  await expect(
    page.getByRole("heading", { name: /Основной тест/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /Резервный тест/ }),
  ).not.toBeVisible();
  await page.getByText("Ещё · 1", { exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /Резервный тест/ }),
  ).toBeVisible();
  assert.ok((await page.content()).includes("tel:+74951234567"));
  await expect(page.getByText("Открыто до 00:00").first()).toBeVisible();
  const alexContext = await browser.newContext();
  const alexPage = await alexContext.newPage();
  assert.equal(
    (
      await alexContext.request.get(
        "http://127.0.0.1:3107/guide/api/alex/audio/missing",
      )
    ).status(),
    401,
  );
  await alexPage.goto("http://127.0.0.1:3107/guide/?q=" + alexToken);
  assert.ok(
    !(await alexContext.cookies()).some((c) => c.name === "guide_alex"),
  );
  await alexPage.goto("http://127.0.0.1:3107/guide/?" + alexToken);
  await expect(alexPage).toHaveURL(/\/guide\/?$/);
  assert.ok(!(await alexPage.content()).includes(alexToken));
  assert.equal(
    (await alexContext.cookies()).find((c) => c.name === "guide_alex").httpOnly,
    true,
  );
  await alexPage.goto("http://127.0.0.1:3107/guide/ivanovo");
  await expect(
    alexPage.getByText("Режим Алекса:", { exact: false }),
  ).toBeVisible();
  await page.goto("http://127.0.0.1:3107/guide/alex");
  await page
    .getByRole("button", { name: "Завершить все сессии Алекса" })
    .click();
  await alexPage.goto("http://127.0.0.1:3107/guide/alex");
  await expect(
    alexPage.getByText("Для входа откройте персональную ссылку."),
  ).toBeVisible();
  await alexContext.close();
  assert.deepEqual(
    outsideMountRequests,
    [],
    "Все браузерные запросы остаются под /guide",
  );
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
    "E2E PASS: вход, SEO, микрофон, модерация, иерархия, 20 зон Москвы, алиасы, редактор локаций и общий ресторан без копий.",
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
