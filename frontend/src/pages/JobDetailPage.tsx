import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { IconCopy, IconDownload, IconPlay, IconTrash, IconX } from "../components/Icons";
import { Transcript } from "../components/Transcript";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatusBadge } from "../components/ui";
import {
  baseName,
  downloadText,
  etaSeconds,
  fmtClock,
  fmtDateTime,
  fmtRemaining,
  speakerColor,
  speakerLabel,
  speakerOrder,
  toJson,
  toSrt,
  toTxt,
} from "../format";
import { useJobStream, useNow, usePolling } from "../hooks";

const ACTION_CLASS =
  "flex items-center gap-2 rounded-lg border border-edge px-3 py-2 text-sm text-muted hover:text-ink";

export function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { job, segments, loading, error } = useJobStream(id);
  const now = useNow(1000);
  const navigate = useNavigate();
  const [copied, setCopied] = useState(false);
  const { data: engines } = usePolling(() => api.engines(), 60000);
  const [reprocessBusy, setReprocessBusy] = useState(false);
  const [reprocessError, setReprocessError] = useState<string | null>(null);

  const speakers = useMemo(() => speakerOrder(segments), [segments]);

  if (loading && !job) {
    return <p className="py-16 text-center text-sm text-muted">Загрузка…</p>;
  }
  if (error && !job) {
    return <ErrorBanner message={`Не удалось загрузить задачу: ${error}`} />;
  }
  if (!job) {
    return <EmptyState title="Задача не найдена" />;
  }

  const live = job.status === "running" || job.status === "queued";
  const eta = etaSeconds(job);
  const elapsed = job.started_at != null ? now / 1000 - job.started_at : null;

  const copyText = async () => {
    await navigator.clipboard.writeText(toTxt(job)).catch(() => {});
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const cancel = () => void api.cancelJob(job.id).catch(() => {});
  const remove = async () => {
    if (!window.confirm(`Удалить задачу «${job.source_name}»?`)) return;
    await api.deleteJob(job.id).catch(() => {});
    navigate("/jobs");
  };
  const improve = async () => {
    setReprocessBusy(true);
    setReprocessError(null);
    try {
      const child = await api.reprocessJob(job.id, engines?.default);
      navigate(`/jobs/${child.id}`);
    } catch (err) {
      setReprocessError(`Не удалось запустить улучшение: ${(err as Error).message}`);
      setReprocessBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-muted">
            <Link to="/jobs" className="hover:text-ink">
              ← Все задачи
            </Link>
          </p>
          <h1 className="mt-1 truncate text-xl font-semibold">{job.source_name}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <StatusBadge status={job.status} />
            {job.meta.engine != null && <Chip>движок: {String(job.meta.engine)}</Chip>}
            {job.language && <Chip>язык: {job.language}</Chip>}
            {job.meta.duration != null && <Chip>аудио: {fmtClock(Number(job.meta.duration))}</Chip>}
            {job.meta.processing_seconds != null && (
              <Chip>обработка: {fmtClock(Number(job.meta.processing_seconds))}</Chip>
            )}
            {speakers.length > 0 && <Chip>спикеров: {speakers.length}</Chip>}
            {job.kind === "reprocess" && <Chip>улучшение записи</Chip>}
            {job.kind === "reprocess" && job.meta.parent != null && (
              <Chip>
                <Link to={`/jobs/${job.meta.parent}`} className="hover:text-accent">
                  ← исходная Live-сессия
                </Link>
              </Chip>
            )}
            {job.meta.reprocess_job != null && (
              <Chip>
                <Link to={`/jobs/${job.meta.reprocess_job}`} className="hover:text-accent">
                  улучшенная версия →
                </Link>
              </Chip>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          {!live && job.kind === "live" && job.meta.audio && !job.meta.reprocess_job && (
            <button
              onClick={() => void improve()}
              disabled={reprocessBusy}
              className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <IconPlay className="size-4" />
              {reprocessBusy ? "Запускаем…" : `Улучшить через ${engines?.default ?? "moss"}`}
            </button>
          )}
          {live && (
            <button
              onClick={cancel}
              className="flex items-center gap-2 rounded-lg border border-warn/40 px-3 py-2 text-sm text-warn hover:bg-warn/10"
            >
              <IconX className="size-4" /> Отменить
            </button>
          )}
          {!live && (
            <button
              onClick={() => void remove()}
              className="flex items-center gap-2 rounded-lg border border-edge px-3 py-2 text-sm text-muted hover:border-err/50 hover:text-err"
            >
              <IconTrash className="size-4" /> Удалить
            </button>
          )}
        </div>
      </header>

      {live && (
        <Card bodyClassName="p-4">
          <div className="flex flex-col gap-2">
            <ProgressBar value={job.progress} />
            <div className="flex items-center justify-between gap-3 text-xs text-muted">
              <span className="min-w-0 truncate">
                {job.progress}% · {job.message || "…"}
              </span>
              <span className="tabular shrink-0">
                {elapsed != null && `прошло ${fmtClock(elapsed)}`}
                {eta != null && ` · осталось ${fmtRemaining(eta)}`}
              </span>
            </div>
          </div>
        </Card>
      )}

      {job.status === "error" && job.error && <ErrorBanner message={job.error} />}
      {reprocessError && <ErrorBanner message={reprocessError} />}

      {job.status === "done" && (
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => void copyText()} className={ACTION_CLASS}>
            <IconCopy className="size-4" /> {copied ? "Скопировано" : "Копировать текст"}
          </button>
          <button
            onClick={() => downloadText(`${baseName(job.source_name)}.txt`, toTxt(job))}
            className={ACTION_CLASS}
          >
            <IconDownload className="size-4" /> TXT
          </button>
          <button
            onClick={() => downloadText(`${baseName(job.source_name)}.srt`, toSrt(job), "application/x-subrip")}
            className={ACTION_CLASS}
          >
            <IconDownload className="size-4" /> SRT
          </button>
          <button
            onClick={() => downloadText(`${baseName(job.source_name)}.json`, toJson(job), "application/json")}
            className={ACTION_CLASS}
          >
            <IconDownload className="size-4" /> JSON
          </button>
        </div>
      )}

      {speakers.length > 0 && (
        <div className="flex flex-wrap gap-2 text-xs">
          {speakers.map((speaker) => (
            <span
              key={speaker}
              className="inline-flex items-center gap-1.5 rounded-md border border-edge bg-surface px-2 py-1"
            >
              <span className="size-2 rounded-full" style={{ background: speakerColor(speaker, speakers) }} />
              {speakerLabel(speaker)}
            </span>
          ))}
        </div>
      )}

      <Card title="Транскрипт" bodyClassName="p-4">
        {segments.length > 0 ? (
          <Transcript segments={segments} speakers={speakers} live={job.status === "running"} />
        ) : job.status === "done" && job.text ? (
          <pre className="max-h-[60vh] overflow-y-auto text-sm leading-relaxed whitespace-pre-wrap">
            {job.text}
          </pre>
        ) : (
          <EmptyState title={live ? "Ожидаем первые сегменты…" : "Транскрипта нет"} />
        )}
      </Card>

      <Card
        title="Метаданные"
        bodyClassName="grid grid-cols-2 gap-x-6 gap-y-3 p-4 text-sm md:grid-cols-3"
      >
        <MetaRow label="Задача" value={job.id} />
        <MetaRow label="Создана" value={fmtDateTime(job.created_at)} />
        <MetaRow label="Завершена" value={fmtDateTime(job.finished_at)} />
        <MetaRow label="Файл" value={job.source_name} />
        {job.meta.format != null && <MetaRow label="Формат" value={String(job.meta.format)} />}
        {job.meta.size != null && (
          <MetaRow label="Размер" value={`${(Number(job.meta.size) / 1048576).toFixed(1)} МБ`} />
        )}
      </Card>
    </div>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-[11px] tracking-wide text-muted uppercase">{label}</p>
      <p className="tabular truncate" title={value}>
        {value}
      </p>
    </div>
  );
}
