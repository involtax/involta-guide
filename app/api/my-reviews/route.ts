import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { all, one } from "@/lib/db.mjs";
export async function GET(request: Request) {
  const visitor = (await cookies()).get("guide_visitor")?.value;
  if (!visitor) return NextResponse.json([]);
  const city = new URL(request.url).searchParams.get("city");
  if (!city || !/^[a-f0-9-]{36}$/.test(city)) return NextResponse.json([]);
  const rows = await all(
    "SELECT id,moderation,status,fraud,created_at FROM reviews WHERE visitor=$1 AND city_id=$2 ORDER BY created_at DESC LIMIT 30",
    visitor,
    city || "",
  );
  return NextResponse.json(
    await Promise.all(
      rows.map(async (r) => ({
        ...r,
        proposals: await all(
          "SELECT status,kind FROM proposals WHERE review_id=$1",
          r.id,
        ),
        published: !!(await one(
          "SELECT vr.review_id FROM version_reviews vr JOIN cities c ON c.published_version=vr.version_id WHERE vr.review_id=$1 AND c.id=$2",
          r.id,
          city || "",
        )),
      })),
    ),
    { headers: { "Cache-Control": "no-store" } },
  );
}
