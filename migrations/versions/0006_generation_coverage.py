"""Repair the stored legacy prompt while retaining an exact backup."""

import json
import re
from uuid import uuid4

from alembic import op
from sqlalchemy import text

revision = "0006_coverage"
down_revision = "0005_current"


def upgrade() -> None:
    connection = op.get_bind()
    previous = connection.execute(
        text("SELECT value FROM settings WHERE key='prompt' FOR UPDATE")
    ).scalar_one_or_none()
    if previous is None:
        return  # A fresh installation seeds the current prompt after migrations.
    current = re.sub(
        r"Размер подборки определяется полем (?:\{лимит\}|\d+), заданным редактором\.",
        "Лимит исследования — до 10 подходящих кандидатов. Настройка числа сразу видимых карточек не ограничивает поиск, rankingReport или restaurants.",
        previous,
    )
    current = current.replace(
        "Твой результат — черновик. Решение о публикации принимает человек.",
        "Проверенный результат сразу обновляет актуальную страницу.",
    )
    current = current.replace(
        "первые {лимит} показываются сразу, остальные раскрываются кнопкой «Ещё»",
        "количество сразу видимых карточек задаёт интерфейс, остальные раскрываются кнопкой «Ещё»",
    )
    current = current.replace(
        "Первые {лимит} видны сразу, остальные доступны через «Ещё».",
        "Количество сразу видимых карточек задаёт интерфейс и не ограничивает размер restaurants.",
    )
    current = current.replace("{лимит}", "10")
    marker = "ОТЧЁТ ОБХОДА КАНДИДАТОВ v2"
    if marker not in current:
        current += (
            "\n\n"
            + marker
            + "\nСначала найди кандидатов, затем проверь каждого, затем ранжируй. В candidateReview перечисли каждое найденное место: name, mapsUrl или null, status (verified / excluded / needs_verification), reason, sources и exclusionCode для excluded (outside_area / closed / not_restaurant / duplicate). Непроверенные места не исключай молча. Для каждого verified обязательны полные восемь оценок и карточка в restaurants. Описание и вступление — простой связный текст; все ссылки — только в sources. Нет черновика: проверенный результат сразу обновляет сайт.\n"
        )
    if current == previous:
        return
    connection.execute(
        text(
            "INSERT INTO settings(key,value) VALUES('prompt_before_coverage_v2',:value) ON CONFLICT DO NOTHING"
        ),
        {"value": previous},
    )
    connection.execute(
        text("UPDATE settings SET value=:value WHERE key='prompt'"), {"value": current}
    )
    connection.execute(
        text(
            "INSERT INTO audit(id,action,target,detail) VALUES(:id,'prompt_upgrade','generation',:detail)"
        ),
        {
            "id": str(uuid4()),
            "detail": json.dumps({"version": 2, "backup": "prompt_before_coverage_v2"}),
        },
    )


def downgrade() -> None:
    raise RuntimeError("Retain prompt backup and history; roll back the application image only")
