"use client";
import { useState } from "react";
import { saveContent } from "@/app/admin/actions";
type Restaurant = {
  id?: string;
  name: string;
  cuisine: string;
  description: string;
  mapsUrl: string;
  sources: string[];
};
export default function ContentEditor({
  cityId,
  initial,
}: {
  cityId: string;
  initial: { intro: string; restaurants: Restaurant[] };
}) {
  const [intro, setIntro] = useState(initial.intro);
  const [restaurants, setRestaurants] = useState(initial.restaurants);
  function edit(i: number, key: keyof Restaurant, value: string) {
    setRestaurants((rs) =>
      rs.map((r, n) => (n === i ? { ...r, [key]: value } : r)),
    );
  }
  function move(i: number, step: number) {
    const next = [...restaurants];
    const j = i + step;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    setRestaurants(next);
  }
  return (
    <form action={saveContent} className="stack">
      <input type="hidden" name="cityId" value={cityId} />
      <input
        type="hidden"
        name="content"
        value={JSON.stringify({ intro, restaurants })}
      />
      <label>
        Вступление
        <textarea
          value={intro}
          onChange={(e) => setIntro(e.target.value)}
          rows={3}
          maxLength={4000}
        />
      </label>
      {restaurants.map((r, i) => (
        <fieldset key={r.id || i}>
          <legend>Место {i + 1}</legend>
          <div className="button-row">
            <button
              type="button"
              className="secondary"
              disabled={i === 0}
              onClick={() => move(i, -1)}
            >
              ↑ Выше
            </button>
            <button
              type="button"
              className="secondary"
              disabled={i === restaurants.length - 1}
              onClick={() => move(i, 1)}
            >
              ↓ Ниже
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() =>
                setRestaurants((rs) => rs.filter((_, n) => n !== i))
              }
            >
              Убрать из топа
            </button>
          </div>
          <label>
            Название
            <input
              value={r.name}
              onChange={(e) => edit(i, "name", e.target.value)}
              required
              maxLength={200}
            />
          </label>
          <label>
            Кухня
            <input
              value={r.cuisine}
              onChange={(e) => edit(i, "cuisine", e.target.value)}
              required
              maxLength={300}
            />
          </label>
          <label>
            Почему стоит зайти
            <textarea
              value={r.description}
              onChange={(e) => edit(i, "description", e.target.value)}
              required
              rows={3}
              maxLength={2000}
            />
          </label>
          <label>
            Яндекс Карты
            <input
              type="url"
              value={r.mapsUrl}
              onChange={(e) => edit(i, "mapsUrl", e.target.value)}
              required
              placeholder="https://yandex.ru/maps/…"
            />
          </label>
          {r.sources?.length > 0 && (
            <div className="sources">
              Источники:{" "}
              {r.sources.map((s) => (
                <a key={s} href={s} target="_blank" rel="noreferrer">
                  {new URL(s).hostname} ↗{" "}
                </a>
              ))}
            </div>
          )}
        </fieldset>
      ))}
      <div className="button-row">
        <button
          type="button"
          className="secondary"
          disabled={restaurants.length >= 10}
          onClick={() =>
            setRestaurants((rs) => [
              ...rs,
              {
                name: "",
                cuisine: "",
                description: "",
                mapsUrl: "",
                sources: [],
              },
            ])
          }
        >
          + Ресторан
        </button>
        <button>Сохранить новую версию</button>
      </div>
      <small>
        Порядок карточек задаёт места в топе. Сохранение создаёт черновик; сайт
        обновится после публикации.
      </small>
    </form>
  );
}
