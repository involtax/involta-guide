import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { testDatabase } from "./database.mjs";
const testDb = await testDatabase();
const directory = mkdtempSync(join(tmpdir(), "involta-unit-"));
process.env.DATA_DIR = directory;
const {
  closeDb,
  transaction,
  claimJob,
  setSetting,
  setting,
  all,
  one,
  run,
  id,
  saveDraft,
  publish,
  locationContent,
  queueGeneration,
} = await import("../lib/db.mjs");
const { reviewSchema, contentSchema } = await import("../lib/schema.mjs");
const { similarity, reviewSignals } = await import("../lib/signals.mjs");
const { processJob } = await import("../lib/ai.mjs");
const { migrate, seedLocations } = await import("../lib/migrate.mjs");
await migrate();
await seedLocations();
const city = await one("SELECT * FROM locations WHERE slug='ivanovo'");
const content = {
  intro: "Местная подборка",
  restaurants: [
    {
      name: "Тестовое место",
      cuisine: "Русская",
      description: "Тестовая рекомендация редактора.",
      mapsUrl: "https://yandex.ru/maps/?text=Иваново",
      sources: [],
    },
  ],
};
test("импорт городов содержит регионы, уникальные URL и Иваново", async () => {
  assert.ok((await all("SELECT id FROM locations")).length > 1100);
  assert.equal(city.name, "Иваново");
  assert.equal(
    (await all("SELECT slug FROM locations GROUP BY slug HAVING count(*)>1"))
      .length,
    0,
  );
});
test("черновик не публикуется сам; публикация и откат сохраняют историю страницы", async () => {
  const first = await saveDraft(
    city.id,
    content,
    "ai",
    "Промпт конкретного запуска",
    "test-model",
  );
  assert.equal(
    (await one("SELECT published_version FROM locations WHERE id=$1", city.id))
      .published_version,
    null,
  );
  await publish(city.id, first);
  const next = await saveDraft(city.id, {
    ...content,
    intro: "Новое вступление",
  });
  assert.equal(
    (await one("SELECT published_version FROM locations WHERE id=$1", city.id))
      .published_version,
    first,
  );
  await publish(city.id, next);
  await publish(city.id, first);
  assert.equal(
    (await one("SELECT published_version FROM locations WHERE id=$1", city.id))
      .published_version,
    first,
  );
  assert.equal(
    (await one("SELECT prompt FROM versions WHERE id=$1", first)).prompt,
    "Промпт конкретного запуска",
  );
  assert.equal(
    (await all("SELECT * FROM versions WHERE location_id=$1", city.id)).length,
    2,
  );
  await assert.rejects(() => publish(city.id, id()), /Версия/);
});
test("URL для карточек ограничены HTTPS и Яндексом", () => {
  assert.ok(contentSchema.safeParse(content).success);
  assert.equal(
    contentSchema.safeParse({
      ...content,
      restaurants: [
        { ...content.restaurants[0], mapsUrl: "javascript:alert(1)" },
      ],
    }).success,
    false,
  );
  assert.equal(
    contentSchema.safeParse({
      ...content,
      restaurants: [
        { ...content.restaurants[0], mapsUrl: "https://yandex.ru.evil.com/" },
      ],
    }).success,
    false,
  );
});
test("шкалы и обязательный результат 1 или 2", () => {
  const entry = {
    restaurantId: null,
    restaurantName: "Место",
    type: "review",
    text: "Вкусно",
    quote: "Вкусно",
    sentiment: 10,
    recommendation: -10,
    rating: 0,
    confidence: 0.9,
    topics: ["food"],
    experience: "personal",
  };
  assert.ok(
    reviewSchema.safeParse({ entries: [entry], suggestions: [] }).success,
  );
  assert.equal(
    reviewSchema.safeParse({ entries: [], suggestions: [] }).success,
    false,
  );
  assert.equal(
    reviewSchema.safeParse({
      entries: [{ ...entry, sentiment: 11 }],
      suggestions: [],
    }).success,
    false,
  );
  assert.equal(
    reviewSchema.safeParse({
      entries: [
        {
          ...entry,
          type: "rating",
          text: null,
          sentiment: null,
          recommendation: null,
          rating: null,
        },
      ],
      suggestions: [],
    }).success,
    false,
  );
});
test("похожие тексты и повтор аудио — сигналы, а не автоматический фрод", () => {
  assert.equal(similarity("Хорошая еда, быстро!", "хорошая еда быстро"), 1);
  assert.equal(similarity("", ""), 0);
  const r = {
    hash: "a",
    visitor: "v1",
    ip_hash: "i1",
    transcript: "Отличный ресторан вкусная еда",
  };
  const found = reviewSignals(r, [
    { ...r, id: "other", visitor: "v2", ip_hash: "i2" },
  ]);
  assert.equal(found.length, 1);
  assert.equal(found[0].sameAudio, true);
  assert.equal(found[0].sameVisitor, false);
});
test("очередь исключает повтор генерации и сохраняет версию промпта", async () => {
  assert.equal(await queueGeneration(city.id, "test prompt"), true);
  assert.equal(await queueGeneration(city.id, "another prompt"), false);
  assert.equal(
    (await one("SELECT prompt FROM jobs WHERE kind='generate'")).prompt,
    "test prompt",
  );
});
test("без ключа задание завершается явной ошибкой, контент не меняется", async () => {
  delete process.env.OPENAI_API_KEY;
  const before = await locationContent(city.id);
  await processJob();
  assert.equal(
    (await one("SELECT status FROM jobs WHERE kind='generate'")).status,
    "failed",
  );
  assert.match(
    (await one("SELECT error FROM jobs WHERE kind='generate'")).error,
    /OPENAI_API_KEY/,
  );
  assert.deepEqual(await locationContent(city.id), before);
});
test("ИИ-генерация сохраняет проверенный структурированный черновик и промпт страницы", async () => {
  const original = globalThis.fetch;
  process.env.OPENAI_API_KEY = "test-key-not-real";
  globalThis.fetch = async (_url, options) => {
    const body = JSON.parse(options.body);
    assert.ok(body.tools.length);
    assert.ok(body.text.format.schema);
    return Response.json({
      output: [
        {
          content: [
            {
              type: "output_text",
              text: JSON.stringify({
                ...content,
                warnings: [],
                rankingReport: [
                  {
                    name: content.restaurants[0].name,
                    mapsUrl: content.restaurants[0].mapsUrl,
                    scores: Object.fromEntries(
                      [
                        "food",
                        "consistency",
                        "identity",
                        "localConnection",
                        "atmosphere",
                        "service",
                        "value",
                        "destination",
                      ].map((k) => [k, 7]),
                    ),
                    evidence: [
                      "food",
                      "consistency",
                      "identity",
                      "localConnection",
                      "atmosphere",
                      "service",
                      "value",
                      "destination",
                    ].map((factor) => ({
                      factor,
                      reason: "Тестовый источник",
                      confidence: "low",
                      sources: [],
                    })),
                    totalScore: 7,
                  },
                ],
              }),
            },
          ],
        },
      ],
    });
  };
  try {
    await queueGeneration(
      city.id,
      "Город {город}, регион {регион}, лимит {лимит}",
    );
    await processJob();
    const v = await one(
      "SELECT * FROM versions WHERE location_id=$1 ORDER BY number DESC LIMIT 1",
      city.id,
    );
    assert.equal(v.source, "ai");
    assert.equal(JSON.parse(v.content).rankingReport[0].totalScore, 7);
    assert.equal(
      contentSchema.parse(await locationContent(city.id)).rankingReport.length,
      1,
    );
    assert.match(v.prompt, /Иваново/);
    assert.ok(!v.prompt.includes("{город}"));
    assert.equal(
      (
        await one(
          "SELECT published_version FROM locations WHERE id=$1",
          city.id,
        )
      ).published_version !== v.id,
      true,
    );
  } finally {
    globalThis.fetch = original;
    delete process.env.OPENAI_API_KEY;
  }
});
test("разбор записи всегда создаёт отзыв или оценку, дополнительные действия сохраняются предложениями", async () => {
  const original = globalThis.fetch;
  process.env.OPENAI_API_KEY = "test-key-not-real";
  const reviewId = id();
  await run(
    "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip_hash,device,transcript) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
    reviewId,
    city.id,
    "visitor",
    "test.webm",
    "audio/webm",
    "hash",
    "ip",
    "device",
    "Очень вкусно в новом месте",
  );
  await run(
    "INSERT INTO jobs(id,kind,location_id,review_id) VALUES($1,'review',$2,$3)",
    id(),
    city.id,
    reviewId,
  );
  const parsed = {
    entries: [
      {
        restaurantId: null,
        restaurantName: "Новое место",
        type: "review",
        text: "Очень вкусно",
        quote: "Очень вкусно",
        sentiment: 8,
        recommendation: 9,
        rating: null,
        confidence: 0.9,
        topics: ["food"],
        experience: "personal",
      },
    ],
    suggestions: [
      {
        kind: "restaurant",
        restaurantId: null,
        name: "Новое место",
        cuisine: "",
        description: "",
        mapsUrl: "",
        reason: "Автор рекомендует новое место",
      },
    ],
  };
  globalThis.fetch = async () =>
    Response.json({
      output: [
        { content: [{ type: "output_text", text: JSON.stringify(parsed) }] },
      ],
    });
  try {
    await processJob();
    const review = await one("SELECT * FROM reviews WHERE id=$1", reviewId);
    assert.equal(review.status, "processed");
    assert.equal(review.moderation, "pending");
    assert.equal(JSON.parse(review.result).entries[0].sentiment, 8);
    assert.equal(
      (await all("SELECT * FROM proposals WHERE review_id=$1", reviewId))
        .length,
      1,
    );
    assert.equal(
      (
        await all(
          "SELECT * FROM proposals WHERE review_id=$1 AND status='approved'",
          reviewId,
        )
      ).length,
      0,
    );
  } finally {
    globalThis.fetch = original;
    delete process.env.OPENAI_API_KEY;
  }
});

