/** Thin typed wrapper around the backend REST API. */
import type { AppConfig, EngineList, Job, QueueSnapshot, Stats } from "./types";

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
};
