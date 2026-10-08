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
}

export interface AppConfig {
  engine: string;
  whisper_model: string;
  live_model: string;
  live_auto_reprocess: boolean;
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
  transcribing: boolean;
  queue_position: number | null;
}

export interface LiveStatus {
  active: boolean;
  supported: boolean;
  sessions: LiveSessionInfo[];
  serving: string | null;
}

export interface LiveDevices {
  supported: boolean;
  devices: { loopback?: string; microphone?: string };
  error?: string;
}
