# Voice API, контракт 2

Voice — отдельный backend с собственной БД/файлами. Guide — каталог и модерация.
[OpenAPI](voice-openapi.json) · [DTO JSON Schema](voice-dtos.schema.json).
Внутренние пути `/internal/v1` требуют Bearer service secret; настройки и ASR-оценки
также передают административную сессию. Публичного доступа к Voice DB/API нет.

Gateway Guide под BASE_PATH `/voice/api/v1`:

- POST `/identity?location={slug}` назначает/сохраняет `guide_visitor`, возвращает content_epoch.
- POST `/submissions?location={slug}&content_epoch={epoch}` принимает тот же blob/key
  при retry. Заголовки/поля actor, visitor, IP клиента не назначают права/владельца.
- GET `/submissions/{reference}`, GET `/submissions/mine?location={slug}` — собственные записи.
- GET/POST `/debug/{request_id}` — приватные диагностические события, флаг VOICE_DEBUG_ENABLED.

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
analysis, audio SHA/MIME/duration, created_at и repeat_signals. Виды: transcript_ready,
analysis_ready, processing_failed. GET `/events?after=N`; POST `/events/ack` после
сохранения входа в Guide inbox. Ошибка одного применения не блокирует независимые
reference; зависимые события одного reference ждут. Повтор ACK/event безопасен.

Сохранены normal new-run/revalidate, локальные GigaAM/Parakeet, ASR-кандидаты и
ручные оценки из PR18. Архивных API, guide-transcription, Alex-команд и второго
Guide-исполнителя нет. ЧПУ карточки Guide `/admin/reviews/{slug}/{number}`, действия
`/actions`, аудио `/audio`; сравнение двух моделей показывается в этой же карточке.

Виджет сохраняет blob/key/**исходный epoch** в IndexedDB. После reset старый blob/key
не получает свежую метку: очередь очищается при следующем контакте, пользователь
видит сообщение и может сознательно записать новый отзыв. Сервер не удаляет файлы
из закрытого чужого браузера, но не принимает их повторно в новый цикл.

## Minimum duration for new reviews

New review intake measures audio with ffprobe before submission/queue creation.
Minimum is 5.0 seconds; short uploads return HTTP 422 with
`{"error":{"code":"audio_too_short","message":"…","min_duration_seconds":5,"duration_seconds":4.999}}`.
The same-origin gateway preserves this response. Encoder padding does not extend
the measured audio duration. Accepted idempotent submissions resolve before new
validation; historical >=1-second OutboxEvent records remain readable.

The centered microphone/stop UI, silence detection, demo and pinned browser assets
are described in [VOICE_WIDGET_UX_IMPLEMENTATION.md](VOICE_WIDGET_UX_IMPLEMENTATION.md).
The widget retains each recording's original content_epoch across all retries.
