export const FACTORS = [
  "food",
  "consistency",
  "identity",
  "localConnection",
  "atmosphere",
  "service",
  "value",
  "destination",
];
export const FACTOR_LABELS = {
  food: "Еда",
  consistency: "Стабильность",
  identity: "Самобытность",
  localConnection: "Связь с местом",
  atmosphere: "Атмосфера",
  service: "Сервис",
  value: "Оправданность цены",
  destination: "Стоит отдельного визита",
};
export function totalScore(scores) {
  const values = FACTORS.map((key) => scores[key]);
  if (
    values.some(
      (value) =>
        value === null || !Number.isInteger(value) || value < 0 || value > 10,
    )
  )
    return null;
  return Math.round((values.reduce((a, b) => a + b, 0) / 8) * 100) / 100;
}
export function rankCandidates(candidates) {
  return candidates
    .map((c) => ({ ...c, totalScore: totalScore(c.scores) }))
    .sort(
      (a, b) =>
        (b.totalScore ?? -1) - (a.totalScore ?? -1) ||
        a.name.localeCompare(b.name, "ru"),
    );
}
export function normalizeGeneration(result, limit) {
  const rankingReport = rankCandidates(result.rankingReport);
  const key = (s) => s.trim().toLocaleLowerCase("ru").replace(/\s+/g, " ");
  if (
    new Set(rankingReport.map((c) => key(c.name))).size !== rankingReport.length
  )
    throw new Error("ИИ вернул дубликаты кандидатов");
  const complete = rankingReport.filter(
    (c) => c.totalScore !== null && c.mapsUrl,
  );
  const eligible = complete.filter(
    (candidate, index) =>
      index < limit ||
      result.restaurants.some((r) => key(r.name) === key(candidate.name)),
  );
  const restaurants = eligible.map((candidate) => {
    const selected = result.restaurants.find(
      (r) => key(r.name) === key(candidate.name),
    );
    if (!selected)
      throw new Error(
        "ИИ не подготовил карточку одного из лидеров рейтинга: " +
          candidate.name,
      );
    return { ...selected, mapsUrl: candidate.mapsUrl };
  });
  if (result.restaurants.length !== restaurants.length)
    throw new Error("Подборка ИИ содержит заведения вне рассчитанного топа");
  return { ...result, restaurants, rankingReport };
}
