# Полный стек в Docker: Jitsi + Стенограф + ASR-сервис

Один `docker-compose.yml` поднимает **всё сразу** (проект `stenograph-stack`):

| Сервис | Что это | Порты на хосте |
| --- | --- | --- |
| `web`,`prosody`,`jicofo`,`jvb` | Jitsi с локального стенда | `8081` (http), `8443` (https), `10000/udp` |
| `transcriber` | jigasi — шлёт звук комнат на мост Стенографа | — |
| `stenograph` | сам сервер (UI + API + мост) | `443` |
| `asr` | внешний ASR-сервис (whisper + moss, GPU) | `127.0.0.1:9001` (только localhost) |

## Как это связано

- Браузеры ходят на `https://<этот-пк>:8443/` (Jitsi) и `https://<этот-пк>/` (Стенограф).
- jigasi (контейнер) дозванивается до моста по `wss://host.docker.internal/ws` — это
  хост-порт 443, который публикует контейнер `stenograph`. Ничего в Jitsi менять не нужно.
- Контейнер `stenograph` запущен с `MEETSCRIBE_ASR_BACKEND=remote`: **он сам модели не грузит**,
  весь ASR уходит по HTTP в контейнер `asr` (внутренняя сеть, `http://asr:9001`).
- `asr` — единственный контейнер с GPU: получает видеокарту через
  `deploy.resources.reservations.devices` и читает модели из папки на хосте.
- LLM-анализ идёт мимо: на LM Studio на хосте (`host.docker.internal:1234`).

## Данные — примонтированы «на месте», ничего не копируется

| Хост | В контейнере | Что там |
| --- | --- | --- |
| `..\..\data` | `/app/data` | задачи, загрузки, SQLite, рабочие файлы |
| `..\..\certs` | `/app/certs` | TLS-сертификат сервера |
| `..\..\.env` | `/app/.env` | все настройки `MEETSCRIBE_*` (читаются, не пишутся) |
| `..\jitsi-windows\config` | (тома Jitsi) | состояние стенда: prosody, jvb, jigasi, транскрипты |
| папка моделей (по умолчанию `G:\LLMs\models`) | `/models` | модели whisper/moss (rw для `asr`, ro для `stenograph`) |

Между запусками контейнеров ничего не теряется — всё живёт в этих папках.

## Запуск

1. Останови то, что занимает те же порты:
   - стенд: `deploy\jitsi-windows\stop.bat`;
   - нативный сервер: Ctrl+C в его окне (`start_server.bat`).
2. `start_stack.bat` (или вручную):
   ```
   cd deploy\stack
   docker compose --env-file ..\jitsi-windows\.env -f docker-compose.yml up -d --build
   ```
   `start_stack.bat` сам подставляет `..\jitsi-windows\.env` — **тот же** файл с паролями
   Jitsi, что и у стенда: состояние в `config/` остаётся логинящимся.
3. **Первая сборка долгая** (~10–25 мин): скачиваются CUDA-torch, transformers, MOSS и т.д.
   Дальнейшие пересборки — секунды (кэш слоёв), перезапуск — `up -d` без `--build`.

## Проверка

```
docker compose --env-file ..\jitsi-windows\.env -f docker-compose.yml ps
curl -sk https://127.0.0.1/api/health          # сервер Стенографа
curl -s  http://127.0.0.1:9001/health          # ASR-сервис (local loopback)
curl -s  http://127.0.0.1:9001/v1/models
```

E2E: позвони в любую комнату Jitsi — jigasi подключится к мосту, субтитры пойдут,
запись появится в «Задачах». Файл: загрузи через UI — задача уйдёт в `asr`.

## Настройки

Переменные окружения (в основном `.env` этого каталога или `..\jitsi-windows\.env`):

| Переменная | По умолчанию | Смысл |
| --- | --- | --- |
| `STENOGRAPH_MODELS_DIR` | `G:/LLMs/models` | папка моделей на хосте |
| `ASR_HOST_PORT` | `9001` | порт ASR-сервиса на хосте (только 127.0.0.1) |
| `STENOGRAPH_IMAGE` | `stenograph-stack:local` | имя образа |
| `STACK_CONFIG` | `../jitsi-windows/config` | каталог состояния Jitsi |

Из `..\..\.env` контейнер `stenograph` читает всё остальное (движок, язык, пороги,
LLM, выгрузка моделей по простою...); из `..\jitsi-windows\.env` — пароли и порты Jitsi.

## Обновление после правок кода

```
cd deploy\stack
docker compose --env-file ..\jitsi-windows\.env -f docker-compose.yml up -d --build
```
Фронтенд собирается в образ из `frontend\dist` — пересобери его (`npm run build`),
потом пересобери контейнеры.

## Оговорки

- Монитор в стеке: карточка «Внешний ASR-сервис» показывает модели из `asr`
  (VRAM/RAM/простой, кнопка «Выгрузить простаивающие» работает через прокси);
  GPU в верхних карточках берётся из сервиса — в контейнере сервера nvidia-smi нет.
- LLM-анализ из контейнеров идёт на `host.docker.internal:1234`: в LM Studio включи
  «Serve on Local Network», иначе он слушает только 127.0.0.1 и из докера недоступен
  (Монитор честно покажет «не отвечает»).
- Контейнер `stenograph` работает только в remote-режиме (локальные модели — это
  нативный запуск; GPU резервируется только у `asr`).
- Нативный сервер и стек **нельзя запускать одновременно** — общий порт 443.
- `..\..\.env` монтируется как файл: если его удалить и запустить стек, Docker создаст
  на его месте *папку* — тогда верни файл (`git checkout .env` / восстанови из бэкапа).
- Если у `asr` нет GPU: проверь Docker Desktop → Settings → Resources → WSL2 и драйвер
  NVIDIA; `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`
  должен показывать карту.
