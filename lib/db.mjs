import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
export const root = process.env.DATA_DIR || join(process.cwd(), "var");
mkdirSync(join(root, "audio"), { recursive: true });
export const db = new DatabaseSync(join(root, "guide.sqlite"));
db.exec("PRAGMA busy_timeout=10000; PRAGMA foreign_keys=ON");
// SQLite may return BUSY immediately when concurrent processes enable WAL.
// Retry only this initialization step, with a bounded deadline.
const initializationDeadline = Date.now() + 10000;
for (;;) {
  try {
    db.exec("PRAGMA journal_mode=WAL");
    break;
  } catch (error) {
    if (
      !error ||
      (Number(error.errcode) & 255) !== 5 ||
      Date.now() >= initializationDeadline
    )
      throw error;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
  }
}
db.exec(`
CREATE TABLE IF NOT EXISTS cities(id TEXT PRIMARY KEY,name TEXT NOT NULL,region TEXT NOT NULL,slug TEXT NOT NULL UNIQUE,population INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,intro TEXT NOT NULL DEFAULT '',draft TEXT NOT NULL DEFAULT '[]',published_version TEXT,draft_version TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS restaurants(id TEXT PRIMARY KEY,city_id TEXT NOT NULL REFERENCES cities(id),name TEXT NOT NULL,cuisine TEXT NOT NULL,description TEXT NOT NULL,maps_url TEXT NOT NULL,sources TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,city_id TEXT NOT NULL REFERENCES cities(id),number INTEGER NOT NULL,content TEXT NOT NULL,prompt TEXT,model TEXT,source TEXT NOT NULL,published INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS version_reviews(version_id TEXT NOT NULL REFERENCES versions(id),review_id TEXT NOT NULL,PRIMARY KEY(version_id,review_id));
CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,city_id TEXT NOT NULL REFERENCES cities(id),visitor TEXT NOT NULL,audio TEXT NOT NULL,mime TEXT NOT NULL,hash TEXT NOT NULL,ip_hash TEXT NOT NULL,device TEXT NOT NULL,transcript TEXT,result TEXT,status TEXT NOT NULL DEFAULT 'pending',moderation TEXT NOT NULL DEFAULT 'pending',fraud INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',error TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS reviews_visitor ON reviews(visitor);
CREATE INDEX IF NOT EXISTS reviews_hash ON reviews(hash);
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES reviews(id),city_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,city_id TEXT,review_id TEXT,prompt TEXT,status TEXT NOT NULL DEFAULT 'queued',error TEXT,attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,started_at TEXT,finished_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,action TEXT NOT NULL,target TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY,count INTEGER NOT NULL,reset_at INTEGER NOT NULL);
`);
/** @returns {Record<string, any>[]} */
export const all = (sql, ...args) => db.prepare(sql).all(...args);
/** @returns {Record<string, any> | undefined} */
export const one = (sql, ...args) => db.prepare(sql).get(...args);
export const run = (sql, ...args) => db.prepare(sql).run(...args);
export const id = () => randomUUID();
export function transaction(fn) {
  db.exec("BEGIN IMMEDIATE");
  try {
    const value = fn();
    db.exec("COMMIT");
    return value;
  } catch (e) {
    db.exec("ROLLBACK");
    throw e;
  }
}
export function audit(action, target, detail = "") {
  run(
    "INSERT INTO audit(id,action,target,detail) VALUES(?,?,?,?)",
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
export const defaultPrompt = `Ты редактор «Инвольта гида». Для города {город}, регион {регион}, подготовь ранжированный топ до {лимит} ресторанов. Используй веб-поиск, проверь существование и актуальность каждого заведения, принадлежность городу и ссылку на Яндекс Карты. Для каждого дай название, кухню, короткое конкретное объяснение, почему стоит зайти, ссылку на Яндекс Карты и URL источников. Без фото и среднего чека. Не выдумывай факты. Если данных недостаточно, верни меньше заведений и объясни сомнения в warnings. intro — короткое вступление, без рекламных штампов. Возвращай только JSON по схеме.`;
export function setting(key, fallback = "") {
  return one("SELECT value FROM settings WHERE key=?", key)?.value ?? fallback;
}
export function setSetting(key, value) {
  run(
    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    key,
    value,
  );
}
if (!one("SELECT id FROM cities LIMIT 1"))
  transaction(() => {
    if (one("SELECT id FROM cities LIMIT 1")) return;
    const rows = JSON.parse(
      readFileSync(join(process.cwd(), "data/cities.json"), "utf8"),
    );
    for (const c of rows) {
      let slug = slugify(c.name);
      if (one("SELECT id FROM cities WHERE slug=?", slug))
        slug += "-" + slugify(c.subject);
      run(
        "INSERT INTO cities(id,name,region,slug,population) VALUES(?,?,?,?,?)",
        id(),
        c.name,
        c.subject,
        slug,
        c.population || 0,
      );
    }
    setSetting("prompt", defaultPrompt);
  });
export function cityById(cityId) {
  const c = one("SELECT * FROM cities WHERE id=?", cityId);
  if (!c) throw new Error("Город не найден");
  return c;
}
export function cityContent(cityId) {
  const c = cityById(cityId);
  return { intro: c.intro, restaurants: JSON.parse(c.draft) };
}
export function saveDraft(
  cityId,
  content,
  source = "manual",
  prompt = null,
  model = null,
) {
  return transaction(() => {
    const previous = cityById(cityId).draft_version;
    const versionId = id();
    const number = Number(
      one(
        "SELECT COALESCE(MAX(number),0)+1 AS n FROM versions WHERE city_id=?",
        cityId,
      ).n,
    );
    const restaurants = content.restaurants.map((r) => ({
      ...r,
      id: r.id || id(),
    }));
    for (const r of restaurants)
      run(
        `INSERT INTO restaurants(id,city_id,name,cuisine,description,maps_url,sources) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,cuisine=excluded.cuisine,description=excluded.description,maps_url=excluded.maps_url,sources=excluded.sources`,
        r.id,
        cityId,
        r.name,
        r.cuisine,
        r.description,
        r.mapsUrl,
        JSON.stringify(r.sources || []),
      );
    const normalized = { intro: content.intro, restaurants };
    run(
      "UPDATE cities SET intro=?,draft=?,draft_version=? WHERE id=?",
      content.intro,
      JSON.stringify(restaurants),
      versionId,
      cityId,
    );
    run(
      "INSERT INTO versions(id,city_id,number,content,prompt,model,source) VALUES(?,?,?,?,?,?,?)",
      versionId,
      cityId,
      number,
      JSON.stringify(normalized),
      prompt,
      model,
      source,
    );
    if (previous && source !== "ai")
      run(
        "INSERT OR IGNORE INTO version_reviews(version_id,review_id) SELECT ?,review_id FROM version_reviews WHERE version_id=?",
        versionId,
        previous,
      );
    audit("draft", cityId, source);
    return versionId;
  });
}
export function publish(cityId, versionId) {
  transaction(() => {
    const v = one(
      "SELECT * FROM versions WHERE id=? AND city_id=?",
      versionId,
      cityId,
    );
    if (!v) throw new Error("Версия не найдена");
    const parsed = JSON.parse(v.content);
    if (!parsed.restaurants.length)
      throw new Error("Нельзя опубликовать пустую подборку");
    run("UPDATE versions SET published=1 WHERE id=?", versionId);
    run("UPDATE cities SET published_version=? WHERE id=?", versionId, cityId);
    audit("publish", cityId, versionId);
  });
}
export function queueGeneration(cityId, prompt) {
  if (
    one(
      "SELECT id FROM jobs WHERE city_id=? AND kind='generate' AND status IN ('queued','running')",
      cityId,
    )
  )
    return false;
  run(
    "INSERT INTO jobs(id,kind,city_id,prompt) VALUES(?,'generate',?,?)",
    id(),
    cityId,
    prompt,
  );
  return true;
}
