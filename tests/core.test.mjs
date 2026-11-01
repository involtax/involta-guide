import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const directory = mkdtempSync(join(tmpdir(), "involta-unit-"));
process.env.DATA_DIR = directory;
const {
  db,
  all,
  one,
  run,
  id,
  saveDraft,
  publish,
  cityContent,
  queueGeneration,
} = await import("../lib/db.mjs");
const { reviewSchema, contentSchema } = await import("../lib/schema.mjs");
const { similarity, reviewSignals } = await import("../lib/signals.mjs");
const { processJob } = await import("../lib/ai.mjs");
const city = one("SELECT * FROM cities WHERE slug='ivanovo'");
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
test("импорт городов содержит регионы, уникальные URL и Иваново", () => {
  assert.ok(all("SELECT id FROM cities").length > 1100);
  assert.equal(city.name, "Иваново");
  assert.equal(
    all("SELECT slug FROM cities GROUP BY slug HAVING count(*)>1").length,
    0,
  );
});
test("черновик не публикуется сам; публикация и откат сохраняют историю страницы", () => {
  const first = saveDraft(
    city.id,
    content,
    "ai",
    "Промпт конкретного запуска",
    "test-model",
  );
  assert.equal(
    one("SELECT published_version FROM cities WHERE id=?", city.id)
      .published_version,
    null,
  );
  publish(city.id, first);
  const next = saveDraft(city.id, { ...content, intro: "Новое вступление" });
  assert.equal(
    one("SELECT published_version FROM cities WHERE id=?", city.id)
      .published_version,
    first,
  );
  publish(city.id, next);
  publish(city.id, first);
  assert.equal(
    one("SELECT published_version FROM cities WHERE id=?", city.id)
      .published_version,
    first,
  );
  assert.equal(
    one("SELECT prompt FROM versions WHERE id=?", first).prompt,
    "Промпт конкретного запуска",
  );
  assert.equal(
    all("SELECT * FROM versions WHERE city_id=?", city.id).length,
    2,
  );
  assert.throws(() => publish(city.id, id()), /Версия/);
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
test("очередь исключает повтор генерации и сохраняет версию промпта", () => {
  assert.equal(queueGeneration(city.id, "test prompt"), true);
  assert.equal(queueGeneration(city.id, "another prompt"), false);
  assert.equal(
    one("SELECT prompt FROM jobs WHERE kind='generate'").prompt,
    "test prompt",
  );
});
test("без ключа задание завершается явной ошибкой, контент не меняется", async () => {
  delete process.env.OPENAI_API_KEY;
  const before = cityContent(city.id);
  await processJob();
  assert.equal(
    one("SELECT status FROM jobs WHERE kind='generate'").status,
    "failed",
  );
  assert.match(
    one("SELECT error FROM jobs WHERE kind='generate'").error,
    /OPENAI_API_KEY/,
  );
  assert.deepEqual(cityContent(city.id), before);
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
              text: JSON.stringify({ ...content, warnings: [] }),
            },
          ],
        },
      ],
    });
  };
  try {
    queueGeneration(city.id, "Город {город}, регион {регион}, лимит {лимит}");
    await processJob();
    const v = one(
      "SELECT * FROM versions WHERE city_id=? ORDER BY number DESC LIMIT 1",
      city.id,
    );
    assert.equal(v.source, "ai");
    assert.match(v.prompt, /Иваново/);
    assert.ok(!v.prompt.includes("{город}"));
    assert.equal(
      one("SELECT published_version FROM cities WHERE id=?", city.id)
        .published_version !== v.id,
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
  run(
    "INSERT INTO reviews(id,city_id,visitor,audio,mime,hash,ip_hash,device,transcript) VALUES(?,?,?,?,?,?,?,?,?)",
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
  run(
    "INSERT INTO jobs(id,kind,city_id,review_id) VALUES(?,'review',?,?)",
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
    const review = one("SELECT * FROM reviews WHERE id=?", reviewId);
    assert.equal(review.status, "processed");
    assert.equal(review.moderation, "pending");
    assert.equal(JSON.parse(review.result).entries[0].sentiment, 8);
    assert.equal(
      all("SELECT * FROM proposals WHERE review_id=?", reviewId).length,
      1,
    );
    assert.equal(
      all(
        "SELECT * FROM proposals WHERE review_id=? AND status='approved'",
        reviewId,
      ).length,
      0,
    );
  } finally {
    globalThis.fetch = original;
    delete process.env.OPENAI_API_KEY;
  }
});
test.after(() => {
  db.close();
  rmSync(directory, { recursive: true, force: true });
});
