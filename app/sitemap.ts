import type { MetadataRoute } from "next";
import { all } from "@/lib/db.mjs";
export const dynamic = "force-dynamic";
export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const base = process.env.SITE_URL || "http://localhost:3000";
  return [
    { url: base + "/guide" },
    ...(
      await all(
        "SELECT c.slug,v.created_at FROM cities c JOIN versions v ON v.id=c.published_version WHERE c.active=1",
      )
    ).map((c) => ({
      url: `${base}/guide/${c.slug}`,
      lastModified: new Date(c.created_at + "Z"),
    })),
  ];
}
