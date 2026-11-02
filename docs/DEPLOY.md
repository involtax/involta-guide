# Production через GitHub Actions

Серверные команды вручную не нужны. Два workflow используют уже настроенные
`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`:

- **Bootstrap production** (`bootstrap-production.yml`) — первоначальная установка.
- **Deploy production** (`deploy-production.yml`) — обновление только Guide.

Оба запускаются вручную через Actions → Run workflow, ветка **main**, полный
40-символьный `sha` коммита из main. PR и push сами production не меняют.
До слияния PR и отдельного подтверждения запускать эти workflows не нужно.

## Однократная настройка GitHub

Обязательные reviewers не нужны. Подтверждение запуска — нажатие **Run workflow**
с выбранным SHA. После успешной сборки bootstrap продолжает настройку сервера
без отдельного Approve deployment. Environment **prod** используется для secrets.
Если в нём ранее включались Required reviewers или wait timer, отключите эти
необязательные ограничения в Settings → Environments → prod: workflow не обходит
настройки GitHub Environment. Доступ к запуску workflow определяется правами GitHub.

Существующие четыре SSH-секрета могут быть repository secrets. Для приложения в
Environment **prod** можно дополнительно задать:

| Secret | Использование |
|---|---|
| `OPENAI_API_KEY` | Только backend; если отсутствует, сайт работает, AI показывает понятную ошибку |
| `ADMIN_PASSWORD_HASH` | Предпочтительный Argon2id hash пароля администратора |
| `ADMIN_PASSWORD` | Альтернатива, минимум 12 символов; задайте здесь известный вам пароль для входа без SSH |
| `ALEX_SECRET` | Необязательно, случайный URL-safe секрет от 32 символов; иначе голосовые привилегии выключены |

Без ADMIN_PASSWORD/HASH bootstrap генерирует случайный пароль и сохраняет его только
в закрытом серверном env-файле. Он **не показывается** в отчёте, артефактах или логах;
для удобного входа в админку задайте свой secret **до** bootstrap.
Пароли PostgreSQL и сессий генерируются сервером автоматически.
Секреты приложения передаются по SSH на stdin в JSON, а не через аргументы команд.
Значения не попадают в исходники, image build args или браузер.

`DEPLOY_KNOWN_HOSTS` содержит публичную идентичность **сервера**, например строку
`45.12.67.102 ssh-ed25519 <полный публичный ключ сервера>`. Проверка SSH строгая;
произвольные новые ключи не принимаются. Опциональная repository variable
`DEPLOY_SSH_PORT` меняет порт с 22. GITHUB_TOKEN выдаётся Actions автоматически;
отдельный токен GHCR не нужен. Пакет GHCR должен разрешать Actions этого репозитория
читать и публиковать образы.

## Предварительные проверки без изменений сервисов

Поддерживаются Ubuntu **22.04/24.04**, Debian **12/13**, systemd, Python **3.10+**,
Bash, curl, apt/dpkg и root либо `sudo -n` без пароля. При недостаточных правах,
неподдерживаемой ОС или init-системе процесс останавливается с диагностикой.

Проверки до установки пакетов, запуска контейнеров и редактирования конфигурации:

1. Синтаксис SHA, image digests и безопасные значения secrets.
2. Права, ОС, утилиты, свободное место, принадлежность существующих ресурсов Guide.
3. Docker daemon / Compose и конфликтующие runtime-пакеты. Существующий неработающий
   daemon не запускается и не перенастраивается автоматически.
4. Текущие сервисы и Nginx: `nginx -T` должен быть успешен; нужен ровно один явный
   HTTPS `server` с `server_name involta.ru`, `listen ...443 ssl`.
5. Фактический исходный файл этого блока; права записи; отсутствие чужих `/guide`
   locations, в том числе в раскрытых include-файлах.
6. Доступность существующей главной страницы по HTTPS, её status/redirect;
   свободные loopback-порты Guide **3080** и его PostgreSQL **15432** для host Nginx.

До конца этих проверок создаются только приватная staging-директория в `/tmp`
для скриптов и lock-файл в `/run/lock`; сервисы и постоянная конфигурация не меняются.
Список running service units и обнаруженная topology попадают в очищенный отчёт.

## Поддерживаемые варианты Nginx

**Nginx на хосте:** активный systemd unit `nginx`. Guide и отдельный PostgreSQL
используют host networking, но слушают **только 127.0.0.1:3080 и 127.0.0.1:15432**.
Docker published ports, bridge/NAT и новые firewall rules не нужны.
Для **нового** Docker создаётся daemon.json с отключёнными bridge, iptables,
ip6tables, ip-forward и ip-masq; конфигурация уже установленного Engine не меняется.

**Nginx в Docker:** обнаруживается по nginx в имени/image работающего контейнера.
Его конфигурация должна быть bind-mounted с хоста. Поддерживаются host networking
либо ровно одна существующая user-defined non-internal bridge-сеть. В последнем
случае контейнеры Guide подключаются к существующей сети, upstream —
`involta-guide:3000`. Новые bridge-сети, NAT и port publishing не создаются.
PostgreSQL также находится в этой существующей сети и защищён случайным паролем,
на хост его порт не публикуется. Чужой nginx-контейнер не пересоздаётся и его
сетевые подключения не изменяются.

