# Один образ: сборка → проверка → публикация → деплой

`ci.yml` проверяет Python/браузерные сценарии в кешируемом тестовом контейнере, один раз собирает linux/amd64
production-образ, затем запускает проверки установленного пакета, настоящих
контейнеров Guide/Voice, PostgreSQL cutover/rollback и Nginx. Проверка ресурсов
сверяет SHA256 всех файлов `src` с Git, CLI, UID 10001 и native ASR.

На main готовый Docker archive передаётся отдельной job `publish`: только у неё
есть `packages: write`. Она загружает архив, сверяет image ID, revision, platform,
публикует и сверяет registry config digest с проверенным image ID. Другой образ
под существующим SHA не перезаписывается. Архив хранится один день, сам образ — GHCR.

`resolve-production-image.yml` ждёт успешный main push CI (до 45 минут), выбирает
SHA-tag и фиксирует digest. Bootstrap и обычный deploy используют этот resolver.
Обычный `deploy-production.yml` обновляет весь стек Guide/Voice; отдельного
voice-deploy workflow нет. PR и push не запускают production автоматически.
Проверки после обновления и rollback сохраняются. Общая concurrency + серверный
flock сериализуют релизы. Ошибка позднего HTTP/admin-check тоже вызывает rollback.

## Docker-слои

Снизу вверх (стабильное → часто изменяемое):

1. Закреплённый digest Python, системные runtime-пакеты.
2. `.venv`: зависимости из lock, включая native Sherpa; без исходников проекта.
3. `/app/project`: установленный Python-пакет, metadata и CLI, без web-ресурсов.
4. Каталог, миграции и служебные файлы.
5. Крупные браузерные VAD-бинарники.
6. Voice JS, Guide CSS/JS.
7. Jinja-шаблоны — последний файловый слой.

Application stage исключает ресурсы до установки пакета, поэтому правка HTML
не инвалидирует установку Python и сборку Sherpa. Runtime не содержит uv/gcc,
работает непривилегированно, ресурсы недоступны ему для записи. Секреты не входят
ни в слои, ни в build args. Для локального proxy CA есть BuildKit secret `proxy_ca`.

BuildKit cache хранится в GitHub Actions (`production-linux-amd64`, mode=max).
Первый прогон холодный; последующие изменения шаблонов используют прежние слои.
GHCR и production Docker передают только отсутствующие слои, однако передача
проверенного archive между CI jobs включает весь образ. Это цена разделения прав
проверки и публикации. Изменение lock/base digest закономерно пересобирает зависимости.

## Что ускоряется, а что остаётся

Убраны повторная сборка при публикации и повторный CI внутри deploy. Правка
текста меняет маленький слой, но остаётся полноценным релизом: обязательные тесты,
новый manifest/digest, замена своих контейнеров и проверки. Горячего редактирования
production-файлов и отдельной несогласованной версии шаблонов нет.
Полный pytest продолжает выполняться на каждом CI. Docker cache хранит окружение,
но никогда не результаты тестов. Длительности 25 самых медленных тестов печатаются
в логе pytest для следующего этапа оптимизации.

Для проверки слоёв собери базовый образ, затем в отдельной копии исходников
добавь комментарий в один Jinja-шаблон и собери probe. `docker image inspect`
должен показать одинаковые `RootFS.Layers` до последнего и изменённый последний.
Probe не публикуется и не попадает в Git/production.

## Тестовое окружение

Docker target `test` ответвляется от `dependencies`: тот же Python и уже собранные
production-зависимости, включая Sherpa. Поверх устанавливаются dev-зависимости
из `uv.lock`, ffmpeg, SSH client и закреплённый lock-файлом Playwright/Chromium.
`uv sync` добавляет dev-пакеты в существующую venv, не компилирует Sherpa повторно.
Runtime остаётся последним/default target и не наследует тестовые инструменты.

В образе тестов нет checkout: `deploy/test-container.sh` монтирует текущую рабочую
копию и запускает команды напрямую, без `uv sync` при запуске. `PYTHONPATH` указывает
на текущий `src`; Python-процессы браузерных тестов наследуют его. Изменения кода,
тестов и шаблонов не пересобирают тестовое окружение. Изменение lock-файла, Python
или команд установки пересобирает соответствующие слои.

BuildKit использует отдельный cache scope `test-linux-amd64`; оба образа могут
читать общий слой зависимостей из обоих scopes. Тестовый образ не публикуется
в GHCR и не входит в release archive. На холодном кеше появятся расходы на
сборку Chromium/tooling; при тёплом — остаётся загрузка и распаковка кеша/образа.

Тестовый контейнер запускается от UID/GID runner, с его Docker CLI/Compose и
socket для существующих container smoke checks. Он имеет полномочия Docker
runner и предназначен только для одноразового CI или локального тестового
окружения, не для production. Host network нужен для локального PostgreSQL и
тестовых HTTP-серверов. Временные файлы лежат в `var/ci-tmp` под тем же абсолютным
путём: соседние контейнеры Nginx/Compose видят правильные bind mounts. Скриншоты
остаются в checkout для существующего upload-artifact. Передача env ограничена
тестовыми настройками; API-ключи и GitHub credentials не передаются.

Локальный запуск (только с тестовой БД):

```sh
docker build --target test -t involta-guide:test .
export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:5432/guide_test
deploy/test-container.sh pytest -q tests/unit tests/integration tests/e2e tests/deployment --durations=25
```

В managed cloud передавай доверенный CA через `--secret id=proxy_ca,src=...`.
Сокет Docker и Compose plugin должны быть доступны. Никакой установки Python-
зависимостей на хосте больше не требуется. Shellcheck, actionlint и проверка
Compose остаются на runner; проверки Python и production-контейнеров — в target `test`.
