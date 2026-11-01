import { createServer } from "node:http";
import next from "next";
import { publicRequest } from "../lib/public-server.mjs";
import { closeDb } from "../lib/db.mjs";
const dev = process.argv.includes("--dev");
const port = Number(process.env.PORT || 3000),
  hostname = process.env.HOST || "0.0.0.0";
const admin = next({ dev, hostname, port });
await admin.prepare();
const handleAdmin = admin.getRequestHandler();
const server = createServer(async (req, res) => {
  try {
    if (!(await publicRequest(req, res))) await handleAdmin(req, res);
  } catch {
    if (!res.headersSent) res.writeHead(500);
    res.end("Не удалось обработать запрос");
  }
});
server.requestTimeout = 240000;
server.headersTimeout = 30000;
server.listen(port, hostname, () =>
  console.log(`Involta Guide: HTML + admin listening on ${hostname}:${port}`),
);
let closing = false;
async function shutdown() {
  if (closing) return;
  closing = true;
  server.close(async () => {
    await closeDb();
    await admin.close();
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 10000).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
