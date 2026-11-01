"use client";
import HoursEditor from "./HoursEditor";
import { useState } from "react";
import { saveContent } from "@/app/admin/actions";
type Restaurant = {
  id?: string;
  locationId?: string;
  locationName?: string;
  latitude?: number | null;
  longitude?: number | null;
  name: string;
  cuisine: string;
  description: string;
  mapsUrl: string;
  sources: string[];
  phone?: string;
  openingHours?: any;
  alexStar?: boolean;
};
export default function ContentEditor({
  locationId,
  initial,
  available = [],
}: {
  locationId: string;
  available?: Restaurant[];
  initial: {
    intro: string;
    restaurants: Restaurant[];
    rankingReport?: unknown[];
  };
}) {
  const [selected, setSelected] = useState("");
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
      <input type="hidden" name="locationId" value={locationId} />
      <input
        type="hidden"
        name="content"
        value={JSON.stringify({
          intro,
          restaurants,
          ...(initial.rankingReport
            ? { rankingReport: initial.rankingReport }
            : {}),
        })}
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
      {available.length > 0 && (
        <div className="panel">
          <h3>Из других локаций направления</h3>
          <p className="muted">
            Только опубликованные подборки дочерних территорий и вручную
            связанные рестораны. Общая страница собирается редактором.
          </p>
          <select
            aria-label="Ресторан из дочерних локаций"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            <option value="">Выберите ресторан</option>
            {available
              .filter((r) => !restaurants.some((item) => item.id === r.id))
              .map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} · {r.locationName}
                </option>
              ))}
          </select>
          <button
            type="button"
            className="secondary"
            disabled={!selected || restaurants.length >= 50}
            onClick={() => {
              const item = available.find((r) => r.id === selected);
              if (item) setRestaurants((items) => [...items, item]);
              setSelected("");
            }}
          >
            Добавить в черновик
          </button>
        </div>
      )}
      {restaurants.map((r, i) => (
        <fieldset key={r.id || i}>
          <legend>Место {i + 1}</legend>
          {r.locationName && (
            <span className="source-badge">
              {r.locationName}
              {r.locationId !== locationId
                ? " · общий ресторан, без копии в БД"
                : ""}
            </span>
          )}
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
            Телефон (обязателен для публикации)
            <input
              type="tel"
              value={r.phone || ""}
              placeholder="+78121234567"
              onChange={(e) =>
                edit(i, "phone", e.target.value.replace(/[ ()-]/g, ""))
              }
            />
          </label>
          <HoursEditor
            value={r.openingHours}
            onChange={(value) =>
              setRestaurants((items) =>
                items.map((item, n) =>
                  n === i ? { ...item, openingHours: value } : item,
                ),
              )
            }
          />
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
          <div className="map-coordinates">
            <label>
              Широта ресторана
              <input
                type="number"
                step="any"
                min={-90}
                max={90}
                value={r.latitude ?? ""}
                onChange={(e) =>
                  setRestaurants((items) =>
                    items.map((v, n) =>
                      n === i
                        ? {
                            ...v,
                            latitude:
                              e.target.value === ""
                                ? null
                                : Number(e.target.value),
                          }
                        : v,
                    ),
                  )
                }
              />
            </label>
            <label>
              Долгота ресторана
              <input
                type="number"
                step="any"
                min={-180}
                max={180}
                value={r.longitude ?? ""}
                onChange={(e) =>
                  setRestaurants((items) =>
                    items.map((v, n) =>
                      n === i
                        ? {
                            ...v,
                            longitude:
                              e.target.value === ""
                                ? null
                                : Number(e.target.value),
                          }
                        : v,
                    ),
                  )
                }
              />
            </label>
          </div>
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
          disabled={restaurants.length >= 50}
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
