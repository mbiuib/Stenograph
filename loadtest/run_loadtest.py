"""Automatic Jitsi load test: N meetings x P participants x S speakers.

Starts a scratch server (unless --base is given), ramps synthetic meetings
against the real bridge protocol (Jigasi streaming-whisper frames), rotates
active speakers between different speech samples, samples GPU/VRAM/RSS/CPU,
event-loop lag and per-meeting text delay, then finalizes, stops the server
and builds the charts. Everything lands in loadtest/results/<timestamp>/.

Run with the project venv python:
  .venv/Scripts/python.exe loadtest/run_loadtest.py \
      --meetings 12 --participants 70 --speakers 10
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import websockets

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = HERE / "results"
SAMPLES_DIR = HERE / "samples"
WARN_PATTERNS = ("декодер не успевает", "переполнена", "не удалась", "Traceback", "ERROR")


def ts() -> str:
    """Wall-clock stamp for console lines."""
    return time.strftime("%H:%M:%S")


def frame_header(participant_id: str) -> bytes:
    """60-byte ``participantId|language`` header of the bridge protocol."""
    return f"{participant_id}|ru-RU".encode().ljust(60, b"\x00")


def load_samples() -> list[np.ndarray]:
    """Read every 16 kHz mono WAV from loadtest/samples/ as int16 arrays."""
    samples = []
    for path in sorted(SAMPLES_DIR.glob("*.wav")):
        with wave.open(str(path)) as handle:
            if handle.getframerate() != 16000 or handle.getnchannels() != 1:
                print(f"пропускаю {path.name}: нужен 16 кГц моно")
                continue
            samples.append(np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2"))
    return samples


# --------------------------------------------------------------------------- server


def start_server(args, run_dir: Path) -> subprocess.Popen | None:
    """Launch the scratch server on --port; None when --base is external."""
    if args.base:
        return None
    env = os.environ.copy()
    env.update(
        {
            "MEETSCRIBE_DATA_DIR": str(run_dir / "data"),
            "MEETSCRIBE_REALTIME_TRANSCRIBE": "true",
            "MEETSCRIBE_LIVE_AUTO_REPROCESS": "false",
            "MEETSCRIBE_ENGINE": "whisper",
            "MEETSCRIBE_MODEL_IDLE_UNLOAD_SEC": "0",
            "MEETSCRIBE_JITSI_IDLE_STOP_SEC": "0",
            "MEETSCRIBE_FILE_WORKERS": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    log = (run_dir / "server.log").open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "stenograph.api.app:create_app",
            "--factory",
            "--port",
            str(args.port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    print(f"[{ts()}] сервер стартует на :{args.port} (pid {proc.pid})", flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit("сервер упал при старте — смотрите server.log в папке запуска")
        try:
            with httpx.Client(timeout=2) as client:
                if client.get(f"http://127.0.0.1:{args.port}/api/health").status_code == 200:
                    print(f"[{ts()}] сервер готов", flush=True)
                    return proc
        except Exception:  # noqa: BLE001 — ещё грузится
            pass
        time.sleep(1)
    raise SystemExit("сервер не поднялся за 90 с")


def stop_server(proc: subprocess.Popen | None, port: int) -> None:
    """Terminate the scratch server and free the port for sure (never raises)."""
    try:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
        if os.name != "nt":
            return
        # netstat на русской Windows печатает не в UTF-8 — декодируем мягко.
        result = subprocess.run(["netstat", "-ano"], capture_output=True)
        listing = (result.stdout or b"").decode("utf-8", errors="ignore")
        for line in listing.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        print(f"[{ts()}] сервер остановлен, порт {port} свободен", flush=True)
    except Exception as exc:  # noqa: BLE001 — остановка не должна ронять прогон
        print(f"[{ts()}] предупреждение при остановке сервера: {exc!r}", flush=True)


def run_charts(run_dir: Path) -> bool:
    """Render charts via the first python that has matplotlib."""
    candidates = [sys.executable, shutil.which("python3") or "", shutil.which("python") or ""]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Programs" / "Python" / "Python312" / "python.exe"))
    for python in candidates:
        if not python or not Path(python).exists():
            continue
        probe = subprocess.run([python, "-c", "import matplotlib"], capture_output=True)
        if probe.returncode != 0:
            continue
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [python, str(HERE / "charts.py"), str(run_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
        print(result.stdout.strip() or result.stderr.strip(), flush=True)
        return result.returncode == 0
    print("! matplotlib не найден — графики не построены (pip install matplotlib)", flush=True)
    return False


# --------------------------------------------------------------------------- traffic


async def drain(ws) -> None:
    """Consume server events so the reader queue never backs up."""
    try:
        async for _ in ws:
            pass
    except Exception:  # noqa: BLE001 — the probe must survive any close
        pass


async def feed_speaker(
    ws, hdr: bytes, samples: np.ndarray, offset: int, stop: asyncio.Event
) -> None:
    """Stream one participant's speech at 1x realtime (0.2 s frames)."""
    step = 3200
    pos = offset
    sent = 0
    started = time.monotonic()
    while not stop.is_set():
        piece = samples[pos : pos + step]
        if piece.size < step:
            piece = np.concatenate((piece, samples[: step - piece.size]))
            pos = 0
        else:
            pos += step
        await ws.send(hdr + piece.tobytes())
        sent += step
        delay = sent / 16000.0 - (time.monotonic() - started)
        if delay > 0:
            await asyncio.sleep(delay)


