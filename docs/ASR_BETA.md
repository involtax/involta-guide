> Историческое сравнение GigaAM/Parakeet. Parakeet исключён владельцем.
> Текущий профиль: [семь профилей GigaAM](VOICE_SHERPA_COMPARISON.md).

# GigaAM / Parakeet: локальная бета распознавания

Каждая новая запись распознаётся двумя моделями последовательно на CPU.
**GigaAM — единственный источник публичного текста**. Ошибка GigaAM не включает
fallback: Parakeet остаётся только в приватной админке. Сайт автоматически выводит
10 последних готовых текстов города без редактора и одобрения. История не удаляется.
Состояния моделей независимы; готовый GigaAM не ждёт Parakeet. OpenAI-анализ
не запускается. Аудио не покидает сервер для распознавания.

Текущий интерфейс, контракты, миграция и ручная проверка: [ADMIN_REVIEWS.md](ADMIN_REVIEWS.md).

## Зафиксированные модели и лицензии

| Ключ | Модель | Локальный ONNX int8, точная ревизия | Лицензия |
|---|---|---|---|
| gigaam | [GigaAM v3 E2E RNN-T](https://github.com/salute-developers/GigaAM) | [istupakov/gigaam-v3-onnx](https://huggingface.co/istupakov/gigaam-v3-onnx/tree/322c3b29492673eb7d0b434bfa9dfb8653e34d02), `322c3b29492673eb7d0b434bfa9dfb8653e34d02` | MIT |
| parakeet | [NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | [istupakov/parakeet-tdt-0.6b-v3-onnx](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/tree/8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce), `8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce` | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode) |

Attribution: NVIDIA, Parakeet TDT 0.6B v3, ONNX conversion by istupakov.
Мы используем преобразованные и квантованные int8 веса, не изменяем исходную
модель для обучения. Ссылки на авторов, модель и лицензию приведены выше;
README/model cards сохраняются с весами. GigaAM и Silero также сохраняют LICENSE.txt.

Движок — `onnx-asr[cpu,hub]`, не PyTorch/NeMo сервис. Версии библиотек закреплены
`uv.lock`; код моделей с Hugging Face не исполняется. PyAV декодирует WAV, Ogg,
WebM, MP4 в mono float32 16 kHz. Для обеих моделей одинаковые исходные bytes,
декодирование и [Silero VAD](https://huggingface.co/istupakov/silero-vad-onnx)
ревизии `b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6`, speech chunks ≤20 с,
минимальная тишина 500 мс, batch=1. Сохраняются SHA исходного файла и PCM,
текст/сегменты, настройки, время и ревизии. Без речи — ошибка, без выдуманного отзыва.

## Установка и эксплуатация

```sh
uv sync --frozen --extra voice-local
uv run voice migrate
uv run voice prepare-model
uv run voice-worker
uv run voice asr-report
```

`VOICE_ASR_BETA_ENABLED=true`, `VOICE_TRANSCRIPTION_MODEL=gigaam`,
`VOICE_ASR_CPU_THREADS=2`, `VOICE_ASR_MODELS_DIR=var/asr-models`.
Serving использует закреплённые локальные файлы; prepare скачивает модели/VAD.
Compose монтирует prepared model volume read-only. В памяти одна модель.
Перезапуск прерванного локального этапа ограничен тремя попытками; сохранённый
raw response разбирается без нового inference. Epoch и lease fencing сохраняются.

`asr-report` — техническая сводка последних реальных результатов по записи/модели:
число, время обработки и длительность аудио. Fake-результаты исключены. Нет голосов,
эталона, WER/CER, предпочтительной модели и ручного ASR backfill. Команды/HTTP API
ручного сравнения, оценивания и повторов удалены. При deploy корпус не перепознаётся;
миграция переотправляет только уже сохранённые GigaAM-результаты.

Исходные ASR-кандидаты содержат настройки/ревизии, текст, время, сегменты и хеши.
Они доступны в отдельном приватном логе. Админская лента показывает только две
компактные строки, ошибки — кратко со ссылкой на лог. Авторские указания и лицензии
моделей выше сохраняются независимо от удаления человеческих оценок качества.
