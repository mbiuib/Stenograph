import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { IconChevron, IconOpen, IconTrash, IconX } from "../components/Icons";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatusBadge } from "../components/ui";
import { fmtClock, fmtDateTime, plural } from "../format";
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
  { key: "analysis", label: "Резюме/протоколы" },
];

/** Родитель задачи: улучшения/анализы ссылаются через «parent», повторы файлов — через «retry_of». */
function parentOf(job: Job): string | null {
  const parent = job.meta.parent;
  if (typeof parent === "string" && parent) return parent;
  const retry = job.meta.retry_of;
  if (typeof retry === "string" && retry) return retry;
  return null;
}

/** Человекочитаемый тип: у анализа — резюме/протокол, у файлового повтора — повтор. */
function kindLabel(job: Job): string {
  if (job.kind === "analysis") {
    return job.meta.analysis_type === "protocol" ? "Протокол" : "Резюме";
  }
  if (job.meta.retry_of) return "Повторная транскрибация";
  return KIND_LABELS[job.kind] ?? job.kind;
}

export function JobsPage() {
  const [tab, setTab] = useState("");
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const { data: jobs, error } = usePolling(() => api.listJobs(tab || undefined, 300), 3000);
  const navigate = useNavigate();

  const filtered = (jobs ?? []).filter(
    (job) =>
      (!kind || job.kind === kind) &&
      job.source_name.toLowerCase().includes(query.trim().toLowerCase()),
  );

  // Дерево: подзадачи (улучшения, повторы, резюме/протоколы) вкладываются в свои задачи.
  const byId = new Map(filtered.map((job) => [job.id, job]));
  const children = new Map<string, Job[]>();
  const roots: Job[] = [];
  for (const job of filtered) {
    const parentId = parentOf(job);
    if (parentId && parentId !== job.id && byId.has(parentId)) {
      const bucket = children.get(parentId);
      if (bucket) bucket.push(job);
      else children.set(parentId, [job]);
    } else {
      roots.push(job); // родителя нет в выборке — показываем как обычную задачу
    }
  }
  for (const bucket of children.values()) bucket.reverse(); // подзадачи — от старой к новой

  const toggleExpand = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

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

  const renderJob = (job: Job, depth: number): ReactNode => {
    if (depth > 6) return null; // защита от зацикленных связей
    const kids = children.get(job.id) ?? [];
    const hasKids = kids.length > 0;
    const open = hasKids && expanded.has(job.id);
    return (
      <li key={job.id}>
        <div
          className="flex cursor-pointer flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 hover:bg-surface2/40"
          onClick={() => navigate(`/jobs/${job.id}`)}
          title={hasKids ? "Открыть задачу · подзадачи — стрелкой" : `ID: ${job.id}`}
        >
          <div className="flex min-w-0 flex-1 basis-52 items-center gap-1.5">
            {hasKids ? (
              <button
                type="button"
                aria-label={open ? "Свернуть подзадачи" : "Показать подзадачи"}
                onClick={(event) => {
                  event.stopPropagation();
                  toggleExpand(job.id);
                }}
                className={`shrink-0 rounded p-0.5 text-muted transition-transform hover:text-ink ${
                  open ? "rotate-90" : ""
                }`}
              >
                <IconChevron className="size-4" />
              </button>
            ) : (
              <span aria-hidden className="size-5 shrink-0" />
            )}
            <div className="min-w-0 flex-1">
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
                {kindLabel(job)} · {fmtDateTime(job.created_at)}
                {hasKids && (
                  <>
                    {" · "}
                    {kids.length} {plural(kids.length, "подзадача", "подзадачи", "подзадач")}
                  </>
                )}
              </span>
            </div>
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
          <div className="flex items-center gap-1" onClick={(event) => event.stopPropagation()}>
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
        </div>
        {open && (
          <ul className="ml-10 divide-y divide-edge/60 border-l border-edge/50">
            {kids.map((kid) => renderJob(kid, depth + 1))}
          </ul>
        )}
      </li>
    );
  };

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Задачи</h1>
          <p className="mt-1 text-sm text-muted">
            История и очередь обработки. Подзадачи (улучшения, повторы, резюме и протоколы) вложены
            в свои задачи и раскрываются стрелкой; клик по задаче открывает её.
          </p>
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
          <ul className="divide-y divide-edge/60">{roots.map((job) => renderJob(job, 0))}</ul>
        )}
      </Card>
    </div>
  );
}
