# Стенограф (Stenograph)

Локальная транскрибация встреч: файлы, live-захват (микрофон + системный звук)
и встречи в Jitsi — с разделением спикеров, живыми субтитрами и ИИ-протоколом
встречи. Распознавание и обработка выполняются на вашей машине: данные не
покидают контур.

## Возможности

- **Файлы** — аудио и видео в транскрипт с таймкодами и спикерами. Движки:
  `whisper` (faster-whisper, быстрый) и `moss` (end-to-end ASR + диаризация за
  проход); длинные записи обрабатываются чанками. Экспорт TXT · SRT · JSON.
- **Live** — запись прямо из браузера: микрофон и звук системы/вкладки
  (Chrome/Edge), с любого компьютера в сети; устойчивый текст на лету с
  задержкой ~1–2 с; после остановки запись автоматически «улучшается» движком
  `moss` — точный транскрипт с реальными спикерами.
- **Jitsi** — штатный Jigasi заходит в конференцию виртуальным участником:
  аудио каждого говорящего приходит отдельным потоком, субтитры partial/final
  возвращаются в интерфейс Jitsi, встреча сохраняется как транскрипт по
  спикерам. Схема и настройка — [docs/JITSI.md](docs/JITSI.md).
- **Анализ встречи** — локальная LLM (LM Studio или любой OpenAI-совместимый
  сервер) собирает документ встречи: **протокол** (тема, обсуждение, решения,
  задачи, открытые вопросы) или **резюме**. Длинные записи — map-reduce
  с прогрессом; результат — markdown с экспортом в `.md`.
- **Веб-интерфейс** — дашборд (активная задача, прогресс и ETA, очередь,
  статистика), история задач, живой транскрипт, кнопки анализа. Плюс HTTP API
  с SSE-событиями и консоль `stenograph`.

## Быстрый старт

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
cp .env.example .env      # поправьте ffmpeg/ffprobe и каталог моделей

# Движок MOSS (опционально; torch тянется из индекса PyTorch, сборка CUDA 13.0):
uv pip install --python .venv/Scripts/python.exe "torch==2.11.0" "torchaudio==2.11.0" --index-url https://download.pytorch.org/whl/cu130
uv pip install --python .venv/Scripts/python.exe -e ".[moss]"

cd frontend && npm install && npm run build && cd ..   # веб-интерфейс
```

Нужны Python 3.12, Node.js 20+ и ffmpeg/ffprobe. Модели скачиваются
автоматически или берутся из локального каталога (`MEETSCRIBE_MODELS_DIR`).

```bash
stenograph transcribe meeting.mp4    # CLI: транскрибация файла
start_server.bat                     # сервер + веб → http://127.0.0.1:8000
start_https.bat                      # то же по HTTPS (:443) — нужен для live
```

## Использование

**Live.** Запись идёт из браузера того, кто открыл страницу: «Вы» — микрофон,
«Они» — звук системы или вкладки (в окне выбора отметьте «Поделиться звуком»;
Chrome/Edge). Аудио стримится на сервер и распознаётся на лету (whisper-turbo,
задержка 1–2 с). Браузеры пускают к микрофону только на HTTPS — на localhost
работает и так, для доступа с других машин есть `start_https.bat`
(см. «HTTPS и сертификаты»). Дорожки сохраняются в `data/live/<job>/`; после
остановки записи автоматически перетранскрибируются движком `moss` и сливаются
в точный транскрипт (системная дорожка — с диаризацией, микрофон — «Вы»).
Автоулучшение отключается настройкой `MEETSCRIBE_LIVE_AUTO_REPROCESS=false`;
кнопка «Улучшить» на странице задачи доступна всегда.

**Jitsi.** На стороне docker-jitsi-meet (профиль `transcriber.yml`):

```bash
ENABLE_TRANSCRIPTIONS=1
JIGASI_TRANSCRIBER_CUSTOM_SERVICE=org.jitsi.jigasi.transcription.WhisperTranscriptionService
JIGASI_TRANSCRIBER_WHISPER_URL=ws://<адрес-стенографа>:8000/ws
PREFERRED_LANGUAGE=ru-RU   # язык распознавания
USE_APP_LANGUAGE=0         # не подменять языком интерфейса

