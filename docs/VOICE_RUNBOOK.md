# Voice: эксплуатация beta core

Несовместимый переход Guide review_urls / Voice 3 → beta_core / Voice 4 и граница
rollback описаны в [BETA_CORE_RUNBOOK.md](BETA_CORE_RUNBOOK.md). Старых архивных
команд, Guide-транскрипции и редакторского голоса нет.

Voice имеет отдельную PostgreSQL БД/роль и audio-том. Не давать его роли доступ
к Guide DB и не публиковать internal API через nginx. Gateway Guide передаёт
доверенный контекст, raw IP/UA/cookie и epoch. Единственный normal flow действует
для посетителя и администратора. Отложенные жесты виджета не входят в изменение.

Для локального запуска нужны Python 3.13, uv, PostgreSQL 17 и ffprobe:

```bash
uv sync --frozen --extra voice-local
# Настроить .env.voice по .env.voice.example; отдельные DB URL и service secret.
uv run voice migrate
uv run voice prepare-model
uv run uvicorn involta_voice.main:create_app --factory --host 127.0.0.1 --port 3100 --no-access-log
# Другой процесс с тем же Voice окружением:
uv run voice-worker
# Guide окружение содержит только свой DATABASE_URL, VOICE_URL и общий service secret:
uv run guide-voice-consumer
```

В изолированной проверочной установке `VOICE_FAKE_PROVIDER=true` исключает реальные
платные вызовы. В production false; ASR GigaAM/Parakeet локальный, pinned weights
и требования ресурсов — [ASR_BETA.md](ASR_BETA.md). OpenAI нужен для анализа текста.
`VOICE_ENABLED` в Guide включает приём для всех ролей. Чтение сохранённых отзывов и
модерация не зависят от возможности принять новую запись.

Readiness: `/health/ready` в Voice, `/guide/health/ready` в Guide. Worker/consumer
health команды сохранены. Логи consumer содержат тип ошибки/stack без текста
отзыва или credentials. Ошибки отдельных применений видны в админском inbox;
consumer продолжает независимые reference и ACK после durable receipt. Повтор
ошибочного сохранённого события — кнопка в `/admin/reviews`.

Модерация: `/admin/reviews/{slug}/{number}`. Аудио, обе ASR-модели, исходный и
редакционный тексты, фрагменты, предложения и решения находятся в одной карточке.
Новая обработка использует зафиксированные настройки и исходный текст; revalidate
проверяет сохранённый raw response без нового OpenAI POST. Unknown outcome не
переотправляется автоматически. Изменение модели/промпта не меняет прежние runs.

Размер/MIME/длительность аудио измеряются сервером. Действуют owner-local окна
visitor/IP, queue/free-disk limits, число активных задач и дневной бюджет. Глобальные
`daily_usage` агрегаты не удаляются при reset; неизвестная стоимость сохраняет
консервативную reservation. Provider-level spending limit по-прежнему нужен отдельно.
Raw-метаданные не показываются публично. Аудио SHA служит целостности, не заменяет IP.

Обычные отзывы не имеют автоматического срока удаления. Полный reset владельца
удаляет их и серверные файлы; rejection/fraud сохраняют материал для модерации.
`voice orphan-audio` — dry-run; `--execute` удаляет только старые unreferenced файлы,
проверяя их под блокировкой владельца. Reset использует отдельный durable manifest,
не GC и не архив. `voice beta-cleanup-files [--execute]` нужен для явной миграционной
очистки; до удаления manifest не теряется. Ошибка unlink должна быть устранена,
а тот же reset продолжен; нельзя открывать новый цикл вручную в БД.

При backup/restore согласовывать обе БД и файлы с остановленными writers. Не
сбрасывать sequences/cursor/ACK отдельно. Старый образ на новой схеме запрещён;
поддерживаемый порядок восстановления приведён в основном runbook.
