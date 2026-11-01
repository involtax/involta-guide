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
  cityContent,
  cityById,
  queueGeneration,
  setSetting,
  setting,
} from "@/lib/db.mjs";
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
  const attempt = one("SELECT * FROM login_attempts WHERE key=?", key);
  if (attempt && attempt.reset_at > Date.now() && attempt.count >= 10)
    back("/admin", "Слишком много попыток. Повторите через 15 минут.");
  const configured = process.env.ADMIN_PASSWORD;
  if (!configured || configured.length < 12)
    back(
      "/admin",
      "Настройте ADMIN_PASSWORD (от 12 символов) и SESSION_SECRET на сервере.",
    );
  if (!secureEqual(str(f, "password"), configured)) {
    run(
      "INSERT INTO login_attempts(key,count,reset_at) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN reset_at<? THEN 1 ELSE count+1 END,reset_at=CASE WHEN reset_at<? THEN excluded.reset_at ELSE reset_at END",
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
  run("DELETE FROM login_attempts WHERE key=?", key);
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
      cityById(cityId);
      run(
        "UPDATE cities SET name=?,region=?,slug=?,population=?,active=? WHERE id=?",
        name,
        region,
        slug,
        population,
        f.get("active") === "on" ? 1 : 0,
        cityId,
      );
    } else {
      cityId = id();
      run(
        "INSERT INTO cities(id,name,region,slug,population) VALUES(?,?,?,?,?)",
        cityId,
        name,
        region,
        slug,
        population,
      );
    }
    audit("city", cityId);
    refresh();
  } catch (e) {
    back(
      "/admin",
      e instanceof Error && e.message.includes("UNIQUE")
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
    saveDraft(cityId, content);
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
    publish(cityId, str(f, "versionId"));
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
  const v = one(
    "SELECT * FROM versions WHERE id=? AND city_id=?",
    versionId,
    cityId,
  );
  if (!v) throw new Error("Версия не найдена");
  const newId = saveDraft(
    cityId,
    contentSchema.parse(JSON.parse(v.content)),
    "restore",
  );
  transaction(() => {
    run("DELETE FROM version_reviews WHERE version_id=?", newId);
    run(
      "INSERT INTO version_reviews(version_id,review_id) SELECT ?,review_id FROM version_reviews WHERE version_id=?",
      newId,
      versionId,
    );
  });
  refresh();
  redirect("/admin?tab=cities&city=" + cityId);
}
export async function savePrompt(f: FormData) {
  await requireAdmin();
  const prompt = z.string().min(50).max(16000).parse(str(f, "prompt"));
  setSetting("prompt", prompt);
  refresh();
  back("/admin?tab=fill", "Промпт сохранён");
}
export async function generate(f: FormData) {
  await requireAdmin();
  const prompt = setting("prompt");
  const cityId = str(f, "cityId");
  let count = 0;
  transaction(() => {
    if (cityId) {
      cityById(cityId);
      if (queueGeneration(cityId, prompt)) count++;
    } else {
      const scope = str(f, "scope");
      if (!["empty", "all"].includes(scope))
        throw new Error("Выберите область");
      for (const c of all(
        "SELECT id FROM cities WHERE active=1" +
          (scope === "empty" ? " AND draft='[]'" : ""),
      )) {
        if (queueGeneration(c.id, prompt)) count++;
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
  transaction(() => {
    const r = one("SELECT * FROM reviews WHERE id=?", reviewId);
    if (!r) throw new Error("Отзыв не найден");
    run(
      "UPDATE reviews SET moderation=?,fraud=?,reason=? WHERE id=?",
      decision === "fraud"
        ? "rejected"
        : decision === "reset"
          ? "pending"
          : decision,
      decision === "fraud" ? 1 : 0,
      reason,
      reviewId,
    );
    audit("review_" + decision, reviewId, reason);
  });
  refresh();
  redirect("/admin?tab=reviews&review=" + reviewId);
}
export async function proposalDecision(f: FormData) {
  await requireAdmin();
  const proposalId = str(f, "id");
  const p = one(
    "SELECT p.*,r.moderation,r.fraud FROM proposals p JOIN reviews r ON r.id=p.review_id WHERE p.id=?",
    proposalId,
  );
  if (!p) throw new Error("Предложение не найдено");
  const target = "/admin?tab=reviews&review=" + p.review_id;
  if (str(f, "decision") === "reject") {
    run(
      "UPDATE proposals SET status='rejected' WHERE id=? AND status='pending'",
      proposalId,
    );
    refresh();
    redirect(target);
  }
  if (p.status !== "pending") back(target, "Предложение уже обработано");
  if (p.moderation !== "approved" || p.fraud)
    back(target, "Сначала одобрите исходный отзыв и снимите отметку фрода");
  try {
    const draft = cityContent(p.city_id);
    const payload = JSON.parse(p.payload);
    const edited = {
      name: str(f, "name"),
      cuisine: str(f, "cuisine"),
      description: str(f, "description"),
      mapsUrl: str(f, "mapsUrl"),
      sources: [],
    };
    if (p.kind === "restaurant") draft.restaurants.push(edited);
    else {
      const index = draft.restaurants.findIndex(
        (r: any) => r.id === payload.restaurantId,
      );
      if (index < 0)
        throw new Error(
          "Ресторан не определён или отсутствует в черновике. Внесите правку на странице города.",
        );
      draft.restaurants[index] = { ...draft.restaurants[index], ...edited };
    }
    const parsed = contentSchema.parse(draft);
    const versionId = saveDraft(p.city_id, parsed, "review");
    transaction(() => {
      run(
        "INSERT OR IGNORE INTO version_reviews(version_id,review_id) VALUES(?,?)",
        versionId,
        p.review_id,
      );
      run("UPDATE proposals SET status='approved' WHERE id=?", proposalId);
      audit("proposal_draft", p.city_id, p.review_id);
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
  const job = one(
    "SELECT * FROM jobs WHERE id=? AND status='failed'",
    str(f, "id"),
  );
  if (job) {
    run(
      "UPDATE jobs SET status='queued',error=NULL,started_at=NULL WHERE id=?",
      job.id,
    );
    if (job.review_id)
      run(
        "UPDATE reviews SET status='pending',error=NULL WHERE id=?",
        job.review_id,
      );
  }
  refresh();
  redirect("/admin?tab=fill");
}
export async function cancelJobs() {
  await requireAdmin();
  run(
    "UPDATE jobs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE status='queued'",
  );
  refresh();
  redirect("/admin?tab=fill");
}
