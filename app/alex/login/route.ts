import { NextResponse } from "next/server";
import { secureEqual, requestFingerprint } from "@/lib/auth";
import { createAlexSession } from "@/lib/alex.mjs";
import { one, run } from "@/lib/db.mjs";
export async function GET(request: Request) {
  const url = new URL(request.url);
  const key = "alex:" + (await requestFingerprint());
  const attempt = await one("SELECT * FROM login_attempts WHERE key=$1", key);
  if (attempt && Number(attempt.reset_at) > Date.now() && attempt.count >= 10)
    return new NextResponse("Повторите вход через 15 минут", { status: 429 });
  const configured = process.env.ALEX_ACCESS_TOKEN;
  const token = url.searchParams.get("token") || "";
  if (
    !configured ||
    configured.length < 32 ||
    !secureEqual(token, configured)
  ) {
    await run(
      "INSERT INTO login_attempts(key,count,reset_at) VALUES($1,1,$2) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN login_attempts.reset_at<$3 THEN 1 ELSE login_attempts.count+1 END, reset_at=CASE WHEN login_attempts.reset_at<$3 THEN excluded.reset_at ELSE login_attempts.reset_at END",
      key,
      Date.now() + 900000,
      Date.now(),
    );
    return new NextResponse("Недействительная ссылка", {
      status: 403,
      headers: {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
      },
    });
  }
  const session = await createAlexSession();
  const response = NextResponse.redirect(
    new URL("/alex", process.env.SITE_URL || url.origin),
  );
  response.cookies.set("guide_alex", session, {
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.COOKIE_SECURE === "true",
    path: "/",
    maxAge: 30 * 86400,
  });
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}
