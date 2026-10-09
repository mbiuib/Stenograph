"""Сценарий с мониторингом: live → авто-улучшение (MOSS) → файл (whisper).

Поднимает синтетическую live-сессию на ``/ws/live``, кормит её заранее
записанной речью в реальном времени, дожидается авто-репроцесса после стопа,
затем загружает тот же файл как обычную задачу через whisper. После каждого
этапа снимается снапшот ``/api/metrics`` — видно, сколько VRAM и RAM реально
занимает каждая модель и как ведёт себя сервер под нагрузкой.

Запуск (на поднятом сервере):

    .venv/Scripts/python.exe scripts/bench_monitor.py \
        --base http://127.0.0.1:8000 \
        --wav data/live/<id>/system.wav \
        --seconds 45

``--wav`` — любой 16 кГц моно WAV (например системная дорожка любой live-записи).
Итоговые снапшоты сохраняются в ``bench_snapshots.json`` рядом с CWD.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import websockets


async def feed_live(ws_url: str, samples: np.ndarray, sr: int, seconds: float) -> str:
    """Отправить ``seconds`` секунд аудио в реальном времени; вернуть job_id."""
    limit = int(seconds * sr)
    async with websockets.connect(ws_url, max_size=None) as ws:
        start = {"type": "start", "tracks": ["mic"], "language": "ru", "title": "Бенч-монитор"}
        await ws.send(json.dumps(start))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 20))
        job_id = ready["job_id"]
        print("live ready:", job_id)
        step = int(0.2 * sr)
        started = time.monotonic()
        sent = 0
        while sent < limit:
            piece = samples[sent : sent + step]
            sent += len(piece)
            await ws.send(b"\x00" + piece.tobytes())
            delay = sent / sr - (time.monotonic() - started)
            if delay > 0:
                await asyncio.sleep(delay)
        print(f"live: отправлено {sent / sr:.0f} c аудио, стоп")
        await ws.send(json.dumps({"type": "stop"}))
        try:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        except (TimeoutError, websockets.ConnectionClosed):
            pass
    return job_id


def main() -> None:
    """Прогнать живой сценарий и напечатать замеры по этапам."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="адрес сервера")
    parser.add_argument("--wav", required=True, help="16 кГц моно WAV с речью")
    parser.add_argument("--seconds", type=float, default=45.0, help="длина live-фрагмента")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    ws_url = base.replace("http", "ws", 1) + "/ws/live"
    snapshots: dict[str, dict] = {}

    def snapshot(tag: str) -> None:
        """Снять снапшот метрик и напечатать его."""
        data = httpx.get(f"{base}/api/metrics", timeout=30).json()
        snapshots[tag] = data
        gpu = data["system"]["gpu"] or {}
        me = data["system"]["self_process"]
        print(f"=== {tag} ===")
        print(
            f"  GPU util {gpu.get('utilization_pct')}% | VRAM {gpu.get('vram_used_mb')}/"
            f"{gpu.get('vram_total_mb')} MB | free {gpu.get('vram_free_mb')} MB"
        )
        print(f"  сервер: RSS {me['rss_mb']} MB | VRAM {me['vram_mb']} MB | CPU {me['cpu_pct']}%")
        for model in data["models"]:
            print(
                f"  модель {model['engine']}:{model['model']} -> VRAM {model['vram_mb']} MB,"
                f" RAM {model['ram_mb']} MB"
            )

    def wait_status(job_id: str, timeout: float = 600) -> dict:
        """Дождаться финального статуса задачи."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = httpx.get(f"{base}/api/jobs/{job_id}", timeout=30).json()
            if job["status"] in ("done", "error", "cancelled"):
                print(f"  job {job_id[:8]} -> {job['status']} ({job.get('progress')}%)")
                return job
            time.sleep(3)
        raise TimeoutError(f"job {job_id} не завершился за {timeout}s")

    with wave.open(args.wav) as handle:
        sr = handle.getframerate()
        if sr != 16000 or handle.getnchannels() != 1:
            raise SystemExit(
                f"нужен 16 кГц моно WAV, а тут {sr} Гц, каналов: {handle.getnchannels()}"
            )
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")

    snapshot("00 базовая линия")
    t0 = time.time()
    live_id = asyncio.run(feed_live(ws_url, samples, sr, args.seconds))

    reprocess_id = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not reprocess_id:
        jobs = httpx.get(f"{base}/api/jobs?limit=20", timeout=30).json()
        for job in jobs:
            if (
                job["id"] != live_id
                and job["created_at"] >= t0 - 5
                and job["status"] in ("queued", "running", "done")
            ):
                reprocess_id = job["id"]
                print(f"цепочка после live: {job['kind']} {job['id'][:8]} ({job['status']})")
                break
        if not reprocess_id:
            time.sleep(5)
    if reprocess_id:
        time.sleep(20)
        snapshot("02 live отработал, идёт улучшение")
        wait_status(reprocess_id)
        snapshot("03 улучшение готово (MOSS)")
    else:
        snapshot("02 live отработал")

    with open(args.wav, "rb") as handle:
        response = httpx.post(
            f"{base}/api/jobs",
            files={"file": (Path(args.wav).name, handle, "audio/wav")},
            data={"engine": "whisper", "language": "ru"},
            timeout=60,
        )
    response.raise_for_status()
    upload_id = response.json()["id"]
    print("загружен файл, job", upload_id[:8])
    time.sleep(15)
    snapshot("04 идёт файловая задача")
    wait_status(upload_id)
    snapshot("05 финал")

    with open("bench_snapshots.json", "w", encoding="utf-8") as handle:
        json.dump(snapshots, handle, ensure_ascii=False, indent=1)
    print("снапшоты сохранены: bench_snapshots.json")


if __name__ == "__main__":
    main()