async def run_meeting(
    idx: int, args, samples_pool: list[np.ndarray], stop: asyncio.Event, state: dict
) -> None:
    """One synthetic meeting: register everyone, keep S speakers talking.

    Active speakers rotate every --rotate seconds to a fresh random subset —
    like a real room where the microphone moves between people.
    """
    mid = f"load-m{idx:02d}"
    rng = random.Random(9000 + idx)
    base = f"http://127.0.0.1:{args.port}"
    ws_url = f"ws://127.0.0.1:{args.port}/ws/{mid}"
    try:
        ws = await websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"[{ts()}] встреча {mid}: НЕ подключилась: {exc!r} (сервер {base})", flush=True)
        return
    reader = asyncio.create_task(drain(ws))
    silence = np.zeros(3200, dtype="<i2").tobytes()
    pids = [f"u{idx:02d}p{p:02d}" for p in range(1, args.participants + 1)]
    for pid in pids:
        await ws.send(frame_header(pid) + silence)

    active: dict[str, asyncio.Task] = {}
    prev: set[str] = set()
    speaker_count = min(args.speakers, len(pids))

    async def set_active(pids_now: list[str]) -> None:
        wanted = set(pids_now)
        for pid in list(active):
            if pid not in wanted:
                active.pop(pid).cancel()
        for pid in pids_now:
            if pid in active:
                continue
            clip = rng.choice(samples_pool)
            offset = rng.randrange(0, max(1, clip.size - 3200))
            active[pid] = asyncio.create_task(
                feed_speaker(ws, frame_header(pid), clip, offset, stop)
            )

    first = rng.sample(pids, speaker_count)
    await set_active(first)
    prev = set(first)
    state["meetings_started"] = idx
    state["speakers_started"] = idx * speaker_count
    rotate_note = f", ротация каждые {args.rotate:.0f} с" if args.rotate > 0 else ""
    print(
        f"[{ts()}] встреча {mid} пошла: {len(pids)} участников, {len(first)} говорят{rotate_note}",
        flush=True,
    )
    try:
        while not stop.is_set() and args.rotate > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), args.rotate)
            if stop.is_set():
                break
            fresh = rng.sample(pids, speaker_count)
            guard = 0
            while set(fresh) == prev and len(pids) > speaker_count and guard < 5:
                fresh = rng.sample(pids, speaker_count)
                guard += 1
            await set_active(fresh)
            prev = set(fresh)
            state["rotations"] += 1
        await stop.wait()
    finally:
        for task in list(active.values()):
            task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
        reader.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), 10)


# --------------------------------------------------------------------------- sampling


