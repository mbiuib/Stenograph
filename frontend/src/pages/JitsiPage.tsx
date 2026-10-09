/** Jitsi bridge meetings: live participants, counters and recent recordings. */
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, EmptyState, ErrorBanner, StatusBadge } from "../components/ui";
import { fmtClock, fmtDateTime, plural } from "../format";
import { useNow, usePolling } from "../hooks";
import type { JitsiMeeting } from "../types";

export function JitsiPage() {
  const { data: status, error } = usePolling(() => api.jitsiStatus(), 2000);
  const { data: jobs } = usePolling(() => api.listJobs(undefined, 100), 5000);
  const now = useNow(1000);
  const meetings = status?.meetings ?? [];
  const recent = (jobs ?? []).filter((job) => job.kind === "jitsi").slice(0, 6);

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
            Субтитры идут: {meetings.length}
          </span>
        )}
      </div>

      {error && <ErrorBanner message={error} />}

      {meetings.length === 0 ? (
        <Card>
          <EmptyState
            title="Сейчас нет активных Jitsi-встреч"
            hint="Субтитры включаются в комнате: «More actions» → «Closed captions». Активная встреча появится здесь автоматически, завершённые — в списке ниже."
          />
        </Card>
      ) : (
        meetings.map((meeting) => <MeetingCard key={meeting.meeting_id} meeting={meeting} now={now} />)
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

function MeetingCard({ meeting, now }: { meeting: JitsiMeeting; now: number }) {
  const elapsed = Math.max(0, now / 1000 - meeting.started_at);
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
        {meeting.pooled && <Chip>общий пул декодера</Chip>}
        <span className="ml-auto flex flex-wrap items-center gap-x-3 text-xs text-muted">
          <span>идёт {fmtClock(elapsed)}</span>
          <span>
            {meeting.segments}{" "}
            {plural(meeting.segments, "реплика", "реплики", "реплик")}
          </span>
          <span>
            {meeting.participants.length}{" "}
            {plural(meeting.participants.length, "говорящий", "говорящих", "говорящих")}
          </span>
        </span>
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
