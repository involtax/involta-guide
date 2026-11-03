# Beta core: переход и эксплуатация

Нормативное ТЗ: [BETA_CORE_SIMPLIFICATION_SPEC.md](BETA_CORE_SIMPLIFICATION_SPEC.md).
Ничего из этого runbook не выполняется на production при подготовке PR.
Поддержаны свежая установка и переход с Guide `admin_routes` или `review_urls` / Voice schema 3.
Для `admin_routes` сначала выполняется существующая миграция `review_urls`.
Цепочек совместимости с прежними Node/Python-базами больше нет.

## Что меняет переход

Guide `beta_core` сохраняет географию/навигацию/настройки/admin-сессии и рестораны
обычных владельцев с их звёздами. Порядок/рейтинг переносятся в ресторан; вторичные
ссылки удаляются. Старое содержимое надлокаций очищается; перечень —
`beta_transition_report(location_id, reason, restaurants_removed)`.

Старые бета-отзывы, редакторские команды, jobs/логи/трейсы и несовместимые hashed
метаданные удаляются явно один раз. Они не превращаются в raw-IP/обычные отзывы.
Пути Guide-аудио сначала попадают в `beta_cleanup_files`. Voice migration 4 удаляет
старые submissions, обработки/ASR/доставку/архивные связи, сохраняя настройки,
global outbox sequence и ACK; пути файлов остаются в `purge_files` до удаления.
Глобальные суточные агрегаты лимитов сохраняются без содержимого локаций.
Миграции повторяемы; startup API не очищает корпус повторно.

## Свежая установка

На выделенных БД и томах приложения:

1. Guide: `alembic upgrade head`, затем `guide seed`.
2. Voice: `voice migrate`, затем `voice prepare-model` с прежними pinned моделями.
3. Проверить dry-run/execute cleanup ниже; на пустой установке файлов нет.
4. Запустить один согласованный SHA Guide/worker/consumer/Voice и проверить readiness.

## Автоматизированный переход

Workflow **Transition beta core production** (`transition-beta-production.yml`)
принимает полный проверенный SHA приложения. Он разрешает образ только после
успешного main CI/publish и использует digest. Запускается по отдельной команде
владельца на production-деплой.

`deploy/beta-transition.py` проверяет фактическую host-топологию, владельцев
контейнеров/томов, исходные схемы и образ до остановки. Под общими deployment locks
останавливает только пять Guide/Voice writers, сохраняет согласованную копию обеих
БД и audio-томов в `/opt/involta-guide/backups/beta-<run>-<attempt>`, обновляет схемы,
выполняет cleanup и сверяет число локаций, ресторанов обычных владельцев и звёзд.
После health/HTTPS записывает current-release. Nginx/SSH/firewall/TLS не меняются.

При ошибке после начала миграции восстанавливает обе БД и аудио до запуска старых
writers. Voice public schema восстанавливается только в предварительно проверенной
выделенной БД Voice с известным составом таблиц; Guide использует существующий
restore с защитой чужих зависимостей. Ошибка restore оставляет writers остановленными
и явную диагностику; image-only rollback через границу не выполняется.

Контроллер одноразовый для этой границы схем. После успешного перехода последующие
релизы используют обычный `deploy-voice-production.yml`.

## Прямой переход существующей установки

Это несовместимая смена обеих БД и контракта, а не обычный rolling update.
Контроллер релизов обнаруживает прежнюю схему и **отказывается автоматически
мигрировать её**, сохраняя старый релиз. Он выводит ссылку на этот документ.
Это исключает старый Voice worker с новой Guide БД или старый образ на новой схеме.

Оператор сначала выбирает прошедший CI commit SHA и digest образа. Подготовленный
контроллером каталог `releases/<SHA>` содержит проверенные Compose snapshots.
Все действия ниже выполняются при закрытом intake и под существующими global/deploy
flock: ни другой deploy, ни старые web/worker/consumer не должны писать в БД/тома.
Не выполнять reset одновременно с переходом.

Для команд используется тот же Compose project, deployment options и env files, что
в remote-release.sh; секреты не передаются через argv и не печатаются. Например:

```bash
export DEPLOY_ROOT=/opt/involta-guide
export RELEASE_SHA=<прошедший-CI-полный-SHA>
export IMAGE_REF=ghcr.io/involtax/involta-guide@sha256:<проверенный-digest>
compose_beta() {
  docker compose --project-name involta-guide-production \
    --env-file "$DEPLOY_ROOT/deploy.env" \
    --env-file "$DEPLOY_ROOT/.env.production" --env-file "$DEPLOY_ROOT/.env.voice" \
    -f "$DEPLOY_ROOT/releases/$RELEASE_SHA/compose.yaml" \
    -f "$DEPLOY_ROOT/releases/$RELEASE_SHA/compose.voice.yaml" "$@"
}
compose_beta stop --timeout 240 web worker voice-consumer voice-api voice-worker
compose_beta run --rm --no-deps web alembic upgrade head
compose_beta run --rm --no-deps voice-migrate
compose_beta run --rm --no-deps web guide beta-cleanup-files
compose_beta run --rm --no-deps voice-api voice beta-cleanup-files
compose_beta run --rm --no-deps web guide beta-cleanup-files --execute
compose_beta run --rm --no-deps voice-api voice beta-cleanup-files --execute
```

