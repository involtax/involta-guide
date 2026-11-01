import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { one } from "@/lib/db.mjs";
import VoiceWidget from "@/components/VoiceWidget";
export const dynamic = "force-dynamic";
async function getCity(slug: string) {
  return one("SELECT * FROM cities WHERE slug=? AND active=1", slug);
}
export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const c = await getCity(slug);
  if (!c) return {};
  return {
    title: `Где поесть: ${c.name} — лучшие рестораны`,
    description: `Короткая подборка ресторанов: ${c.name}, ${c.region}. Кухня, причины зайти и ссылки на Яндекс Карты.`,
    alternates: { canonical: `/guide/${c.slug}` },
    robots: c.published_version
      ? { index: true, follow: true }
      : { index: false, follow: true },
  };
}
export default async function City({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const c = await getCity(slug);
  if (!c) notFound();
  const version = c.published_version
    ? one("SELECT * FROM versions WHERE id=?", c.published_version)
    : null;
  const content = version
    ? JSON.parse(version.content)
    : { intro: "", restaurants: [] };
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "ItemList",
    name: `Где поесть: ${c.name}`,
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
          addressLocality: c.name,
          addressRegion: c.region,
          addressCountry: "RU",
        },
      },
    })),
  };
  return (
    <main>
      <nav className="breadcrumbs">
        <Link href="/guide">Все города</Link>
        <span>/</span>
        <span>{c.region}</span>
      </nav>
      <section className="city-hero">
        <p className="eyebrow">ИНВОЛЬТА ГИД / {c.region}</p>
        <h1>
          Где поесть.
          <br />
          <span className="serif">{c.name}.</span>
        </h1>
        <p className="lead">
          {content.intro ||
            "Здесь скоро появится короткая подборка мест, которые стоит посетить. Знаете такое? Расскажите голосом — ваш опыт поможет редакции."}
        </p>
        {version && (
          <p className="muted">
            Редакционная подборка · обновлено{" "}
            {new Date(version.created_at + "Z").toLocaleDateString("ru-RU")}
          </p>
        )}
      </section>
      <section className="restaurant-list">
        <div className="section-heading">
          <h2>
            {content.restaurants.length ? "Стоит зайти" : "Подборка в работе"}
          </h2>
          <span className="muted">
            {String(content.restaurants.length).padStart(2, "0")} мест
          </span>
        </div>
        {content.restaurants.map((r: any, i: number) => (
          <article className="restaurant" key={r.id}>
            <span className="rank">{String(i + 1).padStart(2, "0")}</span>
            <div>
              <p className="eyebrow">{r.cuisine}</p>
              <h2>{r.name}</h2>
              <p>{r.description}</p>
              <a href={r.mapsUrl} target="_blank" rel="noopener noreferrer">
                На Яндекс Картах ↗
              </a>
            </div>
          </article>
        ))}
      </section>
      <VoiceWidget cityId={c.id} cityName={c.name} />
      {content.restaurants.length > 0 && (
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c"),
          }}
        />
      )}
    </main>
  );
}