async def sample_loop(args, state: dict, client: httpx.AsyncClient, run_dir: Path) -> None:
    """Record resources + backlog every --sample seconds into samples.jsonl."""
    base = f"http://127.0.0.1:{args.port}"
    out = (run_dir / "samples.jsonl").open("a", encoding="utf-8")
    prev_committed = prev_wall = None
    health_fail = 0
    log_off = 0
    warns = 0
    n = 0
    while not state["finished"]:
        n += 1
        row: dict = {
            "t": ts(),
            "wall": round(time.time(), 2),
            "n": n,
            "k": state["meetings_started"],
        }
        try:
            health = (await client.get(f"{base}/api/health", timeout=10)).json()
            health_fail = 0
            row["health_ok"] = True
            row["loop_lag_ms"] = health.get("loop_lag_ms")
            row["loop_lag_max_ms"] = health.get("loop_lag_max_ms")
        except Exception as exc:  # noqa: BLE001
            health_fail += 1
            row["health_ok"] = False
            row["health_err"] = repr(exc)[:140]
        dur_by_job: dict[str, float] = {}
        try:
            metrics = (await client.get(f"{base}/api/metrics", timeout=30)).json()
            gpu = metrics["system"]["gpu"]
            proc = metrics["system"]["self_process"]
            row.update(
                gpu_pct=gpu["utilization_pct"],
                vram_used_mb=gpu["vram_used_mb"],
                vram_free_mb=gpu["vram_free_mb"],
                rss_mb=proc["rss_mb"],
                cpu_pct=proc["cpu_pct"],
                models=[
                    {
                        "model": m.get("model"),
                        "vram_mb": m.get("vram_mb"),
                        "busy": m.get("busy"),
                    }
                    for m in metrics.get("models") or []
                ],
            )
        except Exception as exc:  # noqa: BLE001
            row["metrics_err"] = repr(exc)[:140]
        try:
            jitsi = (await client.get(f"{base}/api/jitsi/status", timeout=30)).json()
            meetings = jitsi.get("meetings") or []
            row["meetings"] = len(meetings)
            row["participants"] = sum(len(m.get("participants") or []) for m in meetings)
            row["speakers_active"] = sum(
                1
                for m in meetings
                for p in (m.get("participants") or [])
                if (p.get("last_frame_sec") or 99) < 3
            )
            dur_by_job = {m["job_id"]: m.get("duration_sec") or 0.0 for m in meetings}
        except Exception as exc:  # noqa: BLE001
            row["jitsi_err"] = repr(exc)[:140]
        if n % 2 == 0:
            try:
                jobs = (await client.get(f"{base}/api/jobs?limit=300", timeout=45)).json()
                committed = 0.0
                delays: dict[str, float] = {}
                for job in jobs:
                    if job.get("kind") != "jitsi" or "load-m" not in (job.get("source_name") or ""):
                        continue
                    ends = [
                        s["end"] for s in (job.get("segments") or []) if s.get("end") is not None
                    ]
                    last_end = max(ends) if ends else 0.0
                    committed += last_end
                    dur = dur_by_job.get(job["id"])
                    if dur is not None:
                        delays[job["source_name"].split("—")[-1].strip()] = round(dur - last_end, 1)
                row["committed_total"] = round(committed, 1)
                if delays:
                    row["delays_map"] = delays
                    row["delay_avg"] = round(sum(delays.values()) / len(delays), 1)
                    row["delay_max"] = max(delays.values())
                if prev_committed is not None and prev_wall and time.time() > prev_wall:
                    rate = (committed - prev_committed) / (time.time() - prev_wall)
                    row["committed_rate"] = round(rate, 2)
                    total = state["meetings_started"] or 1
                    row["rt_factor"] = round(rate / total, 3)
                prev_committed, prev_wall = committed, time.time()
            except Exception as exc:  # noqa: BLE001
                row["jobs_err"] = repr(exc)[:140]
        try:
            with (run_dir / "server.log").open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(log_off)
                chunk = fh.read()
                log_off = fh.tell()
            for pattern in WARN_PATTERNS:
                warns += chunk.count(pattern)
            row["warns_total"] = warns
        except FileNotFoundError:
            pass
        abort = None
        if health_fail >= 3:
            abort = "сервер не отвечает на /api/health (3 промаха подряд)"
        elif row.get("vram_free_mb") is not None and row["vram_free_mb"] <= 800:
            abort = f"VRAM: свободно всего {row['vram_free_mb']} МБ"
        elif row.get("loop_lag_max_ms") and row["loop_lag_max_ms"] >= 5000:
            abort = f"event loop вставал на {row['loop_lag_max_ms']} мс"
        elif row.get("rss_mb") and row["rss_mb"] >= 8000:
            abort = f"RSS сервера {row['rss_mb']} МБ"
        if abort and not state.get("abort"):
            state["abort"] = f"{abort} (K={state['meetings_started']})"
            print(f"[{ts()}] !!! СТЕНА: {state['abort']}", flush=True)
        parts = [
            f"[{row['t']}] K={state['meetings_started']:02d} "
            f"говорят={row.get('speakers_active', '?')}",
            f"GPU {row.get('gpu_pct', '?')}% "
            f"VRAM {round((row.get('vram_used_mb') or 0) / 1024, 1)}ГБ",
            f"loop {row.get('loop_lag_ms', '?')}/{row.get('loop_lag_max_ms', '?')}мс",
            f"RSS {round((row.get('rss_mb') or 0) / 1024, 2)}ГБ CPU {row.get('cpu_pct', '?')}%",
        ]
        if row.get("rt_factor") is not None:
            parts.append(f"коммит {row.get('committed_total')}с ({row['rt_factor']}x)")
        if row.get("delay_avg") is not None:
            parts.append(f"задержка ср {row['delay_avg']} макс {row.get('delay_max')} с")
        parts.append(f"warn {warns} ротаций {state['rotations']}")
        print(" | ".join(parts), flush=True)
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()
        await asyncio.sleep(args.sample)


# --------------------------------------------------------------------------- main


