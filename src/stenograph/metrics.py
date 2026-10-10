"""Resource metrics for the monitor: GPU, per-model memory, queue/live state.

What is observable on a Windows box:

- ``nvidia-smi`` — device totals (utilization, VRAM used/free); per-process
  VRAM is *not* available there under WDDM, hence the perf counters below;
- ``Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory`` (WMI) —
  per-process dedicated VRAM, the only reliable source on consumer Windows;
- ``psapi`` via ctypes — this process' working set / commit / CPU share;
- load-time deltas — the engines call :func:`note_model_loaded` right after a
  model finishes loading, so each model gets a measured VRAM/RAM footprint.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MODELS: dict[str, dict] = {}
_BASELINE: tuple[int | None, float | None] | None = None
_CPU_PREV: tuple[float, float] | None = None

_NVSMI: str | None | bool = False  # False = not resolved yet
_GPU_PROCS_CACHE: list[dict] | None = None
_GPU_PROCS_AT = 0.0
_GPU_PROCS_TTL = 5.0

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Per-process dedicated VRAM (bytes -> MB) with process names resolved.
_PS_GPU_PROCS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$procs = @{}
foreach ($p in Get-Process) { $procs[[string]$p.Id] = $p.ProcessName }
$rows = Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory |
    Where-Object { $_.DedicatedUsage -gt 52428800 }
$out = foreach ($r in $rows) {
    if ($r.Name -match '^pid_(\d+)_') {
        $n = [int]$Matches[1]
        $mb = [int][math]::Round($r.DedicatedUsage / 1MB)
        [pscustomobject]@{ pid = $n; name = $procs[[string]$n]; vram_mb = $mb }
    }
}
@($out) | Sort-Object vram_mb -Descending | Select-Object -First 15 | ConvertTo-Json -Compress
"""


def _iso_now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _nvsmi_path() -> str | None:
    global _NVSMI
    if _NVSMI is False:
        found = shutil.which("nvidia-smi")
        if not found:
            for candidate in (
                r"C:\Windows\System32\nvidia-smi.exe",
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            ):
                if os.path.isfile(candidate):
                    found = candidate
                    break
        _NVSMI = found
    return _NVSMI if isinstance(_NVSMI, str) else None


