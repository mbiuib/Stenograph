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
источникам («Они» / «Вы»); аудио обеих дорожек сохраняется в `data/live/<job>/`,
и после остановки запись автоматически уходит на улучшение точным движком (moss)
в отдельную задачу с реальными спикерами.

**Milestone 4 — Jitsi-мост.** Нативный streaming-whisper эндпоинт для Jigasi
(`WS /ws/{meeting_id}`): виртуальный участник-транскрибатор дозванивается к нам,
аудио каждого участника приходит отдельным потоком с идентификатором, титры
(partial/final) возвращаются в конференцию и видны в интерфейсе Jitsi; встреча
сохраняется как задача с дорожкой каждого участника. Для Jigasi спец-сервис
`org.jitsi.jigasi.transcription.WhisperTranscriptionService` и URL моста
(см. «Подключение к Jitsi»).

Дальше по плану: LLM-постобработка (протоколы и резюме через LM Studio).

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
Записанное аудио дорожек лежит в `data/live/<job>/`; после остановки запись
**автоматически уходит на улучшение**: обе дорожки перетранскрибируются с нуля
движком по умолчанию (moss) и сливаются в один точный транскрипт с реальными
спикерами — системная дорожка с диаризацией, микрофон помечен «Вы». Автозапуск
отключается (`MEETSCRIBE_LIVE_AUTO_REPROCESS=false`), кнопка «Улучшить» на
странице задачи остаётся доступной всегда; движок можно переопределить
(например, `whisper` — быстрее, без диаризации).

## Подключение к Jitsi

Участник-транскрибатор — штатный Jigasi с кастомным сервисом: он подключается к
конференции, получает аудио каждого участника отдельно (диаризация не нужна —
говорящий известен) и стримит его в «Стенограф» по протоколу streaming-whisper;
титры возвращаются в конференцию и показываются в UI Jitsi.

На стороне Jitsi (docker-jitsi-meet, профиль `transcriber.yml`):

```bash
ENABLE_TRANSCRIPTIONS=1
JIGASI_TRANSCRIBER_CUSTOM_SERVICE=org.jitsi.jigasi.transcription.WhisperTranscriptionService
JIGASI_TRANSCRIBER_WHISPER_URL=ws://<адрес-стенографа>:8000/ws
PREFERRED_LANGUAGE=ru-RU   # язык транскрибации по умолчанию
USE_APP_LANGUAGE=0         # не подменять языком интерфейса
```

```
docker compose -f docker-compose.yml -f transcriber.yml up -d
```

Сервер «Стенографа» должен слушать `0.0.0.0` (`start_server.bat` уже настроен) —
Jigasi обращается к нему по LAN. В конференции: «More actions» → «Closed
captions» → «Start closed captions». Результат — задача «Jitsi-сессия …» в вебе:
живой транскрипт по участникам, аудио дорожек в `data/jitsi/<job>/`, экспорт
TXT/SRT/JSON. Если язык конференции не совпадает с `PREFERRED_LANGUAGE`,
устойчивый русский текст не получится: whisper получает язык из запроса Jitsi.

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
├── live/       # live-режим: WASAPI-захват, streaming, сессии
├── bridge/     # Jitsi-мост: протокол Jigasi, сессии встреч
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