test("ошибка откатывает все запросы транзакции", async () => {
  await assert.rejects(
    () =>
      transaction(async () => {
        await setSetting("rollback_test", "value");
        throw new Error("rollback");
      }),
    /rollback/,
  );
  assert.equal(await setting("rollback_test", "missing"), "missing");
});
test("одновременные сохранения страницы получают разные номера версий", async () => {
  await Promise.all(
    Array.from({ length: 5 }, (_, i) =>
      saveDraft(city.id, { ...content, intro: `Версия ${i}` }),
    ),
  );
  const versions = await all(
    "SELECT number FROM versions WHERE location_id=$1",
    city.id,
  );
  assert.equal(new Set(versions.map((v) => v.number)).size, versions.length);
});
test("дубликаты очереди исключены при конкурентных запросах, задания не выдаются двум воркерам", async () => {
  const cities = await all(
    "SELECT id FROM locations WHERE id<>$1 LIMIT 8",
    city.id,
  );
  const inserted = await Promise.all(
    Array.from({ length: 12 }, () => queueGeneration(cities[0].id, "prompt")),
  );
  assert.equal(inserted.filter(Boolean).length, 1);
  await Promise.all(
    cities.slice(1).map((c) => queueGeneration(c.id, "prompt")),
  );
  const jobs = (
    await Promise.all(Array.from({ length: 12 }, () => claimJob()))
  ).filter(Boolean);
  assert.equal(jobs.length, 8);
  assert.equal(new Set(jobs.map((j) => j.id)).size, 8);
});
test("принятие предложения атомарно и защищено от двойного применения", async () => {
  const { applyProposal } = await import("../lib/editorial.mjs");
  const review = await one("SELECT id FROM reviews LIMIT 1");
  await run("UPDATE reviews SET moderation='approved' WHERE id=$1", review.id);
  const proposal = await one(
    "SELECT id FROM proposals WHERE review_id=$1 LIMIT 1",
    review.id,
  );
  const before = (await locationContent(city.id)).restaurants.length;
  const outcomes = await Promise.allSettled([
    applyProposal(proposal.id, content.restaurants[0]),
    applyProposal(proposal.id, content.restaurants[0]),
  ]);
  assert.equal(outcomes.filter((o) => o.status === "fulfilled").length, 1);
  assert.equal((await locationContent(city.id)).restaurants.length, before + 1);
  const link = await one(
    "SELECT review_id FROM version_reviews WHERE version_id=(SELECT draft_version FROM locations WHERE id=$1)",
    city.id,
  );
  assert.equal(link.review_id, review.id);
});
test("устаревшая генерация не перезаписывает изменённый черновик", async () => {
  const previous = (
    await one("SELECT draft_version FROM locations WHERE id=$1", city.id)
  ).draft_version;
  await saveDraft(city.id, content);
  await assert.rejects(
    () =>
      saveDraft(
        city.id,
        { ...content, intro: "Устаревший результат" },
        "ai",
        null,
        null,
        previous,
      ),
    /Черновик изменён/,
  );
  assert.equal((await locationContent(city.id)).intro, content.intro);
});
test.after(async () => {
  await closeDb();
  await testDb.cleanup();
  rmSync(directory, { recursive: true, force: true });
});
