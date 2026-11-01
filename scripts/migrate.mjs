import { migrate } from "../lib/migrate.mjs";
import { closeDb } from "../lib/db.mjs";
try {
  await migrate();
  console.log("Миграции PostgreSQL применены.");
} catch (e) {
  console.error(e.message);
  process.exitCode = 1;
} finally {
  await closeDb();
}
