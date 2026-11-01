import { NextRequest, NextResponse } from "next/server";
import { timingSafeEqual } from "node:crypto";
import { createAlexSession } from "./lib/alex.mjs";
import { BASE_PATH, COOKIE_PATH, siteUrl } from "./lib/urls.mjs";
export async function proxy(request: NextRequest) {
  const incoming = new URL(request.url);
  if (BASE_PATH && incoming.pathname === BASE_PATH) {
    const destination = new URL(siteUrl() + "/");
    destination.search = incoming.search;
    const response = NextResponse.redirect(destination, 302);
    response.headers.set("Cache-Control", "no-store");
    response.headers.set("Referrer-Policy", "no-referrer");
    return response;
  }
  const configured = process.env.ALEX_SECRET;
  const entries = [...request.nextUrl.searchParams.entries()];
  // Next.js may normalize ?SECRET to ?SECRET= during slash normalization.
  // Accept exactly one empty-value key; q=… and other named parameters never grant access.
  if (
    !configured ||
    configured.length < 32 ||
    entries.length !== 1 ||
    entries[0][1] !== ""
  )
    return NextResponse.next();
  const secret = entries[0][0];
  const a = Buffer.from(secret),
    b = Buffer.from(configured);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    const response = NextResponse.next();
    response.headers.set("Referrer-Policy", "no-referrer");
    return response;
  }
  const session = await createAlexSession();
  const response = NextResponse.redirect(siteUrl() + "/");
  response.cookies.set("guide_alex", session, {
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.COOKIE_SECURE === "true",
    path: COOKIE_PATH,
    maxAge: 30 * 86400,
  });
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}
export const config = { matcher: ["/:path*"] };