async def main() -> None:
    """Parse arguments, run the ramp, finalise and render the charts."""
    parser = argparse.ArgumentParser(description="Автоматический нагрузочный тест Jitsi-моста")
    parser.add_argument(
        "--meetings", type=int, default=12, help="максимум встреч (рампа по --step)"
    )
    parser.add_argument("--participants", type=int, default=70, help="всего участников на встречу")
    parser.add_argument(
        "--speakers", type=int, default=10, help="одновременно говорящих на встречу"
    )
    parser.add_argument("--step", type=float, default=45, help="секунд на шаг рампы")
    parser.add_argument("--hold", type=float, default=150, help="секунд держать максимум")
    parser.add_argument("--sample", type=float, default=5, help="период снятия статистики")
    parser.add_argument(
        "--rotate",
        type=float,
        default=60,
        help="секунд между сменами состава говорящих (0 = не менять)",
    )
    parser.add_argument("--port", type=int, default=8010, help="порт скретч-сервера")
    parser.add_argument(
        "--base", action="store_true", help="не поднимать сервер (использовать :порт как есть)"
    )
    parser.add_argument("--label", default="", help="суффикс папки результатов")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="сохранить записанное аудио (data/ в папке запуска)",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=2700, help="жёсткий предохранитель всего прогона"
    )
    args = parser.parse_args()

    samples_pool = load_samples()
    if not samples_pool:
        raise SystemExit(
            "нет сэмплов речи — сначала: .venv/Scripts/python.exe loadtest/prepare_samples.py"
        )
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    run_dir = RESULTS / (f"{stamp}_{args.label}" if args.label else stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"результаты: {run_dir}", flush=True)
    print(f"сэмплов речи: {len(samples_pool)}", flush=True)

    proc = start_server(args, run_dir)
    stop = asyncio.Event()
    state: dict = {
        "meetings_started": 0,
        "speakers_started": 0,
        "rotations": 0,
        "abort": None,
        "finished": False,
    }
    started_wall = time.time()
    started_mono = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            sampler = asyncio.create_task(sample_loop(args, state, client, run_dir))
            tasks = []
            for k in range(1, args.meetings + 1):
                if state["abort"] or time.monotonic() - started_mono > args.max_seconds:
                    break
                tasks.append(asyncio.create_task(run_meeting(k, args, samples_pool, stop, state)))
                waited = 0.0
                while waited < args.step and not state["abort"]:
                    await asyncio.sleep(1)
                    waited += 1
            if not state["abort"]:
                print(
                    f"[{ts()}] рампа пройдена: встреч {state['meetings_started']}, "
                    f"держим {args.hold:.0f} с",
                    flush=True,
                )
                waited = 0.0
                while waited < args.hold and not state["abort"]:
                    await asyncio.sleep(1)
                    waited += 1
            print(f"[{ts()}] завершаю: закрываю {len(tasks)} встреч", flush=True)
            state["finished"] = True
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(30)  # сессиям нужно время финализироваться
            sampler.cancel()
            try:
                jobs = (
                    await client.get(f"http://127.0.0.1:{args.port}/api/jobs?limit=300", timeout=60)
                ).json()
            except Exception:  # noqa: BLE001
                jobs = []
    finally:
        stop_server(proc, args.port)
        if not args.keep_data:
            shutil.rmtree(run_dir / "data", ignore_errors=True)

    mine = [
        j for j in jobs if j.get("kind") == "jitsi" and "load-m" in (j.get("source_name") or "")
    ]
    summary = {
        "args": vars(args),
        "samples": [p.name for p in sorted(SAMPLES_DIR.glob("*.wav"))],
        "elapsed_sec": round(time.time() - started_wall, 1),
        "abort": state["abort"],
        "meetings_started": state["meetings_started"],
        "speakers_started": state["speakers_started"],
        "rotations": state["rotations"],
        "jobs": [
            {
                "name": j["source_name"],
                "status": j["status"],
                "segments": len(j.get("segments") or []),
                "message": (j.get("message") or "")[:120],
            }
            for j in sorted(mine, key=lambda x: x["source_name"])
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n===== ИТОГ =====", flush=True)
    print(
        f"встреч запущено: {summary['meetings_started']}, говорят суммарно: "
        f"{summary['speakers_started']}, ротаций состава: {summary['rotations']}",
        flush=True,
    )
    print(f"стена: {summary['abort'] or 'не достигнута (рампа пройдена целиком)'}", flush=True)
    print(f"время: {summary['elapsed_sec']} с", flush=True)
    for row in summary["jobs"]:
        print(f"  {row['name'][:52]:52} {row['status']:9} сегментов {row['segments']}", flush=True)
    run_charts(run_dir)
    print(f"папка результатов: {run_dir}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
