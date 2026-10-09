import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { IconCopy, IconDownload, IconPencil, IconRefresh, IconTrash, IconX } from "../components/Icons";
import { Markdown } from "../components/Markdown";
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
import type { AnalysisKind } from "../types";

const ACTION_CLASS =
  "flex items-center gap-2 rounded-lg border border-edge px-3 py-2 text-sm text-muted hover:text-ink";

export function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { job, segments, loading, error } = useJobStream(id);
  const now = useNow(1000);
  const navigate = useNavigate();
  const [copied, setCopied] = useState(false);
  const { data: engines } = usePolling(() => api.engines(), 60000);
  const engineOptions = engines?.available ?? [];
  const [improveEngine, setImproveEngine] = useState("");
  useEffect(() => {
    // По умолчанию — движок улучшений из настроек (MEETSCRIBE_REPROCESS_ENGINE),
    // иначе общий движок сервиса.
    if (!improveEngine && engines) setImproveEngine(engines.improve_default ?? engines.default);
  }, [engines, improveEngine]);
  const { data: appConfig } = usePolling(() => api.config(), 120000);
  const [reprocessBusy, setReprocessBusy] = useState(false);
  const [reprocessError, setReprocessError] = useState<string | null>(null);
  const [retryBusy, setRetryBusy] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [nameValue, setNameValue] = useState("");
  const [nameOverride, setNameOverride] = useState<string | null>(null);
  const [namesOverride, setNamesOverride] = useState<Record<string, string> | null>(null);
  const [editingSpeaker, setEditingSpeaker] = useState<string | null>(null);
  const [speakerValue, setSpeakerValue] = useState("");
  const [stopping, setStopping] = useState(false);

  const [analysisText, setAnalysisText] = useState<Partial<Record<AnalysisKind, string>>>({});
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [analysisBusy, setAnalysisBusy] = useState<{
    type: AnalysisKind;
    jobId: string;
    progress: number;
    message: string;
  } | null>(null);

  const analysisMeta = job?.meta.analysis ?? {};
  const llmInfo = appConfig?.llm;

  // Подхватываем незавершённый анализ после перезагрузки страницы.
  useEffect(() => {
    if (!job || job.kind === "analysis" || analysisBusy) return;
    const pending = (["protocol", "summary"] as AnalysisKind[]).find(
      (type) => analysisMeta[type]?.job_id && !analysisMeta[type]?.text && !analysisText[type],
    );
    if (!pending) return;
    const pendingId = analysisMeta[pending]!.job_id;
    let cancelled = false;
    void (async () => {
      try {
        const child = await api.getJob(pendingId);
        if (cancelled) return;
        if (child.status === "queued" || child.status === "running") {
          setAnalysisBusy({
            type: pending,
            jobId: pendingId,
            progress: child.progress,
            message: child.message,
          });
        } else if (child.status === "done" && child.text) {
          setAnalysisText((prev) => ({ ...prev, [pending]: child.text }));
        } else if (child.status === "error") {
          setAnalysisError(`Не удалось подготовить документ: ${child.error ?? "ошибка"}`);
        }
      } catch {
        /* страница просто останется без результата */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [job, analysisBusy, analysisText, analysisMeta]);

  // Поллим дочернюю задачу, пока идёт генерация.
  useEffect(() => {
    if (!analysisBusy) return;
    const { type, jobId } = analysisBusy;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const child = await api.getJob(jobId);
          if (child.status === "done" && child.text) {
            setAnalysisText((prev) => ({ ...prev, [type]: child.text }));
            setAnalysisBusy(null);
          } else if (child.status === "error" || child.status === "cancelled") {
            setAnalysisError(`Анализ не удался: ${child.error ?? "отменено"}`);
            setAnalysisBusy(null);
          } else {
            setAnalysisBusy((prev) =>
              prev && prev.jobId === jobId
                ? { ...prev, progress: child.progress, message: child.message }
                : prev,
            );
          }
        } catch {
          /* пропускаем такт */
        }
      })();
    }, 1500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisBusy?.jobId]);

  const startAnalysis = async (type: AnalysisKind) => {
    if (!job) return;
    setAnalysisError(null);
    try {
      const child = await api.analyzeJob(job.id, type);
      setAnalysisBusy({
        type,
        jobId: child.id,
        progress: child.progress,
        message: child.message,
      });
    } catch (err) {
      setAnalysisError(`Не удалось запустить анализ: ${(err as Error).message}`);
    }
  };

  const speakers = useMemo(() => speakerOrder(segments), [segments]);
  const speakerNames =
    namesOverride ?? ((job?.meta.speaker_names as Record<string, string> | undefined) ?? {});

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
    const value = job.kind === "analysis" ? job.text : toTxt(job);
    await navigator.clipboard.writeText(value).catch(() => {});
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const cancel = () => void api.cancelJob(job.id).catch(() => {});
  const stopLive = async () => {
    setStopping(true);
    setReprocessError(null);
    try {
      await api.liveStop(job.id);
    } catch (err) {
      setReprocessError(`Не удалось остановить запись: ${(err as Error).message}`);
    } finally {
      setStopping(false);
    }
  };
  const remove = async () => {
    if (!window.confirm(`Удалить задачу «${job.source_name}»?`)) return;
    await api.deleteJob(job.id).catch(() => {});
    navigate("/jobs");
  };
  const improve = async (targetId: string = job.id) => {
    setReprocessBusy(true);
    setReprocessError(null);
    try {
      const child = await api.reprocessJob(targetId, improveEngine || engines?.default);
      navigate(`/jobs/${child.id}`);
    } catch (err) {
      setReprocessError(`Не удалось запустить улучшение: ${(err as Error).message}`);
      setReprocessBusy(false);
    }
  };
  const retry = async () => {
    setRetryBusy(true);
    setRetryError(null);
    try {
      const next = await api.retryJob(job.id);
      navigate(`/jobs/${next.id}`);
    } catch (err) {
      setRetryError(`Не удалось запустить повторную обработку: ${(err as Error).message}`);
      setRetryBusy(false);
    }
  };
  const saveName = async () => {
    const value = nameValue.trim();
    setRenaming(false);
    const current = nameOverride ?? job.source_name;
    if (!value || value === current) return;
    try {
      await api.updateJob(job.id, { source_name: value });
      setNameOverride(value);
    } catch {
      /* имя останется прежним */
    }
  };
  const saveSpeaker = async (speaker: string) => {
    setEditingSpeaker(null);
    const value = speakerValue.trim();
    const next = { ...speakerNames };
    if (value) next[speaker] = value;
    else delete next[speaker];
    try {
      const updated = await api.updateJob(job.id, { speaker_names: next });
      setNamesOverride((updated.meta.speaker_names as Record<string, string> | undefined) ?? {});
    } catch {
      /* маппинг останется прежним */
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
          {renaming ? (
            <input
              autoFocus
              value={nameValue}
              onChange={(event) => setNameValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void saveName();
                if (event.key === "Escape") setRenaming(false);
              }}
              onBlur={() => void saveName()}
              className="mt-1 w-full max-w-xl rounded-lg border border-edge bg-surface px-3 py-1.5 text-xl font-semibold outline-none focus:border-accent/50"
            />
          ) : (
            <h1 className="mt-1 flex items-center gap-2 text-xl font-semibold">
              <span className="min-w-0 truncate">{nameOverride ?? job.source_name}</span>
              <button
                title="Переименовать"
                onClick={() => {
                  setNameValue(nameOverride ?? job.source_name);
                  setRenaming(true);
                }}
                className="rounded-md p-1 text-muted hover:bg-surface2 hover:text-ink"
              >
                <IconPencil className="size-4" />
              </button>
            </h1>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <StatusBadge status={job.status} />
            {job.created_at != null && <Chip>{fmtDateTime(job.created_at)}</Chip>}
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
            {job.meta.retry_of != null && <Chip>повторная обработка</Chip>}
            {job.meta.retry_of != null && (
              <Chip>
                <Link to={`/jobs/${job.meta.retry_of}`} className="hover:text-accent">
                  ← первая обработка
                </Link>
              </Chip>
            )}
            {job.kind === "analysis" && <Chip>анализ</Chip>}
            {job.kind === "analysis" && job.meta.parent != null && (
              <Chip>
                <Link to={`/jobs/${job.meta.parent}`} className="hover:text-accent">
                  ← исходная задача
                </Link>
              </Chip>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          {!live && (job.kind === "live" || job.kind === "jitsi") && job.meta.audio && (
            <>
              <EnginePicker
                value={improveEngine}
                options={engineOptions}
                onChange={setImproveEngine}
              />
              <button
                onClick={() => void improve()}
                disabled={reprocessBusy}
                className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <IconRefresh className="size-4" />
                {reprocessBusy ? "Запускаем…" : `Улучшить через ${improveEngine || "…"}`}
              </button>
            </>
          )}
          {job.kind === "reprocess" && job.meta.parent != null && (
            <>
              <EnginePicker
                value={improveEngine}
                options={engineOptions}
                onChange={setImproveEngine}
              />
              <button
                onClick={() => void improve(String(job.meta.parent))}
                disabled={reprocessBusy}
                className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <IconRefresh className="size-4" />
                {reprocessBusy ? "Запускаем…" : `Улучшить заново (${improveEngine || "…"})`}
              </button>
            </>
          )}
          {!live && job.kind === "file" && job.status !== "running" && job.status !== "queued" && (
            <button
              onClick={() => void retry()}
              disabled={retryBusy}
              className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <IconRefresh className="size-4" />
              {retryBusy ? "Запускаем…" : "Обработать заново"}
            </button>
          )}
          {live && job.kind === "live" && (
            <button
              onClick={() => void stopLive()}
              disabled={stopping}
              className="flex items-center gap-2 rounded-lg border border-warn/40 px-3 py-2 text-sm text-warn hover:bg-warn/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <IconX className="size-4" />
              {stopping ? "Останавливаем…" : "Остановить запись"}
            </button>
          )}
          {live && job.kind !== "live" && job.kind !== "jitsi" && (
            <button
              onClick={cancel}
              className="flex items-center gap-2 rounded-lg border border-warn/40 px-3 py-2 text-sm text-warn hover:bg-warn/10"
            >
              <IconX className="size-4" /> Отменить
            </button>
          )}
          {live && job.kind === "jitsi" && (
            <span className="flex items-center rounded-lg border border-edge px-3 py-2 text-xs text-muted">
              запись идёт через Jitsi — остановится с встречей
            </span>
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
      {retryError && <ErrorBanner message={retryError} />}

      {job.status === "done" && (
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => void copyText()} className={ACTION_CLASS}>
            <IconCopy className="size-4" /> {copied ? "Скопировано" : "Копировать текст"}
          </button>
          {job.kind === "analysis" ? (
            <button
              onClick={() =>
                downloadText(`${baseName(job.source_name)}.md`, job.text, "text/markdown")
              }
              className={ACTION_CLASS}
            >
              <IconDownload className="size-4" /> Markdown
            </button>
          ) : (
            <>
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
            </>
          )}
        </div>
      )}

      {job.kind !== "analysis" && (
        <Card
          title="Анализ встречи"
          action={
            llmInfo ? <span className="text-xs text-muted">LLM: {llmInfo.model}</span> : undefined
          }
          bodyClassName="flex flex-col gap-3 p-4"
        >
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => void startAnalysis("protocol")}
              disabled={analysisBusy != null || job.status !== "done"}
              className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {analysisBusy?.type === "protocol" ? "Составляем…" : "Составить протокол"}
            </button>
            <button
              onClick={() => void startAnalysis("summary")}
              disabled={analysisBusy != null || job.status !== "done"}
              className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-2 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {analysisBusy?.type === "summary" ? "Готовим…" : "Сделать резюме"}
            </button>
          </div>
          {analysisError && <ErrorBanner message={analysisError} />}
          {job.status !== "done" && (
            <p className="text-xs text-muted">Анализ станет доступен после завершения транскрибации.</p>
          )}
          {(["protocol", "summary"] as AnalysisKind[]).map((type) => {
            const entry = analysisMeta[type];
            const text = analysisText[type] ?? entry?.text;
            const busy = analysisBusy?.type === type;
            if (!text && !busy && !entry?.job_id) return null;
            return (
              <div key={type} className="rounded-lg border border-edge bg-surface2/40 px-4 py-3">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm font-medium">
                    {type === "protocol" ? "Протокол" : "Резюме"}
                  </p>
                  <div className="flex items-center gap-3 text-[11px] text-muted">
                    {entry?.model && !busy && <span>{entry.model}</span>}
                    {entry?.finished_at != null && !busy && (
                      <span>{fmtDateTime(entry.finished_at)}</span>
                    )}
                    {entry?.job_id && (
                      <Link to={`/jobs/${entry.job_id}`} className="hover:text-accent">
                        открыть
                      </Link>
                    )}
                  </div>
                </div>
                {busy ? (
                  <div className="flex flex-col gap-2 py-1">
                    <ProgressBar value={analysisBusy.progress} />
                    <p className="text-xs text-muted">
                      {analysisBusy.progress}% · {analysisBusy.message || "Генерируем…"}
                    </p>
                  </div>
                ) : text ? (
                  <Markdown text={text} />
                ) : (
                  <p className="text-xs text-muted">Готовим документ…</p>
                )}
              </div>
            );
          })}
          {!analysisMeta.protocol?.job_id &&
            !analysisMeta.summary?.job_id &&
            !analysisBusy &&
            job.status === "done" && (
              <p className="text-xs text-muted">
                Локальная LLM соберёт из транскрипта протокол с решениями и задачами либо
                краткое резюме. Текст не покидает машину.
              </p>
            )}
        </Card>
      )}

      {speakers.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {speakers.map((speaker) => {
            const color = speakerColor(speaker, speakers);
            if (editingSpeaker === speaker) {
              return (
                <input
                  key={speaker}
                  autoFocus
                  value={speakerValue}
                  onChange={(event) => setSpeakerValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void saveSpeaker(speaker);
                    if (event.key === "Escape") {
                      setSpeakerValue(speakerNames[speaker] ?? "");
                      setEditingSpeaker(null);
                    }
                  }}
                  onBlur={() => void saveSpeaker(speaker)}
                  placeholder={speakerLabel(speaker)}
                  className="w-36 rounded-md border border-edge bg-surface px-2 py-1 text-xs text-ink outline-none focus:border-accent/50"
                />
              );
            }
            return (
              <button
                key={speaker}
                title="Переименовать спикера"
                onClick={() => {
                  setSpeakerValue(speakerNames[speaker] ?? "");
                  setEditingSpeaker(speaker);
                }}
                className="inline-flex items-center gap-1.5 rounded-md border border-edge bg-surface px-2 py-1 hover:border-accent/50"
              >
                <span className="size-2 rounded-full" style={{ background: color }} />
                {speakerLabel(speaker, speakerNames)}
              </button>
            );
          })}
          <span className="text-muted/70">клик по спикеру — дать имя</span>
        </div>
      )}

      {job.kind === "analysis" ? (
        <Card
          title={job.meta.analysis_type === "summary" ? "Резюме встречи" : "Протокол встречи"}
          bodyClassName="p-4"
        >
          {job.text ? (
            <div className="max-h-[65vh] overflow-y-auto">
              <Markdown text={job.text} />
            </div>
          ) : live ? (
            <EmptyState title="Готовим документ…" hint={job.message || undefined} />
          ) : (
            <EmptyState title="Результата нет" />
          )}
        </Card>
      ) : (
        <Card title="Транскрипт" bodyClassName="p-4">
          {segments.length > 0 ? (
            <Transcript
              segments={segments}
              speakers={speakers}
              live={job.status === "running"}
              speakerNames={speakerNames}
            />
          ) : job.status === "done" && job.text ? (
            <pre className="max-h-[60vh] overflow-y-auto text-sm leading-relaxed whitespace-pre-wrap">
              {job.text}
            </pre>
          ) : (
            <EmptyState title={live ? "Ожидаем первые сегменты…" : "Транскрипта нет"} />
          )}
        </Card>
      )}

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

function EnginePicker({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  if (!value || options.length === 0) return null;
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      title="Движок для улучшения записи"
      className="rounded-lg border border-edge bg-surface px-2 py-2 text-sm text-ink outline-none focus:border-accent/50"
    >
      {options.map((name) => (
        <option key={name} value={name}>
          {name}
        </option>
      ))}
    </select>
  );
}
