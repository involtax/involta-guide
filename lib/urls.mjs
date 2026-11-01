// Must match the value passed to next build; Next.js embeds basePath in client assets.
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "/guide";
export const COOKIE_PATH = BASE_PATH || "/";
export function publicPath(path = "/") {
  return BASE_PATH + (path === "/" ? "/" : path);
}
export function siteUrl() {
  const configured =
    process.env.SITE_URL || `http://localhost:3000${BASE_PATH}`;
  const url = new URL(configured);
  const path = url.pathname.replace(/\/+$/, "");
  if (path && path !== BASE_PATH)
    throw new Error("Путь SITE_URL должен совпадать с NEXT_PUBLIC_BASE_PATH");
  return url.origin + BASE_PATH;
}
export function absoluteUrl(path = "/") {
  return siteUrl() + (path === "/" ? "/" : path);
}
