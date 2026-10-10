/** Jitsi bridge meetings: live participants, counters and recent recordings. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, EmptyState, ErrorBanner, StatusBadge } from "../components/ui";
import { fmtClock, fmtDateTime, plural } from "../format";
import { useNow, usePolling } from "../hooks";
import type { JitsiMeeting } from "../types";

export function JitsiPage() {
  const [actionError, setActionError] = useState<string | null>(null);
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  const { data: status, error } = usePolling(() => api.jitsiStatus(), 2000);
  const { data: jobs } = usePolling(() => api.listJobs(undefined, 100), 5000);
  const now = useNow(1000);
  const meetings = status?.meetings ?? [];
  const recent = (jobs ?? []).filter((job) => job.kind === "jitsi").slice(0, 6);
  const transcribing = meetings.filter((meeting) => meeting.transcribe).length;

  const toggleTranscribe = async (meetingId: string, enabled: boolean) => {
    setActionError(null);
    try {
      await api.jitsiTranscribe(meetingId, enabled);
    } catch (err) {
      setActionError(`Не удалось переключить распознавание: ${(err as Error).message}`);
    }
  };

  const stopMeeting = async (meetingId: string) => {
    setActionError(null);
    setStoppingId(meetingId);
    try {
      await api.jitsiStop({ meetingId });
    } catch (err) {
      setActionError(`Не удалось остановить запись: ${(err as Error).message}`);
    } finally {
      setStoppingId(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Jitsi-встречи</h1>
        {meetings.length > 0 && (
          <span className="inline-flex items-center gap-2 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            <span className="relative flex size-2.5 shrink-0">
              <span className="absolute inline-flex size-2.5 animate-ping rounded-full bg-err opacity-60" />
              <span className="relative inline-flex size-2.5 rounded-full bg-err" />
            </span>
            {transcribing > 0 ? `Субтитры идут: ${transcribing}` : `Идут встречи: ${meetings.length}`}
          </span>
        )}
      </div>

      {error && <ErrorBanner message={error} />}
      {actionError && <ErrorBanner message={actionError} />}

      {meetings.length === 0 ? (
        <Card>
          <EmptyState
            title="Сейчас нет активных Jitsi-встреч"
            hint="Субтитры включаются в комнате: «More actions» → «Closed captions». Активная встреча появится здесь автоматически, завершённые — в списке ниже."
          />
        </Card>
      ) : (
        meetings.map((meeting) => (
          <MeetingCard
            key={meeting.meeting_id}
            meeting={meeting}
            now={now}
            onToggle={toggleTranscribe}
            onStop={stopMeeting}
            stoppingId={stoppingId}
          />
        ))
      )}

      {recent.length > 0 && (
        <Card title="Последние Jitsi-записи" bodyClassName="p-0">
          <ul className="divide-y divide-edge/60">
            {recent.map((job) => (
              <li key={job.id} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <Link to={`/jobs/${job.id}`} className="truncate text-sm hover:text-accent">
                    {job.source_name}
                  </Link>
                  <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
                    <span>{fmtDateTime(job.created_at)}</span>
                    <span>
                      {job.segments.length}{" "}
                      {plural(job.segments.length, "реплика", "реплики", "реплик")}
                    </span>
                  </div>
                </div>
                <StatusBadge status={job.status} />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function MeetingCard({
  meeting,
  now,
  onToggle,
  onStop,
  stoppingId,
}: {
  meeting: JitsiMeeting;
  now: number;
  onToggle: (meetingId: string, enabled: boolean) => void;
  onStop: (meetingId: string) => void;
  stoppingId: string | null;
}) {
  const elapsed = Math.max(0, now / 1000 - meeting.started_at);
  const silence = meeting.silence_sec ?? 0;
  const idleLimit = meeting.idle_stop_sec ?? 0;
  const remaining = idleLimit > 0 ? Math.max(0, Math.ceil(idleLimit - silence)) : null;
  const silenceShown = silence >= (idleLimit > 0 ? Math.min(60, idleLimit / 2) : 60);
  const stopBusy = stoppingId === meeting.meeting_id || meeting.stopping;
  return (
    <Card bodyClassName="p-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-3">
        <span className="relative flex size-2.5 shrink-0">
          <span className="absolute inline-flex size-2.5 animate-ping rounded-full bg-err opacity-60" />
          <span className="relative inline-flex size-2.5 rounded-full bg-err" />
        </span>
        <span className="font-mono text-sm">{meeting.meeting_id}</span>
        <Link to={`/jobs/${meeting.job_id}`} className="text-xs text-accent hover:underline">
          открыть транскрипт ↗
        </Link>
        {meeting.transcribe ? (
          meeting.pooled && <Chip>общий пул декодера</Chip>
        ) : (
          <Chip>без распознавания — расшифровка после встречи</Chip>
        )}
        <label
          className="ml-auto flex cursor-pointer items-center gap-2 text-xs text-muted"
          title="Распознавать речь во время встречи; выкл — только запись, расшифровка после встречи в очереди"
        >
          <input
            type="checkbox"
            checked={meeting.transcribe}
            onChange={(event) => onToggle(meeting.meeting_id, event.target.checked)}
            className="size-4 accent-cyan-400"
          />
          распознавание
        </label>
        <span className="flex flex-wrap items-center gap-x-3 text-xs text-muted">
          <span>идёт {fmtClock(elapsed)}</span>
          <span>
            {meeting.segments}{" "}
            {plural(meeting.segments, "реплика", "реплики", "реплик")}
          </span>
          <span>
            {meeting.participants.length}{" "}
            {plural(meeting.participants.length, "говорящий", "говорящих", "говорящих")}
          </span>
          {silenceShown && (
            <span
              className={remaining != null && remaining <= 120 ? "text-warn" : undefined}
              title="Никто не говорит; при достижении порога встреча завершится сама"
            >
              тишина {fmtClock(silence)}
              {remaining != null && remaining > 0 && ` · авто-стоп через ${fmtClock(remaining)}`}
            </span>
          )}
        </span>
        <button
          onClick={() => onStop(meeting.meeting_id)}
          disabled={stopBusy}
          title="Завершить запись и субтитры этой встречи"
          className="rounded-lg border border-warn/40 px-2.5 py-1.5 text-xs text-warn transition-colors hover:bg-warn/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {stopBusy ? "Останавливается…" : "Остановить запись"}
        </button>
      </div>
      <ul className="divide-y divide-edge/60">
        {meeting.participants.map((participant) => {
          const speaking = participant.last_frame_sec != null && participant.last_frame_sec < 2.5;
          return (
            <li key={participant.id} className="flex items-center gap-3 px-4 py-3">
              <span
                className={`size-2 shrink-0 rounded-full ${speaking ? "animate-pulse-soft bg-ok" : "bg-muted/40"}`}
              />
              <div className="min-w-0 flex-1">
                <span className="text-sm">{participant.label}</span>
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
                  {participant.language && <span>{participant.language}</span>}
                  <span>аудио {Math.round(participant.audio_sec)} с</span>
                  <span>
                    {participant.segments}{" "}
                    {plural(participant.segments, "реплика", "реплики", "реплик")}
                  </span>
                </div>
              </div>
              <span className="shrink-0 text-xs text-muted">
                {participant.last_frame_sec == null
                  ? "нет сигнала"
                  : speaking
                    ? "говорит"
                    : `тишина ${Math.round(participant.last_frame_sec)} с`}
              </span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
