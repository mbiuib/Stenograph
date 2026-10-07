"""Application settings: environment variables and .env file.

All variables use the MEETSCRIBE_ prefix (see .env.example). HF_TOKEN is
accepted as-is because huggingface_hub reads it directly.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Relative paths resolve against the CWD."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MEETSCRIBE_", extra="ignore")

    data_dir: Path = Path("data")
    models_dir: Path | None = None
    hf_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MEETSCRIBE_HF_TOKEN", "HF_TOKEN"),
    )

    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    frontend_dist: Path | None = None  # built React app; default: <repo>/frontend/dist

    device: str = "cuda"
    compute_type: str = "float16"
    whisper_model: str = "large-v3"
    language: str = "auto"

    engine: str = "whisper"  # default ASR engine name (see the engines registry)

    # Live mode: WASAPI capture + local-agreement streaming on whisper.
    live_model: str = "large-v3-turbo"
    live_chunk_sec: float = 0.2
    live_step_sec: float = 0.8
    live_max_window_sec: float = 25.0

    # MOSS-Transcribe-Diarize (end-to-end ASR + diarization).
    moss_chunk_sec: float = 300.0
    moss_chunk_overlap_sec: float = 2.0
    moss_max_new_tokens: int = 4096

    @property
    def uploads_dir(self) -> Path:
        """Where uploaded source files are stored."""
        return self.data_dir / "uploads"

    @property
    def work_dir(self) -> Path:
        """Scratch space for extracted audio."""
        return self.data_dir / "work"

    @property
    def db_path(self) -> Path:
        """SQLite database file."""
        return self.data_dir / "stenograph.db"

    def ensure_dirs(self) -> None:
        """Create data directories if they do not exist yet."""
        for path in (self.data_dir, self.uploads_dir, self.work_dir):
            path.mkdir(parents=True, exist_ok=True)

    def language_or_none(self) -> str | None:
        """Return the configured language, mapping "auto" to None (engine detects)."""
        return None if self.language in ("", "auto") else self.language


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
