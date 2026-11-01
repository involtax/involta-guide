import { seedCities } from "../lib/migrate.mjs";
import { closeDb } from "../lib/db.mjs";
try {
  console.log("Добавлено городов:", await seedCities());
} catch (e) {
  console.error(e.message);
  process.exitCode = 1;
} finally {
  await closeDb();
}
