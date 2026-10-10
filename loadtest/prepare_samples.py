"""Cut speech fixtures for load tests into loadtest/samples/.

Slices 16 kHz mono WAV sources into fixed-length chunks and places every chunk
at the loudest nearby window — silence-heavy tracks (jitsi participant
recordings carry long silent fills) would otherwise yield silent chunks; a
source without speech is skipped. Sources are given explicitly (files or
directories), or — without arguments — discovered locally: the monitor-test
feed sample, recent live recordings (system.wav + mic.wav) and the largest
participant track of each of the newest jitsi jobs. Stale samples left from
previous runs are pruned after a successful cut. Sources of any format are
accepted — anything that is not already 16 kHz mono WAV is converted via
ffmpeg (MEETSCRIBE_FFMPEG from the project .env, or PATH).

Usage:
  .venv/Scripts/python.exe loadtest/prepare_samples.py [--per-source 3]
      [--chunk-sec 14] [--count 100] [--latest-live 6] [--from-jitsi 8] [файл | папка ...]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "samples"
SILENT_RMS = 0.004  # ниже этого среднего RMS источник считается молчащим
SOURCE_GLOBS = ("*.wav", "*.mp3", "*.m4a", "*.aac", "*.flac", "*.ogg", "*.opus", "*.wma")


def find_ffmpeg() -> str | None:
    """ffmpeg: переменная окружения → .env проекта → PATH."""
    env = os.environ.get("MEETSCRIBE_FFMPEG")
    if env and Path(env).exists():
        return env
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "MEETSCRIBE_FFMPEG":
                value = value.strip().strip('"').strip("'")
                if Path(value).exists():
                    return value
    return shutil.which("ffmpeg")


def ensure_wav16k(source: Path, ffmpeg: str | None, temp_dir: Path) -> Path | None:
    """Путь к WAV 16 кГц моно: исходник, если он уже такой, иначе конверсия."""
    try:
        with wave.open(str(source)) as src:
            if src.getframerate() == 16000 and src.getnchannels() == 1:
                return source
        reason = "не 16 кГц моно"
    except wave.Error:
        reason = "не WAV"
    if not ffmpeg:
        print(f"  пропускаю ({reason}; ffmpeg не найден — перегнать вручную)")
        return None
    target = temp_dir / (source.stem + ".conv.wav")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        print(f"  пропускаю ({reason}; ffmpeg не осилил): {source.name}")
        return None
    print(f"  {source.name}: {reason} → перегнал в 16 кГц моно")
    return target


def find_sources(latest_live: int, from_jitsi: int) -> list[Path]:
    """Local speech sources: monitor sample, recent live tracks, jitsi tracks."""
    found: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        monitor = Path(local) / "hermes" / "cache" / "scratch" / "monitor-test" / "live_feed.wav"
        if monitor.exists():
            found.append(monitor)
    live = HERE.parent / "data" / "live"
    if live.exists() and latest_live > 0:
        for name in ("system.wav", "mic.wav"):
            tracks = [p for p in live.glob(f"*/{name}") if p.stat().st_size > 2_000_000]
            tracks.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            found.extend(tracks[:latest_live])
    jitsi = HERE.parent / "data" / "jitsi"
    if jitsi.exists() and from_jitsi > 0:
        dirs = [d for d in jitsi.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        picked = 0
        for directory in dirs:
            if picked >= from_jitsi:
                break
            tracks = [p for p in directory.glob("*.wav") if p.stat().st_size > 2_000_000]
            if not tracks:
                continue
            tracks.sort(key=lambda p: p.stat().st_size, reverse=True)
            found.append(tracks[0])
            picked += 1
    return found


def cut(source: Path, per_source: int, chunk_sec: float, out: Path, start_index: int) -> int:
    """Write ``per_source`` chunks of ``source`` as sample_<NN>.wav; 0 if skipped."""
    with wave.open(str(source)) as src:
        rate = src.getframerate()
        channels = src.getnchannels()
        frames = src.getnframes()
        if rate != 16000 or channels != 1:
            print(f"  пропускаю (нужен WAV 16 кГц моно, тут {rate} Гц × {channels}): {source}")
            return 0
        chunk = int(chunk_sec * rate)
        if frames < chunk * 2:
            print(f"  пропускаю (короче двух чанков, {frames / rate:.0f} с): {source}")
            return 0
        data = np.frombuffer(src.readframes(frames), dtype=np.int16)
        seconds = frames // rate
        block = data[: seconds * rate].astype(np.float32) / 32768.0
        rms = np.sqrt((block**2).reshape(-1, rate).mean(axis=1))
        chunk_s = max(1, min(seconds, int(round(chunk_sec))))
        cum = np.concatenate([[0.0], np.cumsum(rms)])
        n_pos = max(1, seconds - chunk_s + 1)

        def best_window(lo: int, hi: int, taken: list[int]) -> int | None:
            """Самый громкий чанк в [lo, hi) со средней речью; занятые исключены."""
            cand = np.arange(lo, hi)
            if cand.size == 0:
                return None
            scores = cum[cand + chunk_s] - cum[cand]
            for pos in taken:
                scores[np.abs(cand - pos) < chunk_s * 2] = -1.0
            if float(scores.max()) < SILENT_RMS * chunk_s:
                return None
            return int(cand[int(scores.argmax())])

        if best_window(0, n_pos, []) is None:
            print(f"  пропускаю (речи не найдено): {source}")
            return 0

        span = max(0, seconds - chunk_s)
        made = 0
        taken: list[int] = []
        for index in range(per_source):
            anchor = int(span * index / per_source)
            pos = best_window(max(0, anchor - 45), min(n_pos, anchor + 46), taken)
            if pos is None:  # рядом тишина или всё занято — ищем по всему файлу
                pos = best_window(0, n_pos, taken)
            if pos is None:
                continue
            taken.append(pos)
            src.setpos(pos * rate)
            frames_block = src.readframes(chunk)
            name = out / f"sample_{start_index + made:02d}.wav"
            with wave.open(str(name), "wb") as dst:
                dst.setnchannels(1)
                dst.setsampwidth(2)
                dst.setframerate(rate)
                dst.writeframes(frames_block)
            made += 1
        return made


def main() -> None:
    """Cut samples from the discovered or given sources into samples/."""
    parser = argparse.ArgumentParser(description="Нарезка речевых сэмплов для нагрузочных тестов")
    parser.add_argument(
        "sources", nargs="*", type=Path, help="входные WAV 16 кГц моно (файлы или папки)"
    )
    parser.add_argument(
        "--per-source",
        type=int,
        default=3,
        help="чанков с каждого источника (если не задан --count)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="целевое общее число сэмплов (распределяется по источникам; 0 — по --per-source)",
    )
    parser.add_argument("--chunk-sec", type=float, default=14.0, help="длина чанка, секунды")
    parser.add_argument(
        "--latest-live", type=int, default=6, help="сколько свежих live-записей (system+mic)"
    )
    parser.add_argument(
        "--from-jitsi",
        type=int,
        default=8,
        help="сколько свежих jitsi-задач (крупнейшая дорожка каждой)",
    )
    args = parser.parse_args()

    sources: list[Path] = []
    for item in args.sources:
        if item.is_dir():
            for pattern in SOURCE_GLOBS:
                sources.extend(sorted(item.glob(pattern)))
        else:
            sources.append(item)
    if not sources:
        sources = find_sources(args.latest_live, args.from_jitsi)
    if not sources:
        raise SystemExit(
            "не нашёл источников речи — передайте файлы/папки явно: "
            "prepare_samples.py путь/к/записи.wav ..."
        )
    per_source = args.per_source
    if args.count > 0 and sources:
        per_source = max(1, (args.count + len(sources) - 1) // len(sources) + 1)
        print(
            f"цель: {args.count} сэмплов → до {per_source} с каждого из {len(sources)} источников"
        )
    OUT.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    temp_dir = Path(tempfile.mkdtemp(prefix="prepare-samples-"))
    total = 0
    try:
        for source in sources:
            if not source.exists():
                print(f"пропускаю (нет файла): {source}")
                continue
            wav = ensure_wav16k(source, ffmpeg, temp_dir)
            if wav is None:
                continue
            made = cut(wav, per_source, args.chunk_sec, OUT, total)
            if made:
                print(f"{source} → {made} чанк(ов) по {args.chunk_sec:.0f} с")
            total += made
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    if args.count > 0 and total > args.count:  # надрезаем до целевого числа
        extras = 0
        for index in range(args.count, total):
            path = OUT / f"sample_{index:02d}.wav"
            if path.exists():
                path.unlink()
                extras += 1
        print(f"подрезал до --count {args.count} (лишних удалено: {extras})")
        total = args.count
    if total > 0:  # после успешной нарезки чистим хвосты прошлых наборов
        fresh = {f"sample_{i:02d}.wav" for i in range(total)}
        for old in sorted(OUT.glob("sample_*.wav")):
            if old.name not in fresh:
                old.unlink()
                print(f"устаревший удалён: {old.name}")
    if total:
        note = f" (материала хватило на {total} из {args.count})" if args.count > total else ""
        print(f"готово: {total} сэмплов в {OUT}{note}")
    else:
        print("готово: ничего не нарезано (все источники пропущены)")


if __name__ == "__main__":
    main()
