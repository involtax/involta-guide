import { readFile, writeFile } from "node:fs/promises";
import { randomBytes } from "node:crypto";
let original = "";
try {
  original = await readFile(".env.local", "utf8");
} catch (e) {
  if (e.code !== "ENOENT") throw e;
}
const values = new Map(
  original.split("\n").map((line) => {
    const split = line.indexOf("=");
    return split < 0
      ? ["", ""]
      : [line.slice(0, split).trim(), line.slice(split + 1).trim()];
  }),
);
const additions = [];
const password =
  values.get("POSTGRES_PASSWORD") || randomBytes(24).toString("hex");
if (!values.get("POSTGRES_PASSWORD"))
  additions.push(["POSTGRES_PASSWORD", password]);
if (!values.get("DATABASE_URL"))
  additions.push([
    "DATABASE_URL",
    `postgresql://guide:${encodeURIComponent(password)}@127.0.0.1:5432/guide`,
  ]);
if (!values.get("TEST_DATABASE_URL"))
  additions.push([
    "TEST_DATABASE_URL",
    `postgresql://guide:${encodeURIComponent(password)}@127.0.0.1:5432/guide`,
  ]);
if (!values.get("SESSION_SECRET"))
  additions.push(["SESSION_SECRET", randomBytes(48).toString("hex")]);
if (!values.get("SITE_URL"))
  additions.push(["SITE_URL", "http://localhost:3000/guide"]);
let updated = original;
for (const [key, value] of additions) {
  const pattern = new RegExp(`^${key}=.*$`, "m");
  if (pattern.test(updated))
    updated = updated.replace(pattern, `${key}=${value}`);
  else
    updated +=
      (updated.endsWith("\n") || !updated ? "" : "\n") + `${key}=${value}\n`;
}
if (additions.length) await writeFile(".env.local", updated, { mode: 0o600 });
console.log(
  "Локальное подключение PostgreSQL настроено в игнорируемом .env.local. Секреты не выводятся. Задайте ADMIN_PASSWORD отдельно. OPENAI_API_KEY берётся только из серверного окружения.",
);