docker compose -f docker-compose.yml -f transcriber.yml up -d
```

Сервер «Стенографа» слушает `0.0.0.0` (см. `start_server.bat`). В конференции:
«More actions» → «Closed captions» → «Start closed captions». Протокол, код и
диагностика — [docs/JITSI.md](docs/JITSI.md).

**Анализ.** По завершённой задаче (файл, live или Jitsi) — кнопки «Составить
протокол» / «Сделать резюме» на странице задачи; результат доступен как
markdown и отдельной задачей «Анализ» в истории. Из CLI:

```bash
stenograph analyze <job_id> --type protocol   # или summary
```

## HTTPS и сертификаты

Браузеры дают доступ к микрофону только на HTTPS (или на localhost), поэтому
для live-записи с других компьютеров веб отдаётся по HTTPS: `start_https.bat`
запускает Caddy, который терминирует TLS на :443 и проксирует всё (страницы,
API, SSE, WebSocket) на приложение `127.0.0.1:8000`. Приложение при этом
работает как раньше — мост для Jigasi остаётся на `ws://` и ничего не знает
о сертификатах.

Сертификат — конфигурация, а не код:

- по умолчанию берутся `certs/stenograph.crt` + `certs/stenograph.key`;
- пути переопределяются переменными `TLS_CERT_FILE` / `TLS_KEY_FILE`;
- подходит любой X.509 PEM: локальный CA, корпоративный CA, Let's Encrypt;
- замена без перезапуска приложения: положить файлы и выполнить `caddy reload`;
- выпуск/перевыпуск из локального CA: `scripts/make_cert.sh 192.168.0.9`
  (CA лежит в `~/stenograph-ca`; можно передать другой IP/имя хоста).

На клиентских машинах нужно один раз добавить `ca.crt` в доверенные — так же,
как для Jitsi: `certutil -user -addstore -f Root <путь>\ca.crt`.

## Настройки

Все параметры — переменные `MEETSCRIBE_*` в `.env` (полный список с
комментариями — в `.env.example`). Читаются один раз при старте процесса.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MEETSCRIBE_ENGINE` | `whisper` | движок по умолчанию: `whisper` или `moss` |
| `MEETSCRIBE_DATA_DIR` | `./data` | задачи, загрузки, БД, аудио |
| `MEETSCRIBE_MODELS_DIR` | — | каталог локальных моделей (иначе — загрузка с HF) |
| `MEETSCRIBE_FFMPEG` / `MEETSCRIBE_FFPROBE` | `ffmpeg` / `ffprobe` | пути к бинарям |
| `MEETSCRIBE_LIVE_MODEL` | `large-v3-turbo` | модель live-режима |
| `MEETSCRIBE_LIVE_AUTO_REPROCESS` | `true` | автоулучшение записей live через `moss` |
| `MEETSCRIBE_LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | OpenAI-совместимый сервер для анализа |
| `MEETSCRIBE_LLM_MODEL` | `gemma-3-4b-it` | модель анализа |

## Архитектура

```
src/stenograph/
├── domain/     # модели и ошибки
├── engines/    # ASR/диаризация за протоколами + реестр движков
├── media.py    # ffmpeg: probe и извлечение аудио (16 кГц, моно)
├── storage.py  # SQLite-репозиторий задач (WAL)
├── events.py   # шина событий (SSE/WebSocket)
├── pipeline.py # стадии обработки, переходы состояний
├── service.py  # очередь задач, воркер, отмена
├── live/       # live-режим: браузерный захват + WASAPI, streaming, сессии
├── bridge/     # Jitsi-мост: streaming-whisper для Jigasi
├── llm/        # анализ встречи: клиент, промпты, map-reduce
├── api/        # FastAPI: HTTP + SSE + WebSocket
└── cli.py      # консоль `stenograph`
```

Движки подключаются как плагины: новый — модуль с реализацией протоколов из
`engines/base.py` плюс регистрация в `engines/__init__.py`; ядро о конкретных
моделях не знает. Подробнее о слоях и потоках — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Статус

Реализовано: ядро и файловая транскрибация (whisper + moss с диаризацией),
веб-интерфейс с дашбордом и живым транскриптом, live-режим (запись из браузера)
с автоулучшением, Jitsi-мост через Jigasi, LLM-анализ (протокол и резюме).

Дальше: масштабирование (несколько воркеров), вынос ASR-движков во внешние
сервисы, тонкая настройка промптов под корпоративные шаблоны протоколов.
