"""Make the editable filling prompt request a short, restaurant-free city intro."""

import json
import re

from alembic import op
from sqlalchemy import text

revision = "short_city_intro"
down_revision = "vip_visitors"

INTRO = "intro — ровно одно короткое, лаконичное предложение об атмосфере этой локации и предвкушении еды. Ориентир — 70–100 символов с пробелами, не более 100: примерно 2–3 строки на мобильном экране. Не называй рестораны, кафе, бары, отели и другие заведения, не перечисляй и не описывай выбранные места. Без числа заведений, описания методологии, рекламных штампов и выдуманного личного опыта. Один обычный текстовый абзац без переносов строк; не объединяй несколько предложений точкой с запятой ради обхода ограничения."


def upgrade() -> None:
    conn = op.get_bind()
    before = conn.execute(
        text("SELECT value FROM settings WHERE key='prompt'")
    ).scalar_one_or_none()
    if before is None:
        return  # A fresh installation receives the updated template through seed.
    after, replaced = re.subn(r"^intro[ \t]*—[^\r\n]*", lambda _: INTRO, before, flags=re.MULTILINE)
    if not replaced:
        after = before.rstrip() + "\n\n" + INTRO + "\n"
    conn.execute(
        text(
            "INSERT INTO audit(id,action,target,detail) VALUES(gen_random_uuid(),'prompt_contract','short_city_intro',:detail)"
        ),
        {"detail": json.dumps({"before": before, "after": after}, ensure_ascii=False)},
    )
    conn.execute(text("UPDATE settings SET value=:prompt WHERE key='prompt'"), {"prompt": after})


def downgrade() -> None:
    conn = op.get_bind()
    saved = conn.execute(
        text("""SELECT detail FROM audit
        WHERE action='prompt_contract' AND target='short_city_intro'
        ORDER BY created_at DESC,id DESC LIMIT 1""")
    ).scalar_one_or_none()
    if saved:
        snapshot = json.loads(saved)
        conn.execute(
            text("UPDATE settings SET value=:before WHERE key='prompt' AND value=:after"),
            snapshot,
        )
