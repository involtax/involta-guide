import { readFile } from "node:fs/promises";
import { timingSafeEqual } from "node:crypto";
import { join } from "node:path";
import {
  BASE_PATH,
  COOKIE_PATH,
  siteUrl,
  absoluteUrl,
  publicPath,
} from "./urls.mjs";
import { alexSession, createAlexSession } from "./alex.mjs";
import { directoryHtml, locationHtml, sitemapXml } from "./public-pages.mjs";
import { documentHtml } from "./public-html.mjs";
const assets = new Map([
  ["/assets/style.css", ["app/style.css", "text/css; charset=utf-8"]],
  ["/assets/hours.js", ["public/hours.js", "text/javascript; charset=utf-8"]],
  [
    "/assets/hours-core.js",
    ["lib/hours.mjs", "text/javascript; charset=utf-8"],
  ],
  ["/widget.js", ["public/widget.js", "text/javascript; charset=utf-8"]],
]);
function respond(
  req,
  res,
  status,
  body,
  type = "text/html; charset=utf-8",
  extra = {},
) {
  res.writeHead(status, {
    "Content-Type": type,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
    ...extra,
  });
  res.end(req.method === "HEAD" ? undefined : body);
}
export async function publicRequest(req, res) {
  const url = new URL(req.url, "http://internal");
  if (BASE_PATH && url.pathname === BASE_PATH) {
    respond(req, res, 302, "", "text/plain", {
      Location: publicPath("/") + url.search,
    });
    return true;
  }
  if (BASE_PATH && !url.pathname.startsWith(BASE_PATH + "/")) return false;
  const path = url.pathname.slice(BASE_PATH.length) || "/";
  if (
    path === "/admin" ||
    path.startsWith("/admin/") ||
    path === "/alex" ||
    path.startsWith("/alex/") ||
    path.startsWith("/api/") ||
    path.startsWith("/_next/")
  )
    return false;
  if (!["GET", "HEAD"].includes(req.method)) {
    respond(req, res, 405, "Method not allowed", "text/plain", {
      Allow: "GET, HEAD",
    });
    return true;
  }
  try {
    const entries = [...url.searchParams.entries()],
      configured = process.env.ALEX_SECRET;
    if (
      configured?.length >= 32 &&
      entries.length === 1 &&
      entries[0][1] === ""
    ) {
      const a = Buffer.from(entries[0][0]),
        b = Buffer.from(configured);
      if (a.length === b.length && timingSafeEqual(a, b)) {
        const token = await createAlexSession();
        respond(req, res, 302, "", "text/plain", {
          Location: siteUrl() + "/",
          "Set-Cookie": `guide_alex=${token}; Path=${COOKIE_PATH}; HttpOnly; SameSite=Strict; Max-Age=2592000${process.env.COOKIE_SECURE === "true" ? "; Secure" : ""}`,
        });
        return true;
      }
    }
    if (assets.has(path)) {
      const [file, type] = assets.get(path);
      respond(req, res, 200, await readFile(join(process.cwd(), file)), type, {
        "Cache-Control": "public, max-age=300",
      });
      return true;
    }
    if (path === "/robots.txt") {
      respond(
        req,
        res,
        200,
        `User-agent: *\nAllow: ${publicPath("/")}\nDisallow: ${publicPath("/admin")}\nDisallow: ${publicPath("/alex")}\nDisallow: ${publicPath("/api")}\nSitemap: ${absoluteUrl("/sitemap.xml")}\n`,
        "text/plain; charset=utf-8",
      );
      return true;
    }
    if (path === "/sitemap.xml") {
      respond(
        req,
        res,
        200,
        await sitemapXml(),
        "application/xml; charset=utf-8",
      );
      return true;
    }
    if (path === "/") {
      respond(req, res, 200, await directoryHtml(url));
      return true;
    }
    const match = path.match(/^\/([^/]+)\/?$/);
    if (match) {
      const cookie = req.headers.cookie
        ?.split(";")
        .map((s) => s.trim())
        .find((s) => s.startsWith("guide_alex="))
        ?.slice("guide_alex=".length);
      const alex = await alexSession(cookie);
      const page = await locationHtml(decodeURIComponent(match[1]), alex);
      if (page?.redirect) {
        respond(req, res, 308, "", "text/plain", { Location: page.redirect });
        return true;
      }
      if (page) {
        respond(req, res, 200, page.html);
        return true;
      }
    }
    respond(
      req,
      res,
      404,
      documentHtml({
        title: "Локация не найдена",
        index: false,
        body: "<h1>Локация не найдена</h1>",
      }),
    );
    return true;
  } catch (error) {
    console.error("Public request failed:", error.code || error.name);
    respond(
      req,
      res,
      500,
      documentHtml({
        title: "Не удалось открыть справочник",
        index: false,
        body: "<h1>Не удалось открыть справочник</h1><p>Попробуйте немного позже.</p>",
      }),
    );
    return true;
  }
}
