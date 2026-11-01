import pg from "pg";
import { AsyncLocalStorage } from "node:async_hooks";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
// Retain JSON as serialized documents at this boundary; PostgreSQL validates JSONB.
pg.types.setTypeParser(20, Number);
pg.types.setTypeParser(114, (value) => value);
pg.types.setTypeParser(3802, (value) => value);
pg.types.setTypeParser(1184, (value) =>
  new Date(value).toISOString().replace("T", " ").replace("Z", ""),
);
export const root = process.env.DATA_DIR || join(process.cwd(), "var");
mkdirSync(join(root, "audio"), { recursive: true });
const context = new AsyncLocalStorage();
let pool;
function validatedSchema() {
  const schema = process.env.DATABASE_SCHEMA;
  if (!/^[a-z_][a-z0-9_]{0,62}$/.test(schema || ""))
    throw new Error("Недопустимое имя схемы PostgreSQL");
  return schema;
}
export function getPool() {
  if (!process.env.DATABASE_URL)
    throw new Error(
      "DATABASE_URL не настроен. Укажите подключение к PostgreSQL и выполните npm run db:migrate и npm run db:seed.",
    );
  if (!pool) {
    pool = new pg.Pool({
      connectionString: process.env.DATABASE_URL,
      max: 10,
      ...(process.env.DATABASE_SCHEMA
        ? { options: `-c search_path=${validatedSchema()}` }
        : {}),
      connectionTimeoutMillis: 5000,
      idleTimeoutMillis: 30000,
      statement_timeout: 30000,
    });
    pool.on("error", (error) =>
      console.error(
        "PostgreSQL: потеряно простаивающее подключение",
        error.code || "connection error",
      ),
    );
  }
  return pool;
}
/** @returns {Promise<Record<string, any>[]>} */
export async function all(sql, ...args) {
  return (await (context.getStore() || getPool()).query(sql, args)).rows;
}
/** @returns {Promise<Record<string, any> | undefined>} */
export async function one(sql, ...args) {
  return (await all(sql, ...args))[0];
}
export async function run(sql, ...args) {
  return (await (context.getStore() || getPool()).query(sql, args)).rowCount;
}
export const id = () => randomUUID();
export async function transaction(fn) {
  if (context.getStore()) return fn();
  const client = await getPool().connect();
  try {
    await client.query("BEGIN");
    const result = await context.run(client, fn);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
export async function closeDb() {
  if (pool) {
    await pool.end();
    pool = undefined;
  }
}
export async function audit(action, target, detail = "") {
  await run(
    "INSERT INTO audit(id,action,target,detail) VALUES($1,$2,$3,$4)",
    id(),
    action,
    target,
    detail,
  );
}
export function slugify(text) {
  const alphabet = {
    а: "a",
    б: "b",
    в: "v",
    г: "g",
    д: "d",
    е: "e",
    ё: "yo",
    ж: "zh",
    з: "z",
    и: "i",
    й: "y",
    к: "k",
    л: "l",
    м: "m",
    н: "n",
    о: "o",
    п: "p",
    р: "r",
    с: "s",
    т: "t",
    у: "u",
    ф: "f",
    х: "kh",
    ц: "ts",
    ч: "ch",
    ш: "sh",
    щ: "shch",
    ъ: "",
    ы: "y",
    ь: "",
    э: "e",
    ю: "yu",
    я: "ya",
  };
  return [...text.toLowerCase()]
    .map((c) => alphabet[c] ?? c)
    .join("")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}
export const defaultPrompt = readFileSync(
  join(process.cwd(), "data/prompts/restaurant-ranking-v1.txt"),
  "utf8",
);

export async function setting(key, fallback = "") {
  return (
    (await one("SELECT value FROM settings WHERE key=$1", key))?.value ??
    fallback
  );
}
export async function setSetting(key, value) {
  await run(
    "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    key,
    value,
  );
}
export async function locationById(locationId) {
  const c = await one("SELECT * FROM locations WHERE id=$1", locationId);
  if (!c) throw new Error("Локация не найдена");
  return c;
}
export async function locationContent(locationId) {
  const c = await locationById(locationId);
  if (c.draft_version) {
    const version = await one(
      "SELECT content FROM versions WHERE id=$1",
      c.draft_version,
    );
    if (version) return JSON.parse(version.content);
  }
  return { intro: c.intro, restaurants: JSON.parse(c.draft) };
}
export async function saveDraft(
  locationId,
  content,
  source = "manual",
  prompt = null,
  model = null,
  expectedVersion = undefined,
) {
  return transaction(async () => {
    const city = await one(
      "SELECT * FROM locations WHERE id=$1 FOR UPDATE",
      locationId,
    );
    if (!city) throw new Error("Локация не найдена");
    if (expectedVersion !== undefined && city.draft_version !== expectedVersion)
      throw new Error(
        "Черновик изменён во время обработки. Обновите страницу и повторите действие.",
      );
    const versionId = id();
    const number = (
      await one(
        "SELECT COALESCE(MAX(number),0)+1 AS n FROM versions WHERE location_id=$1",
        locationId,
      )
    ).n;
    const restaurants = content.restaurants.map((r) => ({
      ...r,
      id: r.id || id(),
    }));
    for (const r of restaurants) {
      const existing = await one(
        "SELECT location_id FROM restaurants WHERE id=$1",
        r.id,
      );
      if (existing && existing.location_id !== locationId) {
        const { restaurantAllowed } = await import("./locations.mjs");
        if (!(await restaurantAllowed(r.id, locationId)))
          throw new Error("Сначала свяжите ресторан с этой локацией");
        r.locationId = existing.location_id;
        r.locationName = (
          await one(
            "SELECT name FROM locations WHERE id=$1",
            existing.location_id,
          )
        ).name;
        continue;
      }
      r.locationId = locationId;
      r.locationName = city.name;
      await run(
        "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,sources,latitude,longitude) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(id) DO UPDATE SET name=excluded.name,cuisine=excluded.cuisine,description=excluded.description,maps_url=excluded.maps_url,sources=excluded.sources,latitude=excluded.latitude,longitude=excluded.longitude",
        r.id,
        locationId,
        r.name,
        r.cuisine,
        r.description,
        r.mapsUrl,
        JSON.stringify(r.sources || []),
        r.latitude ?? null,
        r.longitude ?? null,
      );
    }
    for (const r of restaurants)
      await run(
        "INSERT INTO restaurant_locations(restaurant_id,location_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
        r.id,
        locationId,
      );
    await run(
      "INSERT INTO versions(id,location_id,number,content,prompt,model,source) VALUES($1,$2,$3,$4,$5,$6,$7)",
      versionId,
      locationId,
      number,
      JSON.stringify({
        intro: content.intro,
        restaurants,
        ...(content.rankingReport
          ? { rankingReport: content.rankingReport }
          : {}),
      }),
      prompt,
      model,
      source,
    );
    await run(
      "UPDATE locations SET intro=$1,draft=$2,draft_version=$3 WHERE id=$4",
      content.intro,
      JSON.stringify(restaurants),
      versionId,
      locationId,
    );
    if (city.draft_version && source !== "ai")
      await run(
        "INSERT INTO version_reviews(version_id,review_id) SELECT $1,review_id FROM version_reviews WHERE version_id=$2 ON CONFLICT DO NOTHING",
        versionId,
        city.draft_version,
      );
    await audit("draft", locationId, source);
    return versionId;
  });
}
export async function publish(locationId, versionId) {
  await transaction(async () => {
    const v = await one(
      "SELECT * FROM versions WHERE id=$1 AND location_id=$2",
      versionId,
      locationId,
    );
    if (!v) throw new Error("Версия не найдена");
    if (!JSON.parse(v.content).restaurants.length)
      throw new Error("Нельзя опубликовать пустую подборку");
    await run("UPDATE versions SET published=1 WHERE id=$1", versionId);
    await run(
      "UPDATE locations SET published_version=$1 WHERE id=$2",
      versionId,
      locationId,
    );
    await audit("publish", locationId, versionId);
  });
}
export async function queueGeneration(locationId, prompt) {
  return !!(await one(
    "INSERT INTO jobs(id,kind,location_id,prompt) VALUES($1,'generate',$2,$3) ON CONFLICT DO NOTHING RETURNING id",
    id(),
    locationId,
    prompt,
  ));
}
export async function claimJob() {
  return one(
    "UPDATE jobs SET status='running',attempts=attempts+1,started_at=now() WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
  );
}