Nginx с конфигом только внутри image, неоднозначными TLS server blocks,
нестандартным управлением процессом/config path или несколькими сетями нельзя
безопасно угадывать: bootstrap остановится до изменений. Это требует адаптации
автоматизации под выявленную схему, а не ручного исправления сервера вслепую.
TLS termination должен уже работать для involta.ru. Сертификаты не выпускаются,
не заменяются; TLS, DNS, firewall/SSH настройки и основной сайт не перенастраиваются.

## Что делает bootstrap

- При необходимости добавляет **официальный APT repository Docker**, проверяет
  fingerprint signing key и устанавливает Engine / Compose Plugin. Конфликтующие
  пакеты не удаляются. Package hooks не могут перезапускать остальные сервисы:
  временный policy-rc.d и NEEDRESTART_MODE=list удаляются/восстанавливаются после apt.
  Существующий policy-rc.d требует остановки до изменений, а не перезаписи.
- Создаёт `/opt/involta-guide` (0700), приватные env-файлы (0600), ownership markers,
  отдельные Guide audio/PostgreSQL volumes и отдельные Compose projects.
- Поднимает **новый PostgreSQL Guide**, не подключается к БД основного сайта.
  Официальный postgres image разрешается на runner в digest и сохраняется в deploy.env.
  Миграции и seed выполняются только для этой выделенной Guide БД.
- Проверяет OCI revision образа приложения, запускает web/worker с UID/GID 10001,
  read-only filesystem и отдельным временным `/tmp`; ждёт health и правильный SHA.
- Сохраняет исходный Nginx-файл с metadata в `nginx-backups/<run-attempt>/site.conf`.
  Добавляет только два location в нужный HTTPS server, сохраняя остальной текст:
  `/guide` → **302** `/guide/` с query; `/guide/` → backend с сохранённым prefix.
- Выполняет `nginx -t` перед `nginx -s reload`. **Restart основного сайта нет.**
- Проверяет HTTPS, SHA readiness, HTML/admin routes, CSS, HTMX/widget JavaScript и
  неизменность status/redirect главной страницы. Runner повторяет проверку извне.

При коллизии имён с ресурсами без label `com.involta.guide.owner=production`
bootstrap не усыновляет и не останавливает их. Существующий Guide без release state
также нельзя молча заменить. Повторный bootstrap после успешной установки
останавливается и предлагает использовать Deploy production.

## Обновления и rollback

CI тестирует выбранный SHA, собирает multi-stage image и публикует **только**
`ghcr.io/involtax/involta-guide:<full-SHA>`. Production запускает захваченный build
**digest**, не изменяемый tag. Проверяются и OCI revision, и `revision` в readiness.
`latest` нигде не используется. SHA-tag может быть повторно опубликован rerun,
поэтому state и rollback всегда хранят именно digest.

Общая concurrency group у обоих production workflow и серверный flock запрещают
одновременные операции. Процессы не отменяются новым запуском.
`deploy-production.yml` обновляет **только web/worker Guide**: PostgreSQL, Nginx,
пакеты, env и volumes не изменяются. Предыдущий worker останавливается gracefully,
затем проверяются новый web, worker и public HTTPS. При неудаче возвращаются
сохранённые Compose snapshot и image digest предыдущей версии. Старые release
snapshots и image digests нужно сохранять для rollback.

При ошибке bootstrap восстанавливается Nginx-файл с `nginx -t` и reload; первая
неудачная установка Guide останавливается и удаляет **только свои web/worker**.
До начальной установки предыдущего контейнера нет; если он уже существует без
подтверждённого release state, проверка откажет до его изменения. При обновлениях
предыдущий контейнер восстанавливается из сохранённого digest.
БД, данные, установленные Docker packages и аудиотом сохраняются.

Если независимая проверка runner провалится после успешного SSH-шага, workflow
снова подключится и запросит rollback. Проверяются SHA + digest + run-attempt,
чтобы старый запуск не откатил более новое обновление. Для bootstrap дополнительно
сверяется checksum изменённого Nginx-файла: чужие последующие правки не перетираются.

Нет `docker compose down`, `prune`, удаления volumes, firewall/SSH/DNS/TLS команд.
Rollback приложения не откатывает схему БД и пользовательские данные. Последующие
изменения схемы требуют отдельного процесса backup + совместимой миграции;
этот workflow не обещает безопасный автоматический downgrade.
SIGTERM/SIGINT/SIGHUP запускают rollback. SIGKILL, потеря сервера/сети или жёсткое
завершение runner не позволяют гарантировать выполнение cleanup: состояние и
резервные копии сохраняются, ошибка rollback явно отражается в отчёте.

## Отчёт и проверка реализации

Bootstrap пишет очищенный JSON в `/opt/involta-guide/bootstrap-report.json`,
показывает его в Actions и прикладывает artifact с версиями, этапом, проверками,
backup path и результатом rollback. Summary содержит commit и digest.
Полный Nginx dump, env и пароли не выводятся. В приватной release-директории могут
сохраняться диагностические логи приложения; в GitHub они не выгружаются.

Локальные тесты проверяют транзакцию bootstrap с подменёнными системными командами,
редактирование nginx и failure/rollback paths; реальные Bash/flock контроллеры
проверяются с fake Docker/curl. CI также проверяет Compose, actionlint и Docker build.
Это **не** утверждение, что production уже настроен: первый реальный запуск выполняется
вручную через Run workflow после слияния изменений в main.
