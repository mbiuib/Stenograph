"""Проверка графиков loadtest: серии ASR-сервиса реально на панелях.

Рендерит трассу смоука и её копию «как в стеке» (у сервера нет своей
nvidia-smi — только цифры сервиса), печатает заголовки панелей и подписи
линий. Запускать системным Python 3.12 (там matplotlib).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("charts", ROOT / "loadtest" / "charts.py")
assert SPEC and SPEC.loader
charts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(charts)

import matplotlib.pyplot as plt  # noqa: E402

captured: list = []
_orig_savefig = plt.Figure.savefig


def _spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    captured.append(self)
    return _orig_savefig(self, *args, **kwargs)


plt.Figure.savefig = _spy  # type: ignore[method-assign]

def _newest_asr_trace() -> Path:
    """Свежайшая трасса, в которой есть цифры ASR-сервиса."""
    results = ROOT / "loadtest" / "results"
    for run_dir in sorted(results.iterdir(), reverse=True):
        samples = run_dir / "samples.jsonl"
        if not samples.exists():
            continue
        if "asr_gpu_pct" in samples.read_text(encoding="utf-8"):
            return run_dir
    raise SystemExit("нет трассы с asr_*-полями — сначала прогони нагрузочный тест")


source = Path(sys.argv[1]) if len(sys.argv) > 1 else _newest_asr_trace()
assert source.exists(), f"нет трассы: {source}"


def report(name: str, run_dir: Path) -> None:
    """Печатает панели и подписи линий отрендеренного таймлайна."""
    captured.clear()
    paths = charts.render(run_dir)
    print(f"\n=== {name}: {[p.name for p in paths]}")
    fig = captured[0]
    for ax in fig.axes:
        title = ax.get_title() or "(вторая ось панели)"
        print("  панель:", title[:120])
        for line in ax.get_lines():
            label = line.get_label()
            if label and not label.startswith("_"):
                print(f"    линия: {label!r}, точек: {len(line.get_xdata())}")


report("смоук (сервер видит GPU сам)", source)

# Копия «как стек»: у сервера GPU-полей нет, всё живёт в цифрах сервиса.
stack_like = ROOT / "loadtest" / "results" / "_tmp_stack_like"
stack_like.mkdir(exist_ok=True)
if (source / "server.log").exists():
    shutil.copy(source / "server.log", stack_like / "server.log")
rows = []
for line in (source / "samples.jsonl").open(encoding="utf-8"):
    if not line.strip():
        continue
    row = json.loads(line)
    for key in ("gpu_pct", "vram_used_mb", "vram_free_mb", "vram_total_mb"):
        row.pop(key, None)
    rows.append(row)
with (stack_like / "samples.jsonl").open("w", encoding="utf-8") as out:
    for row in rows:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")

report("как стек (GPU только у сервиса)", stack_like)
shutil.rmtree(stack_like, ignore_errors=True)
print("\nOK")
