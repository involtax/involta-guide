"""Remove seeded restaurant clues; expected discoveries belong in evaluation only."""

import json

from alembic import op
from sqlalchemy import text

revision = "unbiased_city_scope"
down_revision = "city_surroundings"
SEEDED_HINTS = {
    34: "Анапа и доступные гастрономические окрестности: курортные посёлки, загородные рестораны и винодельни с полноценной кухней. Рассмотри Витязево, Большой Утриш, Виноградный и район Семигорья. Обязательно проверь для общего сравнения ресторан «Винотеррия» (https://vinoterria.com/): район хутора Семигорский, фактический адрес относится к Новороссийску. Эта административная граница не исключает его из подборки Анапы. Подтверди актуальные факты и сравни по общим восьми факторам; место в тройке не гарантировано.",
    622: "Медвежьегорск и доступные гастрономические окрестности, включая Заонежье. Обязательно проверь для общего сравнения ресторан «Руна» при бутик-отеле «Заонежье» (https://zaonezhie.com/restaurant), деревня Кажма, 74, Медвежьегорский район. Не путай с одноимёнными объектами. Ресторан относится к подборке Медвежьегорска; отдельная страница Кажмы не нужна. Учитывай реальный подъезд и доступность для внешних гостей, а не расстояние по прямой через озеро. Подтверди актуальные факты и сравни по общим восьми факторам; место в тройке не гарантировано.",
}


def upgrade() -> None:
    conn = op.get_bind()
    changes = []
    for location_id, seeded_note in SEEDED_HINTS.items():
        changed = conn.execute(
            text("""UPDATE locations SET coverage_note=''
            WHERE id=:id AND coverage_note=:seeded_note RETURNING id"""),
            {"id": location_id, "seeded_note": seeded_note},
        ).scalar_one_or_none()
        if changed is not None:
            changes.append({"id": location_id, "before": seeded_note})
    conn.execute(
        text("""INSERT INTO audit(id,action,target,detail)
        VALUES(gen_random_uuid(),'coverage_contract','unbiased_city_scope',:detail)"""),
        {"detail": json.dumps({"changes": changes}, ensure_ascii=False)},
    )
    # Job prompts contain the general template. Geography is resolved before first send.
    # Recorded requests already own their input and must not be changed or resubmitted.


def downgrade() -> None:
    conn = op.get_bind()
    saved = conn.execute(
        text("""SELECT detail FROM audit
        WHERE action='coverage_contract' AND target='unbiased_city_scope'
        ORDER BY created_at DESC,id DESC LIMIT 1"""),
    ).scalar_one_or_none()
    if saved:
        for change in json.loads(saved)["changes"]:
            conn.execute(
                text("""UPDATE locations SET coverage_note=:before
                WHERE id=:id AND coverage_note=''"""),
                change,
            )
