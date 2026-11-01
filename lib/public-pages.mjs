import { all, one } from "./db.mjs";
import {
  locationBySlug,
  locationHeading,
  ancestors,
  childLocations,
  relatedLocations,
  hasPublishedDescendants,
  searchLocations,
  LOCATION_KINDS,
} from "./locations.mjs";
import { absoluteUrl, publicPath } from "./urls.mjs";
import {
  escapeHtml as e,
  documentHtml,
  restaurantHtml,
  voiceHtml,
  link,
} from "./public-html.mjs";
export async function directoryHtml(url) {
  const query = (url.searchParams.get("q") || "").trim().slice(0, 100),
    kind = url.searchParams.get("kind") || "";
  const result = await searchLocations({
    query,
    kind,
    status: "active",
    page: Number(url.searchParams.get("page") || 1),
    rootsOnly: true,
    publicOnly: true,
  });
  const total = await one(
    "SELECT count(*) AS n FROM locations WHERE active=1 AND (listed OR published_version IS NOT NULL)",
  );
  const pageUrl = (n) =>
    "/?" + new URLSearchParams({ q: query, kind, page: String(n) });
  return documentHtml({
    body: `<section class="hero"><p class="eyebrow">ЕДА. МЕСТА. ЛИЧНЫЙ ОПЫТ.</p><h1>Хорошие места.<br><span class="serif">Без лишних слов.</span></h1><div class="hero-bottom"><p>Короткие подборки ресторанов в городах, районах и на курортах. Выбираем внимательно. Прислушиваемся к вам.</p><span class="counter">${Number(total.n).toLocaleString("ru-RU")}<small>локаций в справочнике</small></span></div></section><section class="cities"><div class="section-heading"><h2>Куда отправимся?</h2><span class="muted">01 / География</span></div><form class="search location-search" action="${e(publicPath("/"))}"><label class="sr-only" for="q">Город, район или курорт</label><input id="q" name="q" placeholder="Город, район или курорт" value="${e(query)}"><select name="kind" aria-label="Тип локации"><option value="">Все типы</option>${Object.entries(
      LOCATION_KINDS,
    )
      .map(
        ([key, label]) =>
          `<option value="${e(key)}"${kind === key ? " selected" : ""}>${e(label)}</option>`,
      )
      .join(
        "",
      )}</select><button>Найти ↗</button></form><p class="muted">${query || kind ? "Найдено: " + result.total : "Города и направления · районы и курорты — внутри или через поиск"}</p><div class="city-grid">${result.rows.map((c) => link("/" + c.slug, `<span><strong>${e(c.name)}</strong><small>${e(LOCATION_KINDS[c.kind])} · ${e(c.parent_name || c.region)}${Number(c.child_count) > 0 ? " · внутри " + c.child_count : ""}</small></span><span class="city-arrow">${c.is_hub ? "↳" : c.published_version ? "↗" : "→"}</span>`, 'class="city-link"')).join("")}</div>${!result.total ? "<p>Пока не нашли такую локацию. Попробуйте другое название.</p>" : ""}<nav class="pagination" aria-label="Страницы справочника">${result.page > 1 ? link(pageUrl(result.page - 1), "← Назад") : ""}<span>${result.page} / ${result.pages}</span>${result.page < result.pages ? link(pageUrl(result.page + 1), "Дальше →") : ""}</nav></section>`,
  });
}
export async function locationHtml(slug, alex) {
  const found = await locationBySlug(slug);
  if (!found) return null;
  if (found.redirect)
    return { redirect: publicPath("/" + found.location.slug) };
  const c = found.location;
  const [parents, children, related, hasChildren, version] = await Promise.all([
    ancestors(c.id),
    childLocations(c.id),
    relatedLocations(c.id),
    hasPublishedDescendants(c.id),
    c.published_version
      ? one("SELECT * FROM versions WHERE id=$1", c.published_version)
      : null,
  ]);
  const content = version
    ? JSON.parse(version.content)
    : { intro: "", restaurants: [] };
  const ids = content.restaurants.map((r) => r.id).filter(Boolean);
  const stars = ids.length
    ? await all(
        "SELECT id,alex_star FROM restaurants WHERE id=ANY($1::uuid[])",
        ids,
      )
    : [];
  const starMap = new Map(stars.map((r) => [r.id, r.alex_star]));
  const homeIds = [
    ...new Set(content.restaurants.map((r) => r.locationId).filter(Boolean)),
  ];
  const homes = homeIds.length
    ? await all(
        "SELECT id,name,slug FROM locations WHERE id=ANY($1::uuid[]) AND active=1",
        homeIds,
      )
    : [];
  const homeMap = new Map(homes.map((h) => [h.id, h]));
  const restaurants = content.restaurants.map((r) => ({
    ...r,
    alexStar: !!starMap.get(r.id),
  }));
  const title = locationHeading(c);
  const structured = [
    {
      "@context": "https://schema.org",
      "@type": "BreadcrumbList",
      itemListElement: [
        { name: "Все локации", slug: "" },
        ...parents,
        { name: c.name, slug: c.slug },
      ].map((b, i) => ({
        "@type": "ListItem",
        position: i + 1,
        name: b.name,
        item: absoluteUrl(b.slug ? "/" + b.slug : "/"),
      })),
    },
  ];
  if (restaurants.length)
    structured.push({
      "@context": "https://schema.org",
      "@type": "ItemList",
      name: title,
      itemListElement: restaurants.map((r, i) => ({
        "@type": "ListItem",
        position: i + 1,
        item: {
          "@type": "Restaurant",
          name: r.name,
          servesCuisine: r.cuisine,
          url: r.mapsUrl,
          ...(r.phone ? { telephone: r.phone } : {}),
          address: {
            "@type": "PostalAddress",
            addressLocality: r.locationName || c.name,
            addressCountry: "RU",
          },
        },
      })),
    });
  const cards = restaurants.map((r, i) =>
    restaurantHtml(r, i, homeMap.get(r.locationId), c.id),
  );
  const body = `<nav class="breadcrumbs" aria-label="Навигационная цепочка">${link("/", "Все локации")}${parents.map((p) => `<span> / ${p.active ? link("/" + p.slug, e(p.name)) : e(p.name)}</span>`).join("")}<span> / ${e(c.name)}</span></nav><section class="city-hero"><p class="eyebrow">ИНВОЛЬТА ГИД / ${e(LOCATION_KINDS[c.kind])} / ${e(c.region)}</p><h1>Где поесть <br><span class="serif">${e(c.in_phrase || "в " + c.name)}.</span></h1><p class="lead">${e(content.intro || (children.length ? "Выберите район или курорт ниже. Здесь появится общая редакционная подборка направления." : "Здесь скоро появится короткая подборка мест, которые стоит посетить. Знаете такое? Расскажите голосом — ваш опыт поможет редакции."))}</p>${c.coverage_note ? `<p class="coverage">${e(c.coverage_note)}</p>` : ""}${version ? `<p class="muted">Редакционная подборка · обновлено ${new Date(version.created_at + "Z").toLocaleDateString("ru-RU")}</p>` : ""}</section>${children.length ? `<section class="location-children"><div class="section-heading"><h2>Выберите район или место</h2><span class="muted">${children.length} локаций</span></div><div class="city-grid">${children.map((l) => link("/" + l.slug, `<span><strong>${e(l.name)}</strong><small>${e(LOCATION_KINDS[l.kind])}${l.child_count ? " · внутри " + l.child_count : ""}${l.published_version ? " · есть подборка" : ""}</small></span><span>→</span>`, 'class="city-link"')).join("")}</div></section>` : ""}<section class="restaurant-list"><div class="section-heading"><h2>${cards.length ? "Стоит зайти" : "Подборка в работе"}</h2><span class="muted">${String(cards.length).padStart(2, "0")} мест</span></div>${cards.slice(0, c.top_limit).join("")}${cards.length > c.top_limit ? `<details class="more-restaurants"><summary>Ещё · ${cards.length - c.top_limit}</summary>${cards.slice(c.top_limit).join("")}</details>` : ""}</section>${related.length ? `<section class="location-children"><h2>Рядом и также интересно</h2><div class="button-row">${related.map((r) => link("/" + r.slug, e(r.name) + " →")).join("")}</div></section>` : ""}${alex ? `<p>Режим Алекса: голосовая команда изменит страницу сразу. ${link("/alex", "Журнал ↗")}</p>` : ""}${voiceHtml(c, alex)}`;
  return {
    html: documentHtml({
      title: title + " — рестораны и рекомендации · Инвольта гид",
      description:
        title +
        ": короткая подборка ресторанов, кухня и причины зайти. " +
        c.region,
      path: "/" + c.slug,
      index: !!version || hasChildren,
      body,
      structured,
      scripts: `<script type="module" src="${e(publicPath("/assets/hours.js"))}"></script><script defer src="${e(publicPath("/widget.js"))}"></script>`,
    }),
  };
}
export async function sitemapXml() {
  const rows = await all(
    `WITH RECURSIVE useful AS (SELECT l.id,l.parent_id,v.created_at FROM locations l JOIN versions v ON v.id=l.published_version WHERE l.active=1 UNION ALL SELECT l.id,l.parent_id,u.created_at FROM locations l JOIN useful u ON l.id=u.parent_id WHERE l.active=1) SELECT l.slug,max(u.created_at) updated FROM useful u JOIN locations l ON l.id=u.id GROUP BY l.slug`,
  );
  return `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${e(absoluteUrl("/"))}</loc></url>${rows.map((l) => `<url><loc>${e(absoluteUrl("/" + l.slug))}</loc><lastmod>${new Date(l.updated + "Z").toISOString()}</lastmod></url>`).join("")}</urlset>`;
}
