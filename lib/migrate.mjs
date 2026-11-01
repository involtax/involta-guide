import { seedGeography, seedMoscowNavigation } from "./location-seed.mjs";
import { readdir, readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join } from "node:path";
import {
  transaction,
  run,
  all,
  one,
  id,
  slugify,
  setSetting,
  defaultPrompt,
} from "./db.mjs";
export async function migrate({ through = undefined } = {}) {
  return transaction(async () => {
    await run("SELECT pg_advisory_xact_lock(47031001)");
    await run(
      "CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())",
    );
    const folder = join(process.cwd(), "migrations");
    const files = (await readdir(folder))
      .filter((f) => f.endsWith(".sql") && (!through || f <= through))
      .sort();
    for (const name of files) {
      const sql = await readFile(join(folder, name), "utf8");
      const checksum = createHash("sha256").update(sql).digest("hex");
      const previous = await one(
        "SELECT checksum FROM schema_migrations WHERE name=$1",
        name,
      );
      if (previous) {
        if (previous.checksum !== checksum)
          throw new Error(
            `Изменена применённая миграция ${name}; добавьте новую миграцию.`,
          );
        continue;
      }
      await run(sql);
      await run(
        "INSERT INTO schema_migrations(name,checksum) VALUES($1,$2)",
        name,
        checksum,
      );
    }
  });
}
async function seedBaseCities() {
  return transaction(async () => {
    await run("SELECT pg_advisory_xact_lock(47031002)");
    if (await one("SELECT value FROM settings WHERE key='cities_seeded'"))
      return 0;
    const rows = JSON.parse(
      await readFile(join(process.cwd(), "data/cities.json"), "utf8"),
    );
    let count = 0;
    const slugs = new Set(
      (await all("SELECT slug FROM locations")).map((c) => c.slug),
    );
    for (const c of rows) {
      if (
        await one(
          "SELECT id FROM locations WHERE name=$1 AND region=$2",
          c.name,
          c.subject,
        )
      )
        continue;
      let slug = slugify(c.name);
      if (slugs.has(slug)) slug += "-" + slugify(c.subject);
      if (slugs.has(slug)) throw new Error("Коллизия URL города: " + c.name);
      slugs.add(slug);
      await run(
        "INSERT INTO locations(id,name,region,slug,population) VALUES($1,$2,$3,$4,$5)",
        id(),
        c.name,
        c.subject,
        slug,
        c.population || 0,
      );
      count++;
    }
    await run(
      "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT DO NOTHING",
      "prompt",
      defaultPrompt,
    );
    await setSetting("cities_seeded", new Date().toISOString());
    return count;
  });
}

export async function seedLocations() {
  return (
    (await seedBaseCities()) +
    (await seedGeography()) +
    (await seedMoscowNavigation())
  );
}
