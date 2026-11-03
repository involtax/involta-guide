# Разработка

Python 3.13+, uv, PostgreSQL 17, ffprobe. `uv sync --frozen` устанавливает зависимости.
Скопируйте `.env.example` в `.env.local`, задайте локальные секреты и отдельную тестовую БД.
`uv run alembic upgrade head`, `uv run guide seed`, затем web и `uv run guide-worker`.

Проверки перед PR: `uv run ruff check src tests migrations`,
`uv run ruff format --check src tests migrations`, `uv run mypy`, `uv run pytest -q`.
Работайте только с изолированной тестовой БД; fixtures создают временные схемы.
Платные OpenAI-запросы в тестах запрещены — используйте fake adapter/HTTPX MockTransport.

Domain не импортирует внешние фреймворки. Application зависит от портов, а не SQL/HTTP.
Presentation не исполняет SQL и не решает бизнес-правила. SQLAlchemy mappings — отдельные
инфраструктурные классы. Не добавляйте React, Node.js, Redis или сервисы без необходимости.
HTMX возвращает HTML-фрагменты, формы поддерживают обычный POST. Редакторские действия
должны передавать ожидаемую текущую версию. Сохранение сразу обновляет сайт; входящие отзывы проходят
модерацию, затем принятые изменения применяются к актуальной странице.

Не изменяйте применённые SQL/Alembic migration files. Добавляйте следующую ревизию,
сохраняйте существующие UUID, снимки и внешние ключи. Проверяйте конкуренцию и rollback
на копии. Не коммитьте .env, аудио, дампы, ключи и каталоги runtime/cache.
