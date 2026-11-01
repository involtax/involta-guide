import pg from "pg";
import { randomBytes } from "node:crypto";
export async function testDatabase() {
  const url = process.env.TEST_DATABASE_URL;
  if (!url)
    throw new Error(
      "TEST_DATABASE_URL не задан. Тестам нужен отдельный PostgreSQL; см. README.",
    );
  const schema = "test_" + randomBytes(12).toString("hex");
  const admin = new pg.Client({ connectionString: url });
  await admin.connect();
  await admin.query(`CREATE SCHEMA "${schema}"`);
  process.env.DATABASE_URL = url;
  process.env.DATABASE_SCHEMA = schema;
  return {
    schema,
    async cleanup() {
      await admin.query(`DROP SCHEMA "${schema}" CASCADE`);
      await admin.end();
    },
  };
}
