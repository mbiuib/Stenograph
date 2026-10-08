"""Russian prompt templates for meeting analysis (protocol / summary).

Prompts are tuned for small local models: an explicit output structure, a hard
«only facts from the transcript» guardrail, and no room for free-form drift.
Two analysis kinds share the same map-reduce shape:

* map — one fragment of a long transcript -> structured notes;
* reduce — all notes -> the final document (skipped for short transcripts,
  which go straight through a single «direct» prompt).
"""

from __future__ import annotations

PROTOCOL = "protocol"
SUMMARY = "summary"

#: Analysis kinds accepted by the API/CLI.
ANALYSIS_TYPES = (PROTOCOL, SUMMARY)

_GUARDRAILS = (
    "Опирайся только на факты из текста: не выдумывай имена, решения, сроки "
    "и договорённости, которых там нет. Если чего-то не упоминалось — просто "
    "не включай это. Пиши на русском языке, в ответе — только markdown, "
    "без вводных фраз вроде «конечно» или «вот результат». Не используй "
    "LaTeX и символ $: стрелки пиши как «→», формулы словами."
)

_SYSTEM_PROTOCOL = f"Ты — опытный секретарь, который составляет протоколы совещаний. {_GUARDRAILS}"

_SYSTEM_SUMMARY = (
    f"Ты — аналитик, который делает краткие выжимки из расшифровок совещаний. {_GUARDRAILS}"
)

_PROTOCOL_STRUCTURE = (
    "Структура результата:\n"
    "## Тема встречи\n"
    "(одно-два предложения)\n"
    "## Участники\n"
    "(если известны из текста; иначе раздел опусти)\n"
    "## Обсуждение\n"
    "(по темам: подзаголовок темы и краткие пункты)\n"
    "## Решения\n"
    "(принятые решения и договорённости; если нет — напиши «Решения не зафиксированы»)\n"
    "## Задачи\n"
    "(кто — что — срок, если упоминался; если задач нет — «Задачи не зафиксированы»)\n"
    "## Открытые вопросы\n"
    "(что осталось нерешённым; можно опустить, если таких нет)"
)

_SUMMARY_STRUCTURE = (
    "Структура результата:\n"
    "## Кратко\n"
    "(связный абзац из 3–5 предложений)\n"
    "## Главное\n"
    "(5–10 коротких пунктов с самыми важными фактами и выводами)"
)


def _system(analysis_type: str) -> str:
    return _SYSTEM_PROTOCOL if analysis_type == PROTOCOL else _SYSTEM_SUMMARY


def _structure(analysis_type: str) -> str:
    return _PROTOCOL_STRUCTURE if analysis_type == PROTOCOL else _SUMMARY_STRUCTURE


def chunk_prompt(analysis_type: str, chunk: str, index: int, total: int) -> tuple[str, str]:
    """Map phase: extract notes for the final document from one fragment."""
    if analysis_type == PROTOCOL:
        task = (
            "Извлеки из фрагмента материал для протокола:\n"
            "- обсуждавшиеся темы и ключевые тезисы (по пунктам);\n"
            "- решения и договорённости (если есть);\n"
            "- задачи и поручения: кто, что, срок (если упоминались);\n"
            "- открытые вопросы."
        )
    else:
        task = (
            "Сожми фрагмент до 3–5 коротких пунктов: о чём говорили и к чему пришли. "
            "Пропусти болтовню, приветствия и повторы."
        )
    user = (
        f"Ниже фрагмент {index} из {total} расшифровки совещания "
        "(говорящие указаны в начале строк, если известны).\n\n"
        f"{task}\n\nФрагмент:\n---\n{chunk}\n---"
    )
    return _system(analysis_type), user


def reduce_prompt(analysis_type: str, notes: str) -> tuple[str, str]:
    """Reduce phase: build the final document from fragment notes."""
    kind = "черновые заметки по последовательным фрагментам одной встречи"
    if analysis_type == PROTOCOL:
        task = (
            "Собери из заметок единый протокол встречи. Убери повторы между "
            "фрагментами и упорядочи материал по смыслу.\n\n"
            f"{_PROTOCOL_STRUCTURE}"
        )
    else:
        task = (
            "Собери из заметок краткое резюме встречи. Убери повторы между "
            "фрагментами.\n\n"
            f"{_SUMMARY_STRUCTURE}"
        )
    user = f"Ниже {kind}:\n\n{notes}\n\n{task}"
    return _system(analysis_type), user


def direct_prompt(analysis_type: str, transcript: str) -> tuple[str, str]:
    """Short-transcript path: one call produces the final document directly."""
    if analysis_type == PROTOCOL:
        task = f"Составь протокол этой встречи.\n\n{_PROTOCOL_STRUCTURE}"
    else:
        task = f"Сделай краткое резюме этой встречи.\n\n{_SUMMARY_STRUCTURE}"
    user = f"Ниже полная расшифровка совещания.\n\n{task}\n\nРасшифровка:\n---\n{transcript}\n---"
    return _system(analysis_type), user
