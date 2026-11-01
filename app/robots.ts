import type { MetadataRoute } from "next";
import { publicPath, absoluteUrl } from "@/lib/urls.mjs";
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: publicPath("/"),
      disallow: [publicPath("/admin"), publicPath("/api"), publicPath("/alex")],
    },
    sitemap: absoluteUrl("/sitemap.xml"),
  };
}
