"use client";
import { useState } from "react";
import { saveLocationAction } from "@/app/admin/actions";
const kinds: Record<string, string> = {
  city: "Город",
  settlement: "Населённый пункт",
  district: "Район",
  neighbourhood: "Городская зона",
  resort: "Курорт",
  tourist_area: "Туристическая территория",
  federal_territory: "Федеральная территория",
  destination: "Направление",
};
export default function LocationForm({
  location,
  options,
  relatedIds,
}: {
  location: any;
  options: { id: string; name: string; region: string; slug: string }[];
  relatedIds: string[];
}) {
  const [parentQuery, setParentQuery] = useState("");
  const [relatedQuery, setRelatedQuery] = useState("");
  const [parent, setParent] = useState(location?.parent_id || "");
  const [related, setRelated] = useState(relatedIds);
  const filter = (q: string) =>
    options
      .filter(
        (o) =>
          o.id !== location?.id &&
          (o.name + " " + o.region).toLowerCase().includes(q.toLowerCase()),
      )
      .slice(0, 100);
  return (
    <form action={saveLocationAction} className="stack location-form">
      <input type="hidden" name="id" value={location?.id || ""} />
      <input type="hidden" name="parentId" value={parent} />
      {related.map((id) => (
        <input key={id} type="hidden" name="relatedIds" value={id} />
      ))}
      <div className="field-grid">
        <label>
          Название
          <input
            name="name"
            defaultValue={location?.name}
            required
            maxLength={150}
          />
        </label>
        <label>
          Административный регион
          <input
            name="region"
            defaultValue={location?.region}
            required
            maxLength={200}
          />
        </label>
        <label>
          URL локации
          <input
            name="slug"
            defaultValue={location?.slug}
            pattern="[a-z0-9]+(-[a-z0-9]+)*"
            placeholder="sirius"
          />
          <small>
            При изменении адреса старый получит постоянное перенаправление.
          </small>
        </label>
        <label>
          Тип
          <select name="kind" defaultValue={location?.kind || "settlement"}>
            {Object.entries(kinds).map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Форма для заголовка
          <input
            name="inPhrase"
            defaultValue={location?.in_phrase}
            required
            minLength={3}
            maxLength={200}
            placeholder="в Сириусе / на Розе Хутор"
          />
          <small>Заголовок: «Где поесть» + эта форма.</small>
        </label>
        <label>
          Размер подборки
          <input
            name="topLimit"
            type="number"
            min={1}
            max={10}
            required
            defaultValue={location?.top_limit || 3}
          />
          <small>Верхний предел, а не обязательное число ресторанов.</small>
        </label>
        <label>
          Приоритет
          <select name="priority" defaultValue={location?.priority || "medium"}>
            <option value="high">Высокий</option>
            <option value="medium">Обычный</option>
            <option value="low">Низкий</option>
          </select>
        </label>
        <label>
          Население, если известно
          <input
            name="population"
            type="number"
            min={0}
            max={100000000}
            defaultValue={location?.population ?? ""}
          />
          <small>Справочная информация. На размер подборки не влияет.</small>
        </label>
      </div>
      <label>
        Альтернативные названия
        <textarea
          name="aliases"
          defaultValue={(location?.aliases || []).join("\n")}
          rows={3}
          placeholder="Газпром&#10;Лаура&#10;Альпика"
        />
      </label>
      <label>
        Надлокация: найти
        <input
          type="search"
          value={parentQuery}
          onChange={(e) => setParentQuery(e.target.value)}
          placeholder="Например, Сочи"
        />
      </label>
      <label>
        Родительская локация
        <select
          aria-label="Родительская локация"
          value={parent}
          onChange={(e) => setParent(e.target.value)}
        >
          <option value="">Без надлокации</option>
          {parent && !filter(parentQuery).some((o) => o.id === parent) && (
            <option value={parent}>
              {options.find((o) => o.id === parent)?.name || "Текущая локация"}
            </option>
          )}
          {filter(parentQuery).map((o) => (
            <option key={o.id} value={o.id}>
              {o.name} · {o.region}
            </option>
          ))}
        </select>
        <small>
          Редакционная иерархия; административный регион хранится отдельно.
          Показано до 100 совпадений.
        </small>
      </label>
      <label>
        Охват и ограничения
        <textarea
          name="coverageNote"
          defaultValue={location?.coverage_note}
          rows={3}
          maxLength={3000}
          placeholder="Какие территории включены, с чем нельзя путать"
        />
      </label>
      <div className="field-grid">
        <label>
          Широта центра
          <input
            name="latitude"
            type="number"
            step="any"
            min={-90}
            max={90}
            defaultValue={location?.latitude ?? ""}
          />
        </label>
        <label>
          Долгота центра
          <input
            name="longitude"
            type="number"
            step="any"
            min={-180}
            max={180}
            defaultValue={location?.longitude ?? ""}
          />
        </label>
      </div>
      <details>
        <summary>Границы охвата</summary>
        <label>
          Прямоугольник координат, JSON
          <textarea
            name="bounds"
            rows={3}
            defaultValue={location?.bounds || ""}
            placeholder={'{"west":39.8,"south":43.3,"east":40.1,"north":43.6}'}
          />
        </label>
        <small>
          Необязательно. Оставьте пустым, если границы не проверены.
          Прямоугольник — ориентир поиска, не административный полигон.
        </small>
      </details>
      <details>
        <summary>Связанные места · {related.length}</summary>
        <input
          type="search"
          aria-label="Найти связанную локацию"
          value={relatedQuery}
          onChange={(e) => setRelatedQuery(e.target.value)}
          placeholder="Место, которое стоит показать рядом"
        />
        {options
          .filter((o) => related.includes(o.id))
          .map((o) => (
            <label className="inline" key={o.id}>
              <input
                type="checkbox"
                checked
                onChange={() =>
                  setRelated((ids) => ids.filter((id) => id !== o.id))
                }
              />
              {o.name}
            </label>
          ))}
        {filter(relatedQuery)
          .filter((o) => !related.includes(o.id))
          .slice(0, 30)
          .map((o) => (
            <label className="inline" key={o.id}>
              <input
                type="checkbox"
                checked={false}
                onChange={() => setRelated((ids) => [...ids, o.id])}
              />
              {o.name} · {o.region}
            </label>
          ))}
      </details>
      <label className="inline">
        <input
          type="checkbox"
          name="isHub"
          defaultChecked={location?.is_hub || false}
        />{" "}
        Надлокация / направление
      </label>
      <label className="inline">
        <input
          type="checkbox"
          name="listed"
          defaultChecked={location ? location.listed : true}
        />{" "}
        Показывать в каталоге и навигации
      </label>
      <label className="inline">
        <input
          type="checkbox"
          name="populationVerified"
          defaultChecked={location?.population_verified || false}
        />{" "}
        Численность населения проверена
      </label>
      <label className="inline">
        <input
          type="checkbox"
          name="active"
          defaultChecked={location ? !!location.active : true}
        />{" "}
        Страница доступна на сайте
      </label>
      <button>Сохранить локацию</button>
    </form>
  );
}
