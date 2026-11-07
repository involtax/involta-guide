# Voice API, контракт 2

Отдельный Voice использует общую PostgreSQL БД со схемой `voice`. API и доставка работают в контейнере `voice`; ASR — в `voice-worker`. Публичные отзывы — тексты GigaAM, без GPT-анализа.
[OpenAPI](voice-openapi.json) · [DTO JSON Schema](voice-dtos.schema.json).
Внутренние пути `/internal/v1` требуют Bearer service secret; административные результаты доступны только через защищённый gateway. Публичного доступа к Voice DB/API нет.

Gateway Guide под BASE_PATH `/voice/api/v1`:

- POST `/identity?location={slug}` назначает/сохраняет `guide_visitor`, возвращает content_epoch.
  Виджет вызывает его при первоначальной подготовке записи. После стопа второго
  запроса нет: аудио сразу отправляется с эпохой, зафиксированной до записи;
  Guide и Voice проверяют её при приёме. HTTP409 stale_epoch отбрасывает запись,
  не подменяя её эпоху и не вызывая повторную отправку.
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

SubmissionMetadata v2 содержит location (id — положительное целое из каталога Guide, slug, name, revision, **content_epoch**,
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

POST `/admin/recognitions` — приватный batch до 30 UUID: до пяти строк результатов,
без оценок и эталона. GET `/admin/submissions/{reference}` и `/asr` содержат
подробности для лога. Ручные new-run/revalidate, ASR-run/evaluation и изменение
настроек анализа удалены. Основной профиль фиксируется в snapshot каждого отзыва. Публичный SubmissionStatus
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
Ошибка Sherpa не меняет публикацию. Удаление даёт 0 и оставляет status=deleted
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

## Один профиль Sherpa и нормализация словоформ

Новые записи создают одно задание `sherpa_gigaam_hotwords_strong`: сила2.5, поиск16,
город и все рестораны. Других заданий нет. Исторические этапы/результаты и уже
принятые задания сохраняют исходные параметры. Schema9 и ключ кандидата прежние.

Guide передаёт LocationContext.recognition_dictionary из города и всех ресторанов,
без ограничения публичной подборкой. ASR получает канонические и строчные названия.
Новые runs.snapshot фиксируют primary_variant=sherpa_hotwords_strong,
restore_dictionary_case=true, name_normalization=catalogue-inflections-v1 и
name_forms — таблицу словоформ, полученную закреплённым pymorphy3/русским словарём.
Словоформы используются только после ASR. Уже принятые запросы не меняются при
редактировании каталога. Старые runs без name_forms используют прежнее правило
точного совпадения, без переобработки истории.

Только frozen primary даёт gigaam_ready/failed. Отсутствующий primary_variant
исторического задания означает onnx. События schema_version=2 и publication_status
0/1 сохраняются. Публикуемый текст восстанавливает регистр/ё словарной формы,
сохраняя падеж: «в иванове» → «в Иванове». Производные прилагательные и созвучия
не заменяются, неизвестные бренды не склоняются автоматически. Сырые ответы
provider_calls и candidate.transcript сохраняются без изменений.

Приватный batch /admin/recognitions: максимум30 references, три SQL-запроса.
Каждая строка содержит variant/variant_label/text/raw_text/primary/state/error,
dictionary, hotwords_score, max_active_paths. У основной строки text — сохранённый
публикуемый текст, raw_text — ответ модели до форматирования. Остальные строки
содержат сырые результаты сравнения. Чтение не запускает ASR. Публичная лента
не раскрывает технические параметры или словарь.

Request хранит dictionary_encoding=gigaam-sentencepiece-native-v2, SHA256 родного
токенизатора и экспортированного bpe.vocab. Файл с весами частей слов версионирован;
старые задания v1 используют собственный неизменный файл. Точная конфигурация и
подготовка: [VOICE_SHERPA_COMPARISON.md](VOICE_SHERPA_COMPARISON.md).

## Административное перераспознавание

`POST /internal/v1/admin/submissions/{reference}/reprocess` (Bearer service secret,
опционально `X-Guide-Session` для аудита):

```json
{"location":{"id":490,"slug":"example-city","name":"Город","content_epoch":1,"recognition_dictionary":["Город","Ресторан"]},"idempotency_key":"00000000-0000-4000-8000-000000000001"}
```

`location.id` — положительный integer из PR47; UUID reference и ресторанов сохраняются.
Guide собирает актуальный контекст и **все** названия из каталога, включая active=0.
Админский POST `/admin/reviews/{slug}/{number}/reprocess` проверяет сессию, CSRF,
удаление и эпоху; он не принимает произвольный аудиофайл или словарь из браузера.

Ответ202: `{reference,run,state}`, state=`queued|existing|busy`. Повтор того же
ключа возвращает уже созданный run, включая завершённый. Пока для записи есть
queued/running стадии, другой ключ не создаёт параллельную работу. После ошибки
можно явно нажать кнопку ещё раз с новым ключом. Нет автоматического массового
перераспознавания или повторной загрузки аудио.

Voice блокирует эпоху и исходную submissions-строку, сохраняет новый run snapshot
с текущими настройками/контекстом и ключом, увеличивает active_run и создаёт одну
очередь первичного ASR. Аудиофайл, reference, владелец, время и исходные metadata
остаются прежними. Все старые runs, provider_calls и кандидаты сохраняются.

До успешного результата опубликованный текст остаётся. Новое recording_received
продвигает voice_links.active_run, сохраняя сам отзыв; старые события меньшего run
ACK-аются без применения. Успех атомарно заменяет текст, ошибка сохраняет прежний
успешный результат, поздний результат не восстанавливает удалённый отзыв.
Номер/дата отзыва и статистика новых отзывов не увеличиваются.

В админке кнопка блокируется на время обработки, карточка обновляется HTMX раз
в2с и прекращает опрос после завершения. При ошибке показана причина; до/после
сохраняют raw и публикуемый текст. Логи/аудио доступны по прежним правилам беты.

### Совместное слияние с PR44/46/47

PR48 основан на числовом контракте PR47. Для общей сборки трёх независимых
миграций нужна одна цепочка, например:
`audience_stats → location_ids → readable_slugs → restaurant_discovery`.
В проверочной локальной сборке использован этот порядок. После объединения
обновить ожидаемый head deployment smoke; beta-transition читает head из образа,
а readiness Voice требует schema10. Не запускать старых писателей с новой БД.

При разрешении JSON-конфликта сохранить `id`/`nearby_location_ids` из PR47 и
изменённые slug/parent_slug/redirects из PR44. Исторический UUID→ID план миграции
не пересчитывать из новых slug. В конфликте Voice gateway сохранить общий
`current_location_context`: он уже включает NULL-обработку PR46 и весь каталог.

Тесты URL-миграции в общей ветке должны начинаться после location_ids и проверять
`nearby_location_ids` вместо удалённой location_relations. Исторический тест
filling_top3 сравнивает промпт своей миграции, а не изменённый PR46 runtime-файл.
Это инструкции интеграции, не уже выполненное слияние чужих PR в main.

В ASR-паре `run` обозначает активный запуск, `result_run` — запуск показанного
сохранённого результата. Пока повторный ASR не завершён, словарь/параметры рядом
со старым raw относятся именно к этому старому результату. Новый словарь остаётся
в request нового stage и сменяет показанные параметры после успешного результата.

Главная Guide выводит «Пульс:»: один SSR-запрос последних 10 reviews
с publication_status=1 и status received/transcribed, только из активных локаций
по PUBLIC-правилу каталога. Порядок created_at DESC, number DESC; подпись города
и канонический slug читаются JOIN-ом из locations. Дата отображается в UTC+3.
У ожидающего текста — «Расшифровка…». Ошибки, резерв публикации и удалённые
отзывы не видны; удаление исключает запись при следующем GET, не удаляя историю.
Отдельной копии ленты, пагинации, фонового опроса, миграции и API для неё нет.

Шапка отзыва главной дополнительно получает reviews.author_name и название
первого ресторана из review_analyses.result.mentions[0].restaurant_id.
Используется только подтверждённый анализ (approved_at задан, source_text
совпадает с текущим transcript); название читается из restaurants того же города.
Неопубликованный ресторан также допустим как подпись: ссылка ведёт в город,
а не к отсутствующей публичной карточке. Нейтральное упоминание не требует голоса.
До ОК и при устаревшем анализе ресторан не показывается. Имя берётся из уже
применённого поля reviews, не из неподтверждённого JSON. Запрос остаётся одним,
без N+1, новой таблицы, вызова ИИ или отдельной копии метаданных.
