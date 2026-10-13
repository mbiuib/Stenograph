# syntax=docker/dockerfile:1
# =============================================================================
#  Stenograph — one image for both the server and the external ASR service.
#  Build from the repository root (the compose stack does this for you):
#      docker compose -f deploy/stack/docker-compose.yml build
#  GPU note: the ASR container needs the NVIDIA runtime (Docker Desktop
#  supports it with the WSL2 backend + a current driver).
# =============================================================================
FROM python:3.12-slim

# ffmpeg decodes/prepares audio for the engines; curl serves the compose
# healthchecks; git is needed to install the pinned moss-transcribe-diarize
# commit from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl git \
    && rm -rf /var/lib/apt/lists/*

# uv installs the project fast and honours [tool.uv.sources] (the CUDA 13
# torch index) from pyproject.toml.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1 UV_LINK_MODE=copy

COPY pyproject.toml README.md ./
COPY src ./src

# STENOGRAPH_EXTRAS=moss,gigaam (default) installs the full engine set (torch/ml
# CUDA + MOSS + ECAPA + GigaAM multilingual deps); pass a subset for a lean build.
ARG STENOGRAPH_EXTRAS=moss,gigaam
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -e ".[${STENOGRAPH_EXTRAS}]"

# ctranslate2 (faster-whisper) loads cuBLAS/cuDNN at runtime; torch ships its
# own CUDA, the nvidia-* wheels keep ctranslate2 covered. The cu13 dir holds
# nvrtc + nvrtc-builtins: torch's runtime JIT (used by MOSS) dlopens the
# builtins by plain soname, so the directory must sit on LD_LIBRARY_PATH —
# without it every moss pass dies with "failed to open libnvrtc-builtins.so.13.0".
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cu13/lib:/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cuda_runtime/lib

# The built React UI (served by the stenograph server container).
COPY frontend/dist ./frontend/dist

# Default: the external ASR service. The compose stack overrides the command
# for the stenograph server container.
CMD ["python", "-m", "stenograph.asr_service"]
