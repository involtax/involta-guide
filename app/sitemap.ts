import type { MetadataRoute } from "next";
import { all } from "@/lib/db.mjs";
export const dynamic = "force-dynamic";
export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const base = process.env.SITE_URL || "http://localhost:3000";
  const rows = await all(`WITH RECURSIVE useful AS (
 SELECT l.id,l.parent_id,v.created_at FROM locations l JOIN versions v ON v.id=l.published_version WHERE l.active=1
 UNION ALL SELECT l.id,l.parent_id,u.created_at FROM locations l JOIN useful u ON l.id=u.parent_id WHERE l.active=1
) SELECT l.slug,max(u.created_at) updated FROM useful u JOIN locations l ON l.id=u.id GROUP BY l.slug`);
  return [
    { url: base + "/guide" },
    ...rows.map((l) => ({
      url: base + "/guide/" + l.slug,
      lastModified: new Date(l.updated + "Z"),
    })),
  ];
}
