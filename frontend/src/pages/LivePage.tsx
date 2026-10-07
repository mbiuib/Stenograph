import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { IconMic } from "../components/Icons";
import { Card, EmptyState, ErrorBanner } from "../components/ui";
import { fmtClock, fmtTimestamp, speakerColor } from "../format";
import { useJobStream, useNow, usePolling } from "../hooks";
import type { Job, LiveDevices } from "../types";

const LIVE_SPEAKERS = ["Они", "Вы"];
const LANGUAGES = [
  { value: "", label: "Авто" },
  { value: "ru", label: "Русский" },
  { value: "en", label: "English" },
];

export function LivePage() {
  const { data: status, error: statusError } = usePolling(() => api.liveStatus(), 2000);
  const { data: devices } = usePolling(() => api.liveDevices(), 60000);
  const { data: engines } = usePolling(() => api.engines(), 60000);
  const { data: config } = usePolling(() => api.config(), 60000);
  const [tracks, setTracks] = useState<Record<string, boolean>>({ system: true, mic: true });
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [lastJobId, setLastJobId] = useState<string | null>(null);
  const [reprocessBusy, setReprocessBusy] = useState(false);

  const activeJob = status?.active && status.job ? status.job : null;
  const stream = useJobStream(activeJob?.id);
  const now = useNow(1000);
  const { data: finishedJob } = usePolling(
    () => (lastJobId ? api.getJob(lastJobId) : Promise.resolve(null)),
    3000,
  );

  const improve = async () => {
    if (!lastJobId) return;
    setReprocessBusy(true);
    setActionError(null);
    try {
      await api.reprocessJob(lastJobId, engines?.default);
    } catch (err) {
      setActionError(`Не удалось запустить улучшение: ${(err as Error).message}`);
    } finally {
      setReprocessBusy(false);
    }
  };

  const start = async () => {
    const selected = Object.entries(tracks)
      .filter(([, enabled]) => enabled)
      .map(([track]) => track);
    if (selected.length === 0) {
      setActionError("Выберите хотя бы один источник");
      return;
    }
    setBusy(true);
    setActionError(null);
    setLastJobId(null);
    try {
      await api.liveStart(selected, language || undefined);
    } catch (err) {
      setActionError(`Не удалось начать запись: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    setActionError(null);
    try {
      const job = await api.liveStop();
      setLastJobId(job.id);
    } catch (err) {
      setActionError(`Не удалось остановить запись: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const errors = [statusError, stream.error, actionError].filter(Boolean);

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Live</h1>
          <p className="mt-1 text-sm text-muted">
            Распознавание в реальном времени: системный звук («Они») и микрофон («Вы»).
          </p>
        </div>
        {activeJob && (
          <span className="inline-flex items-center gap-2 rounded-lg border border-warn/40 px-3 py-2 text-sm text-warn">
            <span className="animate-pulse-soft size-2 rounded-full bg-warn" />
            Запись идёт
          </span>
        )}
      </header>

      {errors.map((message) => (
        <ErrorBanner key={message} message={message as string} />
      ))}
      {status && !status.supported && (
        <ErrorBanner message="WASAPI-захват недоступен на этой системе — live-режим выключен." />
      )}

      {activeJob ? (
        <ActiveSession job={activeJob} stream={stream} now={now} busy={busy} onStop={() => void stop()} />
      ) : lastJobId ? (
        <Card bodyClassName="p-6">
          <div className="flex flex-col items-start gap-3">
            <p className="text-sm">Сессия завершена — черновой транскрипт сохранён.</p>
            {finishedJob?.meta.reprocess_job ? (
              <>
                <p className="text-sm text-muted">
                  Улучшение записи запущено — точный текст с реальными спикерами появится в
                  отдельной задаче.
                </p>
                <Link
                  to={`/jobs/${finishedJob.meta.reprocess_job}`}
                  className="rounded-lg bg-gradient-to-r from-accent2 to-accent px-4 py-2 text-sm font-medium text-bg hover:opacity-90"
                >
                  Открыть улучшенную версию
                </Link>
              </>
            ) : finishedJob?.meta.audio ? (
              <>
                <p className="text-sm text-muted">
                  {config?.live_auto_reprocess
                    ? "Улучшение записи запускается автоматически…"
                    : `Перепрогнать запись с нуля движком ${engines?.default ?? "moss"} — точнее и с реальными спикерами.`}
                </p>
                <button
                  onClick={() => void improve()}
                  disabled={reprocessBusy}
                  className="rounded-lg bg-gradient-to-r from-accent2 to-accent px-4 py-2 text-sm font-medium text-bg hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {reprocessBusy ? "Запускаем…" : `Улучшить через ${engines?.default ?? "moss"}`}
                </button>
              </>
            ) : null}
            <Link
              to={`/jobs/${lastJobId}`}
              className="text-sm text-muted underline-offset-4 hover:text-ink hover:underline"
            >
              Открыть черновой транскрипт
            </Link>
          </div>
        </Card>
      ) : (
        <StartPanel
          devices={devices}
          tracks={tracks}
          onToggle={(track) => setTracks((prev) => ({ ...prev, [track]: !prev[track] }))}
          language={language}
          onLanguage={setLanguage}
          onStart={() => void start()}
          busy={busy}
          supported={status ? status.supported : true}
        />
      )}
    </div>
  );
}

function StartPanel({
  devices,
  tracks,
  onToggle,
  language,
  onLanguage,
  onStart,
  busy,
  supported,
}: {
  devices: LiveDevices | null;
  tracks: Record<string, boolean>;
  onToggle: (track: string) => void;
  language: string;
  onLanguage: (value: string) => void;
  onStart: () => void;
  busy: boolean;
  supported: boolean;
}) {
  const sources = [
    {
      track: "system",
      title: "Системный звук — «Они»",
      hint: devices?.devices.loopback
        ? `Loopback: ${devices.devices.loopback}`
        : "Всё, что воспроизводится на колонках: созвон, видео, плеер",
    },
    {
      track: "mic",
      title: "Микрофон — «Вы»",
      hint: devices?.devices.microphone
        ? `Устройство: ${devices.devices.microphone}`
        : "Микрофон по умолчанию (можно использовать наушники с гарнитурой)",
    },
  ];

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Источники записи" bodyClassName="flex flex-col gap-3 p-4">
        {sources.map((source) => (
          <label
            key={source.track}
            className="flex cursor-pointer items-start gap-3 rounded-lg border border-edge p-3 transition-colors hover:border-accent/40"
          >
            <input
              type="checkbox"
              checked={tracks[source.track] ?? false}
              onChange={() => onToggle(source.track)}
              className="mt-0.5 size-4 accent-cyan-400"
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{source.title}</span>
              <span className="block truncate text-xs text-muted">{source.hint}</span>
            </span>
          </label>
        ))}
        <label className="flex items-center justify-between gap-3 rounded-lg border border-edge p-3">
          <span className="text-sm font-medium">Язык</span>
          <select
            value={language}
            onChange={(event) => onLanguage(event.target.value)}
            className="rounded-lg border border-edge bg-surface px-3 py-1.5 text-sm outline-none focus:border-accent/50"
          >
            {LANGUAGES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={onStart}
          disabled={busy || !supported}
          className="mt-1 inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-accent2 to-accent px-5 py-2.5 text-sm font-medium text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <IconMic className="size-4" />
          {busy ? "Запускаем…" : "Начать запись"}
        </button>
      </Card>

      <Card title="Как это работает" bodyClassName="flex flex-col gap-3 p-4 text-sm text-muted">
        <p>
          Речь распознаётся по ходу записи моделью whisper-turbo: устойчивый текст
          появляется с задержкой 1–2 с, черновик текущей фразы показан серым.
        </p>
        <p>
          Спикеры разделяются по источникам: собеседники из созвона («Они») и ваш
          микрофон («Вы»). Двух собеседников внутри системного звука live-режим не
          различает — диаризацию сделает точный движок при повторной обработке.
        </p>
        <p>
          Аудио обеих дорожек сохраняется. После остановки запись автоматически
          уходит на повторную обработку точным движком (moss): итоговая задача —
          с реальными спикерами; при отключённом автозапуске есть кнопка
          «Улучшить» на странице задачи.
        </p>
      </Card>
    </div>
  );
}

function ActiveSession({
  job,
  stream,
  now,
  busy,
  onStop,
}: {
  job: Job;
  stream: ReturnType<typeof useJobStream>;
  now: number;
  busy: boolean;
  onStop: () => void;
}) {
  const elapsed = job.started_at != null ? Math.max(0, now / 1000 - job.started_at) : 0;
  const partials = LIVE_SPEAKERS.map((speaker) => [speaker, stream.partials[speaker]] as const).filter(
    (entry) => entry[1],
  );

  return (
    <>
      <Card bodyClassName="flex flex-col gap-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <span className="tabular text-3xl font-semibold">{fmtClock(elapsed)}</span>
            <div className="flex flex-col text-xs text-muted">
              <span>движок: {String(job.meta.engine ?? "whisper")}</span>
              <span>язык: {job.language ?? "авто"}</span>
            </div>
          </div>
          <button
            onClick={onStop}
            disabled={busy}
            className="rounded-lg border border-warn/40 px-4 py-2 text-sm text-warn transition-colors hover:bg-warn/10 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Останавливаем…" : "Остановить запись"}
          </button>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {LIVE_SPEAKERS.map((speaker) => (
            <Meter
              key={speaker}
              label={speaker}
              color={speakerColor(speaker, LIVE_SPEAKERS)}
              level={speaker === "Вы" ? stream.levels.mic ?? 0 : stream.levels.system ?? 0}
            />
          ))}
        </div>
      </Card>

      <Card title="Транскрипт" bodyClassName="p-4">
        {stream.segments.length === 0 && partials.length === 0 ? (
          <EmptyState
            title="Ожидаем речь…"
            hint="Проверьте, что звук идёт в выбранный источник — индикаторы выше должны двигаться."
          />
        ) : (
          <div className="flex max-h-[60vh] flex-col gap-2.5 overflow-y-auto">
            {stream.segments.map((segment) => (
              <div key={`${segment.index}-${segment.start}`} className="flex flex-col gap-0.5">
                <div className="flex items-center gap-2 text-xs text-muted">
                  <span
                    className="size-2 rounded-full"
                    style={{ background: speakerColor(segment.speaker ?? "", LIVE_SPEAKERS) }}
                  />
                  <span className="tabular">{fmtTimestamp(segment.start)}</span>
                  <span>{segment.speaker}</span>
                </div>
                <p className="text-sm leading-relaxed">{segment.text}</p>
              </div>
            ))}
            {partials.map(([speaker, text]) => (
              <p key={speaker} className="text-sm leading-relaxed text-muted italic">
                {speaker}: {text}
                <span className="animate-pulse-soft">▍</span>
              </p>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

function Meter({ label, color, level }: { label: string; color: string; level: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="size-2 shrink-0 rounded-full" style={{ background: color }} />
      <span className="w-7 shrink-0 text-xs text-muted">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
        <div
          className="h-full rounded-full transition-[width] duration-150"
          style={{ width: `${Math.min(100, Math.max(2, level * 500))}%`, background: color }}
        />
      </div>
    </div>
  );
}
