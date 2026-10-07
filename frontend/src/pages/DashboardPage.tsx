import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatCard, StatusBadge } from "../components/ui";
import { etaSeconds, fmtClock, fmtHours, fmtRelative, fmtRemaining } from "../format";
import { useNow, usePolling } from "../hooks";

export function DashboardPage() {
  const { data: stats, error: statsError } = usePolling(() => api.stats(), 2000);
  const { data: queue, error: queueError } = usePolling(() => api.queue(), 1500);
  const now = useNow(1000);

  const active = queue?.active ?? null;
  const activeEta = active ? etaSeconds(active) : null;
  const activeElapsed = active?.started_at != null ? now / 1000 - active.started_at : null;
  const counts = stats?.jobs.by_status ?? {};

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Дашборд</h1>
        <p className="mt-1 text-sm text-muted">Состояние конвейера и статистика обработки.</p>
      </header>

      {(statsError || queueError) && (
        <ErrorBanner message={`Нет связи с сервером: ${statsError ?? queueError}`} />
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Всего задач" value={stats?.jobs.total ?? "—"} />
        <StatCard label="Аудио обработано" value={fmtHours(stats?.audio_seconds)} />
        <StatCard label="Время обработки" value={fmtClock(stats?.processing_seconds)} />
        <StatCard
          label="Средний темп"
          value={stats?.avg_speed_factor != null ? `${stats.avg_speed_factor}×` : "—"}
          hint="аудио / время"
        />
        <StatCard label="В работе" value={counts.running ?? 0} />
        <StatCard label="Ошибок" value={counts.error ?? 0} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Сейчас в работе">
          {active ? (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-3">
                <Link
                  to={`/jobs/${active.id}`}
                  className="truncate text-sm font-medium hover:text-accent"
                >
                  {active.source_name}
                </Link>
                <StatusBadge status={active.status} />
              </div>
              <ProgressBar value={active.progress} />
              <div className="flex items-center justify-between gap-3 text-xs text-muted">
                <span className="min-w-0 truncate">
                  {active.progress}% · {active.message || "…"}
                </span>
                <span className="tabular shrink-0">
                  {activeElapsed != null && `прошло ${fmtClock(activeElapsed)}`}
                  {activeEta != null && ` · осталось ${fmtRemaining(activeEta)}`}
                </span>
              </div>
              {(active.meta.engine || active.meta.duration != null) && (
                <div className="flex gap-2 text-xs">
                  {active.meta.engine != null && <Chip>движок: {String(active.meta.engine)}</Chip>}
                  {active.meta.duration != null && <Chip>аудио: {fmtClock(Number(active.meta.duration))}</Chip>}
                </div>
              )}
            </div>
          ) : (
            <EmptyState title="Воркер свободен" hint="Загрузите файл, чтобы начать обработку." />
          )}
        </Card>

        <Card title={`Очередь (${queue?.waiting.length ?? 0})`}>
          {queue && queue.waiting.length > 0 ? (
            <ol className="flex flex-col gap-2">
              {queue.waiting.map((job, index) => (
                <li key={job.id} className="flex items-center gap-3 text-sm">
                  <span className="tabular w-5 text-right text-xs text-muted">{index + 1}</span>
                  <Link to={`/jobs/${job.id}`} className="min-w-0 flex-1 truncate hover:text-accent">
                    {job.source_name}
                  </Link>
                  <span className="shrink-0 text-xs text-muted">{fmtRelative(job.created_at)}</span>
                </li>
              ))}
            </ol>
          ) : (
            <EmptyState title="Очередь пуста" />
          )}
        </Card>
      </div>

      <Card title="Активность за 30 дней">
        <ActivityChart activity={stats?.activity ?? []} />
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Последние задачи"
          action={
            <Link to="/jobs" className="text-xs text-muted hover:text-ink">
              все задачи →
            </Link>
          }
          bodyClassName="p-2"
        >
          {stats && stats.recent.length > 0 ? (
            <ul className="flex flex-col">
              {stats.recent.map((job) => (
                <li key={job.id}>
                  <Link
                    to={`/jobs/${job.id}`}
                    className="flex items-center gap-3 rounded-lg px-2 py-2 hover:bg-surface2/60"
                  >
                    <span className="min-w-0 flex-1 truncate text-sm">{job.source_name}</span>
                    {job.meta.speakers && job.meta.speakers.length > 0 && (
                      <span className="text-xs text-muted">{job.meta.speakers.length} спк.</span>
                    )}
                    <span className="tabular text-xs text-muted">{fmtClock(job.meta.duration)}</span>
                    <StatusBadge status={job.status} />
                    <span className="hidden w-20 shrink-0 text-right text-xs text-muted sm:block">
                      {fmtRelative(job.created_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="Пока пусто" />
          )}
        </Card>

        <Card title="Движки">
          {stats && stats.engines.length > 0 ? (
            <ul className="flex flex-col gap-2 text-sm">
              {stats.engines.map((entry) => (
                <li key={entry.engine} className="flex items-center justify-between">
                  <span className="font-medium">{entry.engine}</span>
                  <span className="text-xs text-muted">{entry.jobs} задач</span>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="Ещё не было завершённых задач" />
          )}
        </Card>
      </div>
    </div>
  );
}

function ActivityChart({ activity }: { activity: { date: string; jobs: number }[] }) {
  const byDate = new Map(activity.map((entry) => [entry.date, entry.jobs]));
  const days: { date: string; jobs: number }[] = [];
  for (let offset = 29; offset >= 0; offset--) {
    const day = new Date(Date.now() - offset * 86400000);
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(
      day.getDate(),
    ).padStart(2, "0")}`;
    days.push({ date: key, jobs: byDate.get(key) ?? 0 });
  }
  const max = Math.max(1, ...days.map((day) => day.jobs));
  return (
    <div className="flex h-28 items-end gap-0.5">
      {days.map((day) => (
        <div key={day.date} className="group flex h-full flex-1 items-end" title={`${day.date}: ${day.jobs}`}>
          <div
            className={`w-full rounded-t transition-colors ${
              day.jobs > 0 ? "bg-gradient-to-t from-accent2/60 to-accent" : "bg-surface2"
            }`}
            style={{ height: `${Math.max(3, (day.jobs / max) * 100)}%` }}
          />
        </div>
      ))}
    </div>
  );
}
