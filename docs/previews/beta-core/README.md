# Локальный браузерный рендер beta core

Это работающие шаблоны PR на синтетических данных тестов, не production.
Исходная компоновка админки PR17/18 сохранена. Архитектура и интерфейс реализованы
по поручению владельца на [BETA_CORE_SIMPLIFICATION_SPEC.md](../../BETA_CORE_SIMPLIFICATION_SPEC.md).

| Локация, desktop | Сброс, mobile |
| --- | --- |
| ![Локация](location-desktop.png) | ![Полный сброс](reset-mobile.png) |

| Единая карточка PR18, desktop | Mobile |
| --- | --- |
| ![Отзыв](review-desktop.png) | ![Отзыв mobile](review-mobile.png) |

Матрица проверяется `tests/integration/test_admin_design.py`: desktop/tablet/mobile,
единый shell, навигация, обе ASR-модели, отсутствие горизонтального переполнения.
Полные скриншоты остальных экранов сохраняются в CI browser-screenshots artifact.
