import Link from "next/link";
import { requireAdmin } from "@/lib/auth";
import { all } from "@/lib/db.mjs";
export const dynamic = "force-dynamic";
export default async function Contacts() {
  await requireAdmin();
  const groups =
    await all(`SELECT p.payload->>'restaurantId' restaurant_id,p.payload->>'phone' phone,
 max(r.name) restaurant_name,max(r.phone) current_phone,count(*) messages,
 count(DISTINCT v.visitor) visitors,count(DISTINCT v.ip_hash) networks,
 count(*) FILTER (WHERE v.fraud=1) fraud,
 count(*) FILTER (WHERE v.moderation='approved' AND v.fraud=0) approved,
 jsonb_agg(jsonb_build_object('reviewId',v.id,'date',v.created_at,'reason',p.payload->>'reason','status',p.status,'moderation',v.moderation)) evidence
 FROM proposals p JOIN reviews v ON v.id=p.review_id
 LEFT JOIN restaurants r ON r.id::text=p.payload->>'restaurantId'
 WHERE COALESCE(p.payload->>'phone','')<>''
 GROUP BY p.payload->>'restaurantId',p.payload->>'phone'
 ORDER BY max(v.created_at) DESC LIMIT 200`);
  return (
    <main>
      <h1>Предложения телефонов</h1>
      <Link href="/admin">Админка ↗</Link>
      <p>
        Сведения копятся без автоматической замены. Число устройств и сетей —
        косвенные признаки, не подтверждение личности или номера.
      </p>
      {groups.map((g, i) => (
        <article key={i} className="panel">
          <h2>{g.restaurant_name || "Новый ресторан"}</h2>
          <p>
            Сейчас: {g.current_phone || "не указан"} → предложено: {g.phone}
          </p>
          <p>
            Сообщений: {g.messages} · браузеров: {g.visitors} · сетей:{" "}
            {g.networks} · одобрено без фрода: {g.approved} · фрод: {g.fraud}
          </p>
          {JSON.parse(g.evidence).map((e: any) => (
            <p key={e.reviewId}>
              <Link href={"/admin?tab=reviews&review=" + e.reviewId}>
                {e.date} · {e.status} · {e.moderation} ↗
              </Link>{" "}
              {e.reason}
            </p>
          ))}
        </article>
      ))}
    </main>
  );
}
