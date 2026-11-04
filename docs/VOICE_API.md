# Voice API, контракт 2

Отдельный Voice использует общую PostgreSQL БД со схемой `voice`. API и доставка работают в контейнере `voice`; ASR — в `voice-worker`. Публичные отзывы — тексты GigaAM, без GPT-анализа.
[OpenAPI](voice-openapi.json) · [DTO JSON Schema](voice-dtos.schema.json).
Внутренние пути `/internal/v1` требуют Bearer service secret; административные результаты доступны только через защищённый gateway. Публичного доступа к Voice DB/API нет.

Gateway Guide под BASE_PATH `/voice/api/v1`:

- POST `/identity?location={slug}` назначает/сохраняет `guide_visitor`, возвращает content_epoch.
- POST `/submissions?location={slug}&content_epoch={epoch}` принимает тот же blob/key
  одной отправкой; серверная идемпотентность сохраняется. Actor, visitor и IP клиента не назначают права/владельца.
- GET `/reviews?location={slug}` — одна общая лента (последние 10 записей), без cookies/входа.
  Поля: reference, number, transcript, created_at, state, publication_status, log_url;
  ответ содержит content_epoch и publication_statuses для запрошенных pending.
  Персональные metadata не выдаются. Старые gateway GET /submissions/{reference} и /mine удалены.
- GET/POST `/debug/{request_id}` — приватные диагностические события, флаг VOICE_DEBUG_ENABLED.

Общий журнал `/guide/logs?kind=uploads` показывает техническое представление
этих же сохранённых событий. Карточка `/guide/logs/{slug}/uploads/{request_id}`
и её `.json` не содержат публичных raw-метаданных или текста отзыва; администратор
видит полный сохранённый отчёт. Правила доступа и этапы: [LOGS.md](LOGS.md).

SubmissionMetadata v2 содержит location (id, slug, name, revision, **content_epoch**,
restaurants), точное `visitor=guide_visitor`, `idempotency_key`, **raw ip**, полный
**user_agent**, widget_version, language/timezone, correlation_id. Actor/purpose,
ip_fingerprint и published_revision удалены. Admin recording — обычный отзыв.
Cookie назначается до forwarding. IP определяется доверенной proxy-конфигурацией
Guide, не произвольным клиентским полем. Raw metadata доступны только администратору.

Voice принимает multipart metadata+audio на POST `/submissions`. Идемпотентность
visitor/key включает owner/epoch/audio; изменение того же намерения — 409. Поля
сохраняются без превращения raw-IP/cookie в fingerprint. Аудио SHA и payload hash
остаются для целостности и идемпотентности.

POST `/locations/{id}/reset` с `{content_epoch: N}` — внутренний идемпотентный purge.
Возвращает `{purged:true,content_epoch:N}` только после фактического удаления файлов
и зависимостей. Текущий epoch хранится отдельно от удалённых submissions. Старый
upload/callback возвращает `stale_epoch` 409. Повтор purge того же epoch не удаляет
новые записи этого цикла. Во время неполной очистки новые intake закрыты.

Outbox event v2: sequence/event_id/reference/run/kind, metadata v2, transcript,
analysis (историческое поле), audio SHA/MIME/duration, created_at и repeat_signals.
Новые виды: `recording_received` (transcript=null), `gigaam_ready` (текст GigaAM)
и `gigaam_failed` (терминальная ошибка primary).
GET `/events?after=N`; POST `/events/ack` после durable inbox commit. Receipt и
primary применяются в любом порядке; повторы безопасны. Старые transcript_ready,
analysis_ready, processing_failed принимаются только как история доставки.

POST `/admin/recognitions` — приватный batch до 30 UUID: по две строки результатов,
без оценок и эталона. GET `/admin/submissions/{reference}` и `/asr` содержат
подробности для лога. Ручные new-run/revalidate, ASR-run/evaluation и изменение
настроек анализа удалены. GigaAM фиксирован как primary. Публичный SubmissionStatus
и общая лента не содержат Parakeet, модерации и редакторского состояния.

