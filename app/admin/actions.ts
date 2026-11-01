"use server";
import { COOKIE_PATH } from "@/lib/urls.mjs";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { z } from "zod";
import {
  one,
  all,
  run,
  id,
  transaction,
  audit,
  slugify,
  saveDraft,
  publish,
  locationById,
  queueGeneration,
  setSetting,
  setting,
} from "@/lib/db.mjs";
import { saveLocation, linkRestaurant } from "@/lib/locations.mjs";
import { applyProposal, restorePageVersion } from "@/lib/editorial.mjs";
import { contentSchema } from "@/lib/schema.mjs";
import {
  requireAdmin,
  session,
  secureEqual,
  requestFingerprint,
} from "@/lib/auth";
const str = (f: FormData, k: string) => String(f.get(k) || "").trim();
function back(path: string, message: string): never {
  redirect(
    path +
      (path.includes("?") ? "&" : "?") +
      "message=" +
      encodeURIComponent(message),
  );
}
function refresh() {
  revalidatePath("/", "layout");
  revalidatePath("/admin");
}
export async function login(f: FormData) {
  const key = await requestFingerprint();
  const attempt = await one("SELECT * FROM login_attempts WHERE key=$1", key);
  if (attempt && attempt.reset_at > Date.now() && attempt.count >= 10)
    back("/admin", "Слишком много попыток. Повторите через 15 минут.");
  const configured = process.env.ADMIN_PASSWORD;
  if (!configured || configured.length < 12)
    back(
      "/admin",
      "Настройте ADMIN_PASSWORD (от 12 символов) и SESSION_SECRET на сервере.",
    );
  if (!secureEqual(str(f, "password"), configured)) {
    await run(
      "INSERT INTO login_attempts(key,count,reset_at) VALUES($1,1,$2) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN login_attempts.reset_at<$3 THEN 1 ELSE login_attempts.count+1 END,reset_at=CASE WHEN login_attempts.reset_at<$4 THEN excluded.reset_at ELSE login_attempts.reset_at END",
      key,
      Date.now() + 900000,
      Date.now(),
      Date.now(),
    );
    back("/admin", "Неверный пароль");
  }
  let token;
  try {
    token = session();
  } catch {
    back("/admin", "Настройте SESSION_SECRET (от 32 символов)");
  }
  await run("DELETE FROM login_attempts WHERE key=$1", key);
  (await cookies()).set("guide_admin", token!, {
    httpOnly: true,
    secure: process.env.COOKIE_SECURE === "true",
    sameSite: "strict",
    path: COOKIE_PATH,
    maxAge: 28800,
  });
  redirect("/admin");
}
export async function logout() {
  (await cookies()).set("guide_admin", "", {
    path: COOKIE_PATH,
    maxAge: 0,
  });
  redirect("/admin");
}
export async function saveLocationAction(f: FormData) {
  await requireAdmin();
  let locationId = str(f, "id");
  const number = (key: string) =>
    str(f, key) === "" ? null : Number(str(f, key));
  try {
    locationId = await saveLocation(locationId || null, {
      name: str(f, "name"),
      region: str(f, "region"),
      slug: str(f, "slug") || slugify(str(f, "name")),
      kind: str(f, "kind"),
      parentId: str(f, "parentId") || null,
      isHub: f.get("isHub") === "on",
      inPhrase: str(f, "inPhrase"),
      aliases: str(f, "aliases")
        .split(/[,;\n]+/)
        .map((s) => s.trim())
        .filter(Boolean),
      priority: str(f, "priority"),
      topLimit: Number(str(f, "topLimit")),
      active: f.get("active") === "on",
      listed: f.get("listed") === "on",
      population: number("population"),
      populationVerified: f.get("populationVerified") === "on",
      latitude: number("latitude"),
      longitude: number("longitude"),
      coverageNote: str(f, "coverageNote"),
      bounds: str(f, "bounds") ? JSON.parse(str(f, "bounds")) : null,
      relatedIds: f.getAll("relatedIds").map(String),
    });
    refresh();
  } catch (e) {
    back(
      "/admin?tab=locations" + (locationId ? "&location=" + locationId : ""),
      "Не сохранено: " + (e instanceof Error ? e.message : "Проверьте данные"),
    );
  }
  redirect("/admin?tab=locations&location=" + locationId);
}
export async function attachRestaurant(f: FormData) {
  await requireAdmin();
  const locationId = str(f, "locationId");
  await linkRestaurant(str(f, "restaurantId"), locationId);
  refresh();
  back(
    "/admin?tab=locations&location=" + locationId,
    "Ресторан связан с локацией. Теперь его можно добавить в черновик.",
  );
}
export async function saveContent(f: FormData) {
  await requireAdmin();
  const locationId = str(f, "locationId");
  try {
    const content = contentSchema.parse(JSON.parse(str(f, "content")));
    await saveDraft(locationId, content);
    refresh();
  } catch (e) {
    back(
      "/admin?tab=locations&location=" + locationId,
      "Не сохранено: " + (e instanceof Error ? e.message : "ошибка"),
    );
  }
  back(
    "/admin?tab=locations&location=" + locationId,
    "Черновик сохранён. Для публикации нажмите отдельную кнопку.",
  );
}
export async function publishVersion(f: FormData) {
  await requireAdmin();
  const locationId = str(f, "locationId");
  try {
    await publish(locationId, str(f, "versionId"));
    refresh();
  } catch (e) {
    back(
      "/admin?tab=locations&location=" + locationId,
      e instanceof Error ? e.message : "Ошибка",
    );
  }
  back("/admin?tab=locations&location=" + locationId, "Версия опубликована");
}
export async function restoreVersion(f: FormData) {
  await requireAdmin();
  const locationId = str(f, "locationId");
  const versionId = str(f, "versionId");
  await restorePageVersion(locationId, versionId);
  refresh();
  redirect("/admin?tab=locations&location=" + locationId);
}
export async function savePrompt(f: FormData) {
  await requireAdmin();
  const prompt = z.string().min(50).max(16000).parse(str(f, "prompt"));
  await setSetting("prompt", prompt);
  refresh();
  back("/admin?tab=fill", "Промпт сохранён");
}
export async function generate(f: FormData) {
  await requireAdmin();
  const prompt = await setting("prompt");
  const locationId = str(f, "locationId");
  let count = 0;
  await transaction(async () => {
    if (locationId) {
      await locationById(locationId);
      if (await queueGeneration(locationId, prompt)) count++;
    } else {
      const scope = str(f, "scope");
      if (!["empty", "all"].includes(scope))
        throw new Error("Выберите область");
      for (const c of await all(
        "SELECT id FROM locations WHERE active=1 AND (listed OR published_version IS NOT NULL)" +
          (scope === "empty" ? " AND draft='[]'" : ""),
      )) {
        if (await queueGeneration(c.id, prompt)) count++;
      }
    }
  });
  refresh();
  back(
    "/admin?tab=fill",
    `Поставлено в очередь: ${count}. Публикация выполняется отдельно.`,
  );
}
export async function moderate(f: FormData) {
  await requireAdmin();
  const reviewId = str(f, "id");
  const decision = z
    .enum(["approved", "rejected", "fraud", "reset"])
    .parse(str(f, "decision"));
  const reason = str(f, "reason").slice(0, 2000);
  await transaction(async () => {
    const r = await one("SELECT * FROM reviews WHERE id=$1", reviewId);
    if (!r) throw new Error("Отзыв не найден");
    await run(
      "UPDATE reviews SET moderation=$1,fraud=$2,reason=$3 WHERE id=$4",
      decision === "fraud"
        ? "rejected"
        : decision === "reset"
          ? "pending"
          : decision,
      decision === "fraud" ? 1 : 0,
      reason,
      reviewId,
    );
    await audit("review_" + decision, reviewId, reason);
  });
  refresh();
  redirect("/admin?tab=reviews&review=" + reviewId);
}
export async function proposalDecision(f: FormData) {
  await requireAdmin();
  const proposalId = str(f, "id");
  const p = await one(
    "SELECT p.*,r.moderation,r.fraud FROM proposals p JOIN reviews r ON r.id=p.review_id WHERE p.id=$1",
    proposalId,
  );
  if (!p) throw new Error("Предложение не найдено");
  const target = "/admin?tab=reviews&review=" + p.review_id;
  if (str(f, "decision") === "reject") {
    await run(
      "UPDATE proposals SET status='rejected' WHERE id=$1 AND status='pending'",
      proposalId,
    );
    refresh();
    redirect(target);
  }
  if (p.status !== "pending") back(target, "Предложение уже обработано");
  if (p.moderation !== "approved" || p.fraud)
    back(target, "Сначала одобрите исходный отзыв и снимите отметку фрода");
  try {
    await applyProposal(proposalId, {
      name: str(f, "name"),
      cuisine: str(f, "cuisine"),
      description: str(f, "description"),
      mapsUrl: str(f, "mapsUrl"),
      phone: str(f, "phone").replace(/[ ()-]/g, ""),
      openingHours: str(f, "openingHours")
        ? JSON.parse(str(f, "openingHours"))
        : null,
      sources: [],
    });
    refresh();
  } catch (e) {
    back(
      target,
      "Не применено: " + (e instanceof Error ? e.message : "ошибка"),
    );
  }
  back(target, "Добавлено в черновик. Публикация — на странице локации.");
}
export async function retryJob(f: FormData) {
  await requireAdmin();
  const job = await one(
    "SELECT * FROM jobs WHERE id=$1 AND status='failed'",
    str(f, "id"),
  );
  if (job) {
    await run(
      "UPDATE jobs SET status='queued',error=NULL,started_at=NULL WHERE id=$1",
      job.id,
    );
    if (job.review_id)
      await run(
        "UPDATE reviews SET status='pending',error=NULL WHERE id=$1",
        job.review_id,
      );
  }
  refresh();
  redirect("/admin?tab=fill");
}
export async function cancelJobs() {
  await requireAdmin();
  await run(
    "UPDATE jobs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE status='queued'",
  );
  refresh();
  redirect("/admin?tab=fill");
}
