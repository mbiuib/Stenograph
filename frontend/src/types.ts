/** Domain types mirroring the backend Pydantic models. */

export type JobStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface Segment {
  index: number;
  start: number;
  end: number;
  text: string;
  speaker: string | null;
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
  language: string;
  device: string;
  compute_type: string;
  models_dir: string | null;
}
