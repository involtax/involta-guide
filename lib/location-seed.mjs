import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { all, one, run, id, slugify, setSetting, transaction } from "./db.mjs";
const regionKey = (s) =>
  s
    .toLowerCase()
    .replace(/республика\s*|\s+республика/g, "")
    .replace(/ё/g, "е")
    .trim();
export async function seedGeography() {
  return transaction(async () => {
    await run("SELECT pg_advisory_xact_lock(47031002)");
    if (
      await one("SELECT value FROM settings WHERE key='locations_geography_v1'")
    )
      return 0;
    const read = async (file) =>
      JSON.parse(await readFile(join(process.cwd(), "data", file), "utf8"));
    const [additions, phrases, districts] = await Promise.all([
      read("location-additions.json"),
      read("location-phrases.json"),
      read("moscow-districts.json"),
    ]);
    const existing = await all("SELECT * FROM locations");
    const bySlug = new Map(existing.map((r) => [r.slug, r]));
    let added = 0;
    for (const row of existing)
      if (!row.in_phrase)
        await run(
          "UPDATE locations SET in_phrase=$1 WHERE id=$2",
          phrases[row.name] || `в ${row.name}`,
          row.id,
        );
    async function ensure(data) {
      let row =
        bySlug.get(data.slug) ||
        existing.find(
          (r) =>
            r.name === data.name &&
            regionKey(r.region) === regionKey(data.region),
        );
      if (!row) {
        row = { id: id(), ...data };
        await run(
          "INSERT INTO locations(id,name,region,slug,kind,in_phrase,population,population_basis,source_metadata,top_limit,priority,coverage_note,is_hub,aliases) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
          row.id,
          data.name,
          data.region,
          data.slug,
          data.kind || "destination",
          data.in_phrase || phrases[data.name] || `в ${data.name}`,
          data.population ?? null,
          data.population_basis || "unknown",
          JSON.stringify(data.source_metadata || {}),
          data.top_limit || 3,
          data.priority || "medium",
          data.coverage_note || "",
          !!data.is_hub,
          data.aliases || [],
        );
        existing.push(row);
        added++;
      } else {
        await run(
          "UPDATE locations SET kind=$1,is_hub=$2,priority=$3,top_limit=$4,aliases=ARRAY(SELECT DISTINCT unnest(aliases || $5::text[])),in_phrase=$6,coverage_note=$7 WHERE id=$8",
          data.kind || row.kind,
          !!data.is_hub,
          data.priority || row.priority,
          data.top_limit || row.top_limit,
          data.aliases || [],
          data.in_phrase || phrases[data.name] || row.in_phrase,
          data.coverage_note || row.coverage_note || "",
          row.id,
        );
      }
      bySlug.set(data.slug, row);
      if (data.slug !== row.slug)
        await run(
          "INSERT INTO location_slug_redirects(slug,location_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
          data.slug,
          row.id,
        );
      return row;
    }
    const special = [
      {
        slug: "moskva",
        match: "Москва",
        name: "Москва",
        region: "Москва",
        kind: "city",
        in_phrase: "в Москве",
        is_hub: true,
        priority: "high",
        top_limit: 5,
      },
      {
        slug: "sochi",
        name: "Сочи",
        region: "Краснодарский край",
        kind: "city",
        in_phrase: "в Сочи",
        is_hub: true,
        priority: "high",
        top_limit: 5,
        coverage_note:
          "Редакционное направление: Сочи и курортные окрестности, включая Сириус и горный кластер. Административные границы учитываются отдельно.",
      },
      {
        slug: "sochi-centre",
        name: "Сочи-центр",
        region: "Краснодарский край",
        kind: "district",
        in_phrase: "в центре Сочи",
        parent: "sochi",
        top_limit: 5,
        aliases: ["Центральный Сочи", "Центр Сочи"],
        coverage_note: "Центральная часть Сочи; не весь Большой Сочи.",
      },
      {
        slug: "krasnaya-polyana",
        name: "Красная Поляна",
        region: "Краснодарский край",
        kind: "destination",
        in_phrase: "в Красной Поляне",
        parent: "sochi",
        is_hub: true,
        priority: "high",
        top_limit: 5,
        coverage_note:
          "Горное направление: посёлок Красная Поляна, Эсто-Садок и курорты. Не путать с одноимённым курортом.",
      },
      {
        slug: "krasnaya-polyana-kurort",
        name: "Курорт Красная Поляна",
        region: "Краснодарский край",
        kind: "resort",
        in_phrase: "на курорте Красная Поляна",
        parent: "esto-sadok",
        top_limit: 3,
        aliases: [
          "Горки Город",
          "Горки-город",
          "Красная Поляна 540",
          "Красная Поляна 960",
        ],
      },
      {
        slug: "koreiz",
        name: "Кореиз",
        region: "Республика Крым",
        kind: "settlement",
        in_phrase: "в Кореизе",
        parent: "yalta",
      },
      {
        slug: "baykal",
        name: "Байкал",
        region: "Иркутская область / Республика Бурятия",
        kind: "destination",
        in_phrase: "на Байкале",
        is_hub: true,
        top_limit: 5,
        priority: "high",
        coverage_note:
          "Туристическое направление. Ресторан обязательно привязывается к конкретному берегу и населённому пункту.",
      },
      {
        slug: "olkhon",
        name: "Ольхон",
        region: "Иркутская область",
        kind: "tourist_area",
        in_phrase: "на Ольхоне",
        parent: "baykal",
        is_hub: true,
        aliases: ["Остров Ольхон"],
      },
      {
        slug: "maloe-more",
        name: "Малое Море",
        region: "Иркутская область",
        kind: "tourist_area",
        in_phrase: "на Малом Море",
        parent: "baykal",
        is_hub: true,
      },
      {
        slug: "gornyy-altay",
        name: "Горный Алтай",
        region: "Республика Алтай",
        kind: "destination",
        in_phrase: "на Горном Алтае",
        is_hub: true,
        priority: "high",
        top_limit: 5,
      },
    ];
    // Reuse existing canonical slugs (Moscow is transliterated as moskva).
    for (const data of special) await ensure(data);
    const plan = [];
    for (const source of additions) {
      if (["Хаджох", "Газпром Лаура", "Газпром Альпика"].includes(source.name))
        continue;
      const d = {
        name: source.name,
        region: source.subject,
        slug: slugify(source.name),
        kind: source.type,
        in_phrase: phrases[source.name],
        priority: source.priority,
        top_limit:
          source.type === "tourist_area"
            ? 3
            : source.priority === "high"
              ? 3
              : 2,
        population:
          source.population_basis === "service_area_proxy"
            ? null
            : source.population,
        population_basis: source.population_basis,
        source_metadata: {
          ...source,
          source: "user-provided involta_guide_additions.json",
        },
        parent: source.parent_destination
          ? slugify(source.parent_destination)
          : null,
      };
      if (["Репино", "Комарово", "Солнечное"].includes(d.name))
        d.region = "Санкт-Петербург";
      if (d.name === "Медовые Водопады")
        d.region = "Карачаево-Черкесская Республика";
      if (d.name === "Бирюзовая Катунь") d.region = "Алтайский край";
      if (d.name === "Каменномостский") d.aliases = ["Хаджох"];
      if (d.name === "Красная Поляна") {
        d.name = "Посёлок Красная Поляна";
        d.slug = "krasnaya-polyana-poselok";
        d.in_phrase = "в посёлке Красная Поляна";
        d.parent = "krasnaya-polyana";
        d.aliases = ["Красная Поляна — посёлок"];
      }
      if (d.name === "Эсто-Садок") {
        d.slug = "esto-sadok";
        d.parent = "krasnaya-polyana";
        d.is_hub = true;
        d.aliases = ["Эстосадок", "Эсто Садок"];
      }
      if (d.parent === "esto-sadok") d.parent = "esto-sadok";
      if (d.name === "Газпром Поляна") {
        d.aliases = [
          "Газпром",
          "Газпром Лаура",
          "Газпром Альпика",
          "Лаура",
          "Альпика",
        ];
        d.top_limit = 2;
        d.coverage_note =
          "Единая локация курорта Газпром Поляна, включая Лауру и Альпику. Отдельные страницы секторов не создаются.";
      }
      if (d.name === "Сириус") {
        d.parent = "sochi";
        d.is_hub = true;
        d.in_phrase = "в Сириусе";
        d.coverage_note =
          "Федеральная территория Сириус. Включена в редакционное туристическое направление Сочи; не административная часть города.";
      }
      if (
        [
          "Олимпийский парк",
          "Имеретинская набережная",
          "Имеретинский курорт",
        ].includes(d.name)
      ) {
        d.region = "Федеральная территория Сириус";
        d.parent = "sirius";
      }
      if (d.name === "Приэльбрусье") {
        d.parent = null;
        d.kind = "destination";
        d.is_hub = true;
      }
      if (["Терскол", "Эльбрус", "Джилы-Су"].includes(d.name))
        d.parent = "prielbruse";
      if (d.name === "Сулак") {
        d.name = "Сулакский каньон";
        d.slug = "sulakskiy-kanyon";
        d.in_phrase = "у Сулакского каньона";
        d.aliases = ["Сулак"];
        d.is_hub = true;
      }
      if (["Дубки", "Чиркей"].includes(d.name)) d.parent = "sulakskiy-kanyon";
      if (d.name === "Катунь") {
        d.name = "Долина Катуни";
        d.slug = "dolina-katuni";
        d.aliases = ["Катунь"];
        d.parent = "gornyy-altay";
        d.is_hub = true;
      }
      if (
        [
          "Чемал",
          "Манжерок",
          "Усть-Сема",
          "Элекмонар",
          "Бирюзовая Катунь",
        ].includes(d.name)
      )
        d.parent = "dolina-katuni";
      if (["Акташ", "Кош-Агач", "Улаган", "Мульта", "Тюнгур"].includes(d.name))
        d.parent = "gornyy-altay";
      if (
        [
          "Листвянка",
          "Большое Голоустное",
          "Горячинск",
          "Усть-Баргузин",
          "Максимиха",
          "Турка",
        ].includes(d.name)
      )
        d.parent = "baykal";
      if (d.name === "Хужир") d.parent = "olkhon";
      if (d.name === "Сахюрта") d.parent = "maloe-more";
      plan.push(d);
      await ensure(d);
    }
    // Districts reuse existing Moscow city records when names coincide (Troitsk, Shcherbinka).
    const moscow = bySlug.get("moskva");
    const zelenograd = existing.find(
      (r) => r.name === "Зеленоград" && regionKey(r.region) === "москва",
    );
    if (zelenograd) {
      bySlug.set("zelenograd", zelenograd);
      await run(
        "UPDATE locations SET parent_id=$1,is_hub=true WHERE id=$2",
        moscow.id,
        zelenograd.id,
      );
    }
    for (const d of districts) {
      const parent =
        d.administrative_area === "ЗелАО" && zelenograd
          ? "zelenograd"
          : "moskva";
      const row = await ensure({
        name: d.name,
        region: "Москва",
        slug: "moskva-" + slugify(d.name),
        kind: "district",
        in_phrase: d.in_phrase,
        parent,
        top_limit: 5,
        priority: [
          "Арбат",
          "Хамовники",
          "Пресненский",
          "Тверской",
          "Басманный",
          "Замоскворечье",
          "Якиманка",
        ].includes(d.name)
          ? "high"
          : "medium",
        source_metadata: d,
      });
      plan.push({ slug: "moskva-" + slugify(d.name), parent });
    }
    for (const d of [...special, ...plan])
      if (d.parent) {
        const child = bySlug.get(d.slug),
          parent = bySlug.get(d.parent);
        if (!child || !parent)
          throw new Error("Не найден родитель: " + d.slug + " → " + d.parent);
        await run(
          "UPDATE locations SET parent_id=$1 WHERE id=$2",
          parent.id,
          child.id,
        );
        await run("UPDATE locations SET is_hub=true WHERE id=$1", parent.id);
      }
    for (const [alias, target] of [
      ["khadzhokh", "kamennomostskiy"],
      ["gazprom-laura", "gazprom-polyana"],
      ["gazprom-alpika", "gazprom-polyana"],
      ["sulak", "sulakskiy-kanyon"],
      ["katun", "dolina-katuni"],
    ]) {
      const row = bySlug.get(target);
      if (row)
        await run(
          "INSERT INTO location_slug_redirects(slug,location_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
          alias,
          row.id,
        );
    }
    await setSetting("locations_geography_v1", new Date().toISOString());
    return added;
  });
}
export async function seedMoscowNavigation() {
  return transaction(async () => {
    await run("SELECT pg_advisory_xact_lock(47031002)");
    if (
      await one("SELECT value FROM settings WHERE key='moscow_navigation_v1'")
    )
      return 0;
    const moscow = await one(
      "SELECT id FROM locations WHERE name='Москва' AND region='Москва'",
    );
    if (!moscow) throw new Error("Сначала импортируйте Москву");
    const areas = JSON.parse(
      await readFile(
        join(process.cwd(), "data/moscow-dining-areas.json"),
        "utf8",
      ),
    );
    let added = 0;
    await run(
      "UPDATE locations SET listed=false WHERE region='Москва' AND (kind='district' OR name='Зеленоград')",
    );
    for (const d of areas) {
      let row = d.existing
        ? await one(
            "SELECT id FROM locations WHERE name=$1 AND region='Москва' AND kind='district'",
            d.name,
          )
        : await one("SELECT id FROM locations WHERE slug=$1", d.slug);
      const note =
        d.coverage_note ||
        `${d.name} и ближайшие ресторанные адреса. Редакционная городская зона; не административные границы.`;
      if (!row) {
        row = { id: id() };
        await run(
          "INSERT INTO locations(id,name,region,slug,kind,parent_id,in_phrase,aliases,priority,top_limit,coverage_note,source_metadata) VALUES($1,$2,'Москва',$3,'neighbourhood',$4,$5,$6,'high',5,$7,$8)",
          row.id,
          d.name,
          d.slug,
          moscow.id,
          d.in_phrase,
          d.aliases || [],
          note,
          JSON.stringify({
            category: "editorial_zone",
            source:
              "Редакционная стартовая подборка гастрономических зон Москвы",
          }),
        );
        added++;
      }
      await run(
        "UPDATE locations SET listed=true,priority=$1,in_phrase=$2,aliases=ARRAY(SELECT DISTINCT unnest(aliases || $3::text[])) WHERE id=$4",
        "high",
        d.in_phrase,
        d.aliases || [],
        row.id,
      );
      if (d.related) {
        const related = await one(
          "SELECT id FROM locations WHERE name=$1 AND region='Москва' AND kind='district'",
          d.related,
        );
        if (related && related.id !== row.id)
          await run(
            "INSERT INTO location_relations(location_id,related_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
            row.id,
            related.id,
          );
      }
    }
    // Existing Moscow settlements remain navigable under the editorial city scope.
    await run(
      "UPDATE locations SET parent_id=$1,listed=false WHERE region='Москва' AND name<>'Москва' AND parent_id IS NULL AND id<>$1",
      moscow.id,
    );
    await setSetting("moscow_navigation_v1", new Date().toISOString());
    return added;
  });
}
