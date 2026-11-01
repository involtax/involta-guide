import { seedLocations } from "../lib/migrate.mjs";
import { closeDb } from "../lib/db.mjs";
try {
  console.log("Добавлено локаций:", await seedLocations());
} catch (e) {
  console.error(e.message);
  process.exitCode = 1;
} finally {
  await closeDb();
}
