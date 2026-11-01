import type { Metadata } from "next";
import Link from "next/link";
import { isAdmin } from "@/lib/auth";
import { all, one, cityContent, setting } from "@/lib/db.mjs";
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
    city?: string;
    review?: string;
    q?: string;
    message?: string;
    version?: string;
    filter?: string;
  }>;
}) {
  const p = await searchParams;
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
  const tab = p.tab || "overview";
  const counts = (await one(
    `SELECT count(*) AS total,sum(CASE WHEN draft='[]' THEN 1 ELSE 0 END) AS empty,sum(CASE WHEN published_version IS NOT NULL THEN 1 ELSE 0 END) AS published FROM cities WHERE active=1`,
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
          ["cities", "Города и страницы"],
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
              [counts.total, "городов"],
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
              Настройте промпт, запустите один город, проверьте черновик и
              опубликуйте. После проверки можно поставить остальные города в
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
      {tab === "cities" && (
        <div className="admin-columns">
          <aside>
            <form className="search">
              <input type="hidden" name="tab" value="cities" />
              <input
                name="q"
                aria-label="Поиск города"
                placeholder="Город или регион"
                defaultValue={p.q}
              />
              <button>→</button>
            </form>
            <Link href="/admin?tab=cities&city=new" className="add-link">
              + Добавить город
            </Link>
            <div className="city-sidebar">
              {(
                await all(
                  "SELECT id,name,region,active,published_version,draft FROM cities WHERE name LIKE $1 OR region LIKE $2 ORDER BY name LIMIT 100",
                  `%${p.q || ""}%`,
                  `%${p.q || ""}%`,
                )
              ).map((c) => (
                <Link
                  className={p.city === c.id ? "active" : ""}
                  key={c.id}
                  href={`/admin?tab=cities&city=${c.id}&q=${encodeURIComponent(p.q || "")}`}
                >
                  <strong>{c.name}</strong>
                  <small>
                    {c.region} ·{" "}
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
            <small>Первые 100 результатов. Уточните поиск.</small>
          </aside>
          <section>
            {p.city ? (
              <CityEditor cityId={p.city} versionId={p.version} />
            ) : (
              <div className="empty">
                <h2>Выберите город</h2>
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
              Переменные: {"{город}"}, {"{регион}"}, {"{лимит}"}. Промпт каждого
              запуска сохраняется в истории соответствующей страницы.
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
            <h2>Сначала один город</h2>
            <p>
              Генерация использует сохранённый промпт. Результат появится в
              черновике, затем вы сможете его отредактировать и опубликовать.
            </p>
            <form action={actions.generate} className="button-row">
              <select name="cityId" aria-label="Город для пробного запуска">
                {(
                  await all(
                    "SELECT id,name,region FROM cities WHERE active=1 ORDER BY name",
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
                Каждый город — отдельный платный API-запрос. Автоматической
                публикации нет. Одновременные задания одного города исключены.
              </p>
              <form action={actions.generate} className="button-row">
                <select name="scope" aria-label="Область наполнения">
                  <option value="empty">
                    Только пустые города ({counts.empty})
                  </option>
                  <option value="all">
                    Все активные города ({counts.total})
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
                    <th>Город</th>
                    <th>Статус</th>
                    <th>Подробности</th>
                  </tr>
                </thead>
                <tbody>
                  {(
                    await all(
                      "SELECT j.*,c.name FROM jobs j LEFT JOIN cities c ON c.id=j.city_id ORDER BY j.created_at DESC,j.id DESC LIMIT 100",
                    )
                  ).map((j) => (
                    <tr key={j.id}>
                      <td>
                        {j.kind === "generate" ? "Наполнение" : "Разбор отзыва"}
                        <small>{time(j.created_at)}</small>
                      </td>
                      <td>
                        <Link href={"/admin?tab=cities&city=" + j.city_id}>
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
                  `SELECT r.*,c.name FROM reviews r JOIN cities c ON c.id=r.city_id ${p.filter === "all" ? "" : "WHERE r.moderation=$1"} ORDER BY r.created_at DESC LIMIT 100`,
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
async function CityEditor({
  cityId,
  versionId,
}: {
  cityId: string;
  versionId?: string;
}) {
  const c =
    cityId === "new"
      ? null
      : await one("SELECT * FROM cities WHERE id=$1", cityId);
  if (cityId !== "new" && !c) return <p>Город не найден</p>;
  const versions = c
    ? await all(
        "SELECT * FROM versions WHERE city_id=$1 ORDER BY number DESC",
        cityId,
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
        <h2>{c ? c.name : "Новый город"}</h2>
        <form action={actions.saveCity} className="stack">
          <input type="hidden" name="id" value={c?.id || ""} />
          <div className="field-grid">
            <label>
              Название
              <input
                name="name"
                defaultValue={c?.name}
                required
                maxLength={150}
              />
            </label>
            <label>
              Регион
              <input
                name="region"
                defaultValue={c?.region}
                required
                maxLength={200}
              />
            </label>
            <label>
              URL города
              <input
                name="slug"
                defaultValue={c?.slug}
                placeholder="ivanovo"
                pattern="[a-z0-9]+(-[a-z0-9]+)*"
              />
            </label>
            <label>
              Население
              <input
                name="population"
                type="number"
                defaultValue={c?.population || 0}
                min={0}
                max={100000000}
              />
            </label>
          </div>
          <label className="inline">
            <input
              type="checkbox"
              name="active"
              defaultChecked={c ? !!c.active : true}
            />{" "}
            Город доступен на сайте
          </label>
          <button>Сохранить город</button>
        </form>
        {c && (
          <Link href={"/guide/" + c.slug} target="_blank">
            Открыть страницу ↗
          </Link>
        )}
      </section>
      {c && (
        <>
          <section className="panel">
            <div className="section-heading">
              <h2>Черновик страницы</h2>
              <form action={actions.generate}>
                <input type="hidden" name="cityId" value={cityId} />
                <button className="secondary">Наполнить через GPT ↗</button>
              </form>
            </div>
            <RankingReport
              report={(await cityContent(cityId)).rankingReport || []}
            />
            <ContentEditor
              key={c.draft_version || cityId}
              cityId={cityId}
              initial={await cityContent(cityId)}
            />
            {c.draft_version && (
              <form action={actions.publishVersion} className="publish">
                <input type="hidden" name="cityId" value={cityId} />
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
                      href={`/admin?tab=cities&city=${cityId}&version=${v.id}`}
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
                        <input type="hidden" name="cityId" value={cityId} />
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
                        <input type="hidden" name="cityId" value={cityId} />
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
            <summary>База ресторанов города</summary>
            {(
              await all(
                "SELECT * FROM restaurants WHERE city_id=$1 ORDER BY name",
                cityId,
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
    "SELECT r.*,c.name,c.slug FROM reviews r JOIN cities c ON c.id=r.city_id WHERE r.id=$1",
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
  const current = await cityContent(r.city_id);
  const proposals = await all(
    "SELECT * FROM proposals WHERE review_id=$1",
    reviewId,
  );
  return (
    <>
      <section className="panel">
        <div className="section-heading">
          <h2>{r.name} · отзыв</h2>
          <Link href={"/admin?tab=cities&city=" + r.city_id}>
            К странице города ↗
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
            подборку города.
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
        <h2>Внутренние оценки ресторанов города</h2>
        <p className="muted">
          Только одобренные, без фрода. Публичные места в топе задаёт редактор.
        </p>
        <InternalStats cityId={r.city_id} />
      </section>
    </>
  );
}
async function InternalStats({ cityId }: { cityId: string }) {
  const rows = await all(
    "SELECT result,visitor FROM reviews WHERE city_id=$1 AND moderation='approved' AND fraud=0 AND result IS NOT NULL",
    cityId,
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
