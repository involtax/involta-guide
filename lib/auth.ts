import { cookies, headers } from "next/headers";
import { createHmac, timingSafeEqual } from "node:crypto";
export function secureEqual(a: string, b: string) {
  const x = Buffer.from(a),
    y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}
function signature(value: string) {
  const secret = process.env.SESSION_SECRET;
  if (!secret || secret.length < 32)
    throw new Error("Настройте SESSION_SECRET (минимум 32 символа)");
  return createHmac("sha256", secret).update(value).digest("hex");
}
export function session() {
  const value = String(Date.now() + 8 * 60 * 60 * 1000);
  return `${value}.${signature(value)}`;
}
export async function isAdmin() {
  const token = (await cookies()).get("guide_admin")?.value;
  if (!token) return false;
  const [expiry, sig] = token.split(".");
  try {
    return (
      Number(expiry) > Date.now() && secureEqual(sig || "", signature(expiry))
    );
  } catch {
    return false;
  }
}
export async function requireAdmin() {
  if (!(await isAdmin())) throw new Error("Требуется вход");
}
export async function checkOrigin(request: Request) {
  const origin = request.headers.get("origin");
  const expected = process.env.SITE_URL
    ? new URL(process.env.SITE_URL).origin
    : `${new URL(request.url).protocol}//${request.headers.get("host") || new URL(request.url).host}`;
  if (!origin || origin !== expected)
    throw new Error("Недопустимый источник запроса");
}
export async function requestFingerprint() {
  const h = await headers();
  return createHmac("sha256", process.env.SESSION_SECRET || "local")
    .update(h.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown")
    .digest("hex");
}
