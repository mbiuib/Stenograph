"""Cut speech fixtures for load tests into loadtest/samples/.

Slices 16 kHz mono WAV sources into fixed-length chunks at spread offsets,
so synthetic speakers decode different audio at different positions instead
of looping one clip in sync. Sources are given explicitly, or — without
arguments — discovered locally: the monitor-test feed sample and the newest
long system tracks from the project's data/live/ recordings.

Usage:
  .venv/Scripts/python.exe loadtest/prepare_samples.py [--per-source 3]
      [--chunk-sec 14] [source.wav ...]
"""

from __future__ import annotations

import argparse
import os
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "samples"


def find_sources() -> list[Path]:
    """Local speech sources: the monitor sample plus recent live recordings."""
    found: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        monitor = Path(local) / "hermes" / "cache" / "scratch" / "monitor-test" / "live_feed.wav"
        if monitor.exists():
            found.append(monitor)
    live = HERE.parent / "data" / "live"
    if live.exists():
        tracks = [p for p in live.glob("*/system.wav") if p.stat().st_size > 2_000_000]
        tracks.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        found.extend(tracks[:3])
    return found


def cut(source: Path, per_source: int, chunk_sec: float, out: Path, start_index: int) -> int:
    """Write ``per_source`` chunks of ``source`` as sample_<NN>.wav files."""
    with wave.open(str(source)) as src:
        rate = src.getframerate()
        channels = src.getnchannels()
        frames = src.getnframes()
        if rate != 16000 or channels != 1:
            raise SystemExit(f"{source}: нужен WAV 16 кГц моно (тут {rate} Гц, каналов {channels})")
        chunk = int(chunk_sec * rate)
        if frames < chunk * 2:
            raise SystemExit(f"{source}: короче двух чанков ({frames / rate:.0f} с)")
        span = frames - chunk
        made = 0
        for index in range(per_source):
            src.setpos(int(span * index / per_source))
            data = src.readframes(chunk)
            name = out / f"sample_{start_index + index:02d}.wav"
            with wave.open(str(name), "wb") as dst:
                dst.setnchannels(1)
                dst.setsampwidth(2)
                dst.setframerate(rate)
                dst.writeframes(data)
            made += 1
        return made


def main() -> None:
    """Cut samples from the discovered or given sources into samples/."""
    parser = argparse.ArgumentParser(description="Нарезка речевых сэмплов для нагрузочных тестов")
    parser.add_argument("sources", nargs="*", type=Path, help="входные WAV 16 кГц моно")
    parser.add_argument("--per-source", type=int, default=3, help="чанков с каждого источника")
    parser.add_argument("--chunk-sec", type=float, default=14.0, help="длина чанка, секунды")
    args = parser.parse_args()

    sources = args.sources or find_sources()
    if not sources:
        raise SystemExit(
            "не нашёл источников речи — передайте файлы явно: "
            "prepare_samples.py путь/к/записи.wav ..."
        )
    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    for source in sources:
        if not source.exists():
            print(f"пропускаю (нет файла): {source}")
            continue
        made = cut(source, args.per_source, args.chunk_sec, OUT, total)
        total += made
        print(f"{source} → {made} чанков по {args.chunk_sec:.0f} с")
    print(f"готово: {total} сэмплов в {OUT}")


if __name__ == "__main__":
    main()
