## Что изменилось

## Проверка

- [ ] `uv run ruff check src tests migrations`
- [ ] `uv run ruff format --check src tests migrations`
- [ ] `uv run mypy`
- [ ] `uv run pytest -q`
- [ ] `docker build .` — если затронут контейнер/зависимости
- [ ] Production-миграции проверены на копии БД и не применяются автоматически
