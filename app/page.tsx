import Link from "next/link";
import { one } from "@/lib/db.mjs";
import { searchLocations, LOCATION_KINDS } from "@/lib/locations.mjs";
export const dynamic = "force-dynamic";
export default async function Guide({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; kind?: string; page?: string }>;
}) {
  const { q = "", kind = "", page = "1" } = await searchParams;
  const query = q.trim().slice(0, 100);
  const result = await searchLocations({
    query,
    kind,
    status: "active",
    page: Number(page),
    rootsOnly: true,
    publicOnly: true,
  });
  const total = (await one(
    "SELECT count(*) AS n FROM locations WHERE active=1 AND (listed OR published_version IS NOT NULL)",
  ))!;
  const pageUrl = (n: number) =>
    "/?" + new URLSearchParams({ q: query, kind, page: String(n) });
  return (
    <main>
      <section className="hero">
        <p className="eyebrow">ЕДА. МЕСТА. ЛИЧНЫЙ ОПЫТ.</p>
        <h1>
          Хорошие места.
          <br />
          <span className="serif">Без лишних слов.</span>
        </h1>
        <div className="hero-bottom">
          <p>
            Короткие подборки ресторанов в городах, районах и на курортах.
            Выбираем внимательно. Прислушиваемся к вам.
          </p>
          <span className="counter">
            {Number(total.n).toLocaleString("ru-RU")}
            <small>локаций в справочнике</small>
          </span>
        </div>
      </section>
      <section className="cities">
        <div className="section-heading">
          <h2>Куда отправимся?</h2>
          <span className="muted">01 / География</span>
        </div>
        <form className="search location-search">
          <label className="sr-only" htmlFor="q">
            Город, район или курорт
          </label>
          <input
            id="q"
            name="q"
            placeholder="Город, район или курорт"
            defaultValue={query}
          />
          <select name="kind" defaultValue={kind} aria-label="Тип локации">
            <option value="">Все типы</option>
            {Object.entries(LOCATION_KINDS).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
          <button>Найти ↗</button>
        </form>
        <p className="muted">
          {query || kind
            ? `Найдено: ${result.total}`
            : "Города и направления · районы и курорты — внутри или через поиск"}
        </p>
        <div className="city-grid">
          {result.rows.map((c) => (
            <Link className="city-link" key={c.id} href={`/${c.slug}`}>
              <span>
                <strong>{c.name}</strong>
                <small>
                  {LOCATION_KINDS[c.kind as keyof typeof LOCATION_KINDS]} ·{" "}
                  {c.parent_name || c.region}
                  {Number(c.child_count) > 0
                    ? ` · внутри ${c.child_count}`
                    : ""}
                </small>
              </span>
              <span className="city-arrow">
                {c.is_hub ? "↳" : c.published_version ? "↗" : "→"}
              </span>
            </Link>
          ))}
        </div>
        {!result.total && (
          <p>Пока не нашли такую локацию. Попробуйте другое название.</p>
        )}
        <nav className="pagination" aria-label="Страницы справочника">
          {result.page > 1 && (
            <Link href={pageUrl(result.page - 1)}>← Назад</Link>
          )}
          <span>
            {result.page} / {result.pages}
          </span>
          {result.page < result.pages && (
            <Link href={pageUrl(result.page + 1)}>Дальше →</Link>
          )}
        </nav>
      </section>
    </main>
  );
}
