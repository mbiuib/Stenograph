# Стенограф (Stenograph)

Локальная транскрибация встреч: файлы, live-захват (микрофон + системный звук),
интеграция с Jitsi через Jigasi, диаризация спикеров и LLM-постобработка
(протокол встречи) через любой OpenAI-совместимый сервис (LM Studio, Ollama, ...).

Данные не покидают вашу машину: и ASR, и диаризация работают локально.

## Статус

**Milestone 0 — ядро.** Файловая транскрибация (faster-whisper), SQLite-история,
HTTP API с SSE-событиями, CLI, шина событий, реестр движков.

Дальше по плану: движок MOSS, live-захват (WASAPI loopback + микрофон),
Jitsi-мост (streaming-whisper для Jigasi), диаризация, LLM-постобработка,
веб-интерфейс.

## Быстрый старт

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
cp .env.example .env    # поправьте пути (ffmpeg, каталог моделей)

stenograph transcribe meeting.mp4          # CLI
uvicorn stenograph.api.app:create_app --factory   # сервер
```

## Архитектура

```
src/stenograph/
├── domain/     # модели и ошибки (не зависят ни от чего)
├── engines/    # ASR/диаризация за протоколами + реестр движков
├── media.py    # ffmpeg: probe и извлечение аудио (16 кГц, моно)
├── storage.py  # SQLite-репозиторий задач (WAL, версия схемы)
├── events.py   # шина событий для SSE/WebSocket
├── pipeline.py # стадии обработки файловой задачи
├── service.py  # очередь задач, воркер, отмена
├── api/        # FastAPI: тонкий HTTP-слой
└── cli.py      # `stenograph transcribe`
```

Движки подключаются как плагины (см. `engines/__init__.py`): новый бэкенд —
это модуль с реализацией протоколов из `engines/base.py` плюс регистрация.
Ядро о конкретных движках не знает.

## Контракт событий

`status` · `meta` · `progress` · `segment` · `segments_replaced` · `done` ·
`error` · `cancelled` — см. `events.py`. Тот же контракт будет использоваться
live-режимом и Jitsi-мостом.
