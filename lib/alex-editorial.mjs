import {
  transaction,
  one,
  run,
  locationContent,
  saveDraft,
  publish,
  audit,
} from "./db.mjs";
import { alexCommandSchema, contentSchema } from "./schema.mjs";
export async function applyAlexCommands(review, commands, expectedVersion) {
  const parsed = alexCommandSchema.parse(commands);
  return transaction(async () => {
    const session = await one(
      "SELECT * FROM alex_sessions WHERE id=$1 FOR UPDATE",
      review.alex_session_id,
    );
    if (
      !session ||
      session.revoked ||
      Date.parse(
        session.expires_at.endsWith("Z")
          ? session.expires_at
          : session.expires_at + "Z",
      ) <= Date.now()
    )
      throw new Error("Сессия Алекса завершена");
    const location = await one(
      "SELECT * FROM locations WHERE id=$1 FOR UPDATE",
      review.location_id,
    );
    if (location.draft_version !== expectedVersion)
      throw new Error(
        "Страница изменилась во время распознавания. Повторите команду.",
      );
    if (
      location.draft_version &&
      location.draft_version !== location.published_version
    )
      throw new Error(
        "У страницы есть неопубликованный черновик. Сначала проверьте его в админке.",
      );
    const prior = await one(
      "SELECT status FROM alex_changes WHERE review_id=$1 FOR UPDATE",
      review.id,
    );
    if (prior?.status === "applied") return;
    const content = await locationContent(location.id);
    const before = JSON.stringify(content);
    if (parsed.intro !== undefined) content.intro = parsed.intro;
    for (const command of parsed.actions) {
      const index = content.restaurants.findIndex(
        (r) => r.id === command.restaurantId,
      );
      if (command.kind === "add") {
        if (command.restaurantId)
          throw new Error(
            "Новый ресторан не должен иметь существующий идентификатор",
          );
        if (
          content.restaurants.some(
            (r) =>
              r.name.trim().toLocaleLowerCase("ru") ===
              command.changes.name?.trim().toLocaleLowerCase("ru"),
          )
        )
          throw new Error("Ресторан с таким названием уже есть");
        content.restaurants.push({ ...command.changes });
      } else {
        if (index < 0)
          throw new Error("Не удалось однозначно определить ресторан");
        if (
          command.kind === "update" &&
          content.restaurants[index].locationId &&
          content.restaurants[index].locationId !== location.id &&
          Object.keys(command.changes).some((key) => key !== "alexStar")
        )
          throw new Error(
            "Измените общий ресторан на странице его основной локации",
          );
        if (command.kind === "remove") content.restaurants.splice(index, 1);
        else {
          if (command.changes.alexStar !== undefined)
            await run(
              "UPDATE restaurants SET alex_star=$1 WHERE id=$2",
              command.changes.alexStar,
              command.restaurantId,
            );
          content.restaurants[index] = {
            ...content.restaurants[index],
            ...command.changes,
          };
        }
      }
    }
    if (before === JSON.stringify(content)) {
      if (!parsed.comment?.trim())
        throw new Error("Нет однозначных изменений. Уточните команду.");
      await run(
        "UPDATE reviews SET moderation='approved',status='processed',result=$1,error=NULL WHERE id=$2",
        JSON.stringify({
          entries: [],
          suggestions: [],
          comment: parsed.comment,
        }),
        review.id,
      );
      await run(
        "UPDATE alex_changes SET status='applied',detail='Комментарий сохранён без изменения страницы',before_content=$1,after_content=$1 WHERE review_id=$2",
        before,
        review.id,
      );
      await audit("alex_comment", location.id, review.id);
      return;
    }
    const next = contentSchema.parse(content);
    const version = await saveDraft(
      location.id,
      next,
      "alex",
      null,
      "voice-editor",
      expectedVersion,
    );
    await run(
      "INSERT INTO version_reviews(version_id,review_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
      version,
      review.id,
    );
    await publish(location.id, version);
    await run(
      "UPDATE reviews SET moderation='approved',status='processed',result=$1,error=NULL WHERE id=$2",
      JSON.stringify(parsed),
      review.id,
    );
    await run(
      "UPDATE alex_changes SET status='applied',detail='Применено и опубликовано',version_id=$1,before_content=$2,after_content=$3 WHERE review_id=$4",
      version,
      before,
      JSON.stringify(await locationContent(location.id)),
      review.id,
    );
    await audit(
      "alex_publish",
      location.id,
      JSON.stringify({
        reviewId: review.id,
        versionId: version,
        sessionId: session.id,
      }),
    );
    return version;
  });
}
