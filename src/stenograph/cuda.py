"""Windows CUDA bootstrap.

CTranslate2 (the engine under faster-whisper) loads cublas64_12.dll and the
cuDNN DLLs at import time, but pip-installed NVIDIA wheels place them under
site-packages/nvidia/*/bin, which Windows does not search by default. This
module registers those directories explicitly and must run before
faster_whisper is imported.
"""

from __future__ import annotations

import contextlib
import os
import site
import sys
from pathlib import Path

_registered: set[str] = set()


def prepare() -> None:
    """Register NVIDIA DLL directories found in site-packages (Windows only).

    Both AddDllDirectory (os.add_dll_directory) and PATH are updated:
    ctranslate2's loader resolves its dependencies through PATH rather than
    through the AddDllDirectory paths.
    """
    if sys.platform != "win32":
        return
    added: list[str] = []
    for base in _site_dirs():
        nvidia_root = Path(base) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for package in sorted(nvidia_root.iterdir()):
            bin_dir = package / "bin"
            key = str(bin_dir)
            if bin_dir.is_dir() and key not in _registered:
                try:
                    os.add_dll_directory(key)
                except OSError:  # pragma: no cover - defensive
                    continue
                _registered.add(key)
                added.append(key)
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")


def _site_dirs() -> list[str]:
    """All candidate site-packages roots for the current interpreter."""
    dirs: list[str] = []
    with contextlib.suppress(AttributeError):  # virtualenv quirk
        dirs.extend(site.getsitepackages())
    user_site = ""
    with contextlib.suppress(AttributeError):
        user_site = site.getusersitepackages()
    if user_site:
        dirs.append(user_site)
    return dirs
