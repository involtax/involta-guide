import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const execute = promisify(execFile);
test("одновременный старт процессов импортирует города только один раз", async () => {
  const directory = mkdtempSync(join(tmpdir(), "involta-concurrent-"));
  try {
    const results = await Promise.all(
      Array.from({ length: 6 }, () =>
        execute(
          process.execPath,
          [
            "--input-type=module",
            "-e",
            "const {db,one}=await import('./lib/db.mjs'); console.log(one('SELECT count(*) AS n FROM cities').n); db.close();",
          ],
          {
            cwd: process.cwd(),
            env: { ...process.env, DATA_DIR: directory },
            timeout: 20000,
          },
        ),
      ),
    );
    for (const result of results)
      assert.equal(Number(result.stdout.trim()), 1134);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