def gpu_info() -> dict | None:
    """Device-wide GPU state via nvidia-smi (or None when unavailable)."""
    path = _nvsmi_path()
    if not path:
        return None
    try:
        out = subprocess.run(
            [
                path,
                "--query-gpu=name,utilization.gpu,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("nvidia-smi failed: %s", exc)
        return None
    line = (out.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = [item.strip() for item in line[0].split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "name": parts[0],
            "utilization_pct": int(float(parts[1])),
            "vram_total_mb": int(float(parts[2])),
            "vram_used_mb": int(float(parts[3])),
            "vram_free_mb": int(float(parts[4])),
        }
    except ValueError:
        return None


def _query_gpu_processes() -> list[dict] | None:
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_GPU_PROCS],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("per-process GPU query failed: %s", exc)
        return None
    raw = (out.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    return [
        {
            "pid": int(item.get("pid") or 0),
            "name": item.get("name"),
            "vram_mb": int(item.get("vram_mb") or 0),
        }
        for item in data
        if isinstance(item, dict)
    ]


def gpu_processes() -> list[dict] | None:
    """Per-process dedicated VRAM (top ~15), cached for a few seconds."""
    global _GPU_PROCS_CACHE, _GPU_PROCS_AT
    now = time.monotonic()
    if _GPU_PROCS_CACHE is not None and now - _GPU_PROCS_AT < _GPU_PROCS_TTL:
        return _GPU_PROCS_CACHE
    data = _query_gpu_processes()
    with _LOCK:
        _GPU_PROCS_CACHE = data
        _GPU_PROCS_AT = now
    return data


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_memory_mb() -> tuple[float | None, float | None]:
    """(working set, commit) of THIS process in MiB."""
    if os.name != "nt":
        return None, None
    pmc = _ProcessMemoryCounters()
    pmc.cb = ctypes.sizeof(_ProcessMemoryCounters)
    try:
        kernel32 = ctypes.windll.kernel32
        # GetCurrentProcess returns the pseudo-handle -1; without an explicit
        # 64-bit restype ctypes truncates it to int32 and the call fails.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel32.GetCurrentProcess()
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            wt.DWORD,
        ]
        ok = kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
    except (OSError, AttributeError) as exc:
        log.debug("process memory query failed: %s", exc)
        return None, None
    if not ok:
        return None, None
    return round(pmc.WorkingSetSize / 1048576, 1), round(pmc.PagefileUsage / 1048576, 1)


def _cpu_pct() -> float | None:
    """CPU share of this process since the previous call (None on first)."""
    global _CPU_PREV
    now, cpu = time.monotonic(), time.process_time()
    with _LOCK:
        prev = _CPU_PREV
        _CPU_PREV = (now, cpu)
    if prev is None:
        return None
    delta_wall, delta_cpu = now - prev[0], cpu - prev[1]
    if delta_wall <= 0:
        return None
    return round(100.0 * delta_cpu / delta_wall, 1)


def llm_status() -> dict:
    """Whether the external LLM server (LM Studio) is reachable."""
    reachable = False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        reachable = sock.connect_ex(("127.0.0.1", 1234)) == 0
    except OSError:
        reachable = False
    finally:
        sock.close()
    return {"lm_studio": reachable}


def prime() -> None:
    """Seed the load-time baseline (call once when the app starts)."""
    global _BASELINE
    gpu = gpu_info()
    rss, _ = process_memory_mb()
    with _LOCK:
        _BASELINE = (gpu["vram_used_mb"] if gpu else None, rss)


def note_model_loaded(engine: str, model: str) -> None:
    """Attribute the memory growth since the last baseline to a fresh model."""
    global _BASELINE
    gpu = gpu_info()
    vram_now = gpu["vram_used_mb"] if gpu else None
    rss, _ = process_memory_mb()
    with _LOCK:
        vram_delta: int | None = None
        ram_delta: float | None = None
        if _BASELINE is not None:
            if vram_now is not None and _BASELINE[0] is not None:
                vram_delta = max(0, vram_now - _BASELINE[0])
            if rss is not None and _BASELINE[1] is not None:
                ram_delta = max(0.0, rss - _BASELINE[1])
        _MODELS[f"{engine}:{model}"] = {
            "engine": engine,
            "model": model,
            "vram_mb": vram_delta,
            "ram_mb": round(ram_delta, 1) if ram_delta is not None else None,
            "loaded_at": _iso_now(),
            "last_used_at": _iso_now(),
            "load_number": len(_MODELS) + 1,
        }
        _BASELINE = (vram_now, rss)
    log.info(
        "metrics: %s:%s loaded (+%s MB VRAM, +%s MB RAM)", engine, model, vram_delta, ram_delta
    )


def touch_engine(engine: object) -> None:
    """Stamp an engine as recently used (best-effort, never raises)."""
    try:
        key = f"{getattr(engine, 'name', '?')}:{getattr(engine, 'model_id', '?')}"
        with _LOCK:
            entry = _MODELS.get(key)
            if entry is not None:
                entry["last_used_at"] = _iso_now()
    except Exception:  # noqa: BLE001 — metrics must never break transcription
        log.debug("metrics: touch failed", exc_info=True)


def note_model_unloaded(engine: str, model: str) -> None:
    """Drop an evicted model from the resident list and rebase load deltas.

    After the unload, the next :func:`note_model_loaded` measures the fresh
    footprint from the post-eviction memory level (otherwise it would report
    a mid-air baseline delta).
    """
    global _BASELINE
    gpu = gpu_info()
    rss, _ = process_memory_mb()
    with _LOCK:
        _MODELS.pop(f"{engine}:{model}", None)
        _BASELINE = (gpu["vram_used_mb"] if gpu else None, rss)
    log.info("metrics: %s:%s unloaded", engine, model)


def snapshot(
    *,
    live: dict | None = None,
    bridge: dict | None = None,
    queue: dict | None = None,
    counts: dict | None = None,
) -> dict:
    """Full resource snapshot for the monitor page."""
    gpu = gpu_info()
    procs = gpu_processes()
    rss, commit = process_memory_mb()
    pid = os.getpid()
    self_vram = next((item["vram_mb"] for item in procs or [] if item["pid"] == pid), None)
    now = time.time()
    with _LOCK:
        models = []
        for entry in _MODELS.values():
            item = dict(entry)
            try:
                used = datetime.fromisoformat(item["last_used_at"]).timestamp()
                item["idle_sec"] = round(max(0.0, now - used), 1)
            except (ValueError, KeyError):
                item["idle_sec"] = None
            models.append(item)
    return {
        "ts": _iso_now(),
        "system": {
            "gpu": gpu,
            "self_process": {
                "pid": pid,
                "rss_mb": rss,
                "commit_mb": commit,
                "cpu_pct": _cpu_pct(),
                "vram_mb": self_vram,
            },
            "gpu_processes": procs,
        },
        "models": models,
        "live": live,
        "bridge": bridge,
        "queue": queue,
        "jobs": counts,
        "llm": llm_status(),
    }
