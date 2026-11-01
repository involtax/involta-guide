import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { all, one } from "@/lib/db.mjs";
export async function GET(request: Request) {
  const visitor = (await cookies()).get("guide_visitor")?.value;
  if (!visitor) return NextResponse.json([]);
  const city = new URL(request.url).searchParams.get("city");
  const rows = all(
    "SELECT id,moderation,status,fraud,created_at FROM reviews WHERE visitor=? AND city_id=? ORDER BY created_at DESC LIMIT 30",
    visitor,
    city || "",
  );
  return NextResponse.json(
    rows.map((r) => ({
      ...r,
      proposals: all(
        "SELECT status,kind FROM proposals WHERE review_id=?",
        r.id,
      ),
      published: !!one(
        "SELECT vr.review_id FROM version_reviews vr JOIN cities c ON c.published_version=vr.version_id WHERE vr.review_id=? AND c.id=?",
        r.id,
        city || "",
      ),
    })),
    { headers: { "Cache-Control": "no-store" } },
  );
}
