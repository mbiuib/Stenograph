/** Domain types mirroring the backend Pydantic models. */

export type JobStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface Segment {
  index: number;
  start: number;
  end: number;
  text: string;
  speaker: string | null;
}

export type AnalysisKind = "protocol" | "summary";

export interface AnalysisEntry {
  job_id: string;
  text?: string;
  model?: string;
  created_at?: number;
  finished_at?: number;
}

export interface JobMeta {
  duration?: number;
  language?: string;
  language_probability?: number;
  engine?: string;
  speakers?: string[];
  diarized?: boolean;
  processing_seconds?: number;
  format?: string;
  size?: number;
  has_audio?: boolean;
  parent?: string;
  reprocess_job?: string;
  tracks?: string[];
  audio?: Record<string, string>;
  track_durations?: Record<string, number>;
  analysis?: Partial<Record<AnalysisKind, AnalysisEntry>>;
  analysis_type?: AnalysisKind;
  chunks?: number;
  transcript_chars?: number;
  model?: string;
  [key: string]: unknown;
}

export interface Job {
  id: string;
  kind: string;
  source_name: string;
  source_path: string | null;
  status: JobStatus;
  progress: number;
  message: string;
  language: string | null;
  text: string;
  segments: Segment[];
  error: string | null;
  meta: JobMeta;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export type JobEvent =
  | { type: "snapshot"; job: Job }
  | { type: "status"; status: JobStatus; message: string; progress: number }
  | { type: "progress"; value: number; message: string }
  | { type: "segment"; segment: Segment }
  | { type: "segments_replaced"; segments: Segment[] }
  | { type: "meta"; meta: JobMeta }
  | { type: "partial"; track: string; speaker: string; text: string }
  | { type: "level"; track: string; rms: number }
  | { type: "done"; text: string; meta: JobMeta }
  | { type: "error"; message: string }
  | { type: "cancelled" };

export interface Stats {
  jobs: { total: number; by_status: Record<string, number> };
  audio_seconds: number;
  processing_seconds: number;
  avg_speed_factor: number | null;
  engines: { engine: string; jobs: number }[];
  activity: { date: string; jobs: number; audio_seconds: number }[];
  recent: Job[];
}

export interface QueueSnapshot {
  active: Job | null;
  waiting: Job[];
}

export interface EngineList {
  available: string[];
  default: string;
  improve_default?: string;
}

export interface AppConfig {
  engine: string;
  whisper_model: string;
  live_model: string;
  live_auto_reprocess: boolean;
  realtime_transcribe: boolean;
  language: string;
  device: string;
  compute_type: string;
  models_dir: string | null;
  llm?: { model: string; base_url: string };
}

export interface LiveSessionInfo {
  job_id: string;
  source_name: string;
  capture: string;
  tracks: string[];
  started_at: number;
  lag_sec: number;
  text_delay_sec: number | null;
  transcribing: boolean;
  transcribe: boolean;
  queue_position: number | null;
}

export interface LiveStatus {
  active: boolean;
  supported: boolean;
  sessions: LiveSessionInfo[];
  serving: string | null;
}

export interface JitsiParticipant {
  id: string;
  label: string;
  language: string | null;
  segments: number;
  audio_sec: number;
  last_frame_sec: number | null;
}

export interface JitsiMeeting {
  meeting_id: string;
  job_id: string;
  job_status: string;
  started_at: number;
  duration_sec: number;
  language: string | null;
  pooled: boolean;
  transcribe: boolean;
  segments: number;
  participants: JitsiParticipant[];
}

export interface JitsiStatus {
  active: boolean;
  meetings: JitsiMeeting[];
}

export interface LiveDevices {
  supported: boolean;
  devices: { loopback?: string; microphone?: string };
  error?: string;
}

export interface MetricsGpu {
  name: string;
  utilization_pct: number;
  vram_total_mb: number;
  vram_used_mb: number;
  vram_free_mb: number;
}

export interface GpuProcess {
  pid: number;
  name: string | null;
  vram_mb: number;
}

export interface ModelMemory {
  engine: string;
  model: string;
  vram_mb: number | null;
  ram_mb: number | null;
  loaded_at: string;
  last_used_at: string;
  idle_sec: number | null;
  load_number: number;
}

export interface MetricsSnapshot {
  ts: string;
  system: {
    gpu: MetricsGpu | null;
    self_process: {
      pid: number;
      rss_mb: number | null;
      commit_mb: number | null;
      cpu_pct: number | null;
      vram_mb: number | null;
    };
    gpu_processes: GpuProcess[] | null;
  };
  models: ModelMemory[];
  live: LiveStatus;
  queue: QueueSnapshot;
  jobs: Record<string, number>;
  llm: { lm_studio: boolean };
}
