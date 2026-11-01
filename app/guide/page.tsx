import Link from "next/link";
import { all, one } from "@/lib/db.mjs";
export const dynamic = "force-dynamic";
export default async function Guide({
  searchParams,
}: {
  searchParams: Promise<{
    q?: string;
  }>;
}) {
  const { q = "" } = await searchParams;
  const query = q.trim().slice(0, 100);
  const cities = await all(
    "SELECT * FROM cities WHERE active=1 AND (name LIKE $1 OR region LIKE $2) ORDER BY name",
    `%${query}%`,
    `%${query}%`,
  );
  const total = (await one("SELECT count(*) AS n FROM cities WHERE active=1"))!;
  return (
    <main>
      <section className="hero">
        <p className="eyebrow">ЕДА. ГОРОДА. ЛИЧНЫЙ ОПЫТ.</p>
        <h1>
          Хорошие места.
          <br />
          <span className="serif">Без лишних слов.</span>
        </h1>
        <div className="hero-bottom">
          <p>
            Короткий список ресторанов, ради которых стоит выйти в город.
            Выбираем внимательно. Прислушиваемся к вам.
          </p>
          <span className="counter">
            {Number(total.n).toLocaleString("ru-RU")}
            <small>городов в справочнике</small>
          </span>
        </div>
      </section>
      <section className="cities">
        <div className="section-heading">
          <h2>С какого города начнём?</h2>
          <span className="muted">01 / География</span>
        </div>
        <form className="search">
          <label className="sr-only" htmlFor="q">
            Название города или региона
          </label>
          <input
            id="q"
            name="q"
            placeholder="Найти город или регион"
            defaultValue={query}
          />
          <button>Найти ↗</button>
        </form>
        <p className="muted">
          {query
            ? `Найдено: ${cities.length}`
            : "Все города России · подборки наполняются редакцией"}
        </p>
        <div className="city-grid">
          {cities.map((c) => (
            <Link className="city-link" key={c.id} href={`/guide/${c.slug}`}>
              <span>
                <strong>{c.name}</strong>
                <small>{c.region}</small>
              </span>
              <span className="city-arrow">
                {c.published_version ? "↗" : "→"}
              </span>
            </Link>
          ))}
        </div>
        {!cities.length && (
          <p>Такого города пока нет. Попробуйте другое название.</p>
        )}
      </section>
    </main>
  );
}
