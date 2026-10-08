"""Command-line interface.

`stenograph transcribe FILE` runs the exact same pipeline as the HTTP API and
prints the streamed events; the CLI doubles as the fastest smoke test.
"""

from __future__ import annotations

import argparse
import queue
import sys
from pathlib import Path

from .config import get_settings
from .domain.models import JobStatus
from .engines import available_asr
from .logging_setup import setup as setup_logging
from .service import TranscriptionService, build_default_service


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `stenograph` console script."""
    parser = argparse.ArgumentParser(
        prog="stenograph", description="Стенограф — локальная транскрибация встреч"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    transcribe = subparsers.add_parser("transcribe", help="транскрибировать файл")
    transcribe.add_argument("path", type=Path, help="аудио или видео файл")
    transcribe.add_argument(
        "--language", default=None, help="код языка (ru, en, ...); по умолчанию авто"
    )
    transcribe.add_argument(
        "--engine",
        default=None,
        help=f"движок ({', '.join(available_asr())}); по умолчанию из настроек",
    )
    transcribe.add_argument("--quiet", action="store_true", help="печатать только итоговый текст")

    analyze = subparsers.add_parser(
        "analyze", help="протокол или резюме завершённой задачи (через локальный LLM)"
    )
    analyze.add_argument("job_id", help="id задачи — смотрите в веб-интерфейсе")
    analyze.add_argument(
        "--type",
        choices=("protocol", "summary"),
        default="protocol",
        help="что составить: protocol (протокол) или summary (резюме)",
    )

    args = parser.parse_args(argv)
    if args.command == "transcribe":
        return _transcribe(args)
    if args.command == "analyze":
        return _analyze(args)
    return 1


def _transcribe(args: argparse.Namespace) -> int:
    setup_logging(get_settings().log_level)
    if not args.path.is_file():
        print(f"файл не найден: {args.path}", file=sys.stderr)
        return 2

    service = build_default_service()
    job = service.submit_file(args.path.resolve(), language=args.language, engine=args.engine)
    channel = service.bus.subscribe(job.id)

    try:
        while True:
            try:
                event = channel.get(timeout=1.0)
            except queue.Empty:
                code = _check_finished(service, job.id, args.quiet)
                if code is not None:
                    return code
                continue

            etype = event.get("type")
            if etype == "status":
                if not args.quiet:
                    print(f"[{event['progress']:3d}%] {event['message']}", file=sys.stderr)
            elif etype == "progress":
                if not args.quiet:
                    print(f"[{event['value']:3d}%] {event['message']}", file=sys.stderr)
            elif etype == "segment":
                if not args.quiet:
                    seg = event["segment"]
                    print(f"  [{seg['start']:7.1f} → {seg['end']:7.1f}] {seg['text']}")
            elif etype == "segments_replaced":
                if not args.quiet:
                    print(f"  (сегменты пересобраны: {len(event['segments'])})", file=sys.stderr)
            elif etype == "done":
                meta = event["meta"]
                if not args.quiet:
                    print(
                        f"\n— готово: язык {meta.get('language')}, "
                        f"длительность {float(meta.get('duration', 0)):.0f} с —",
                        file=sys.stderr,
                    )
                print(event["text"])
                return 0
            elif etype == "error":
                print(f"ошибка: {event['message']}", file=sys.stderr)
                return 1
            elif etype == "cancelled":
                print("отменено", file=sys.stderr)
                return 130
    finally:
        service.bus.unsubscribe(job.id, channel)


def _analyze(args: argparse.Namespace) -> int:
    """Run protocol/summary generation and print the markdown result."""
    setup_logging(get_settings().log_level)
    service = build_default_service()
    job = service.get(args.job_id)
    if job is None:
        print(f"задача не найдена: {args.job_id}", file=sys.stderr)
        return 2
    try:
        child = service.request_analysis(job, args.type)
    except ValueError as exc:
        print(f"не получилось: {exc}", file=sys.stderr)
        return 2

    channel = service.bus.subscribe(child.id)
    try:
        while True:
            try:
                event = channel.get(timeout=1.0)
            except queue.Empty:
                current = service.get(child.id)
                if current and current.status == JobStatus.DONE:
                    print(current.text)
                    return 0
                if current and current.status in (JobStatus.ERROR, JobStatus.CANCELLED):
                    print(f"ошибка: {current.error or 'отменено'}", file=sys.stderr)
                    return 1
                continue
            etype = event.get("type")
            if etype in ("status", "progress"):
                value = event.get("progress", event.get("value", 0))
                message = event.get("message", "")
                print(f"[{value:3d}%] {message}", file=sys.stderr)
            elif etype == "done":
                print(event["text"])
                return 0
            elif etype == "error":
                print(f"ошибка: {event['message']}", file=sys.stderr)
                return 1
            elif etype == "cancelled":
                print("отменено", file=sys.stderr)
                return 130
    finally:
        service.bus.unsubscribe(child.id, channel)


def _check_finished(service: TranscriptionService, job_id: str, quiet: bool) -> int | None:
    """Fallback poll in case terminal events were published before we subscribed."""
    current = service.get(job_id)
    if current is None or current.status not in (
        JobStatus.DONE,
        JobStatus.ERROR,
        JobStatus.CANCELLED,
    ):
        return None
    if current.status == JobStatus.DONE:
        if not quiet:
            print("— готово —", file=sys.stderr)
        print(current.text)
        return 0
    if current.status == JobStatus.CANCELLED:
        print("отменено", file=sys.stderr)
        return 130
    print(f"ошибка: {current.error}", file=sys.stderr)
    return 1
