import { publicPath } from "@/lib/urls.mjs";
import Link from "next/link";
import { cookies } from "next/headers";
import { isAdmin } from "@/lib/auth";
import { alexSession } from "@/lib/alex.mjs";
import { all } from "@/lib/db.mjs";
import { revokeAlexSessions, alexLogout } from "./actions";
export const dynamic = "force-dynamic";
export const metadata = {
  title: "Редактор Алекс",
  robots: { index: false, follow: false },
};
export default async function AlexPage() {
  const session = await alexSession((await cookies()).get("guide_alex")?.value);
  const admin = await isAdmin();
  if (!session && !admin)
    return (
      <main>
        <h1>Редактор Алекс</h1>
        <p>Для входа откройте персональную ссылку.</p>
      </main>
    );
  const changes = await all(
    "SELECT c.*,l.name,r.transcript,r.audio FROM alex_changes c LEFT JOIN locations l ON l.id=c.location_id LEFT JOIN reviews r ON r.id=c.review_id ORDER BY c.created_at DESC LIMIT 100",
  );
  const sessions = await all(
    "SELECT id,created_at,expires_at,revoked FROM alex_sessions ORDER BY created_at DESC LIMIT 30",
  );
  return (
    <main>
      <h1>Редактор Алекс</h1>
      <p>
        На странице локации голосовые команды применяются сразу. Можно менять
        текст, телефон, расписание, добавлять и удалять рестораны, выдавать и
        снимать звезду. Неполные или неоднозначные команды сохраняются с ошибкой
        для уточнения.
      </p>
      <a href={publicPath("/")}>Выбрать локацию ↗</a>
      {session && (
        <form action={alexLogout}>
          <button>Выйти</button>
        </form>
      )}
      {admin && (
        <form action={revokeAlexSessions}>
          <button>Завершить все сессии Алекса</button>
        </form>
      )}
      <h2>Сессии</h2>
      {sessions.map((s) => (
        <p key={s.id}>
          {s.created_at} · {s.revoked ? "завершена" : `до ${s.expires_at}`} ·{" "}
          {s.id}
        </p>
      ))}
      <h2>Журнал голосовых правок</h2>
      {changes.map((c) => (
        <article key={c.id} className="panel">
          <h3>
            {c.name} · {c.status}
          </h3>
          <p>
            {c.created_at} · {c.detail}
          </p>
          <p>{c.transcript}</p>
          {c.review_id && (
            <audio
              controls
              preload="none"
              src={publicPath("/api/alex/audio/" + c.review_id)}
            />
          )}
          {c.review_id && (
            <Link href={"/admin?tab=reviews&review=" + c.review_id}>
              Запись и разбор ↗
            </Link>
          )}
          <details>
            <summary>Было / стало</summary>
            <pre>
              {JSON.stringify(
                {
                  before: c.before_content
                    ? JSON.parse(c.before_content)
                    : null,
                  after: c.after_content ? JSON.parse(c.after_content) : null,
                },
                null,
                2,
              )}
            </pre>
          </details>
        </article>
      ))}
    </main>
  );
}