До команд миграции проверить фактические revisions и состав схемы; перед началом
зафиксировать план восстановления ниже. Cleanup возобновляем: при ошибке unlink
ссылка остаётся в manifest. После очистки оба dry-run обязаны показывать 0.
Проверить `beta_transition_report`, список обычных владельцев/звёзд/порядок,
настройки и admin-вход. Только затем повторно запустить штатный immutable release.
Его SHA/readiness/public-check gates, serial deployment и обычный rollback сохранены.
Не запускать paid smoke для проверки этого перехода: использовать fake provider
в изолированной проверочной установке. На production ручной первый запуск —
отдельное сознательное действие пользователя.

## Восстановление и граница rollback

Образ до beta_core **нельзя** запускать с новой БД. Контроллер проверяет это и
отказывается от такого image-only rollback, оставляя writers остановленными.
Обычный rollback между совместимыми beta_core образами использует существующий
контроллер и совпадающие схемы/тома.

Если нужен rollback через границу, **до перехода** оператор должен иметь проверенный
согласованный emergency backup обеих БД и обоих audio-томов, соответствующий старому
SHA. Это обычный инфраструктурный backup вне приложения, не архив при reset и не
новый формат истории беты. Guide dump/restore использует существующий
`python -m involta_guide.infrastructure.persistence.release_backup backup|restore`;
Voice — штатные `pg_dump/pg_restore` собственной выделенной БД. Отдельно сохраняются
соответствующие тома и release state/config. Backup нельзя подменять только старым
образом или Guide dump без Voice/файлов.

Восстановление: остановить все пять writers под теми же locks; восстановить **обе**
БД и соответствующие файлы, проверить revisions/целостность, вернуть matching
release state и только затем запустить старый SHA и health/public checks. Guide
restore транзакционно отказывается затрагивать зависимости чужих таблиц.
После удаления файлов без согласованной копии восстановление прежнего содержимого
не поддерживается: оставить intake закрытым и выпустить forward fix. Бета не
обещает архивирование удалённых данных. Старые app archive bindings удаляются.

## Полный сброс одного города

В `/admin/locations/{slug}` нажать **«Очистить локацию и наполнить заново»**, ввести
название. POST требует admin+CSRF, frozen epoch и request_key. Флаг BETA_RESET_ENABLED
закрывает и действие, и исполнение. При его выключении pending reset не исчезает.

Начало повышает epoch и закрывает запись. Worker использует внутренний Voice purge,
подтверждает файлы, удаляет Guide-зависимости и ставит одно новое наполнение с
зафиксированными настройками. Повторный клик/lease recovery не создаёт второй цикл.
При отказе Voice/диска остаются pending+причина; кнопка продолжения использует тот же
ключ. Нельзя объявлять город очищенным или принимать новые записи до подтверждения.
Старый IndexedDB blob очищается при следующем контакте, никогда не получает новый epoch.

Фактический список удаления:

- Guide: restaurants, intro/initialized_at; reviews, review_texts, proposals,
  voice_links/proposals/results/fragments, review_fragment_bindings, review_decisions;
  voice_inbox payload/failures; jobs, generation_requests, ai_usage, request_logs;
  scoped audit/manual_intro/upload reports; beta_transition_report; Guide audio manifests/files.
- Voice: submissions, runs, stages, provider_calls, outbox, asr_candidates,
  asr_evaluations/events/run_requests, scoped admin_events/intake_windows; audio,
  interrupted `.upload`/orphan files owned by this location/older epoch and purge manifest.
- Не удаляются география, соседние города, global prompts/settings/admin sessions,
  global sequence/cursor/ACK, монотонные ID/счётчики и обезличенные суточные лимиты.

## Ручная проверка PR

На тестовой установке с новым SHA, BASE_PATH `/guide`:

1. `/admin/locations`: desktop/mobile, sidebar и прежний дизайн. Открыть обычный
   город, сохранить вступление отдельной кнопкой; проверить публичную страницу.
   Повторить тот же текст: revision/журнал не растут. Две вкладки: stale edit отклоняется.
2. `/admin/restaurants?location=ivanovo`: карточки принадлежат одному городу,
   рейтинг/данные сохранены. Надлокация показывает детей, без виджета/наполнения.
3. `/ivanovo`: один первоначальный запуск, двойной клик даёт одну job. Ошибка
   оставляет статус без публичного повтора. Админ повторяет из `/logs/ivanovo/1`.
   Успех убирает кнопку; успешный лог не даёт regenerate/reprocess.
4. Записать и прослушать отзыв посетителем, затем администратором. Оба проходят
   обычную модерацию. Потерять 202/перезагрузить: повторяется тот же blob/key.
5. `/admin/reviews`: открыть ЧПУ `/admin/reviews/{slug}/{number}`; оба ASR-результата,
   текст/аудио/пакет, raw IP/UA/cookie доступны администратору. Анониму карточка закрыта.
   Проверить оценки/привязки/пакет и сохранение звезды. Ошибочный event виден с retry,
   другой отзыв доставляется независимо.
6. Полный сброс: подтвердить название, дважды отправить форму. При остановленном
   Voice/ошибке unlink — pending без нового fill; после восстановления продолжить.
   Проверить исчезновение старых карточек/отзывов/логов/файлов, один новый fill,
   рост номеров, сохранность другого города, настроек, сессии и навигации.
7. Держать старую запись в другой вкладке: после сброса retry получает stale_epoch,
   старая очередь очищается. Сознательная новая запись получает новый key/epoch.
8. Выключить временные флаги по одному и проверить backend, а не только отсутствие кнопки.
