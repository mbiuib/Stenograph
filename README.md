# Стенограф (Stenograph)

Локальная транскрибация встреч: файлы, live-захват (микрофон + системный звук),
интеграция с Jitsi через Jigasi, диаризация спикеров и LLM-постобработка
(протокол встречи) через любой OpenAI-совместимый сервис (LM Studio, Ollama, ...).

Данные не покидают вашу машину: и ASR, и диаризация работают локально.

## Статус

**Milestone 0 — ядро.** Файловая транскрибация (faster-whisper), SQLite-история,
HTTP API с SSE-событиями, CLI, шина событий, реестр движков.

**Milestone 1 — движок MOSS** (end-to-end ASR + диаризация за проход): чанкинг
длинных файлов по 300 с с перехлёстом, дедупликация границ, метки спикеров;
переключается настройкой `MEETSCRIBE_ENGINE=moss` или флагом `--engine`.

**Milestone 2 — веб-интерфейс** (React + TypeScript + Vite + Tailwind): дашборд
(статистика, активная задача с прогрессом и ETA «сколько осталось», очередь,
график активности), история задач с фильтрами, загрузка файлов, страница задачи
с живым SSE-транскриптом, спикерами и экспортом (TXT/SRT/JSON).

**Milestone 3 — live-режим** (страница «Live»): захват системного звука (WASAPI
loopback) и микрофона с распознаванием на лету — whisper-turbo + local-agreement:
устойчивый текст с задержкой ~1–2 с, черновик текущей фразы серым; спикеры по
источникам («Они» / «Вы»); аудио обеих дорожек сохраняется в `data/live/<job>/`.

Дальше по плану: Jitsi-мост (streaming-whisper для Jigasi), LLM-постобработка
(протоколы и резюме через LM Studio).

## Быстрый старт

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
# Опционально — движок MOSS (torch ставится из индекса PyTorch, сборка CUDA 13.0):
uv pip install --python .venv/Scripts/python.exe "torch==2.11.0" "torchaudio==2.11.0" --index-url https://download.pytorch.org/whl/cu130
uv pip install --python .venv/Scripts/python.exe -e ".[moss]"
cp .env.example .env    # поправьте пути (ffmpeg, каталог моделей)

cd frontend && npm install && npm run build && cd ..   # веб-интерфейс

stenograph transcribe meeting.mp4                  # CLI (движок из настроек)
stenograph transcribe meeting.mp4 --engine whisper # ...или явно
uvicorn stenograph.api.app:create_app --factory    # сервер + веб (http://127.0.0.1:8000)
```

## Live-режим

Страница **Live**: отметьте источники и нажмите «Начать запись». «Они» — звук с
устройства вывода по умолчанию (loopback), «Вы» — микрофон по умолчанию. При
встрече без наушников микрофон слышит колонки, и речь дублируется на обеих
дорожках — используйте гарнитуру. Модель live-режима — `large-v3-turbo`
(`MEETSCRIBE_LIVE_MODEL`); устойчивый текст появляется с задержкой ~1–2 с.
Записанное аудио дорожек лежит в `data/live/<job>/` и годится для повторной
обработки точным движком через CLI.

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
