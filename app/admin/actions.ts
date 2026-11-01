"use server";
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
  cityById,
  queueGeneration,
  setSetting,
  setting,
} from "@/lib/db.mjs";
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
  revalidatePath("/guide", "layout");
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
    path: "/",
    maxAge: 28800,
  });
  redirect("/admin");
}
export async function logout() {
  (await cookies()).delete("guide_admin");
  redirect("/admin");
}
export async function saveCity(f: FormData) {
  await requireAdmin();
  let cityId = str(f, "id");
  try {
    const name = z.string().min(1).max(150).parse(str(f, "name"));
    const region = z.string().min(1).max(200).parse(str(f, "region"));
    const slug = z
      .string()
      .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/)
      .max(200)
      .parse(str(f, "slug") || slugify(name));
    const population = z.coerce
      .number()
      .int()
      .min(0)
      .max(100000000)
      .parse(str(f, "population") || 0);
    if (cityId) {
      await cityById(cityId);
      await run(
        "UPDATE cities SET name=$1,region=$2,slug=$3,population=$4,active=$5 WHERE id=$6",
        name,
        region,
        slug,
        population,
        f.get("active") === "on" ? 1 : 0,
        cityId,
      );
    } else {
      cityId = id();
      await run(
        "INSERT INTO cities(id,name,region,slug,population) VALUES($1,$2,$3,$4,$5)",
        cityId,
        name,
        region,
        slug,
        population,
      );
    }
    await audit("city", cityId);
    refresh();
  } catch (e) {
    back(
      "/admin",
      e instanceof Error && /unique|duplicate/i.test(e.message)
        ? "Такой URL уже занят"
        : "Проверьте название, регион, URL и население",
    );
  }
  redirect("/admin?tab=cities&city=" + cityId);
}
export async function saveContent(f: FormData) {
  await requireAdmin();
  const cityId = str(f, "cityId");
  try {
    const content = contentSchema.parse(JSON.parse(str(f, "content")));
    await saveDraft(cityId, content);
    refresh();
  } catch (e) {
    back(
      "/admin?tab=cities&city=" + cityId,
      "Не сохранено: " + (e instanceof Error ? e.message : "ошибка"),
    );
  }
  back(
    "/admin?tab=cities&city=" + cityId,
    "Черновик сохранён. Для публикации нажмите отдельную кнопку.",
  );
}
export async function publishVersion(f: FormData) {
  await requireAdmin();
  const cityId = str(f, "cityId");
  try {
    await publish(cityId, str(f, "versionId"));
    refresh();
  } catch (e) {
    back(
      "/admin?tab=cities&city=" + cityId,
      e instanceof Error ? e.message : "Ошибка",
    );
  }
  back("/admin?tab=cities&city=" + cityId, "Версия опубликована");
}
export async function restoreVersion(f: FormData) {
  await requireAdmin();
  const cityId = str(f, "cityId");
  const versionId = str(f, "versionId");
  await restorePageVersion(cityId, versionId);
  refresh();
  redirect("/admin?tab=cities&city=" + cityId);
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
  const cityId = str(f, "cityId");
  let count = 0;
  await transaction(async () => {
    if (cityId) {
      await cityById(cityId);
      if (await queueGeneration(cityId, prompt)) count++;
    } else {
      const scope = str(f, "scope");
      if (!["empty", "all"].includes(scope))
        throw new Error("Выберите область");
      for (const c of await all(
        "SELECT id FROM cities WHERE active=1" +
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
      sources: [],
    });
    refresh();
  } catch (e) {
    back(
      target,
      "Не применено: " + (e instanceof Error ? e.message : "ошибка"),
    );
  }
  back(target, "Добавлено в черновик. Публикация — на странице города.");
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
