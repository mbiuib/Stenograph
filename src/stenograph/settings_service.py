"""Runtime-editable settings: the registry behind the «Настройки» tab.

Settings live in :class:`stenograph.config.Settings` (environment and .env).
This module adds what the UI needs on top:

- a curated registry of the keys worth exposing — grouped, labelled, typed,
  range/choice-validated, each with a hint on when a change takes effect
  (right away / with the next session / after a restart);
- a runtime override store (``data/settings_overrides.json``) layered on top
  of the environment at startup, so changes survive restarts;
- :class:`SettingsService` — patch/reset with validation, persistence and
  side-effect appliers (the model-eviction threshold, the managers' flags…).

An override mutates the live :class:`Settings` object, and consumers read
``settings.<key>`` at use time — an update takes effect without a restart.
Where a component pinned its value at construction (a running meeting, a
loaded engine), the registry says so honestly via ``applies=`` hints.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

# When a change takes effect (shown in the UI next to every item).
APPLIES_LIVE = "live"  # сразу, без перезагрузки
APPLIES_NEXT = "next"  # новые задачи/сессии или следующая загрузка модели
APPLIES_RESTART = "restart"  # после перезапуска сервера

# (id, title, description) — the order matters: this is the tab layout.
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("processing", "Обработка и модели", "Чем и на каком языке распознаются записи."),
    ("live", "Live-запись", "Захват и распознавание живой записи (браузер и машина)."),
    ("bridge", "Jitsi-мост", "Субтитры встреч Jitsi и защита декодера от перегрузки."),
    (
        "load",
        "Нагрузка и завершение",
        "Сколько задач идёт параллельно и что происходит при завершении записей.",
    ),
    (
        "remote",
        "Внешний ASR-сервис",
        "Вынесенное распознавание в отдельном процессе (docker-стек).",
    ),
    ("llm", "LLM-анализ", "Протоколы и резюме через OpenAI-совместимый сервер (LM Studio)."),
    ("misc", "Сервер и данные", "Расположение данных и параметры запуска — только чтение."),
)

@dataclass(frozen=True)
class SettingDef:
    """One exposed setting: how to label it, validate it and apply it."""

    key: str
    label: str
    group: str
    kind: str  # "bool" | "int" | "float" | "str" | "secret"
    applies: str = APPLIES_LIVE
    hint: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    editable: bool = True


_GROUPS = [group[0] for group in GROUPS]


@lru_cache(maxsize=1)
def _defs() -> tuple[SettingDef, ...]:
    """The registry: («все настройки приложения», по группам)."""
    return (
        # -- обработка и модели ------------------------------------------------
        SettingDef(
            "engine",
            "Движок по умолчанию",
            "processing",
            "str",
            APPLIES_RESTART,
            "whisper, moss или gigaam; на отдельной задаче можно выбрать другой.",
        ),
        SettingDef(
            "live_engine",
            "Движок живой расшифровки",
            "live",
            "str",
            APPLIES_RESTART,
            "Окна субтитров Live/Jitsi: whisper (large-v3-turbo) или gigaam "
            "(мультиязычный CTC). Применяется после перезапуска.",
            choices=("whisper", "gigaam"),
        ),
        SettingDef(
            "gigaam_model",
            "Модель GigaAM",
            "processing",
            "str",
            APPLIES_RESTART,
            "HF-репозиторий или папка «GigaAM-Multilingual» в каталоге моделей.",
        ),
        SettingDef(
            "gigaam_revision",
            "Ревизия GigaAM",
            "processing",
            "str",
            APPLIES_RESTART,
            "ctc (220M) — быстрая; large_ctc (600M) — точнее.",
            choices=("ctc", "large_ctc"),
        ),
        SettingDef(
            "gigaam_chunk_sec",
            "Чанк GigaAM, с",
            "processing",
            "float",
            APPLIES_RESTART,
            "Кусок нарезки длинных файлов (модель принимает не больше 25 с за проход).",
            minimum=5,
            maximum=24.5,
        ),
        SettingDef(
            "reprocess_engine",
            "Движок улучшений",
            "processing",
            "str",
            APPLIES_NEXT,
            "Авто-улучшение записей Live/Jitsi; пусто — движок по умолчанию.",
        ),
        SettingDef(
            "language",
            "Язык распознавания",
            "processing",
            "str",
            APPLIES_NEXT,
            "auto — определять автоматически; иначе код языка (ru, en, …).",
        ),
        SettingDef(
            "device",
            "Устройство",
            "processing",
            "str",
            APPLIES_NEXT,
            "cuda или cpu — применится при следующей загрузке модели.",
        ),
        SettingDef(
            "compute_type",
            "Точность вычислений",
            "processing",
            "str",
            APPLIES_NEXT,
            "float16 (по умолчанию), int8_float16, int8 — при следующей загрузке модели.",
        ),
        SettingDef(
            "whisper_model",
            "Модель Whisper (файлы)",
            "processing",
            "str",
            APPLIES_NEXT,
            "Имя модели для обработки файлов; применится при следующей загрузке.",
        ),
        SettingDef(
            "live_model",
            "Модель Whisper (live/субтитры)",
            "processing",
            "str",
            APPLIES_NEXT,
            "Лёгкая модель для живой расшифровки; применится при следующей загрузке.",
        ),
        SettingDef(
            "moss_chunk_sec",
            "MOSS: длина чанка, с",
            "processing",
            "float",
            APPLIES_NEXT,
            "Нарезка длинных записей для MOSS.",
            minimum=30,
            maximum=3600,
        ),
        SettingDef(
            "moss_chunk_overlap_sec",
            "MOSS: перекрытие чанков, с",
            "processing",
            "float",
            APPLIES_NEXT,
            "Склейка на границах чанков.",
            minimum=0,
            maximum=60,
        ),
        SettingDef(
            "moss_max_new_tokens",
            "MOSS: лимит токенов",
            "processing",
            "int",
            APPLIES_NEXT,
            "Максимум токенов на чанк; сторожевой кэп вывода.",
            minimum=256,
            maximum=65536,
        ),
        # -- live --------------------------------------------------------------
        SettingDef(
            "realtime_transcribe",
            "Распознавать во время записи",
            "live",
            "bool",
            APPLIES_LIVE,
            "Глобальный дефолт; на странице Live есть галочка для отдельной записи. "
            "Выключено — пишется только звук, текст появится при улучшении.",
        ),
        SettingDef(
            "live_auto_reprocess",
            "Улучшать записи после остановки",
            "live",
            "bool",
            APPLIES_LIVE,
            "Автоматически перезапускать запись через движок улучшений "
            "(Live и встречи Jitsi).",
        ),
        SettingDef(
            "live_chunk_sec",
            "Кадр захвата, с",
            "live",
            "float",
            APPLIES_NEXT,
            "Размер кадра аудио от захвата; применяется к новым записям.",
            minimum=0.05,
            maximum=2.0,
        ),
        SettingDef(
            "live_step_sec",
            "Шаг расшифровки, с",
            "live",
            "float",
            APPLIES_NEXT,
            "Как часто обновляется текст; к новым записям.",
            minimum=0.2,
            maximum=5.0,
        ),
        SettingDef(
            "live_max_window_sec",
            "Окно расшифровки, с",
            "live",
            "float",
            APPLIES_NEXT,
            "Максимум аудио за один прогон live-модели; к новым записям.",
            minimum=5,
            maximum=60,
        ),
        SettingDef(
            "live_turn_ticks",
            "Инференсов за ход очереди",
            "live",
            "int",
            APPLIES_LIVE,
            "Максимум прогонов live-модели за один ход в общей очереди.",
            minimum=1,
            maximum=64,
        ),
        SettingDef(
            "live_turn_sec",
            "Секунд GPU за ход",
            "live",
            "float",
            APPLIES_LIVE,
            "Лимит времени на один ход очереди (чтобы одна запись не заняла декодер).",
            minimum=0.5,
            maximum=60,
        ),
        SettingDef(
            "live_batch_max",
            "Окон в батче",
            "live",
            "int",
            APPLIES_LIVE,
            "Сколько окон разных записей объединяется в один прогон модели.",
            minimum=1,
            maximum=64,
        ),
        SettingDef(
            "live_batch_window_sec",
            "Окно для live, с",
            "live",
            "float",
            APPLIES_LIVE,
            "Размер окна живой расшифровки в батче.",
            minimum=1,
            maximum=30,
        ),
        SettingDef(
            "live_batch_hold_sec",
            "Удержание хвоста (live), с",
            "live",
            "float",
            APPLIES_LIVE,
            "Хвост окна, переносимый в следующий раунд (точность на стыках).",
            minimum=0,
            maximum=5,
        ),
        # -- jitsi bridge ------------------------------------------------------
        SettingDef(
            "bridge_language",
            "Язык встреч (форс)",
            "bridge",
            "str",
            APPLIES_NEXT,
            "Пусто — доверять языку из Jigasi; иначе форсировать (ru, en, …).",
        ),
        SettingDef(
            "jitsi_idle_stop_sec",
            "Авто-стоп при тишине, с",
            "bridge",
            "float",
            APPLIES_NEXT,
            "Завершать встречу после стольких секунд без речи (0 — выключено).",
            minimum=0,
            maximum=86400,
        ),
        SettingDef(
            "bridge_batch_window_sec",
            "Окно субтитров, с",
            "bridge",
            "float",
            APPLIES_NEXT,
            "Задержка субтитров примерно равна окну; к новым встречам.",
            minimum=0.5,
            maximum=30,
        ),
        SettingDef(
            "bridge_batch_hold_sec",
            "Удержание хвоста (Jitsi), с",
            "bridge",
            "float",
            APPLIES_NEXT,
            "К новым встречам.",
            minimum=0,
            maximum=5,
        ),
        SettingDef(
            "bridge_drain_max_sec",
            "Дренаж за раунд, с",
            "bridge",
            "float",
            APPLIES_LIVE,
            "Сколько аудио одного участника скармливается за один раунд (0 — без лимита).",
            minimum=0,
            maximum=120,
        ),
        SettingDef(
            "bridge_pending_max_sec",
            "Очередь участника, с",
            "bridge",
            "float",
            APPLIES_LIVE,
            "Переполнение — старейшие кадры дропаются (запись на диске не тронута; "
            "улучшение восстановит текст).",
            minimum=0,
            maximum=3600,
        ),
        SettingDef(
            "bridge_total_pending_max_sec",
            "Общий бюджет бэклога, с",
            "bridge",
            "float",
            APPLIES_LIVE,
            "Суммарное непереработанное аудио всех встреч; сверх — дроп старейшего "
            "(0 — без бюджета).",
            minimum=0,
            maximum=100000,
        ),
        SettingDef(
            "bridge_finalize_concurrency",
            "Одновременных финализаций",
            "bridge",
            "int",
            APPLIES_LIVE,
            "Сколько встреч доигрывают хвост одновременно при завершении "
            "(0 — без ограничения); к новым встречам.",
            minimum=0,
            maximum=16,
        ),
        # -- нагрузка и завершение ----------------------------------------------
        SettingDef(
            "drop_realtime_on_finalize",
            "Отбрасывать рилтайм при завершении",
            "load",
            "bool",
            APPLIES_LIVE,
            "Когда встреча/запись завершается, недораспознанный хвост (субтитры, "
            "live-текст) отбрасывается вместо дожимa через декодер. Запись на диске "
            "полная — улучшение распознает всё после. Для тяжёлых прогонов под нагрузкой.",
        ),
        SettingDef(
            "file_workers",
            "Параллельных задач обработки",
            "load",
            "int",
            APPLIES_LIVE,
            "Применяется сразу: новые воркеры стартуют, лишние доработают текущую "
            "задачу и завершатся. Каждый воркер держит свою копию модели (память × N).",
            minimum=1,
            maximum=8,
        ),
        SettingDef(
            "model_idle_unload_sec",
            "Выгружать модель через, с",
            "load",
            "float",
            APPLIES_LIVE,
            "Простаивающие модели освобождают VRAM/RAM (0 — только вручную).",
            minimum=0,
            maximum=86400,
        ),
        SettingDef(
            "restart_recover",
            "Поднимать задачи после рестарта",
            "load",
            "bool",
            APPLIES_RESTART,
            "Решает, что делать при СЛЕДУЮЩЕМ старте сервера: поднять очередь и "
            "прерванные задачи или только пометить их «прервано». Это правило "
            "самого запуска — применять его «на лету» нечего.",
        ),
        SettingDef(
            "log_level",
            "Уровень логов",
            "load",
            "str",
            APPLIES_LIVE,
            "DEBUG, INFO, WARNING или ERROR.",
            choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        ),
        # -- внешний ASR-сервис --------------------------------------------------
        SettingDef(
            "asr_backend",
            "Где считаются модели",
            "remote",
            "str",
            APPLIES_RESTART,
            "local — в процессе сервера; remote — во внешнем ASR-сервисе. Режим "
            "выбирает набор движков и фабрик окон при сборке сервиса — смена "
            "подменила бы движки у работающих воркеров и живых сессий, поэтому "
            "только с перезапуском.",
            choices=("local", "remote"),
        ),
        SettingDef(
            "asr_url",
            "Адрес ASR-сервиса",
            "remote",
            "str",
            APPLIES_LIVE,
            "Например http://127.0.0.1:9001 или http://asr:9001 в докере. Смена "
            "подхватывается со следующего обращения (файлы и субтитры); уже "
            "идущий вызов досчитает по старому адресу.",
        ),
        SettingDef(
            "asr_timeout_sec",
            "Тайм-аут ASR-сервиса, с",
            "remote",
            "float",
            APPLIES_LIVE,
            "Тайм-аут одного вызова транскрибации; применяется со следующего вызова.",
            minimum=30,
            maximum=86400,
        ),
        SettingDef(
            "asr_port",
            "Порт ASR-сервиса",
            "remote",
            "int",
            APPLIES_RESTART,
            "Порт, который слушает сам asr_service при своём запуске "
            "(start_asr.bat / контейнер). У работающего сервиса порт не меняется — "
            "нужен перезапуск сервиса; адрес для подключения — настройка выше.",
            minimum=1,
            maximum=65535,
        ),
        # -- LLM ----------------------------------------------------------------
        SettingDef(
            "llm_base_url",
            "Адрес LLM-сервера",
            "llm",
            "str",
            APPLIES_NEXT,
            "OpenAI-совместимый endpoint, например http://127.0.0.1:1234/v1 "
            "(LM Studio) или host.docker.internal из контейнера.",
        ),
        SettingDef(
            "llm_model",
            "Модель LLM",
            "llm",
            "str",
            APPLIES_NEXT,
            "Имя модели в LM Studio; новые анализы.",
        ),
        SettingDef(
            "llm_timeout_sec",
            "Тайм-аут LLM, с",
            "llm",
            "float",
            APPLIES_NEXT,
            "Новые анализы.",
            minimum=30,
            maximum=7200,
        ),
        SettingDef(
            "llm_max_tokens",
            "Лимит ответа LLM",
            "llm",
            "int",
            APPLIES_NEXT,
            "Новые анализы.",
            minimum=256,
            maximum=65536,
        ),
        SettingDef(
            "llm_chunk_chars",
            "Размер куска текста",
            "llm",
            "int",
            APPLIES_NEXT,
            "Насколько крупно нарезается транскрипт для LLM; новые анализы.",
            minimum=1000,
            maximum=100000,
        ),
        SettingDef(
            "llm_api_key",
            "Ключ LLM-сервера",
            "llm",
            "secret",
            APPLIES_NEXT,
            "Если сервер требует ключ; значение не показывается.",
            editable=False,
        ),
        # -- сервер и данные (только чтение) --------------------------------------
        SettingDef("data_dir", "Папка данных", "misc", "str", APPLIES_RESTART, editable=False),
        SettingDef(
            "models_dir",
            "Папка моделей",
            "misc",
            "str",
            APPLIES_RESTART,
            "Локальные веса (G:/LLMs/models в нативной установке).",
            editable=False,
        ),
        SettingDef("host", "Адрес сервера", "misc", "str", APPLIES_RESTART, editable=False),
        SettingDef("port", "Порт сервера", "misc", "int", APPLIES_RESTART, editable=False),
        SettingDef("ffmpeg", "Путь ffmpeg", "misc", "str", APPLIES_RESTART, editable=False),
        SettingDef("ffprobe", "Путь ffprobe", "misc", "str", APPLIES_RESTART, editable=False),
        SettingDef(
            "frontend_dist",
            "Сборка интерфейса",
            "misc",
            "str",
            APPLIES_RESTART,
            editable=False,
        ),
        SettingDef("hf_token", "HF-токен", "misc", "secret", APPLIES_RESTART, editable=False),
    )


_TRUE = {"true", "1", "on", "yes", "да", "вкл"}
_FALSE = {"false", "0", "off", "no", "нет", "выкл"}


def _coerce(def_: SettingDef, raw: Any) -> Any:
    """Validate one raw value against its definition; ValueError on failure."""
    if def_.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError("ожидается да/нет")
    if def_.kind in ("int", "float"):
        if isinstance(raw, bool):
            raise ValueError("ожидается число")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("ожидается число") from exc
        if def_.kind == "int":
            if value != int(value):
                raise ValueError("ожидается целое число")
            value = int(value)
        if def_.minimum is not None and value < def_.minimum:
            raise ValueError(f"минимум {def_.minimum:g}")
        if def_.maximum is not None and value > def_.maximum:
            raise ValueError(f"максимум {def_.maximum:g}")
        return value
    if def_.kind == "secret":
        raise ValueError("значение этого поля меняется только в конфигурации")
    text = str(raw).strip()
    if len(text) > 300:
        raise ValueError("слишком длинное значение")
    if def_.choices is not None and text not in def_.choices:
        raise ValueError(f"допустимо: {', '.join(def_.choices)}")
    return text


class SettingsService:
    """Live settings: registry + overrides store + appliers.

    Construction loads the persisted overrides and applies them to the
    provided :class:`Settings` before anything else reads it, so a restart
    keeps every runtime change.
    """

    def __init__(self, settings: Settings, path: Path | None = None) -> None:
        self._settings = settings
        self._path = path or (settings.data_dir / "settings_overrides.json")
        self._defs = {def_.key: def_ for def_ in _defs()}
        self._defaults = {key: getattr(settings, key) for key in self._defs}
        self._overrides: dict[str, Any] = {}
        self._appliers: dict[str, list[Callable[[Any], None]]] = {}
        self._lock = threading.Lock()
        self._load()

    # -- appliers (side effects beyond settings.<key>) -------------------------

    def add_applier(self, key: str, apply: Callable[[Any], None]) -> None:
        """Register a side-effect callback fired after ``key`` changes."""
        self._appliers.setdefault(key, []).append(apply)

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("настройки: не удалось прочитать %s (%s)", self._path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("настройки: %s повреждён — ожидался объект", self._path)
            return
        for key, value in raw.items():
            def_ = self._defs.get(key)
            if def_ is None or not def_.editable:
                continue
            try:
                coerced = _coerce(def_, value)
            except ValueError as exc:
                log.warning("настройки: %s = %r пропущено (%s)", key, value, exc)
                continue
            setattr(self._settings, key, coerced)
            self._overrides[key] = coerced
        if self._overrides:
            log.info(
                "настройки: применены сохранённые изменения (%d): %s",
                len(self._overrides),
                ", ".join(sorted(self._overrides)),
            )

    def _persist(self) -> None:
        payload = json.dumps(self._overrides, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    # -- public API ------------------------------------------------------------

    def update(self, values: dict[str, Any]) -> dict:
        """Validate and apply a batch of changes; per-key errors are reported."""
        applied: dict[str, Any] = {}
        errors: dict[str, str] = {}
        staged: list[tuple[SettingDef, Any]] = []
        for key, raw in values.items():
            def_ = self._defs.get(key)
            if def_ is None:
                errors[key] = "неизвестная настройка"
                continue
            if not def_.editable:
                errors[key] = "только для чтения"
                continue
            try:
                staged.append((def_, _coerce(def_, raw)))
            except ValueError as exc:
                errors[key] = str(exc)
        if not staged:
            return {"applied": applied, "errors": errors}
        with self._lock:
            for def_, value in staged:
                setattr(self._settings, def_.key, value)
                self._overrides[def_.key] = value
                applied[def_.key] = value
            try:
                self._persist()
            except OSError as exc:
                log.exception("настройки: не удалось сохранить %s", self._path)
                for key in applied:
                    errors[key] = f"применено, но не сохранено на диск: {exc}"
        for def_, value in staged:
            self._fire(def_, value, errors)
        return {"applied": applied, "errors": errors}

    def reset(self, key: str) -> dict:
        """Return one setting to its environment/default value."""
        def_ = self._defs.get(key)
        if def_ is None:
            return {"applied": {}, "errors": {key: "неизвестная настройка"}}
        if not def_.editable:
            return {"applied": {}, "errors": {key: "только для чтения"}}
        value = self._defaults[key]
        errors: dict[str, str] = {}
        with self._lock:
            setattr(self._settings, def_.key, value)
            self._overrides.pop(def_.key, None)
            try:
                self._persist()
            except OSError as exc:
                log.exception("настройки: не удалось сохранить %s", self._path)
                errors[def_.key] = f"применено, но не сохранено на диск: {exc}"
        self._fire(def_, value, errors)
        return {"applied": {def_.key: value}, "errors": errors}

    def _fire(self, def_: SettingDef, value: Any, errors: dict[str, str]) -> None:
        for apply in self._appliers.get(def_.key, []):
            try:
                apply(value)
            except Exception as exc:  # noqa: BLE001 — an applier must not undo the change
                log.exception("настройки: апплаер %s упал", def_.key)
                errors[def_.key] = f"применено к настройкам, но публикация не удалась: {exc}"

    def snapshot(self) -> dict:
        """Grouped view for the UI (values, sources, hints)."""
        items_by_group: dict[str, list[dict]] = {gid: [] for gid in _GROUPS}
        dynamic_choices = self._engine_choices()
        for def_ in _defs():
            choices = def_.choices
            if def_.key in ("engine", "reprocess_engine"):
                choices = dynamic_choices.get(def_.key)
            items_by_group[def_.group].append(self._item(def_, choices))
        return {
            "groups": [
                {
                    "id": gid,
                    "title": title,
                    "description": description,
                    "items": items_by_group[gid],
                }
                for gid, title, description in GROUPS
            ]
        }

    def _engine_choices(self) -> dict[str, tuple[str, ...]]:
        try:
            from .engines import available_asr

            names = tuple(available_asr())
        except Exception:  # noqa: BLE001 — the registry must render regardless
            log.debug("настройки: список движков недоступен", exc_info=True)
            return {}
        return {"engine": names, "reprocess_engine": ("", *names)}

    def _item(self, def_: SettingDef, choices: tuple[str, ...] | None) -> dict:
        value = getattr(self._settings, def_.key)
        if isinstance(value, Path):
            value = str(value)
        default = self._defaults[def_.key]
        if isinstance(default, Path):
            default = str(default)
        if def_.kind == "secret":
            value = None
            default = None
        if def_.key in self._overrides:
            source = "runtime"
        elif os.environ.get(f"MEETSCRIBE_{def_.key.upper()}") is not None:
            source = "env"
        else:
            source = "default"
        return {
            "key": def_.key,
            "label": def_.label,
            "kind": def_.kind,
            "hint": def_.hint,
            "applies": def_.applies,
            "editable": def_.editable,
            "minimum": def_.minimum,
            "maximum": def_.maximum,
            "choices": list(choices) if choices else None,
            "value": value,
            "default_value": default,
            "source": source,
            "is_set": bool(getattr(self._settings, def_.key)) if def_.kind == "secret" else None,
        }
