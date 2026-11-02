# Изменения

## 0.2.0 — Python modular monolith

- Python 3.13, FastAPI, Jinja2, HTMX, SQLAlchemy 2, Alembic, psycopg 3, HTTPX and uv.
- Existing PostgreSQL identities, legacy migration ledger and JSONB snapshots preserved.
- Revocable admin sessions, Argon2id, CSRF, measured audio duration and upload quotas.
- PostgreSQL worker leases/heartbeat, bounded retry, owner fencing and AI usage accounting.
- Python public/admin/voice flows tested with pytest, PostgreSQL and Chromium; Docker builds.
- Removed Next.js, React, TypeScript and Node runtime/build manifests.


## Unreleased

Города обобщены до локаций с редакционной иерархией, дополнительными названиями, SEO-формами и сохранением старых URL. Импортированы дополнительные населённые пункты и туристические территории. Москва получила 20 публичных гастрономических зон и внутренний справочник районов. Газпром Поляна объединён в одну локацию. Добавлены географический контекст генерации, управление охватом и повторное использование ресторанов в подборках направлений.

Переход на PostgreSQL 17: асинхронные запросы и транзакции, SQL-миграции с контрольными суммами, импорт городов отдельной командой, конкурентная очередь `SKIP LOCKED`, Docker Compose для локальной БД и PostgreSQL в CI.

## 0.1.0 — MVP

Первая версия «Инвольта гида»: справочник городов, редакционная админка, версии страниц, ручная публикация, голосовые отзывы, очередь OpenAI и базовые сигналы фрода. Добавлены проверки TypeScript, тесты бизнес-логики, браузерный сценарий и GitHub Actions CI.

Живые API-запросы и размещение на involta.ru требуют отдельной проверки деплоя. Подробности: [README](README.md), [границы MVP](docs/MVP.md).
