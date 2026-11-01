import { isAdmin } from "@/lib/auth";
import { all } from "@/lib/db.mjs";
export async function GET() {
  if (!(await isAdmin())) return new Response("Unauthorized", { status: 401 });
  const rows = await all(
    "SELECT id,name,region,slug,kind,parent_id,is_hub,in_phrase,aliases,priority,top_limit,active,listed,population,population_verified,population_basis,coverage_note,latitude,longitude,bounds FROM locations ORDER BY region,name",
  );
  return Response.json(
    rows.map((l) => ({ ...l, bounds: l.bounds ? JSON.parse(l.bounds) : null })),
    {
      headers: {
        "Content-Disposition": 'attachment; filename="involta-locations.json"',
        "Cache-Control": "private, no-store",
      },
    },
  );
}
