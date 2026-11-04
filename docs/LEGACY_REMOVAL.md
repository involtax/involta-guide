# Оставшиеся временные механизмы

| Механизм | Выключение | Условие удаления |
| --- | --- | --- |
| Полный бета-сброс | `BETA_RESET_ENABLED=false` в Guide | Завершить pending resets, затем убрать кнопку/маршрут/обработчик reset; epoch-фехтование оставить для действующих отправок |
| Первое публичное наполнение | `TEMPORARY_PUBLIC_FILL_ENABLED=false` | Удалить публичную кнопку, POST/status и public-fill.js; сохранить административные логи и задания |
| Публичные подробные логи | `PUBLIC_FILL_LOGS_ENABLED=false` | Закрыть анонимный просмотр; админские логи оставить |
| Upload debug | `VOICE_DEBUG_ENABLED=false` в Guide | Удалить browser/server collector и закрыть debug endpoints после стабилизации транспорта |
| ASR-сравнение | `VOICE_ASR_BETA_ENABLED=false` в Voice | Завершить эксперимент, выбрать модель, убрать сравнение/оценки/дополнительные задания и временные таблицы |

Дополнительные одноразовые переходы перечислены ниже. Удалены вторичные restaurant links,
attach/shared updates и глобальные блокировки каталога, page_data, редакторский голос,
Alex-сессии/команды, Guide review jobs, guide_transcription и архивные API/CLI.
Исторические SQL-миграции остаются только цепочкой установки текущей схемы;
совместимость с дорефакторинговой матрицей баз не поддерживается.

## Одноразовый production-переход beta_core

После успешного перехода production и проверки восстановления убрать
`deploy/beta-transition.py`, `transition-beta-production.yml` и режим
`beta-transition` из SSH wrapper вместе с тестами одноразового контроллера.
До завершения перехода/приёмки сохранять; штатные деплои и их rollback не удалять.

## Объединение Voice и базы

После проверки production и завершения окна отката на старый образ удалить
`shared_database.py`, `deploy/shared-database-env.py`, старые имена сервисов
из перехода/rollback и `VOICE_DATABASE_URL` из серверной конфигурации. Старый
PostgreSQL volume Voice остаётся резервной копией до отдельного разрешения на удаление.
Новые процессы уже используют единственный `DATABASE_URL`.
