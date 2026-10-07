import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { IconPlay, IconTrash, IconX } from "../components/Icons";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatusBadge } from "../components/ui";
import { fmtClock, fmtRelative } from "../format";
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

export function JobsPage() {
  const [tab, setTab] = useState("");
  const [query, setQuery] = useState("");
  const { data: jobs, error } = usePolling(() => api.listJobs(tab || undefined, 200), 3000);
  const navigate = useNavigate();

  const filtered = (jobs ?? []).filter((job) =>
    job.source_name.toLowerCase().includes(query.trim().toLowerCase()),
  );

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
                <button
                  className="min-w-0 flex-1 basis-52 text-left"
                  onClick={() => navigate(`/jobs/${job.id}`)}
                >
                  <span className="block truncate text-sm font-medium">{job.source_name}</span>
                  <span className="text-xs text-muted">
                    {job.id} · {fmtRelative(job.created_at)}
                  </span>
                </button>
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
                    title="Открыть"
                    onClick={() => navigate(`/jobs/${job.id}`)}
                    className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-ink"
                  >
                    <IconPlay className="size-4" />
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
