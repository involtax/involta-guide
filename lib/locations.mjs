import { z } from "zod";
import { all, one, run, id, transaction, audit, slugify } from "./db.mjs";
export const LOCATION_KINDS = {
  city: "Город",
  settlement: "Населённый пункт",
  district: "Район",
  neighbourhood: "Городская зона",
  resort: "Курорт",
  tourist_area: "Туристическая территория",
  federal_territory: "Федеральная территория",
  destination: "Направление",
};
export const locationInput = z.object({
  name: z.string().trim().min(1).max(150),
  region: z.string().trim().min(1).max(200),
  slug: z
    .string()
    .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/)
    .max(200),
  kind: z.enum(Object.keys(LOCATION_KINDS)),
  parentId: z.string().uuid().nullable(),
  isHub: z.boolean(),
  inPhrase: z.string().trim().min(3).max(200),
  aliases: z.array(z.string().trim().min(1).max(150)).max(30),
  priority: z.enum(["high", "medium", "low"]),
  topLimit: z.number().int().min(1).max(10),
  active: z.boolean(),
  population: z.number().int().min(0).max(100000000).nullable(),
  populationVerified: z.boolean(),
  latitude: z.number().min(-90).max(90).nullable(),
  longitude: z.number().min(-180).max(180).nullable(),
  coverageNote: z.string().max(3000),
  bounds: z
    .object({
      west: z.number().min(-180).max(180),
      south: z.number().min(-90).max(90),
      east: z.number().min(-180).max(180),
      north: z.number().min(-90).max(90),
    })
    .refine(
      (b) => b.west < b.east && b.south < b.north,
      "Проверьте границы охвата",
    )
    .nullable(),
  listed: z.boolean(),
  relatedIds: z.array(z.string().uuid()).max(30),
});
export function locationHeading(location) {
  return `Где поесть ${location.in_phrase || "в " + location.name}`;
}
export async function locationBySlug(slug) {
  const direct = await one(
    "SELECT * FROM locations WHERE slug=$1 AND active=1",
    slug,
  );
  if (direct) return { location: direct, redirect: false };
  const alias = await one(
    "SELECT l.* FROM location_slug_redirects s JOIN locations l ON l.id=s.location_id WHERE s.slug=$1 AND l.active=1",
    slug,
  );
  return alias ? { location: alias, redirect: true } : null;
}
export async function ancestors(locationId) {
  return all(
    `WITH RECURSIVE chain AS (
 SELECT l.*,0 depth,ARRAY[l.id] path FROM locations l WHERE id=$1
 UNION ALL SELECT l.*,c.depth+1,c.path||l.id FROM locations l JOIN chain c ON l.id=c.parent_id WHERE NOT l.id=ANY(c.path) AND c.depth<32
) SELECT * FROM chain WHERE depth>0 ORDER BY depth DESC`,
    locationId,
  );
}
export async function descendants(locationId) {
  return all(
    `WITH RECURSIVE tree AS (
 SELECT l.*,ARRAY[l.id] path FROM locations l WHERE id=$1
 UNION ALL SELECT l.*,t.path||l.id FROM locations l JOIN tree t ON l.parent_id=t.id WHERE NOT l.id=ANY(t.path)
) SELECT * FROM tree`,
    locationId,
  );
}
const publicCondition = `(l.listed OR l.id IN (WITH RECURSIVE useful AS (SELECT id,parent_id FROM locations WHERE active=1 AND published_version IS NOT NULL UNION SELECT p.id,p.parent_id FROM locations p JOIN useful u ON p.id=u.parent_id WHERE p.active=1) SELECT id FROM useful))`;
export async function childLocations(
  locationId,
  { includeHidden = false } = {},
) {
  return all(
    `SELECT l.*,(SELECT count(*) FROM locations c WHERE c.parent_id=l.id AND c.active=1) child_count FROM locations l WHERE l.parent_id=$1 AND l.active=1 ${includeHidden ? "" : "AND " + publicCondition} ORDER BY CASE l.priority WHEN 'high' THEN 0 ELSE 1 END,l.name`,
    locationId,
  );
}
export async function relatedLocations(
  locationId,
  { includeHidden = false } = {},
) {
  return all(
    `SELECT DISTINCT l.* FROM location_relations r JOIN locations l ON l.id=CASE WHEN r.location_id=$1 THEN r.related_id ELSE r.location_id END WHERE (r.location_id=$1 OR r.related_id=$1) AND l.active=1 ${includeHidden ? "" : "AND " + publicCondition} ORDER BY l.name`,
    locationId,
  );
}
/** @param {{query?:string,kind?:string,parentId?:string|null,status?:string,page?:number,pageSize?:number,rootsOnly?:boolean,publicOnly?:boolean}} [options] */
export async function searchLocations({
  query = "",
  kind = "",
  parentId = null,
  status = "all",
  page = 1,
  pageSize = 60,
  rootsOnly = false,
  publicOnly = false,
} = {}) {
  const clauses = [];
  const params = [];
  if (publicOnly) clauses.push(publicCondition);
  const add = (value) => {
    params.push(value);
    return "$" + params.length;
  };
  if (status === "inactive") clauses.push("l.active=0");
  else if (status === "active") clauses.push("l.active=1");
  else if (status === "empty") clauses.push("l.draft='[]'");
  else if (status === "draft")
    clauses.push(
      "l.draft_version IS NOT NULL AND l.draft_version IS DISTINCT FROM l.published_version",
    );
  else if (status === "published")
    clauses.push("l.published_version IS NOT NULL");
  else if (status === "hidden") clauses.push("NOT l.listed");
  const q = query.trim().slice(0, 100);
  if (q) {
    const p = add(
      "%" +
        q
          .toLowerCase()
          .replace(/ё/g, "е")
          .replace(/[\\%_]/g, "\\$&") +
        "%",
    );
    clauses.push(
      `translate(lower(l.name||' '||l.region||' '||array_to_string(l.aliases,' ')),'ё','е') LIKE ${p}`,
    );
  }
  if (kind && kind in LOCATION_KINDS) clauses.push(`l.kind=${add(kind)}`);
  if (parentId) {
    const p = add(parentId);
    clauses.push(
      `l.id IN (WITH RECURSIVE tree AS (SELECT id FROM locations WHERE parent_id=${p} UNION ALL SELECT c.id FROM locations c JOIN tree t ON c.parent_id=t.id) SELECT id FROM tree)`,
    );
  } else if (rootsOnly && !q && !kind) clauses.push("l.parent_id IS NULL");
  const where = clauses.length ? "WHERE " + clauses.join(" AND ") : "";
  const total = (
    await one("SELECT count(*) AS n FROM locations l " + where, ...params)
  ).n;
  const safeSize = Math.min(Math.max(Math.floor(pageSize) || 60, 1), 100);
  const pages = Math.max(1, Math.ceil(total / safeSize));
  const safePage = Math.min(Math.max(Math.floor(page) || 1, 1), pages);
  const exact = q ? add(q.toLowerCase().replace(/ё/g, "е")) : null;
  const order = exact
    ? `CASE WHEN translate(lower(l.name),'ё','е')=${exact} THEN 0 WHEN EXISTS(SELECT 1 FROM unnest(l.aliases) a WHERE translate(lower(a),'ё','е')=${exact}) THEN 1 ELSE 2 END,`
    : "";
  const rows = await all(
    `SELECT l.*,p.name AS parent_name,(SELECT count(*) FROM locations c WHERE c.parent_id=l.id AND c.active=1) child_count FROM locations l LEFT JOIN locations p ON p.id=l.parent_id ${where} ORDER BY ${order}CASE l.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,l.name,l.id LIMIT ${add(safeSize)} OFFSET ${add((safePage - 1) * safeSize)}`,
    ...params,
  );
  return { rows, total, page: safePage, pages };
}
export async function saveLocation(locationId, input) {
  const data = locationInput.parse(input);
  return transaction(async () => {
    await run("SELECT pg_advisory_xact_lock(47031003)");
    const target = locationId || id();
    const previous = locationId
      ? await one("SELECT * FROM locations WHERE id=$1 FOR UPDATE", target)
      : null;
    if (locationId && !previous) throw new Error("Локация не найдена");
    if (data.parentId === target)
      throw new Error("Локация не может быть собственным родителем");
    if (data.parentId) {
      const parent = await one(
        "SELECT * FROM locations WHERE id=$1",
        data.parentId,
      );
      if (!parent) throw new Error("Родитель не найден");
      if (data.active && !parent.active)
        throw new Error("Сначала включите родительскую локацию");
    }
    if (
      !data.active &&
      (await one(
        "SELECT id FROM locations WHERE parent_id=$1 AND active=1 LIMIT 1",
        target,
      ))
    )
      throw new Error("Сначала отключите или перенесите дочерние локации");
    if (data.relatedIds.includes(target))
      throw new Error("Нельзя связать локацию с самой собой");
    const hasChildren = !!(await one(
      "SELECT id FROM locations WHERE parent_id=$1 LIMIT 1",
      target,
    ));
    const vals = [
      data.name,
      data.region,
      data.slug,
      data.kind,
      data.parentId,
      data.isHub || hasChildren,
      data.inPhrase,
      [...new Set(data.aliases)],
      data.priority,
      data.topLimit,
      data.active ? 1 : 0,
      data.population,
      data.populationVerified,
      data.latitude,
      data.longitude,
      data.coverageNote,
      data.bounds ? JSON.stringify(data.bounds) : null,
      data.listed,
      target,
    ];
    if (previous)
      await run(
        "UPDATE locations SET name=$1,region=$2,slug=$3,kind=$4,parent_id=$5,is_hub=$6,in_phrase=$7,aliases=$8,priority=$9,top_limit=$10,active=$11,population=$12,population_verified=$13,latitude=$14,longitude=$15,coverage_note=$16,bounds=$17,listed=$18 WHERE id=$19",
        ...vals,
      );
    else
      await run(
        "INSERT INTO locations(name,region,slug,kind,parent_id,is_hub,in_phrase,aliases,priority,top_limit,active,population,population_verified,latitude,longitude,coverage_note,bounds,listed,id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)",
        ...vals,
      );
    if (data.parentId)
      await run("UPDATE locations SET is_hub=true WHERE id=$1", data.parentId);
    await run(
      "DELETE FROM location_relations WHERE location_id=$1 OR related_id=$1",
      target,
    );
    for (const relatedId of new Set(data.relatedIds))
      await run(
        "INSERT INTO location_relations(location_id,related_id) VALUES($1,$2)",
        target,
        relatedId,
      );
    await audit(
      "location",
      target,
      JSON.stringify({
        previous: previous
          ? {
              name: previous.name,
              slug: previous.slug,
              parentId: previous.parent_id,
            }
          : null,
        name: data.name,
        slug: data.slug,
        parentId: data.parentId,
      }),
    );
    return target;
  });
}
export async function availableRestaurants(locationId) {
  return all(
    `WITH RECURSIVE tree AS (
 SELECT id,name FROM locations WHERE id=$1 AND active=1
 UNION ALL SELECT l.id,l.name FROM locations l JOIN tree t ON l.parent_id=t.id WHERE l.active=1
), snapshots AS (
 SELECT l.id AS area_id,jsonb_array_elements(v.content->'restaurants') item FROM tree l JOIN locations x ON x.id=l.id JOIN versions v ON v.id=x.published_version
) SELECT DISTINCT ON (r.id) r.id,COALESCE(s.item->>'name',r.name) name,COALESCE(s.item->>'cuisine',r.cuisine) cuisine,COALESCE(s.item->>'description',r.description) description,COALESCE(s.item->>'mapsUrl',r.maps_url) maps_url,COALESCE((s.item->'sources')::text,r.sources::text) sources,r.phone,r.opening_hours,r.alex_star,r.location_id,l.name AS location_name FROM restaurants r JOIN locations l ON l.id=r.location_id LEFT JOIN snapshots s ON s.item->>'id'=r.id::text WHERE r.active=1 AND l.active=1 AND (s.item IS NOT NULL OR EXISTS(SELECT 1 FROM restaurant_locations rl WHERE rl.restaurant_id=r.id AND rl.location_id=$1 AND r.location_id<>$1)) ORDER BY r.id,(s.area_id=r.location_id) DESC NULLS LAST`,
    locationId,
  );
}
export async function restaurantAllowed(restaurantId, locationId) {
  return !!(await one(
    `WITH RECURSIVE tree AS (SELECT id FROM locations WHERE id=$2 UNION ALL SELECT l.id FROM locations l JOIN tree t ON l.parent_id=t.id) SELECT r.id FROM restaurants r WHERE r.id=$1 AND (r.location_id IN(SELECT id FROM tree) OR EXISTS(SELECT 1 FROM restaurant_locations rl WHERE rl.restaurant_id=r.id AND rl.location_id=$2))`,
    restaurantId,
    locationId,
  ));
}
export async function linkRestaurant(restaurantId, locationId) {
  await transaction(async () => {
    if (!(await one("SELECT id FROM restaurants WHERE id=$1", restaurantId)))
      throw new Error("Ресторан не найден");
    if (!(await one("SELECT id FROM locations WHERE id=$1", locationId)))
      throw new Error("Локация не найдена");
    await run(
      "INSERT INTO restaurant_locations(restaurant_id,location_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
      restaurantId,
      locationId,
    );
    await audit("restaurant_location", locationId, restaurantId);
  });
}
export async function hasPublishedDescendants(locationId) {
  return !!(await one(
    `WITH RECURSIVE tree AS (SELECT id,published_version FROM locations WHERE parent_id=$1 AND active=1 UNION ALL SELECT l.id,l.published_version FROM locations l JOIN tree t ON l.parent_id=t.id WHERE l.active=1) SELECT id FROM tree WHERE published_version IS NOT NULL LIMIT 1`,
    locationId,
  ));
}
export async function knownRestaurants(locationId) {
  return all(
    `WITH RECURSIVE tree AS (SELECT id FROM locations WHERE id=$1 UNION ALL SELECT l.id FROM locations l JOIN tree t ON l.parent_id=t.id) SELECT DISTINCT r.id,r.name,r.location_id FROM restaurants r LEFT JOIN restaurant_locations rl ON rl.restaurant_id=r.id WHERE r.location_id IN(SELECT id FROM tree) OR rl.location_id=$1`,
    locationId,
  );
}
