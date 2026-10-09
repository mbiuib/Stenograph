import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatCard } from "../components/ui";
import { usePolling } from "../hooks";

function fmtMb(mb: number | null | undefined): string {
  if (mb == null) return "—";
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} ГБ`;
  return `${Math.round(mb)} МБ`;
}

function fmtIdle(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 60) return `${Math.round(sec)} с`;
  if (sec < 3600) return `${Math.round(sec / 60)} мин`;
  return `${(sec / 3600).toFixed(1)} ч`;
}

export function MonitorPage() {
  const { data, error } = usePolling(() => api.metrics(), 2000);

  const gpu = data?.system.gpu ?? null;
  const self = data?.system.self_process ?? null;
  const processes = data?.system.gpu_processes ?? [];
  const models = data?.models ?? [];
  const live = data?.live ?? null;
  const queue = data?.queue ?? null;

  const vramPct = gpu ? Math.round((gpu.vram_used_mb / gpu.vram_total_mb) * 100) : 0;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Монитор</h1>
        <p className="mt-1 text-sm text-muted">
          Ресурсы сервера, модели в памяти и конвейер — в реальном времени.
        </p>
      </header>

      {error && <ErrorBanner message={`Нет связи с сервером: ${error}`} />}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="GPU" value={gpu ? `${gpu.utilization_pct}%` : "—"} hint={gpu?.name ?? undefined} />
        <StatCard
          label="Память GPU"
          value={gpu ? fmtMb(gpu.vram_used_mb) : "—"}
          hint={gpu ? `из ${fmtMb(gpu.vram_total_mb)}` : undefined}
        />
        <StatCard label="Моделей в памяти" value={models.length} />
        <StatCard label="Эфир" value={live ? live.sessions.length : "—"} hint="сессий" />
        <StatCard label="RAM сервера" value={self ? fmtMb(self.rss_mb) : "—"} />
        <StatCard label="CPU сервера" value={self?.cpu_pct != null ? `${self.cpu_pct}%` : "—"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Видеокарта">
          {gpu ? (
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between text-xs text-muted">
                  <span>Загрузка чипа</span>
                  <span className="tabular">{gpu.utilization_pct}%</span>
                </div>
                <ProgressBar value={gpu.utilization_pct} />
              </div>
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between text-xs text-muted">
                  <span>Выделенная память</span>
                  <span className="tabular">
                    {fmtMb(gpu.vram_used_mb)} / {fmtMb(gpu.vram_total_mb)} ({vramPct}%)
                  </span>
                </div>
                <ProgressBar value={vramPct} />
              </div>
              {processes.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  <div className="text-xs text-muted">Процессы на GPU</div>
                  <ul className="flex flex-col gap-1">
                    {processes.map((proc) => (
                      <li key={proc.pid} className="flex items-center justify-between gap-3 text-sm">
                        <span
                          className={`truncate ${proc.pid === self?.pid ? "font-medium text-accent" : ""}`}
                        >
                          {proc.name ?? "?"}{" "}
                          <span className="tabular text-xs text-muted">#{proc.pid}</span>
                          {proc.pid === self?.pid && <span className="text-xs text-accent"> — этот сервер</span>}
                        </span>
                        <span className="tabular shrink-0 text-xs text-muted">{fmtMb(proc.vram_mb)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <EmptyState title="GPU не обнаружена" hint="nvidia-smi недоступен — метрики видеопамяти появятся на машине с NVIDIA." />
          )}
        </Card>

        <Card title={`Модели в памяти (${models.length})`}>
          {models.length > 0 ? (
            <ul className="flex flex-col gap-3">
              {models.map((model) => (
                <li
                  key={`${model.engine}:${model.model}`}
                  className="flex flex-col gap-1.5 border-b border-edge/60 pb-3 last:border-0 last:pb-0"
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate text-sm font-medium">
                      {model.engine}: <span className="text-muted">{model.model}</span>
                    </span>
                    {model.idle_sec != null && model.idle_sec < 30 ? (
                      <span className="shrink-0 text-xs text-accent">в работе</span>
                    ) : (
                      <span className="shrink-0 text-xs text-muted">простой {fmtIdle(model.idle_sec)}</span>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Chip>VRAM: {fmtMb(model.vram_mb)}</Chip>
                    <Chip>RAM: {fmtMb(model.ram_mb)}</Chip>
                    {model.load_number === 1 && <Chip>вкл. CUDA-контекст</Chip>}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="Ни одна модель ещё не загружена"
              hint="Модель загрузится при первой задаче и останется в памяти до перезапуска."
            />
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Эфир (${live?.sessions.length ?? 0})`}>
          {live && live.sessions.length > 0 ? (
            <ul className="flex flex-col gap-3">
              {live.sessions.map((session) => (
                <li key={session.job_id} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-3">
                    <Link
                      to={`/jobs/${session.job_id}`}
                      className="min-w-0 truncate text-sm font-medium hover:text-accent"
                    >
                      {session.source_name}
                    </Link>
                    <span className="tabular shrink-0 text-xs text-muted">
                      {session.text_delay_sec != null && (
                        <span className={session.text_delay_sec > 20 ? "text-err" : ""}>
                          отстаёт на {Math.round(session.text_delay_sec)} с ·{" "}
                        </span>
                      )}
                      лаг {session.lag_sec.toFixed(1)} с
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {session.tracks.map((track) => (
                      <Chip key={track}>{track}</Chip>
                    ))}
                    {session.transcribing && <Chip className="text-accent">транскрибируется</Chip>}
                    {!session.transcribing && session.queue_position != null && (
                      <Chip>в очереди: {session.queue_position}</Chip>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="Нет активных сессий" hint="Live-записи и Jitsi-встречи появятся здесь." />
          )}
        </Card>

        <Card title="Очередь и внешние сервисы">
          <div className="flex flex-col gap-3">
            {queue?.active ? (
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between gap-3">
                  <Link
                    to={`/jobs/${queue.active.id}`}
                    className="truncate text-sm font-medium hover:text-accent"
                  >
                    {queue.active.source_name}
                  </Link>
                  <span className="shrink-0 text-xs text-muted">в работе</span>
                </div>
                <ProgressBar value={queue.active.progress} />
              </div>
            ) : (
              <p className="text-sm text-muted">Воркер свободен.</p>
            )}
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted">Ожидают обработки</span>
              <span className="tabular">{queue?.waiting.length ?? 0}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted">LM Studio (анализ, порт 1234)</span>
              <span className={data?.llm.lm_studio ? "text-accent" : "text-muted"}>
                {data?.llm.lm_studio ? "доступен" : "не отвечает"}
              </span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
