import { transaction, one, run, saveDraft, cityContent, audit } from "./db.mjs";
import { contentSchema } from "./schema.mjs";
export async function restorePageVersion(cityId, versionId) {
  return transaction(async () => {
    const version = await one(
      "SELECT * FROM versions WHERE id=$1 AND city_id=$2",
      versionId,
      cityId,
    );
    if (!version) throw new Error("Версия не найдена");
    const newId = await saveDraft(
      cityId,
      contentSchema.parse(JSON.parse(version.content)),
      "restore",
    );
    await run("DELETE FROM version_reviews WHERE version_id=$1", newId);
    await run(
      "INSERT INTO version_reviews(version_id,review_id) SELECT $1,review_id FROM version_reviews WHERE version_id=$2",
      newId,
      versionId,
    );
    return newId;
  });
}
export async function applyProposal(proposalId, edited) {
  return transaction(async () => {
    const p = await one(
      "SELECT p.*,r.moderation,r.fraud FROM proposals p JOIN reviews r ON r.id=p.review_id WHERE p.id=$1 FOR UPDATE OF p,r",
      proposalId,
    );
    if (!p) throw new Error("Предложение не найдено");
    if (p.status !== "pending") throw new Error("Предложение уже обработано");
    if (p.moderation !== "approved" || p.fraud)
      throw new Error(
        "Сначала одобрите исходный отзыв и снимите отметку фрода",
      );
    await one("SELECT id FROM cities WHERE id=$1 FOR UPDATE", p.city_id);
    const draft = await cityContent(p.city_id);
    const payload = JSON.parse(p.payload);
    if (p.kind === "restaurant") draft.restaurants.push(edited);
    else {
      const index = draft.restaurants.findIndex(
        (r) => r.id === payload.restaurantId,
      );
      if (index < 0)
        throw new Error(
          "Ресторан не определён или отсутствует в черновике. Внесите правку на странице города.",
        );
      draft.restaurants[index] = { ...draft.restaurants[index], ...edited };
    }
    const versionId = await saveDraft(
      p.city_id,
      contentSchema.parse(draft),
      "review",
    );
    await run(
      "INSERT INTO version_reviews(version_id,review_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
      versionId,
      p.review_id,
    );
    await run("UPDATE proposals SET status='approved' WHERE id=$1", proposalId);
    await audit("proposal_draft", p.city_id, p.review_id);
    return versionId;
  });
}
