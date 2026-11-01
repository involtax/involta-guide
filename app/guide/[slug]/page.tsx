import type { Metadata } from "next";
import { cache } from "react";
import Link from "next/link";
import { notFound, permanentRedirect } from "next/navigation";
import { all, one } from "@/lib/db.mjs";
import {
  locationBySlug,
  locationHeading,
  ancestors,
  childLocations,
  relatedLocations,
  hasPublishedDescendants,
  LOCATION_KINDS,
} from "@/lib/locations.mjs";
import VoiceWidget from "@/components/VoiceWidget";
export const dynamic = "force-dynamic";
const load = cache(async (slug: string) => {
  const found = await locationBySlug(slug);
  if (!found) return null;
  if (found.redirect) permanentRedirect("/guide/" + found.location.slug);
  const c = found.location;
  const [parents, children, related, hasChildrenContent, version] =
    await Promise.all([
      ancestors(c.id),
      childLocations(c.id),
      relatedLocations(c.id),
      hasPublishedDescendants(c.id),
      c.published_version
        ? one("SELECT * FROM versions WHERE id=$1", c.published_version)
        : Promise.resolve(null),
    ]);
  return {
    c,
    parents,
    children,
    related,
    indexable: !!version || hasChildrenContent,
    version,
  };
});
export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const data = await load(slug);
  if (!data) return {};
  return {
    title: locationHeading(data.c) + " — рестораны и рекомендации",
    description:
      locationHeading(data.c) +
      `: короткая подборка ресторанов, кухня, причины зайти и Яндекс Карты. ${data.c.region}.`,
    alternates: { canonical: "/guide/" + data.c.slug },
    robots: { index: data.indexable, follow: true },
  };
}
export default async function LocationPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const data = await load(slug);
  if (!data) notFound();
  const { c, parents, children, related, version } = data;
  const content = version
    ? JSON.parse(version.content)
    : { intro: "", restaurants: [] };
  const homeIds = [
    ...new Set(
      content.restaurants.map((r: any) => r.locationId).filter(Boolean),
    ),
  ];
  const homes = homeIds.length
    ? await all(
        "SELECT id,name,slug FROM locations WHERE id=ANY($1::uuid[]) AND active=1",
        homeIds,
      )
    : [];
  const homeMap = new Map(homes.map((h) => [h.id, h]));
  const title = locationHeading(c);
  const base = process.env.SITE_URL || "http://localhost:3000";
  const breadcrumbs = [
    { name: "Все локации", slug: "" },
    ...parents.map((p) => ({ name: p.name, slug: p.slug })),
    { name: c.name, slug: c.slug },
  ];
  const structured = [
    {
      "@context": "https://schema.org",
      "@type": "BreadcrumbList",
      itemListElement: breadcrumbs.map((b, i) => ({
        "@type": "ListItem",
        position: i + 1,
        name: b.name,
        item: base + "/guide" + (b.slug ? "/" + b.slug : ""),
      })),
    },
    ...(content.restaurants.length
      ? [
          {
            "@context": "https://schema.org",
            "@type": "ItemList",
            name: title,
            itemListElement: content.restaurants.map((r: any, i: number) => ({
              "@type": "ListItem",
              position: i + 1,
              item: {
                "@type": "Restaurant",
                name: r.name,
                servesCuisine: r.cuisine,
                url: r.mapsUrl,
                address: {
                  "@type": "PostalAddress",
                  addressLocality: r.locationName || c.name,
                  addressCountry: "RU",
                },
              },
            })),
          },
        ]
      : []),
  ];
  return (
    <main>
      <nav className="breadcrumbs" aria-label="Навигационная цепочка">
        <Link href="/guide">Все локации</Link>
        {parents.map((p) => (
          <span key={p.id}>
            {" "}
            /{" "}
            {p.active ? (
              <Link href={"/guide/" + p.slug}>{p.name}</Link>
            ) : (
              p.name
            )}
          </span>
        ))}
        <span> / {c.name}</span>
      </nav>
      <section className="city-hero">
        <p className="eyebrow">
          ИНВОЛЬТА ГИД / {LOCATION_KINDS[c.kind as keyof typeof LOCATION_KINDS]}{" "}
          / {c.region}
        </p>
        <h1>
          Где поесть <br />
          <span className="serif">{c.in_phrase || "в " + c.name}.</span>
        </h1>
        <p className="lead">
          {content.intro ||
            (children.length
              ? "Выберите район или курорт ниже. Здесь появится общая редакционная подборка направления."
              : "Здесь скоро появится короткая подборка мест, которые стоит посетить. Знаете такое? Расскажите голосом — ваш опыт поможет редакции.")}
        </p>
        {c.coverage_note && <p className="coverage">{c.coverage_note}</p>}
        {version && (
          <p className="muted">
            Редакционная подборка · обновлено{" "}
            {new Date(version.created_at + "Z").toLocaleDateString("ru-RU")}
          </p>
        )}
      </section>
      {children.length > 0 && (
        <section className="location-children">
          <div className="section-heading">
            <h2>Выберите район или место</h2>
            <span className="muted">{children.length} локаций</span>
          </div>
          <div className="city-grid">
            {children.map((l) => (
              <Link key={l.id} className="city-link" href={"/guide/" + l.slug}>
                <span>
                  <strong>{l.name}</strong>
                  <small>
                    {LOCATION_KINDS[l.kind as keyof typeof LOCATION_KINDS]}
                    {l.child_count ? ` · внутри ${l.child_count}` : ""}
                    {l.published_version ? " · есть подборка" : ""}
                  </small>
                </span>
                <span>→</span>
              </Link>
            ))}
          </div>
        </section>
      )}
      <section className="restaurant-list">
        <div className="section-heading">
          <h2>
            {content.restaurants.length ? "Стоит зайти" : "Подборка в работе"}
          </h2>
          <span className="muted">
            {String(content.restaurants.length).padStart(2, "0")} мест
          </span>
        </div>
        {content.restaurants.map((r: any, i: number) => {
          const home = homeMap.get(r.locationId);
          return (
            <article className="restaurant" key={r.id}>
              <span className="rank">{String(i + 1).padStart(2, "0")}</span>
              <div>
                <p className="eyebrow">
                  {r.cuisine}
                  {home && home.id !== c.id && (
                    <>
                      {" "}
                      · <Link href={"/guide/" + home.slug}>{home.name}</Link>
                    </>
                  )}
                </p>
                <h2>
                  {r.name}{" "}
                  <a
                    href={r.mapsUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`${r.name} — Яндекс Карты`}
                    title="Яндекс Карты"
                  >
                    ↗
                  </a>
                </h2>
                <p>{r.description}</p>
              </div>
            </article>
          );
        })}
      </section>
      {related.length > 0 && (
        <section className="location-children">
          <h2>Рядом и также интересно</h2>
          <div className="button-row">
            {related.map((r) => (
              <Link key={r.id} href={"/guide/" + r.slug}>
                {r.name} →
              </Link>
            ))}
          </div>
        </section>
      )}
      <VoiceWidget locationId={c.id} locationName={c.name} />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(structured).replace(/</g, "\\u003c"),
        }}
      />
    </main>
  );
}
