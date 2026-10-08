/** Formatting and export helpers. */
import type { Job, Segment } from "./types";

export function fmtClock(totalSeconds?: number | null): string {
  if (totalSeconds == null || Number.isNaN(totalSeconds)) return "—";
  const total = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${minutes}:${ss}`;
}

export function fmtHours(seconds?: number | null): string {
  if (!seconds) return "0 ч";
  const hours = seconds / 3600;
  return hours >= 10 ? `${Math.round(hours)} ч` : `${hours.toFixed(1)} ч`;
}

export function fmtTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${rest.toFixed(1).padStart(4, "0")}`;
}

export function fmtDateTime(unix?: number | null): string {
  if (!unix) return "—";
  return new Date(unix * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtRelative(unix: number): string {
  const diff = Date.now() / 1000 - unix;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return `${Math.floor(diff / 86400)} дн назад`;
}

export function fmtRemaining(seconds: number): string {
  if (seconds < 60) return "меньше минуты";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `≈ ${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  return `≈ ${hours} ч ${minutes % 60} мин`;
}

/** Rough ETA for a running job based on progress rate; null until enough data. */
export function etaSeconds(job: Job): number | null {
  if (job.status !== "running" || !job.started_at || job.progress < 3) return null;
  const elapsed = Date.now() / 1000 - job.started_at;
  const remaining = (elapsed / job.progress) * (100 - job.progress);
  return Number.isFinite(remaining) && remaining > 0 ? remaining : null;
}

export function speakerLabel(speaker: string, names?: Record<string, string>): string {
  const custom = names?.[speaker];
  if (custom) return custom;
  const match = /(\d+)$/.exec(speaker);
  return match ? `S${Number(match[1]) + 1}` : speaker;
}

/** Human speaker names stored on the job (``meta.speaker_names``). */
export function speakerNamesFrom(job: Job): Record<string, string> | undefined {
  const raw: unknown = job.meta?.speaker_names;
  return raw && typeof raw === "object" ? (raw as Record<string, string>) : undefined;
}

/** Unique speakers in order of first appearance. */
export function speakerOrder(segments: Segment[]): string[] {
  const seen: string[] = [];
  for (const segment of segments) {
    if (segment.speaker && !seen.includes(segment.speaker)) {
      seen.push(segment.speaker);
    }
  }
  return seen;
}

export function speakerColor(speaker: string, order: string[]): string {
  const index = Math.max(0, order.indexOf(speaker));
  const hue = (index * 57 + 190) % 360;
  return `hsl(${hue} 75% 66%)`;
}

export function downloadText(filename: string, text: string, mime = "text/plain"): void {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function srtTime(seconds: number): string {
  const ms = Math.round((seconds % 1) * 1000);
  const total = Math.floor(seconds);
  const hh = String(Math.floor(total / 3600)).padStart(2, "0");
  const mm = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss},${String(ms).padStart(3, "0")}`;
}

export function toTxt(job: Job): string {
  const names = speakerNamesFrom(job);
  return job.segments
    .map((segment) =>
      segment.speaker ? `[${speakerLabel(segment.speaker, names)}] ${segment.text}` : segment.text,
    )
    .join("\n");
}

export function toSrt(job: Job): string {
  const names = speakerNamesFrom(job);
  return job.segments
    .map((segment, index) => {
      const prefix = segment.speaker ? `${speakerLabel(segment.speaker, names)}: ` : "";
      return `${index + 1}\n${srtTime(segment.start)} --> ${srtTime(segment.end)}\n${prefix}${segment.text}\n`;
    })
    .join("\n");
}

export function toJson(job: Job): string {
  return JSON.stringify(job, null, 2);
}

export function baseName(name: string): string {
  return name.replace(/\.[^.]+$/, "");
}
