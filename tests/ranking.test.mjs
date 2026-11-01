import test from "node:test";
import assert from "node:assert/strict";
import {
  FACTORS,
  totalScore,
  rankCandidates,
  normalizeGeneration,
} from "../lib/ranking.mjs";
const scores = (values) =>
  Object.fromEntries(FACTORS.map((key, i) => [key, values[i]]));
test("восемь одинаковых весов и арифметическое округление согласованной таблицы", () => {
  for (const [values, expected] of [
    [[9, 8, 9, 8, 9, 8, 7, 9], 8.38],
    [[8, 8, 8, 6, 9, 8, 7, 8], 7.75],
    [[8, 8, 7, 7, 8, 8, 7, 8], 7.63],
    [[8, 7, 8, 5, 8, 8, 7, 8], 7.38],
    [[8, 7, 6, 3, 8, 7, 8, 7], 6.75],
    [[8, 7, 6, 3, 7, 7, 8, 7], 6.63],
  ])
    assert.equal(totalScore(scores(values)), expected);
});
test("отсутствующий фактор не заменяется нулём и не меняет знаменатель", () =>
  assert.equal(totalScore(scores([8, 8, 8, 8, 8, 8, 8, null])), null));
test("сервер пересчитывает итог и сортирует по убыванию, неизвестное в конце", () => {
  const report = rankCandidates([
    { name: "Б", scores: scores([5, 5, 5, 5, 5, 5, 5, 5]), totalScore: 10 },
    { name: "А", scores: scores([9, 9, 9, 9, 9, 9, 9, 9]), totalScore: 1 },
    {
      name: "В",
      scores: scores([10, 10, 10, 10, 10, 10, 10, null]),
      totalScore: 10,
    },
  ]);
  assert.deepEqual(
    report.map((r) => r.name),
    ["А", "Б", "В"],
  );
  assert.equal(report[0].totalScore, 9);
});
test("публичная подборка следует рассчитанному топу, а не порядку ответа модели", () => {
  const candidates = [
    {
      name: "Б",
      mapsUrl: "https://yandex.ru/maps/2",
      scores: scores([7, 7, 7, 7, 7, 7, 7, 7]),
    },
    {
      name: "А",
      mapsUrl: "https://yandex.ru/maps/1",
      scores: scores([9, 9, 9, 9, 9, 9, 9, 9]),
    },
  ];
  const result = normalizeGeneration(
    { rankingReport: candidates, restaurants: [{ name: "Б" }, { name: "А" }] },
    2,
  );
  assert.deepEqual(
    result.restaurants.map((r) => r.name),
    ["А", "Б"],
  );
});
