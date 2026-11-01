import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import LocationForm from "@/components/LocationForm";
import {
  searchLocations,
  LOCATION_KINDS,
  ancestors,
  childLocations,
  relatedLocations,
  availableRestaurants,
} from "@/lib/locations.mjs";
import { isAdmin } from "@/lib/auth";
import { all, one, locationContent, setting } from "@/lib/db.mjs";
import { reviewSignals } from "@/lib/signals.mjs";
import ContentEditor from "@/components/ContentEditor";
import QueueRunner from "@/components/QueueRunner";
import RankingReport from "@/components/RankingReport";
import * as actions from "./actions";
export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "Редакция",
  robots: { index: false, follow: false },
};
const time = (s: string) => new Date(s + "Z").toLocaleString("ru-RU");
const labels: Record<string, string> = {
  queued: "В очереди",
  running: "Обрабатывается",
  done: "Готово",
  failed: "Ошибка",
  cancelled: "Отменено",
  pending: "Ожидает решения",
  approved: "Одобрено",
  rejected: "Отклонено",
  processed: "Разобрано",
  review: "Отзыв",
  rating: "Оценка",
  edit: "Правка",
  restaurant: "Новый ресторан",
};
function Snapshot({ content }: { content: any }) {
  return (
    <div className="snapshot">
      <p>{content.intro || "Без вступления"}</p>
      {content.restaurants.map((r: any, i: number) => (
        <div key={r.id || i}>
          <strong>
            {i + 1}. {r.name}
          </strong>
          <small>{r.cuisine}</small>
          <p>{r.description}</p>
          <a href={r.mapsUrl} target="_blank" rel="noreferrer">
            Карты ↗
          </a>
        </div>
      ))}
    </div>
  );
}
export default async function Admin({
  searchParams,
}: {
  searchParams: Promise<{
    tab?: string;
    location?: string;
    city?: string;
    kind?: string;
    parent?: string;
    state?: string;
    page?: string;
    review?: string;
    q?: string;
    message?: string;
    version?: string;
    filter?: string;
  }>;
}) {
  const p = await searchParams;
  if (p.tab === "cities" || p.city) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(p))
      if (v)
        params.set(
          k === "city" ? "location" : k,
          k === "tab" && v === "cities" ? "locations" : v,
        );
    redirect("/admin?" + params);
  }

  if (!(await isAdmin()))
    return (
      <main className="login">
        <p className="eyebrow">ИНВОЛЬТА / РЕДАКЦИЯ</p>
        <h1>Вход.</h1>
        <p>Работа с подборками и голосовыми отзывами.</p>
        {p.message && (
          <p className="notice" role="alert">
            {p.message}
          </p>
        )}
        <form action={actions.login} className="stack">
          <label>
            Пароль редакции
            <input
              type="password"
              name="password"
              required
              autoComplete="current-password"
            />
          </label>
          <button>Войти ↗</button>
        </form>
      </main>
    );
  const listing = await searchLocations({
    query: p.q || "",
    kind: p.kind || "",
    parentId: p.parent || null,
    status: p.state || "all",
    page: Number(p.page || 1),
    pageSize: 60,
  });
  const tab = p.tab || "overview";
  const counts = (await one(
    `SELECT count(*) AS total,count(*) FILTER(WHERE listed OR published_version IS NOT NULL) AS public_count,count(*) FILTER(WHERE NOT listed AND published_version IS NULL) AS internal_count,sum(CASE WHEN draft='[]' AND (listed OR published_version IS NOT NULL) THEN 1 ELSE 0 END) AS empty,sum(CASE WHEN published_version IS NOT NULL THEN 1 ELSE 0 END) AS published FROM locations WHERE active=1`,
  ))!;
  const reviewCount = (await one(
    "SELECT count(*) n FROM reviews WHERE moderation='pending'",
  ))!.n;
  return (
    <main className="admin">
      <div className="section-heading">
        <div>
          <p className="eyebrow">ИНВОЛЬТА / РЕДАКЦИЯ</p>
          <h1>Рабочий стол.</h1>
        </div>
        <form action={actions.logout}>
          <button className="secondary">Выйти</button>
        </form>
      </div>
      <nav className="tabs">
        {[
          ["overview", "Обзор"],
          ["locations", "Локации и страницы"],
          ["fill", "Наполнение"],
          ["reviews", `Отзывы · ${reviewCount}`],
        ].map(([key, label]) => (
          <Link
            key={key}
            className={tab === key ? "selected" : ""}
            href={"/admin?tab=" + key}
          >
            {label}
          </Link>
        ))}
      </nav>
      {p.message && (
        <p role="status" className="notice">
          {p.message}
        </p>
      )}
      {tab === "overview" && (
        <>
          <div className="stats">
            {[
              [counts.public_count, "локаций в каталоге"],
              [counts.internal_count, "внутренних территорий"],
              [counts.empty, "без подборки"],
              [counts.published, "опубликовано"],
              [reviewCount, "отзывов на проверке"],
            ].map(([n, label]) => (
              <div key={String(label)}>
                <strong>{n}</strong>
                <span>{label}</span>
              </div>
            ))}
          </div>
          <section className="panel">
            <h2>Порядок работы</h2>
            <p>
              Настройте промпт, запустите одну локацию, проверьте черновик и
              опубликуйте. После проверки можно поставить остальные локации в
              очередь.
            </p>
            <p>
              Голосовые отзывы сохраняются все. Одобренные отзывы можно
              учитывать в подборке; предложения ИИ сначала попадают в черновик.
              Публикация — отдельное действие.
            </p>
            <Link href="/admin?tab=fill">Начать наполнение ↗</Link>
          </section>
          <section className="panel">
            <h2>Подключение ИИ</h2>
            <p>
              {process.env.OPENAI_API_KEY
                ? "Ключ задан. Доступность API проверяется при выполнении задания."
                : "Ключ OpenAI не задан. Ручное редактирование, публикация и запись отзывов доступны. Для ИИ задайте OPENAI_API_KEY на сервере и перезапустите приложение и воркер."}
            </p>
            <p className="muted">
              Автоматическая обработка очереди: npm run worker. До подключения
              воркера можно выполнять по одному заданию во вкладке «Наполнение».
            </p>
          </section>
        </>
      )}
      {tab === "locations" && (
        <div className="admin-columns">
          <aside>
            <form className="stack">
              <input type="hidden" name="tab" value="locations" />
              <input
                name="q"
                aria-label="Поиск локации"
                placeholder="Локация или регион"
                defaultValue={p.q}
              />
              <select
                name="kind"
                aria-label="Тип локации в админке"
                defaultValue={p.kind || ""}
              >
                <option value="">Все типы</option>
                {Object.entries(LOCATION_KINDS).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
              <select
                name="state"
                aria-label="Статус локации"
                defaultValue={p.state || "all"}
              >
                <option value="all">Все статусы</option>
                <option value="active">Включённые</option>
                <option value="inactive">Отключённые</option>
                <option value="empty">Без подборки</option>
                <option value="draft">Есть черновик</option>
                <option value="published">Опубликованные</option>
                <option value="hidden">Внутренний справочник</option>
              </select>
              <select
                name="parent"
                aria-label="Внутри направления"
                defaultValue={p.parent || ""}
              >
                <option value="">Все направления</option>
                {(
                  await all(
                    "SELECT id,name,region FROM locations WHERE is_hub=true ORDER BY name",
                  )
                ).map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name} · {l.region}
                  </option>
                ))}
              </select>
              <button>→</button>
            </form>
            <a href="/api/admin/locations-export" className="add-link">
              Выгрузить справочник JSON ↗
            </a>
            <Link href="/admin?tab=locations&location=new" className="add-link">
              + Добавить локацию
            </Link>
            <div className="city-sidebar">
              {listing.rows.map((c) => (
                <Link
                  className={p.location === c.id ? "active" : ""}
                  key={c.id}
                  href={`/admin?tab=locations&location=${c.id}&q=${encodeURIComponent(p.q || "")}`}
                >
                  <strong>{c.name}</strong>
                  <small>
                    {LOCATION_KINDS[c.kind as keyof typeof LOCATION_KINDS]} ·{" "}
                    {c.parent_name || c.region}
                    {!c.listed && !c.published_version
                      ? " · внутренний справочник"
                      : ""}{" "}
                    ·{" "}
                    {!c.active
                      ? "отключён"
                      : c.published_version
                        ? "опубликован"
                        : c.draft === "[]"
                          ? "пустой"
                          : "черновик"}
                  </small>
                </Link>
              ))}
            </div>
            <nav className="pagination">
              <span>
                {listing.total} · {listing.page}/{listing.pages}
              </span>
              {listing.page > 1 && (
                <Link
                  href={
                    "/admin?" +
                    new URLSearchParams({
                      tab: "locations",
                      q: p.q || "",
                      kind: p.kind || "",
                      state: p.state || "all",
                      parent: p.parent || "",
                      page: String(listing.page - 1),
                    })
                  }
                >
                  ←
                </Link>
              )}
              {listing.page < listing.pages && (
                <Link
                  href={
                    "/admin?" +
                    new URLSearchParams({
                      tab: "locations",
                      q: p.q || "",
                      kind: p.kind || "",
                      state: p.state || "all",
                      parent: p.parent || "",
                      page: String(listing.page + 1),
                    })
                  }
                >
                  →
                </Link>
              )}
            </nav>
          </aside>
          <section>
            {p.location ? (
              <LocationEditor locationId={p.location} versionId={p.version} />
            ) : (
              <div className="empty">
                <h2>Выберите локацию</h2>
                <p>
                  Здесь — ручное редактирование, публикация и история страницы.
                </p>
              </div>
            )}
          </section>
        </div>
      )}
      {tab === "fill" && (
        <>
          <section className="panel">
            <h2>Промпт наполнения</h2>
            <p className="muted">
              Переменные: {"{локация}"}, {"{регион}"}, {"{лимит}"}. Промпт
              каждого запуска сохраняется в истории соответствующей страницы.
            </p>
            <form action={actions.savePrompt} className="stack">
              <label>
                Инструкция для GPT
                <textarea
                  name="prompt"
                  defaultValue={await setting("prompt")}
                  rows={9}
                  required
                  minLength={50}
                  maxLength={16000}
                />
              </label>
              <button>Сохранить промпт</button>
            </form>
          </section>
          <section className="panel">
            <h2>Сначала одна локация</h2>
            <p>
              Генерация использует сохранённый промпт. Результат появится в
              черновике, затем вы сможете его отредактировать и опубликовать.
            </p>
            <form action={actions.generate} className="button-row">
              <select
                name="locationId"
                aria-label="Локация для пробного запуска"
              >
                {(
                  await all(
                    "SELECT id,name,region FROM locations WHERE active=1 ORDER BY name",
                  )
                ).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.region}
                  </option>
                ))}
              </select>
              <button>Создать черновик ↗</button>
            </form>
            <details>
              <summary>Массовое наполнение</summary>
              <p>
                Каждый локация — отдельный платный API-запрос. Автоматической
                публикации нет. Одновременные задания одной локации исключены.
              </p>
              <form action={actions.generate} className="button-row">
                <select name="scope" aria-label="Область наполнения">
                  <option value="empty">
                    Только пустые локации ({counts.empty})
                  </option>
                  <option value="all">
                    Все локации каталога ({counts.public_count})
                  </option>
                </select>
                <button>Поставить в очередь</button>
              </form>
            </details>
          </section>
          <section className="panel">
            <div className="section-heading">
              <h2>Очередь</h2>
              <QueueRunner />
            </div>
            <p className="muted">
              Последние 100 заданий. Для фоновой работы запустите npm run
              worker. Ошибки не повторяются автоматически.
            </p>
            <div className="button-row">
              <form action={actions.cancelJobs}>
                <button className="secondary">
                  Отменить ожидающие задания
                </button>
              </form>
              <Link href="/admin?tab=fill">Обновить ↻</Link>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Задание</th>
                    <th>Локация</th>
                    <th>Статус</th>
                    <th>Подробности</th>
                  </tr>
                </thead>
                <tbody>
                  {(
                    await all(
                      "SELECT j.*,c.name FROM jobs j LEFT JOIN locations c ON c.id=j.location_id ORDER BY j.created_at DESC,j.id DESC LIMIT 100",
                    )
                  ).map((j) => (
                    <tr key={j.id}>
                      <td>
                        {j.kind === "generate" ? "Наполнение" : "Разбор отзыва"}
                        <small>{time(j.created_at)}</small>
                      </td>
                      <td>
                        <Link
                          href={
                            "/admin?tab=locations&location=" + j.location_id
                          }
                        >
                          {j.name}
                        </Link>
                      </td>
                      <td>{labels[j.status]}</td>
                      <td>
                        {j.error}
                        {j.status === "failed" && (
                          <form action={actions.retryJob}>
                            <input name="id" type="hidden" value={j.id} />
                            <button className="secondary">Повторить</button>
                          </form>
                        )}
                        {j.review_id && (
                          <Link
                            href={"/admin?tab=reviews&review=" + j.review_id}
                          >
                            К отзыву ↗
                          </Link>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
      {tab === "reviews" && (
        <div className="admin-columns review-columns">
          <aside>
            <form className="stack">
              <input type="hidden" name="tab" value="reviews" />
              <select
                name="filter"
                aria-label="Фильтр отзывов"
                defaultValue={p.filter || "pending"}
              >
                <option value="pending">На проверке</option>
                <option value="all">Все отзывы</option>
                <option value="approved">Одобренные</option>
                <option value="rejected">Отклонённые / фрод</option>
              </select>
              <button className="secondary">Показать</button>
            </form>
            <div className="city-sidebar">
              {(
                await all(
                  `SELECT r.*,c.name FROM reviews r JOIN locations c ON c.id=r.location_id ${p.filter === "all" ? "" : "WHERE r.moderation=$1"} ORDER BY r.created_at DESC LIMIT 100`,
                  ...(p.filter === "all"
                    ? []
                    : [
                        p.filter === "approved"
                          ? "approved"
                          : p.filter === "rejected"
                            ? "rejected"
                            : "pending",
                      ]),
                )
              ).map((r) => (
                <Link
                  key={r.id}
                  href={
                    "/admin?tab=reviews&review=" +
                    r.id +
                    "&filter=" +
                    (p.filter || "pending")
                  }
                  className={p.review === r.id ? "active" : ""}
                >
                  <strong>
                    {r.name} {r.fraud ? "· ФРОД" : ""}
                  </strong>
                  <small>
                    {time(r.created_at)} · {labels[r.status]}
                  </small>
                  <span>
                    {r.transcript?.slice(0, 100) || "Аудио ожидает расшифровки"}
                  </span>
                </Link>
              ))}
            </div>
          </aside>
          <section>
            {p.review ? (
              <ReviewEditor reviewId={p.review} />
            ) : (
              <div className="empty">
                <h2>Выберите отзыв</h2>
                <p>Здесь будет запись, разбор ИИ и предложения для страницы.</p>
              </div>
            )}
          </section>
        </div>
      )}
    </main>
  );
}
async function LocationEditor({
  locationId,
  versionId,
}: {
  locationId: string;
  versionId?: string;
}) {
  const c =
    locationId === "new"
      ? null
      : await one("SELECT * FROM locations WHERE id=$1", locationId);
  if (locationId !== "new" && !c) return <p>Локация не найдена</p>;
  const versions = c
    ? await all(
        "SELECT * FROM versions WHERE location_id=$1 ORDER BY number DESC",
        locationId,
      )
    : [];
  const selected = versionId
    ? versions.find((v) => v.id === versionId)
    : versions[0];
  const pub = c?.published_version
    ? await one("SELECT content FROM versions WHERE id=$1", c.published_version)
    : null;
  return (
    <>
      <section className="panel">
        <h2>{c ? c.name : "Новая локация"}</h2>
        <LocationForm
          key={c?.id || "new"}
          location={c}
          options={(
            await all("SELECT id,name,region,slug FROM locations ORDER BY name")
          ).map((l) => ({
            id: l.id,
            name: l.name,
            region: l.region,
            slug: l.slug,
          }))}
          relatedIds={
            c
              ? (
                  await relatedLocations(locationId, { includeHidden: true })
                ).map((l) => l.id)
              : []
          }
        />
        {c && c.source_metadata && c.source_metadata !== "{}" && (
          <details>
            <summary>Источник и исходные данные</summary>
            <pre>{JSON.stringify(JSON.parse(c.source_metadata), null, 2)}</pre>
          </details>
        )}
        {c && (
          <Link href={"/guide/" + c.slug} target="_blank">
            Открыть страницу ↗
          </Link>
        )}
      </section>
      {c && (
        <>
          <section className="panel">
            <h2>Иерархия и охват</h2>
            <div className="button-row">
              {(await ancestors(locationId)).map((a) => (
                <Link key={a.id} href={"/admin?tab=locations&location=" + a.id}>
                  {a.name} →
                </Link>
              ))}
              <strong>{c.name}</strong>
            </div>
            <div className="location-tree">
              {(await childLocations(locationId, { includeHidden: true })).map(
                (l) => (
                  <Link
                    key={l.id}
                    href={"/admin?tab=locations&location=" + l.id}
                  >
                    {l.name} ·{" "}
                    {LOCATION_KINDS[l.kind as keyof typeof LOCATION_KINDS]}
                  </Link>
                ),
              )}
            </div>
            <form action={actions.attachRestaurant} className="stack">
              <h3>Связать существующий ресторан</h3>
              <input type="hidden" name="locationId" value={locationId} />
              <label>
                Ресторан
                <select name="restaurantId">
                  {(
                    await all(
                      "SELECT r.id,r.name,l.name location_name FROM restaurants r JOIN locations l ON l.id=r.location_id ORDER BY r.name LIMIT 1000",
                    )
                  ).map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name} · {r.location_name}
                    </option>
                  ))}
                </select>
              </label>
              <button className="secondary">Связать с локацией</button>
              <small>
                Создаётся связь, без копии ресторана и без публикации.
              </small>
            </form>
          </section>
          <section className="panel">
            <div className="section-heading">
              <h2>Черновик страницы</h2>
              <form action={actions.generate}>
                <input type="hidden" name="locationId" value={locationId} />
                <button className="secondary">Наполнить через GPT ↗</button>
              </form>
            </div>
            <RankingReport
              report={(await locationContent(locationId)).rankingReport || []}
            />
            <ContentEditor
              key={c.draft_version || locationId}
              locationId={locationId}
              initial={await locationContent(locationId)}
              available={(await availableRestaurants(locationId)).map((r) => ({
                id: r.id,
                name: r.name,
                cuisine: r.cuisine,
                description: r.description,
                mapsUrl: r.maps_url,
                sources: JSON.parse(r.sources),
                locationId: r.location_id,
                locationName: r.location_name,
              }))}
            />
            {c.draft_version && (
              <form action={actions.publishVersion} className="publish">
                <input type="hidden" name="locationId" value={locationId} />
                <input type="hidden" name="versionId" value={c.draft_version} />
                <button>Опубликовать текущий черновик ↗</button>
                <small>Публичная страница изменится сразу после нажатия.</small>
              </form>
            )}
          </section>
          <section className="panel">
            <h2>История страницы</h2>
            {!versions.length ? (
              <p>Версии появятся после первого сохранения или генерации.</p>
            ) : (
              <>
                <div className="version-links">
                  {versions.map((v) => (
                    <Link
                      key={v.id}
                      className={selected?.id === v.id ? "selected" : ""}
                      href={`/admin?tab=locations&location=${locationId}&version=${v.id}`}
                    >
                      v{v.number} · {v.source}{" "}
                      {v.id === c.published_version ? "· на сайте" : ""}
                    </Link>
                  ))}
                </div>
                {selected && (
                  <>
                    <p className="muted">
                      v{selected.number} · {time(selected.created_at)} ·{" "}
                      {selected.model || "редактор"}
                    </p>
                    <div className="compare">
                      <div>
                        <h3>Сейчас на сайте</h3>
                        {pub ? (
                          <Snapshot content={JSON.parse(pub.content)} />
                        ) : (
                          <p>Ещё не опубликовано</p>
                        )}
                      </div>
                      <div>
                        <h3>Выбранная версия v{selected.number}</h3>
                        <Snapshot content={JSON.parse(selected.content)} />
                      </div>
                    </div>
                    {selected.prompt && (
                      <details>
                        <summary>Промпт этого запуска</summary>
                        <pre>{selected.prompt}</pre>
                      </details>
                    )}
                    <div className="button-row">
                      <form action={actions.restoreVersion}>
                        <input
                          type="hidden"
                          name="locationId"
                          value={locationId}
                        />
                        <input
                          type="hidden"
                          name="versionId"
                          value={selected.id}
                        />
                        <button className="secondary">
                          Восстановить в новый черновик
                        </button>
                      </form>
                      <form action={actions.publishVersion}>
                        <input
                          type="hidden"
                          name="locationId"
                          value={locationId}
                        />
                        <input
                          type="hidden"
                          name="versionId"
                          value={selected.id}
                        />
                        <button>Опубликовать эту версию</button>
                      </form>
                    </div>
                  </>
                )}
              </>
            )}
          </section>
          <details className="panel">
            <summary>База ресторанов локации</summary>
            {(
              await all(
                "SELECT * FROM restaurants WHERE location_id=$1 ORDER BY name",
                locationId,
              )
            ).map((r) => (
              <p key={r.id}>
                <strong>{r.name}</strong> · {r.cuisine}
                <small>{r.id}</small>
              </p>
            ))}
          </details>
        </>
      )}
    </>
  );
}
async function ReviewEditor({ reviewId }: { reviewId: string }) {
  const r = await one(
    "SELECT r.*,c.name,c.slug FROM reviews r JOIN locations c ON c.id=r.location_id WHERE r.id=$1",
    reviewId,
  );
  if (!r) return <p>Отзыв не найден</p>;
  const result = r.result ? JSON.parse(r.result) : null;
  const signals = reviewSignals(
    r,
    await all(
      "SELECT * FROM reviews WHERE id<>$1 ORDER BY created_at DESC LIMIT 2000",
      reviewId,
    ),
  );
  const current = await locationContent(r.location_id);
  const proposals = await all(
    "SELECT * FROM proposals WHERE review_id=$1",
    reviewId,
  );
  return (
    <>
      <section className="panel">
        <div className="section-heading">
          <h2>{r.name} · отзыв</h2>
          <Link href={"/admin?tab=locations&location=" + r.location_id}>
            К странице локации ↗
          </Link>
        </div>
        <p className="muted">
          {time(r.created_at)} · {labels[r.moderation]}{" "}
          {r.fraud ? "· ФРОД" : ""}
        </p>
        <audio controls preload="none" src={"/api/admin/audio/" + r.id} />
        <h3>Расшифровка</h3>
        <p className="transcript">
          {r.transcript || "Пока нет. Запустите обработку очереди."}
        </p>
        {r.error && <p className="error">{r.error}</p>}
        <form action={actions.moderate} className="stack">
          <input type="hidden" name="id" value={r.id} />
          <label>
            Причина решения / заметка
            <textarea
              name="reason"
              defaultValue={r.reason}
              rows={2}
              maxLength={2000}
            />
          </label>
          <div className="button-row">
            <button name="decision" value="approved">
              Одобрить отзыв
            </button>
            <button name="decision" value="rejected" className="secondary">
              Не одобрять
            </button>
            <button name="decision" value="fraud" className="secondary">
              Пометить фродом
            </button>
            <button name="decision" value="reset" className="secondary">
              Вернуть на проверку
            </button>
          </div>
        </form>
        <small>
          Запись сохраняется при любом решении. Неодобренные и фродовые отзывы
          исключаются из внутренних оценок.
        </small>
      </section>
      {result && (
        <section className="panel">
          <h2>Разбор ИИ</h2>
          {result.entries.map((e: any, i: number) => (
            <article className="entry" key={i}>
              <h3>
                {e.restaurantName || "Ресторан не определён"} · {labels[e.type]}
              </h3>
              <p>{e.text || "Оценка без текста"}</p>
              <blockquote>{e.quote}</blockquote>
              <div className="signal-grid">
                <span>
                  Тональность <b>{e.sentiment ?? "—"}</b>
                </span>
                <span>
                  Рекомендация <b>{e.recommendation ?? "—"}</b>
                </span>
                <span>
                  Оценка <b>{e.rating ?? "—"}</b>
                </span>
                <span>
                  Уверенность извлечения{" "}
                  <b>{Math.round(e.confidence * 100)}%</b>
                </span>
              </div>
              <small>
                {e.topics.join(", ")} ·{" "}
                {e.experience === "personal"
                  ? "личный опыт"
                  : e.experience === "hearsay"
                    ? "пересказ"
                    : "тип опыта неизвестен"}
              </small>
            </article>
          ))}
          <p className="muted">
            Уверенность модели не подтверждает правдивость рассказа. Эти оценки
            сами не меняют топ.
          </p>
        </section>
      )}
      <section className="panel">
        <h2>Предлагаемые изменения · {proposals.length}</h2>
        {proposals.length === 0 && (
          <p>
            Дополнительных предложений нет. Редактор может вручную изменить
            подборку локации.
          </p>
        )}
        {proposals.map((p) => {
          const payload = JSON.parse(p.payload);
          const old = current.restaurants.find(
            (v: any) => v.id === payload.restaurantId,
          );
          return (
            <article className="proposal" key={p.id}>
              <h3>
                {labels[p.kind]} · {labels[p.status]}
              </h3>
              <p>{payload.reason}</p>
              <div className="compare">
                <div>
                  <h4>Было в черновике</h4>
                  {old ? (
                    <Snapshot content={{ intro: "", restaurants: [old] }} />
                  ) : (
                    <p>
                      {p.kind === "restaurant"
                        ? "Нового ресторана ещё нет"
                        : "Ресторан не сопоставлен"}
                    </p>
                  )}
                </div>
                <form action={actions.proposalDecision} className="stack">
                  <h4>Станет</h4>
                  <input name="id" type="hidden" value={p.id} />
                  <label>
                    Название
                    <input
                      name="name"
                      defaultValue={payload.name || old?.name}
                      maxLength={200}
                    />
                  </label>
                  <label>
                    Кухня
                    <input
                      name="cuisine"
                      defaultValue={payload.cuisine || old?.cuisine}
                      maxLength={300}
                    />
                  </label>
                  <label>
                    Описание
                    <textarea
                      name="description"
                      defaultValue={payload.description || old?.description}
                      rows={4}
                      maxLength={2000}
                    />
                  </label>
                  <label>
                    Яндекс Карты
                    <input
                      name="mapsUrl"
                      type="url"
                      defaultValue={payload.mapsUrl || old?.mapsUrl}
                      placeholder="Проверьте и добавьте ссылку"
                    />
                  </label>
                  <div className="button-row">
                    <button
                      name="decision"
                      value="approve"
                      disabled={p.status !== "pending"}
                    >
                      В черновик
                    </button>
                    <button
                      name="decision"
                      value="reject"
                      className="secondary"
                      disabled={p.status !== "pending"}
                    >
                      Отклонить
                    </button>
                  </div>
                </form>
              </div>
            </article>
          );
        })}
      </section>
      <section className="panel">
        <h2>Косвенные признаки и похожие отзывы</h2>
        <p className="muted">
          Совпадения помогают проверке, но не доказывают фрод. Несогласие с
          большинством не считается фродом.
        </p>
        <p>
          Просмотрены последние 2000 записей. Найдено связанных:{" "}
          {signals.length} (до 20).
        </p>
        {signals.map((s: any) => (
          <p key={s.id}>
            <Link href={"/admin?tab=reviews&review=" + s.id}>
              {time(s.created_at)} ↗
            </Link>{" "}
            · {s.sameAudio ? "одинаковое аудио · " : ""}
            {s.sameVisitor ? "тот же браузер · " : ""}
            {s.sameIp ? "общий IP · " : ""}сходство слов{" "}
            {Math.round(s.similarity * 100)}%
          </p>
        ))}
        <details>
          <summary>Технические признаки</summary>
          <p>Cookie: {r.visitor}</p>
          <p>Хеш аудио: {r.hash}</p>
          <p>Хеш IP: {r.ip_hash}</p>
          <p>Браузер: {r.device}</p>
        </details>
      </section>
      <section className="panel">
        <h2>Внутренние оценки ресторанов локации</h2>
        <p className="muted">
          Только одобренные, без фрода. Публичные места в топе задаёт редактор.
        </p>
        <InternalStats locationId={r.location_id} />
      </section>
    </>
  );
}
async function InternalStats({ locationId }: { locationId: string }) {
  const rows = await all(
    "SELECT result,visitor FROM reviews WHERE location_id=$1 AND moderation='approved' AND fraud=0 AND result IS NOT NULL",
    locationId,
  );
  const groups = new Map<string, any>();
  for (const r of rows) {
    for (const e of JSON.parse(r.result).entries) {
      const key = e.restaurantId || e.restaurantName;
      if (!key) continue;
      const group = groups.get(key) || {
        name: e.restaurantName,
        visitors: new Set(),
        sentiment: [],
        recommendation: [],
        rating: [],
      };
      group.visitors.add(r.visitor);
      for (const attr of ["sentiment", "recommendation", "rating"])
        if (e[attr] !== null) group[attr].push(e[attr]);
      groups.set(key, group);
    }
  }
  return (
    <div>
      {[...groups.values()].map((g, i) => (
        <p key={i}>
          <strong>{g.name}</strong> · авторов: {g.visitors.size} ·{" "}
          {["sentiment", "recommendation", "rating"]
            .map(
              (k, n) =>
                `${["тональность", "рекомендация", "оценка"][n]}: ${g[k].length ? (g[k].reduce((a: number, b: number) => a + b, 0) / g[k].length).toFixed(1) : "—"}`,
            )
            .join(" · ")}
        </p>
      ))}
      {!groups.size && <p>Одобренных оценок пока нет.</p>}
    </div>
  );
}
