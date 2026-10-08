import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { IconOpen, IconTrash, IconX } from "../components/Icons";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatusBadge } from "../components/ui";
import { fmtClock, fmtDateTime } from "../format";
import { usePolling } from "../hooks";
import type { Job } from "../types";

const TABS = [
  { key: "", label: "Все" },
  { key: "queued", label: "В очереди" },
  { key: "running", label: "В работе" },
  { key: "done", label: "Готово" },
  { key: "error", label: "Ошибки" },
  { key: "cancelled", label: "Отменённые" },
];

const KIND_LABELS: Record<string, string> = {
  file: "Файл",
  live: "Live",
  jitsi: "Jitsi",
  reprocess: "Улучшение",
  analysis: "Анализ",
};

const KINDS = [
  { key: "", label: "Все типы" },
  { key: "file", label: "Файлы" },
  { key: "live", label: "Live" },
  { key: "jitsi", label: "Jitsi" },
  { key: "reprocess", label: "Улучшения" },
  { key: "analysis", label: "Анализы" },
];

export function JobsPage() {
  const [tab, setTab] = useState("");
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const { data: jobs, error } = usePolling(() => api.listJobs(tab || undefined, 200), 3000);
  const navigate = useNavigate();

  const filtered = (jobs ?? []).filter(
    (job) =>
      (!kind || job.kind === kind) &&
      job.source_name.toLowerCase().includes(query.trim().toLowerCase()),
  );

  const commitRename = async (job: Job) => {
    const value = editValue.trim();
    setEditingId(null);
    if (!value || value === job.source_name) return;
    await api.updateJob(job.id, { source_name: value }).catch(() => {});
  };

  const onCancel = (id: string) => void api.cancelJob(id).catch(() => {});
  const onDelete = async (job: Job) => {
    if (!window.confirm(`Удалить задачу «${job.source_name}»?`)) return;
    await api.deleteJob(job.id).catch(() => {});
  };

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Задачи</h1>
          <p className="mt-1 text-sm text-muted">История и очередь обработки.</p>
        </div>
        <Link
          to="/jobs/new"
          className="rounded-lg bg-gradient-to-r from-accent2 to-accent px-4 py-2 text-sm font-medium text-bg hover:opacity-90"
        >
          Новая задача
        </Link>
      </header>

      {error && <ErrorBanner message={`Не удалось получить список: ${error}`} />}

      <div className="flex flex-wrap items-center gap-2">
        {TABS.map((item) => (
          <button
            key={item.key}
            onClick={() => setTab(item.key)}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${
              tab === item.key
                ? "border-accent/50 bg-surface2 text-ink"
                : "border-edge text-muted hover:text-ink"
            }`}
          >
            {item.label}
          </button>
        ))}
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Поиск по имени…"
          className="ml-auto w-56 rounded-lg border border-edge bg-surface px-3 py-1.5 text-sm outline-none placeholder:text-muted/60 focus:border-accent/50"
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">Тип:</span>
        {KINDS.map((item) => (
          <button
            key={item.key}
            onClick={() => setKind(item.key)}
            className={`rounded-lg border px-3 py-1 text-xs transition-colors ${
              kind === item.key
                ? "border-accent/50 bg-surface2 text-ink"
                : "border-edge text-muted hover:text-ink"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      <Card bodyClassName="p-0">
        {filtered.length === 0 ? (
          <EmptyState title="Ничего не найдено" hint="Измените фильтр или загрузите файл." />
        ) : (
          <ul className="divide-y divide-edge/60">
            {filtered.map((job) => (
              <li
                key={job.id}
                className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 hover:bg-surface2/40"
              >
                <div
                  className="min-w-0 flex-1 basis-52 cursor-pointer text-left"
                  onClick={() => navigate(`/jobs/${job.id}`)}
                  title={`ID: ${job.id}`}
                >
                  {editingId === job.id ? (
                    <input
                      autoFocus
                      value={editValue}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => setEditValue(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void commitRename(job);
                        if (event.key === "Escape") {
                          setEditValue(job.source_name);
                          setEditingId(null);
                        }
                      }}
                      onBlur={() => void commitRename(job)}
                      className="w-full rounded-md border border-edge bg-surface px-2 py-0.5 text-sm outline-none focus:border-accent/50"
                    />
                  ) : (
                    <span
                      className="block truncate text-sm font-medium"
                      title="Двойной клик — переименовать"
                      onDoubleClick={(event) => {
                        event.stopPropagation();
                        setEditValue(job.source_name);
                        setEditingId(job.id);
                      }}
                    >
                      {job.source_name}
                    </span>
                  )}
                  <span className="text-xs text-muted">
                    {KIND_LABELS[job.kind] ?? job.kind} · {fmtDateTime(job.created_at)}
                  </span>
                </div>
                <div className="w-28">
                  {job.status === "running" ? (
                    <div className="flex flex-col gap-1">
                      <ProgressBar value={job.progress} />
                      <span className="tabular text-[11px] text-muted">{job.progress}%</span>
                    </div>
                  ) : (
                    <StatusBadge status={job.status} />
                  )}
                </div>
                <div className="hidden w-44 gap-2 md:flex">
                  {job.meta.engine != null && <Chip>{String(job.meta.engine)}</Chip>}
                  {job.meta.speakers && job.meta.speakers.length > 0 && (
                    <Chip>{job.meta.speakers.length} спк.</Chip>
                  )}
                </div>
                <span className="tabular w-16 text-right text-xs text-muted">
                  {fmtClock(job.meta.duration)}
                </span>
                <div className="flex items-center gap-1">
                  <button
                    title="Открыть задачу"
                    onClick={() => navigate(`/jobs/${job.id}`)}
                    className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-ink"
                  >
                    <IconOpen className="size-4" />
                  </button>
                  {(job.status === "running" || job.status === "queued") && (
                    <button
                      title="Отменить"
                      onClick={() => onCancel(job.id)}
                      className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-warn"
                    >
                      <IconX className="size-4" />
                    </button>
                  )}
                  <button
                    title="Удалить"
                    onClick={() => void onDelete(job)}
                    className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-err"
                  >
                    <IconTrash className="size-4" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
