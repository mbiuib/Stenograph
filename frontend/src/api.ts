/** Thin typed wrapper around the backend REST API. */
import type {
  AnalysisKind,
  AppConfig,
  EngineList,
  JitsiStatus,
  Job,
  LiveDevices,
  LiveStatus,
  MetricsSnapshot,
  QueueSnapshot,
  Stats,
} from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`${response.status} ${detail || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listJobs: (status?: string, limit = 100): Promise<Job[]> => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("status", status);
    return request<Job[]>(`/api/jobs?${params.toString()}`);
  },

  getJob: (id: string): Promise<Job> => request<Job>(`/api/jobs/${id}`),

  submitJob: (file: File, options: { engine?: string; language?: string }): Promise<Job> => {
    const form = new FormData();
    form.append("file", file);
    if (options.engine) form.append("engine", options.engine);
    if (options.language) form.append("language", options.language);
    return request<Job>("/api/jobs", { method: "POST", body: form });
  },

  cancelJob: (id: string): Promise<{ cancelled: boolean }> =>
    request<{ cancelled: boolean }>(`/api/jobs/${id}/cancel`, { method: "POST" }),

  reprocessJob: (id: string, engine?: string): Promise<Job> => {
    const form = new FormData();
    if (engine) form.append("engine", engine);
    return request<Job>(`/api/jobs/${id}/reprocess`, { method: "POST", body: form });
  },

  retryJob: (id: string): Promise<Job> =>
    request<Job>(`/api/jobs/${id}/retry`, { method: "POST" }),

  updateJob: (
    id: string,
    payload: { source_name?: string; speaker_names?: Record<string, string> },
  ): Promise<Job> =>
    request<Job>(`/api/jobs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  analyzeJob: (id: string, type: AnalysisKind): Promise<Job> => {
    const form = new FormData();
    form.append("type", type);
    return request<Job>(`/api/jobs/${id}/analyze`, { method: "POST", body: form });
  },

  deleteJob: async (id: string): Promise<void> => {
    const response = await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    if (!response.ok && response.status !== 204) {
      throw new Error(`${response.status}`);
    }
  },

  stats: (): Promise<Stats> => request<Stats>("/api/stats"),
  queue: (): Promise<QueueSnapshot> => request<QueueSnapshot>("/api/queue"),
  engines: (): Promise<EngineList> => request<EngineList>("/api/engines"),
  config: (): Promise<AppConfig> => request<AppConfig>("/api/config"),
  metrics: (): Promise<MetricsSnapshot> => request<MetricsSnapshot>("/api/metrics"),

  liveStatus: (): Promise<LiveStatus> => request<LiveStatus>("/api/live/status"),

  jitsiStatus: (): Promise<JitsiStatus> => request<JitsiStatus>("/api/jitsi/status"),
  liveDevices: (): Promise<LiveDevices> => request<LiveDevices>("/api/live/devices"),
  liveStart: (tracks: string[], language?: string): Promise<Job> => {
    const form = new FormData();
    form.append("tracks", tracks.join(","));
    if (language) form.append("language", language);
    return request<Job>("/api/live/start", { method: "POST", body: form });
  },
  liveStop: (jobId?: string): Promise<Job> => {
    const form = new FormData();
    if (jobId) form.append("job_id", jobId);
    return request<Job>("/api/live/stop", { method: "POST", body: form });
  },
};
