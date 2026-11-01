import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { testDatabase } from "./database.mjs";
const execute = promisify(execFile);
test("параллельные миграции и импорт городов выполняются один раз", async () => {
  const testDb = await testDatabase();
  try {
    const results = await Promise.all(
      Array.from({ length: 6 }, () =>
        execute(
          process.execPath,
          [
            "--input-type=module",
            "-e",
            "const {migrate,seedCities}=await import('./lib/migrate.mjs');const {one,closeDb}=await import('./lib/db.mjs');await migrate();await seedCities();console.log((await one('SELECT count(*) AS n FROM cities')).n);await closeDb();",
          ],
          { cwd: process.cwd(), env: { ...process.env }, timeout: 30000 },
        ),
      ),
    );
    for (const result of results)
      assert.equal(Number(result.stdout.trim()), 1134);
  } finally {
    await testDb.cleanup();
  }
});
