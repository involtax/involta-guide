import { createHash, randomBytes } from "node:crypto";
import { one, run, id, audit } from "./db.mjs";
export const tokenHash = (value) =>
  createHash("sha256").update(value).digest("hex");
export async function alexSession(token) {
  if (!token || !/^[a-f0-9]{64}$/.test(token)) return null;
  return await one(
    "SELECT * FROM alex_sessions WHERE token_hash=$1 AND NOT revoked AND expires_at>now()",
    tokenHash(token),
  );
}
export async function createAlexSession() {
  const token = randomBytes(32).toString("hex");
  const sessionId = id();
  await run(
    "INSERT INTO alex_sessions(id,token_hash,expires_at) VALUES($1,$2,now()+interval '30 days')",
    sessionId,
    tokenHash(token),
  );
  await audit("alex_login", sessionId, "Вход по персональной ссылке");
  return token;
}
