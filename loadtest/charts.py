"""Charts for a load-test run: <run_dir>/samples.jsonl + server.log -> PNGs.

Run by run_loadtest.py automatically (via an interpreter that has matplotlib)
or manually:
  <python с matplotlib> loadtest/charts.py loadtest/results/<папка запуска>
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def render(run_dir: Path) -> list[Path]:
    """Build the two PNG dashboards next to the trace; returns their paths."""
    rows = [
        json.loads(line)
        for line in (run_dir / "samples.jsonl").open(encoding="utf-8")
        if line.strip()
    ]
    rows = [r for r in rows if r.get("gpu_pct") is not None or r.get("health_ok")]
    if not rows:
        raise SystemExit("samples.jsonl пуст — нечего рисовать")

    def clean(xs_in: list[float], vals_in: list) -> tuple[list[float], list[float]]:
        """Пары (x, y) без пропусков: matplotlib 3.11 не рисует линии с NaN-разрывами."""
        pairs = [(x, v) for x, v in zip(xs_in, vals_in, strict=False) if v is not None]
        return [p[0] for p in pairs], [p[1] for p in pairs]

    t0 = datetime.strptime(rows[0]["t"], "%H:%M:%S")

    def rel(row: dict) -> float:
        return (datetime.strptime(row["t"], "%H:%M:%S") - t0).total_seconds() / 60.0

    xs = [rel(r) for r in rows]
    ks = [r.get("k") or 0 for r in rows]
    gpu = [r.get("gpu_pct") for r in rows]
    rtf = [None if r.get("rt_factor") is None else r["rt_factor"] * 100 for r in rows]
    vram = [None if r.get("vram_used_mb") is None else r["vram_used_mb"] / 1024 for r in rows]
    d_avg = [r.get("delay_avg") for r in rows]
    d_max = [r.get("delay_max") for r in rows]
    rss = [r.get("rss_mb") for r in rows]
    cpu = [r.get("cpu_pct") for r in rows]
    loopmax = [r.get("loop_lag_max_ms") or 0 for r in rows]
    gx, gv = clean(xs, gpu)
    vx, vv = clean(xs, vram)
    rx, rv = clean(xs, rtf)
    dx, dv = clean(xs, d_max)
    ax_, av_ = clean(xs, d_avg)
    sx, sv = clean(xs, rss)
    cx, cv = clean(xs, cpu)

    adds: list[tuple[float, int]] = []
    seen = 0
    for x, k in zip(xs, ks, strict=False):
        if k > seen:
            seen = k
            adds.append((x, k))

    pat = re.compile(
        r"батч-раунд: (\d+) окон за ([\d.]+) с "
        r"\(сбор ([\d.]+), инференс ([\d.]+), применение ([\d.]+)\)"
    )
    log_path = run_dir / "server.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    rounds = [
        (int(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)))
        for m in (pat.search(line) for line in log_text.splitlines())
        if m
    ]

    maxk = max(ks) if ks else 0
    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9))
    fig.suptitle(
        f"Стенограф — нагрузочный тест Jitsi: рампа до {maxk} встреч по 10 говорящих", fontsize=13
    )

    ax = axes[0][0]
    if rounds:
        seq = range(1, len(rounds) + 1)
        ax.plot(seq, [r[1] for r in rounds], "o-", color="#d64545", label="раунд целиком")
        ax.plot(seq, [r[2] for r in rounds], "s--", color="#e0a030", label="сбор (дренаж кадров)")
        ax.plot(seq, [r[3] for r in rounds], "^-", color="#3d7dd6", label="инференс")
        ax.legend(fontsize=9, framealpha=1.0)
    else:
        ax.text(
            0.5,
            0.5,
            "Раундов с предупреждением «не успевает»\nне было — декодер держал темп",
            ha="center",
            va="center",
            fontsize=12,
            color="#2e9e5b",
            transform=ax.transAxes,
        )
    ax.set_title("Раунды батч-декодера, с предупреждением «не успевает» (сбор vs инференс)")
    ax.set_xlabel("номер раунда")
    ax.set_ylabel("секунды")
    ax.grid(alpha=0.3)

    ax = axes[0][1]
    ax.plot(gx, gv, color="#3d7dd6", label="GPU %")
    ax.set_ylabel("GPU %", color="#3d7dd6")
    ax.set_ylim(0, 105)
    ax2 = ax.twinx()
    ax2.plot(vx, vv, color="#7a4fd6")
    ax2.set_ylabel("видеопамять, ГБ", color="#7a4fd6")
    vram_peak = max([v for v in vram if v is not None] or [0])
    ax2.set_ylim(0, max(vram_peak * 1.15, 1))
    for x, k in adds:
        ax.axvline(x, color="#999", ls="--", lw=0.8)
        ax.text(x, 103, f"K={k}", fontsize=7, ha="center", color="#666")
    ax.set_title(f"GPU: загрузка и видеопамять (пик {vram_peak:.1f} ГБ)")
    ax.set_xlabel("минуты от старта")
    ax.grid(alpha=0.3)

    ax = axes[1][0]
    dm = ax.plot(dx, dv, color="#d64545", label="макс задержка текста")[0]
    da = ax.plot(ax_, av_, color="#e0a030", label="средняя")[0]
    ax.set_title("Отставание текста (с) и успевание декодера (100% = не отстаём)")
    ax.set_xlabel("минуты от старта")
    ax.set_ylabel("с")
    ax3 = ax.twinx()
    ok = ax3.plot(rx, rv, color="#2e9e5b", label="успеваем, % от реального времени")[0]
    ax3.axhline(100, color="#2e9e5b", ls=":", lw=1)
    ax3.set_ylabel("успеваем, %", color="#2e9e5b")
    ax3.set_ylim(0, 110)
    ax.legend(handles=[dm, da, ok], fontsize=9, framealpha=1.0)
    ax.grid(alpha=0.3)
    for x, _ in adds:
        ax.axvline(x, color="#999", ls="--", lw=0.8)

    ax = axes[1][1]
    ax.plot(sx, sv, color="#7a4fd6", label="RSS сервера, МБ")
    ax.set_ylabel("RSS, МБ", color="#7a4fd6")
    ax.set_ylim(0, max([v for v in rss if v] or [1000]) * 1.15)
    ax2 = ax.twinx()
    ax2.plot(cx, cv, color="#c4552e")
    ax2.set_ylabel("CPU, % (одно ядро = 100)", color="#c4552e")
    ax2.set_ylim(0, 220)
    peak = max(loopmax)
    ax.set_title(f"Ресурсы: RAM/CPU (пик стопора event loop: {peak} мс)")
    ax.set_xlabel("минуты от старта")
    ax.grid(alpha=0.3)
    for x, _ in adds:
        ax.axvline(x, color="#999", ls="--", lw=0.8)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out1 = run_dir / "chart_timeline.png"
    fig.savefig(out1, dpi=140)
    plt.close(fig)

    per_k: dict[int, dict[str, list[float]]] = {}
    for r in rows:
        k = r.get("k") or 0
        if not k:
            continue
        slot = per_k.setdefault(k, {"rtf": [], "delay": []})
        if r.get("rt_factor") is not None:
            slot["rtf"].append(r["rt_factor"])
        if r.get("delay_avg") is not None:
            slot["delay"].append(r["delay_avg"])

    def med(vals: list[float]) -> float | None:
        vals = sorted(vals)
        return vals[len(vals) // 2] if vals else None

    ksorted = sorted(per_k)
    fig2, axes2 = plt.subplots(1, 2, figsize=(12.5, 4.6))
    fig2.suptitle("Устойчивость по числу встреч (10 говорящих на встречу)", fontsize=13)
    ax = axes2[0]
    ax.plot(ksorted, [med(per_k[k]["rtf"]) for k in ksorted], "o-", color="#2e9e5b")
    ax.axhline(1.0, color="#2e9e5b", ls=":", lw=1)
    ax.set_title("Успеваем, x от реального времени (медиана на шаге)")
    ax.set_xlabel("встреч одновременно")
    ax.set_ylabel("x")
    ax.set_ylim(0, 1.08)
    ax.grid(alpha=0.3)
    ax = axes2[1]
    ax.plot(ksorted, [max(per_k[k]["delay"]) for k in ksorted], "o-", color="#d64545")
    ax.set_title("Максимальная задержка текста к концу шага")
    ax.set_xlabel("встреч одновременно")
    ax.set_ylabel("с")
    ax.grid(alpha=0.3)
    fig2.tight_layout(rect=(0, 0, 1, 0.94))
    out2 = run_dir / "chart_scale.png"
    fig2.savefig(out2, dpi=140)
    plt.close(fig2)
    return [out1, out2]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("использование: charts.py <папка запуска с samples.jsonl>")
    for out in render(Path(sys.argv[1])):
        print(f"график: {out}")
