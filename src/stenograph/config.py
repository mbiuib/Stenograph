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
    # MEETSCRIBE_REPROCESS_ENGINE: engine for auto-chained improvements of
    # live/jitsi recordings; None = the default engine above.
    reprocess_engine: str | None = None
    # MEETSCRIBE_BRIDGE_LANGUAGE: force the language of Jitsi bridge meetings
    # (live transcription and the meeting's improvement pass); None = trust the
    # per-frame language Jigasi sends.
    bridge_language: str | None = None

    # Live mode: WASAPI/browser capture + local-agreement streaming on whisper.
    live_model: str = "large-v3-turbo"
    live_chunk_sec: float = 0.2
    live_step_sec: float = 0.8
    live_max_window_sec: float = 25.0
    live_auto_reprocess: bool = True  # re-run the recording through MOSS after stop
    # MEETSCRIBE_REALTIME_TRANSCRIBE: decode Live/Jitsi audio while it is being
    # recorded. False (default) = record-only: sessions only save the audio,
    # the quality pass (auto-chained or manual) transcribes it afterwards.
    realtime_transcribe: bool = False
    # Transcription queue: one decode worker serves all live sessions; these
    # bound how much GPU work a single session may take per queue turn (so a
    # backlogged recording cannot starve the others).
    live_turn_ticks: int = 6  # max whisper steps per session per turn
    live_turn_sec: float = 2.0  # max wall-clock seconds of GPU work per turn
    live_batch_max: int = 20  # windows merged into one batched engine pass
    live_batch_window_sec: float = 5.0  # per-window audio cap inside one pass
    live_batch_hold_sec: float = 0.5  # trailing edge kept for the next window
    # Jitsi captions: shorter windows than live — the caption lag equals the cap.
    bridge_batch_window_sec: float = 3.0
    bridge_batch_hold_sec: float = 0.4
    # MEETSCRIBE_JITSI_IDLE_STOP_SEC: finalize a Jitsi meeting automatically
    # after this many seconds without any speech (0 = disabled). Covers the
    # "everybody left, but a forgotten client keeps the room alive" case:
    # Jigasi only disconnects when the last real participant leaves.
    jitsi_idle_stop_sec: float = 600.0
    # MEETSCRIBE_RESTART_RECOVER: a fresh process starts with an empty
    # in-memory queue, so queued/running rows are jobs orphaned by a previous
    # shutdown. True (default): waiting jobs return to the queue and
    # interrupted ones restart from scratch in the same job. False: orphans
    # are only marked "interrupted by a server stop" (retry manually).
    restart_recover: bool = True
    # MEETSCRIBE_FILE_WORKERS: how many file/reprocess jobs may run in
    # parallel (1 = serial, the default). Each worker loads its own copy of
    # the model it needs — GPU/CPU memory multiplies by the count. While a
    # live/Jitsi stream decodes, every worker holds heavy ASR back.
    file_workers: int = 1
    # MEETSCRIBE_MODEL_IDLE_UNLOAD_SEC: unload ASR models that sat idle this
    # long (frees VRAM/RAM; 0 disables the automatic behaviour, the manual
    # button on the monitor stays available). A model that is ABOUT to load
    # always evicts currently idle ones first — it takes their memory.
    model_idle_unload_sec: float = 600.0

    # MOSS-Transcribe-Diarize (end-to-end ASR + diarization).
    moss_chunk_sec: float = 300.0
    moss_chunk_overlap_sec: float = 2.0
    moss_max_new_tokens: int = 4096

    # LLM post-processing (protocol / summary) via an OpenAI-compatible server.
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    llm_model: str = "gemma-3-4b-it"
    llm_api_key: str | None = None
    llm_timeout_sec: float = 600.0
    llm_max_tokens: int = 4096
    llm_chunk_chars: int = 9000

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
        return clean_language(self.language)


def clean_language(value: str | None) -> str | None:
    """Normalize a language code for the engines.

    ""/"auto" (any case, surrounded by spaces) mean auto-detect — None;
    everything else is treated as a real code and returned trimmed.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return None if cleaned.lower() in ("", "auto") else cleaned


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
