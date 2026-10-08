import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { IconMic } from "../components/Icons";
import { Card, EmptyState, ErrorBanner } from "../components/ui";
import { fmtClock, fmtTimestamp, speakerColor } from "../format";
import { useJobStream, useNow, usePolling } from "../hooks";
import type { LiveTrack } from "../live/capture";
import {
  clearFinishedLiveSession,
  getLiveSession,
  startLiveSession,
  stopLiveSession,
  subscribeLiveSession,
} from "../live/session";
import type { Job, LiveSessionInfo } from "../types";

const LIVE_SPEAKERS = ["Они", "Вы"];
const LANGUAGES = [
  { value: "", label: "Авто" },
  { value: "ru", label: "Русский" },
  { value: "en", label: "English" },
];

export function LivePage() {
  const [mic, setMic] = useState(true);
  const [system, setSystem] = useState(true);
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reprocessBusy, setReprocessBusy] = useState(false);
  // Запись живёт в модульном сторе: переживает переходы по вкладкам сервиса.
  const session = useSyncExternalStore(subscribeLiveSession, getLiveSession);
  const active = session.active;
  const activeJobId = active?.jobId ?? null;
  const lastJobId = session.lastJobId;
  const levels = active?.levels ?? { mic: 0, system: 0 };

  const { data: status } = usePolling(() => api.liveStatus(), 3000);
  const { data: engines } = usePolling(() => api.engines(), 60000);
  const { data: config } = usePolling(() => api.config(), 60000);
  const stream = useJobStream(activeJobId ?? undefined);
  const now = useNow(1000);
  const { data: finishedJob } = usePolling(
    () => (lastJobId ? api.getJob(lastJobId) : Promise.resolve(null)),
    3000,
  );

  const mySession = status?.sessions.find((item) => item.job_id === activeJobId) ?? null;
  const otherSessions = (status?.sessions ?? []).filter((item) => item.job_id !== activeJobId);
  const insecure = !window.isSecureContext;

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
    const tracks: LiveTrack[] = [];
    if (mic) tracks.push("mic");
    if (system) tracks.push("system");
    if (tracks.length === 0) {
      setActionError("Выберите хотя бы один источник");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await startLiveSession({ tracks, language: language || null });
    } catch (err) {
      setActionError(`Не удалось начать запись: ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!active) return;
    setBusy(true);
    setActionError(null);
    try {
      await stopLiveSession();
    } catch {
      /* сервер завершит сессию сам */
    } finally {
      setBusy(false);
    }
  };

  const errors = [
    actionError,
    session.closedReason ? `Сессия завершена: ${session.closedReason}` : null,
    stream.error,
  ].filter(Boolean);

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Live</h1>
          <p className="mt-1 text-sm text-muted">
            Запись из браузера: микрофон («Вы») и звук системы («Они») — с распознаванием на лету.
          </p>
        </div>
        {activeJobId && (
          <span className="inline-flex items-center gap-2 rounded-lg border border-warn/40 px-3 py-2 text-sm text-warn">
            <span className="animate-pulse-soft size-2 rounded-full bg-warn" />
            Запись идёт
          </span>
        )}
      </header>

      {errors.map((message) => (
        <ErrorBanner key={message} message={message as string} />
      ))}
      {otherSessions.length > 0 && (
        <div className="rounded-lg border border-edge bg-surface px-3 py-2 text-xs text-muted">
          Сейчас идут ещё записи: {otherSessions.length}. Распознавание общее — текст может
          появляться с задержкой.
        </div>
      )}

      {activeJobId ? (
        <ActiveSession
          job={stream.job}
          session={mySession}
          startedAt={active?.startedAt ?? null}
          stream={stream}
          levels={levels}
          now={now}
          busy={busy}
          onStop={() => void stop()}
        />
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
            <button
              onClick={() => clearFinishedLiveSession()}
              className="rounded-lg bg-gradient-to-r from-accent2 to-accent px-4 py-2 text-sm font-medium text-bg hover:opacity-90"
            >
              Начать новую запись
            </button>
          </div>
        </Card>
      ) : (
        <StartPanel
          mic={mic}
          system={system}
          onMic={setMic}
          onSystem={setSystem}
          language={language}
          onLanguage={setLanguage}
          onStart={() => void start()}
          busy={busy}
          insecure={insecure}
          othersRecording={otherSessions.length}
        />
      )}
    </div>
  );
}

function StartPanel({
  mic,
  system,
  onMic,
  onSystem,
  language,
  onLanguage,
  onStart,
  busy,
  insecure,
  othersRecording,
}: {
  mic: boolean;
  system: boolean;
  onMic: (value: boolean) => void;
  onSystem: (value: boolean) => void;
  language: string;
  onLanguage: (value: string) => void;
  onStart: () => void;
  busy: boolean;
  insecure: boolean;
  othersRecording: number;
}) {
  const sources = [
    {
      track: "mic",
      checked: mic,
      toggle: () => onMic(!mic),
      title: "Микрофон — «Вы»",
      hint: "Микрофон этого компьютера — браузер попросит разрешение",
    },
    {
      track: "system",
      checked: system,
      toggle: () => onSystem(!system),
      title: "Звук системы — «Они»",
      hint: "Созвон, видео, вкладка: в окне выбора отметьте «Поделиться звуком» (Chrome/Edge)",
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
              checked={source.checked}
              onChange={source.toggle}
              className="mt-0.5 size-4 accent-cyan-400"
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{source.title}</span>
              <span className="block text-xs text-muted">{source.hint}</span>
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
        {insecure && (
          <div className="rounded-lg border border-warn/40 p-3 text-xs text-warn">
            Страница открыта не по HTTPS — браузер не даст доступ к микрофону на этом адресе.
            Откройте веб «Стенографа» по https:// (в локальной сети) или на localhost.
          </div>
        )}
        <button
          onClick={onStart}
          disabled={busy || insecure}
          className="mt-1 inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-accent2 to-accent px-5 py-2.5 text-sm font-medium text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <IconMic className="size-4" />
          {busy ? "Запускаем…" : "Начать запись"}
        </button>
        {othersRecording > 0 && (
          <p className="text-xs text-muted">
            Сейчас идут ещё записи: {othersRecording}. Начинать можно — распознавание встанет в
            общую очередь, первые фразы появятся чуть позже.
          </p>
        )}
      </Card>

      <Card title="Как это работает" bodyClassName="flex flex-col gap-3 p-4 text-sm text-muted">
        <p>
          Запись идёт из этого браузера: микрофон и, по желанию, звук системы. Аудио уходит
          на сервер «Стенографа» и распознаётся на ходу моделью whisper-turbo — устойчивый
          текст появляется с задержкой 1–2 с, черновик фразы показан серым.
        </p>
        <p>
          Записывать могут несколько человек одновременно. Распознавание идёт через общую
          очередь сервера: при нескольких записях очередь достаётся каждой по кругу, поэтому
          чужой текст может появиться раньше вашего, а ваша запись ничего не теряет — она
          просто догоняет следом. Задержка и позиция в очереди видны над транскриптом.
          Запись продолжается, даже если вы уйдёте на другие страницы сервиса (Задачи,
          Дашборд) — вернитесь на Live, чтобы следить за ней и остановить.
        </p>
        <p>
          Спикеры разделяются по источникам: собеседники из созвона («Они») и ваш микрофон
          («Вы»). Двух собеседников внутри системного звука live-режим не различает —
          диаризацию сделает точный движок при повторной обработке.
        </p>
        <p>
          Аудио обеих дорожек сохраняется на сервере. После остановки запись автоматически
          уходит на повторную обработку точным движком (moss): итоговая задача — с реальными
          спикерами; при отключённом автозапуске есть кнопка «Улучшить» на странице задачи.
        </p>
      </Card>
    </div>
  );
}

function ActiveSession({
  job,
  session,
  startedAt,
  stream,
  levels,
  now,
  busy,
  onStop,
}: {
  job: Job | null;
  session: LiveSessionInfo | null;
  startedAt: number | null;
  stream: ReturnType<typeof useJobStream>;
  levels: Record<string, number>;
  now: number;
  busy: boolean;
  onStop: () => void;
}) {
  const elapsed = startedAt != null ? Math.max(0, now / 1000 - startedAt) : 0;
  const transcriptionState = session
    ? session.transcribing
      ? "распознавание идёт"
      : session.queue_position != null
        ? `в очереди на распознавание (№${session.queue_position})`
        : "распознавание успевает"
    : null;
  const lagHint =
    session && session.lag_sec >= 2 ? `задержка ≈ ${Math.round(session.lag_sec)} с` : null;
  const partials = LIVE_SPEAKERS.map((speaker) => [speaker, stream.partials[speaker]] as const).filter(
    (entry) => entry[1],
  );
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const partialText = partials.map(([, text]) => text).join("|");

  useEffect(() => {
    const element = scrollRef.current;
    if (!element || !pinnedRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [stream.segments.length, partialText]);

  const trackScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    pinnedRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
  };

  return (
    <>
      <Card bodyClassName="flex flex-col gap-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <span className="tabular text-3xl font-semibold">{fmtClock(elapsed)}</span>
            <div className="flex flex-col text-xs text-muted">
              <span>источник: браузер</span>
              <span>язык: {job?.language ?? "авто"}</span>
              {transcriptionState && (
                <span>
                  {transcriptionState}
                  {lagHint ? ` · ${lagHint}` : ""}
                </span>
              )}
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
              level={speaker === "Вы" ? levels.mic ?? 0 : levels.system ?? 0}
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
          <div
            ref={scrollRef}
            onScroll={trackScroll}
            className="flex max-h-[60vh] flex-col gap-2.5 overflow-y-auto"
          >
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