Админка: `/admin/reviews/{slug}` — лента города. ЧПУ `/admin/reviews/{slug}/{number}`
открывает страницу с якорем. `/audio` доступно только администратору.
Публичный технический лог `/logs/reviews/{slug}/{number}`, без metadata/cookies/секретов.
POST `/admin/reviews/{slug}/{number}/delete` требует admin session + CSRF: status=deleted,
аудит review_deleted; повтор безопасен, поздняя доставка не публикует запись снова.
Подробности и переход общей БД:
[ADMIN_REVIEWS.md](ADMIN_REVIEWS.md).

Виджет держит blob/key/исходный epoch только в памяти одной отправки. После
ошибки, отмены или reload старое аудио не отправляется. IndexedDB-очередь удалена.
Серверная защита owner/key/epoch и сохранённые отзывы/аудио/логи остаются прежними.
Общий список GET /reviews?location={slug} дополнен content_epoch локации для
очистки устаревшего отображения после reset. В UI один список «Отзывы»: после
приёма — «Расшифровка…», затем серверный текст в той же записи.

## Minimum duration for new reviews

New review intake measures audio with ffprobe before submission/queue creation.
Minimum is 5.0 seconds; short uploads return HTTP 422 with
`{"error":{"code":"audio_too_short","message":"…","min_duration_seconds":5,"duration_seconds":4.999}}`.
The same-origin gateway preserves this response. Encoder padding does not extend
the measured audio duration. Accepted idempotent submissions resolve before new
validation; historical >=1-second OutboxEvent records remain readable.

The centered microphone/stop UI, silence detection, demo and pinned browser assets
are described in [VOICE_WIDGET_UX_IMPLEMENTATION.md](VOICE_WIDGET_UX_IMPLEMENTATION.md).
The widget retains each recording's original content_epoch until its single upload finishes.

## Время в пользовательском списке и результате

`GET /voice/api/v1/reviews?location={slug}` читает Guide и сохраняет исходный
`created_at` Voice в `reviews.created_at`. Для чтения не нужен доступ к Voice;
время загрузки страницы не подставляется.
Виджет показывает дату/время в часовом поясе браузера через `<time datetime="…">`
вместо редакторского статуса. Поля состояния API сохраняются для обработки/опроса.

## Статус публикации и ошибка GigaAM

Guide хранит reviews.publication_status: 0 — ошибка, 1 — опубликован,
2 — резерв модерации. Поступление аудио и успешный GigaAM автоматически дают 1;
терминальная ошибка/unknown/пустой результат GigaAM дают 0. Приложение не ставит 2.
Ошибка Parakeet не меняет публикацию. Удаление даёт 0 и оставляет status=deleted
как защиту от позднего события.

Voice отправляет OutboxEvent v2 kind=gigaam_failed, error:string|null, с прежними
reference/run/metadata/epoch и данными аудио. Ошибка пишется в той же транзакции,
что завершает primary stage; queued-повтор не публикует ошибку. Consumer сохраняет
ошибку и publication_status=0, не удаляя отзыв, файл или лог. Готовый текст GigaAM
не затирается запоздавшим сообщением об ошибке; receipt не возвращает failed в received.

GET /voice/api/v1/reviews?location={slug} содержит только publication_status=1.
Неудачные отзывы не занимают места в последних 10. Дополнительный pending — до 30
UUID через запятую: ответ publication_statuses сообщает текущие статусы этих записей
только в указанном городе. Это позволяет убрать временную строку сразу после ошибки,
даже если исходное подтверждение приёма ещё не появилось в общей ленте. Запрос read-only;
причина ошибки и raw-ответ в публичную ленту не попадают.

События и DTO: [voice-dtos.schema.json](voice-dtos.schema.json). Guide migration
review_publication добавляет ограничение IN(0,1,2). Voice migration 7 создаёт новые
события для уже сохранённых терминальных ошибок GigaAM, не вызывая ASR снова.
