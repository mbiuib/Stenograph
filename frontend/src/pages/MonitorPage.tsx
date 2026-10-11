import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, EmptyState, ErrorBanner, ProgressBar, StatCard } from "../components/ui";
import { plural } from "../format";
import { usePolling } from "../hooks";
import type { ModelMemory } from "../types";

function fmtMb(mb: number | null | undefined): string {
  if (mb == null) return "—";
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} ГБ`;
  return `${Math.round(mb)} МБ`;
}

function fmtIdle(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 60) return `${Math.round(sec)} с`;
  if (sec < 3600) return `${Math.round(sec / 60)} мин`;
  return `${(sec / 3600).toFixed(1)} ч`;
}

function ModelRow({ model, prefix }: { model: ModelMemory; prefix?: string }) {
  return (
    <li
      key={`${prefix ?? ""}${model.engine}:${model.model}`}
      className="flex flex-col gap-1.5 border-b border-edge/60 pb-3 last:border-0 last:pb-0"
    >
      <div className="flex items-center justify-between gap-3">
        <span className="truncate text-sm font-medium">
          {model.engine}: <span className="text-muted">{model.model}</span>
        </span>
        {model.busy || (model.idle_sec != null && model.idle_sec < 30) ? (
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
  );
}

export function MonitorPage() {
  const { data, error } = usePolling(() => api.metrics(), 2000);
  const [unloadNote, setUnloadNote] = useState<string | null>(null);

  const handleUnload = async () => {
    try {
      const result = await api.unloadModels();
      setUnloadNote(
        result.unloaded.length > 0
          ? `Выгружено: ${result.unloaded.join(", ")}`
          : "Нечего выгружать — все модели заняты",
      );
    } catch (err) {
      setUnloadNote(`Ошибка выгрузки: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const service = data?.asr_service ?? null;
  const remote = service != null; // сервер в remote-режиме: модели живут в сервисе
  const ownGpu = data?.system.gpu ?? null;
  const serviceGpu = service?.system?.gpu ?? null;
  const gpu = ownGpu ?? serviceGpu;
  const gpuSource = ownGpu ? "процесс стенографа" : "внешний ASR-сервис";
  const self = data?.system.self_process ?? null;
  const server = data?.server ?? null;
  const serviceSelf = service?.system?.self_process ?? null;
  const processes = data?.system.gpu_processes ?? [];
  const models = data?.models ?? [];
  const serviceModels = service?.models ?? [];
  const live = data?.live ?? null;
  const bridge = data?.bridge ?? null;
  const queue = data?.queue ?? null;
  const airCount = (live?.sessions.length ?? 0) + (bridge?.meetings.length ?? 0);
  const activeCount = (queue?.active_jobs?.length ?? 0) + (queue?.active ? 1 : 0);

  const vramPct = gpu ? Math.round((gpu.vram_used_mb / gpu.vram_total_mb) * 100) : 0;
  const lagMs = server?.loop_lag_ms ?? null;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Монитор</h1>
        <p className="mt-1 text-sm text-muted">
          У каждой панели указано, откуда цифры: процесс сервера стенографа, видеокарта целиком или
          внешний ASR-сервис.
        </p>
      </header>

      {error && <ErrorBanner message={`Нет связи с сервером: ${error}`} />}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard
          label="CPU сервера"
          value={self?.cpu_pct != null ? `${self.cpu_pct}%` : "—"}
          hint="процесс стенографа"
        />
        <StatCard label="RAM сервера" value={self ? fmtMb(self.rss_mb) : "—"} hint="тот же процесс" />
        <StatCard
          label="GPU (вся карта)"
          value={gpu ? `${gpu.utilization_pct}%` : "—"}
          hint={gpu?.name ?? "нет данных"}
        />
        <StatCard
          label="Память GPU"
          value={gpu ? fmtMb(gpu.vram_used_mb) : "—"}
          hint={gpu ? `из ${fmtMb(gpu.vram_total_mb)} · вся карта` : undefined}
        />
        <StatCard
          label="Моделей в памяти"
          value={data ? models.length + serviceModels.length : "—"}
          hint={remote ? "сервер + внешний сервис" : "сервер"}
        />
        <StatCard label="Эфир" value={data ? airCount : "—"} hint="live + Jitsi" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Сервер стенографа">
          <p className="-mt-1 mb-3 text-xs text-muted">
            Один процесс: API, очередь задач, обработка файлов и live. Ниже — его собственные цифры.
          </p>
          <div className="flex flex-col gap-2 text-sm">
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">Версия и PID</span>
              <span className="tabular text-xs">
                {server?.version ?? "—"} · #{self?.pid ?? "—"}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">Аптайм процесса</span>
              <span className="tabular text-xs">{fmtIdle(self?.uptime_sec)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span
                className="text-muted"
                title="Насколько отставал event loop за последнюю секунду"
              >
                Отставание цикла
              </span>
              <span className={`tabular text-xs ${lagMs != null && lagMs > 500 ? "text-err" : ""}`}>
                {lagMs != null ? `${lagMs} мс` : "—"}
                {server?.loop_lag_max_ms ? ` · макс ${server.loop_lag_max_ms} мс` : ""}
              </span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span
                className="text-muted"
                title="Выделенная видеопамять этого процесса (Windows-счётчики)"
              >
                VRAM процесса сервера
              </span>
              <span className="tabular text-xs">{fmtMb(self?.vram_mb)}</span>
            </div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-muted">Очередь задач</span>
              <span className="tabular text-xs">
                {activeCount} в работе · {queue?.waiting.length ?? 0} ожидают
              </span>
            </div>
          </div>
        </Card>

        <Card title="Видеокарта (вся карта)">
          <p className="-mt-1 mb-3 text-xs text-muted">
            Значения по всей карте, не только по Стенографу. Источник: {gpuSource}.
          </p>
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
                  <div className="text-xs text-muted">Крупнейшие потребители видеопамяти</div>
                  <ul className="flex flex-col gap-1">
                    {processes.map((proc) => {
                      const isServer = proc.pid === self?.pid;
                      const isService = serviceSelf != null && proc.pid === serviceSelf.pid;
                      return (
                        <li key={proc.pid} className="flex items-center justify-between gap-3 text-sm">
                          <span
                            className={`truncate ${isServer || isService ? "font-medium text-accent" : ""}`}
                          >
                            {proc.name ?? "?"}{" "}
                            <span className="tabular text-xs text-muted">#{proc.pid}</span>
                            {isServer && <span className="text-xs text-accent"> — этот сервер</span>}
                            {isService && <span className="text-xs text-accent"> — ASR-сервис</span>}
                          </span>
                          <span className="tabular shrink-0 text-xs text-muted">
                            {fmtMb(proc.vram_mb)}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <EmptyState
              title="GPU не обнаружена"
              hint="nvidia-smi недоступен — метрики видеопамяти появятся на машине с NVIDIA."
            />
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Модели сервера в памяти (${models.length})`}>
          <p className="-mt-1 mb-3 text-xs text-muted">
            Загружены в процесс сервера стенографа. Простаивающие выгружаются автоматически.
          </p>
          {models.length > 0 ? (
            <ul className="flex flex-col gap-3">
              {models.map((model) => (
                <ModelRow key={`${model.engine}:${model.model}`} model={model} />
              ))}
            </ul>
          ) : (
            <EmptyState
              title={remote ? "Локальных моделей нет" : "Ни одна модель ещё не загружена"}
              hint={
                remote
                  ? "Сервер в remote-режиме: распознавание выполняет внешний ASR-сервис — смотри панель рядом."
                  : "Модель загрузится при первой задаче; простаивающие выгружаются автоматически."
              }
            />
          )}
          {!remote && (
            <div className="mt-3 flex items-center justify-between gap-3 border-t border-edge/60 pt-3">
              <span className="min-w-0 truncate text-xs text-muted">
                {unloadNote ?? "Простаивающие модели выгружаются автоматически."}
              </span>
              <button
                type="button"
                className="shrink-0 rounded-md border border-edge px-2.5 py-1 text-xs text-muted transition-colors hover:border-accent/60 hover:text-accent"
                onClick={() => {
                  void handleUnload();
                }}
              >
                Выгрузить простаивающие
              </button>
            </div>
          )}
        </Card>

        {service && (
          <Card
            title={`Внешний ASR-сервис (${
              serviceModels.length ? "работает" : service.ok ? "на связи" : "недоступен"
            })`}
          >
            <p className="-mt-1 mb-3 text-xs text-muted">
              Отдельный процесс, который считает модели. Все цифры в этой панели — его собственные.
            </p>
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between gap-3 text-xs text-muted">
                <span className="truncate">{service.url}</span>
                <span className={service.ok ? "text-accent" : "text-err"}>
                  {service.ok
                    ? "на связи"
                    : `недоступен${service.error ? `: ${service.error}` : ""}`}
                </span>
              </div>
              {serviceSelf && (
                <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted">CPU</span>
                    <span className="tabular text-xs">
                      {serviceSelf.cpu_pct != null ? `${serviceSelf.cpu_pct}%` : "—"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted">RAM</span>
                    <span className="tabular text-xs">{fmtMb(serviceSelf.rss_mb)}</span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted">VRAM</span>
                    <span className="tabular text-xs">{fmtMb(serviceSelf.vram_mb)}</span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-muted">GPU</span>
                    <span className="tabular text-xs">
                      {serviceGpu ? `${serviceGpu.utilization_pct}%` : "—"}
                    </span>
                  </div>
                </div>
              )}
              {serviceModels.length > 0 ? (
                <ul className="mt-1 flex flex-col gap-3">
                  {serviceModels.map((model) => (
                    <ModelRow
                      key={`svc:${model.engine}:${model.model}`}
                      model={model}
                      prefix="svc:"
                    />
                  ))}
                </ul>
              ) : (
                service.ok && (
                  <EmptyState
                    title="Модели ещё не загружены"
                    hint="Загрузятся при первой задаче; кнопка «Выгрузить простаивающие» работает через сервис."
                  />
                )
              )}
              <div className="mt-1 flex items-center justify-between gap-3 border-t border-edge/60 pt-3">
                <span className="min-w-0 truncate text-xs text-muted">
                  {unloadNote ?? "Модели сервиса выгружаются автоматически по простою."}
                </span>
                <button
                  type="button"
                  className="shrink-0 rounded-md border border-edge px-2.5 py-1 text-xs text-muted transition-colors hover:border-accent/60 hover:text-accent"
                  onClick={() => {
                    void handleUnload();
                  }}
                >
                  Выгрузить простаивающие
                </button>
              </div>
            </div>
          </Card>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Эфир (${airCount})`}>
          {airCount > 0 ? (
            <ul className="flex flex-col gap-3">
              {live?.sessions.map((session) => (
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
              {bridge?.meetings.map((meeting) => (
                <li key={meeting.job_id} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-3">
                    <Link
                      to={`/jobs/${meeting.job_id}`}
                      className="min-w-0 truncate text-sm font-medium hover:text-accent"
                    >
                      {meeting.source_name}
                    </Link>
                    <span className="tabular shrink-0 text-xs text-muted">
                      идёт {fmtIdle(meeting.duration_sec)}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Chip>Jitsi</Chip>
                    <Chip>
                      {meeting.participants.length}{" "}
                      {plural(meeting.participants.length, "участник", "участника", "участников")}
                    </Chip>
                    {meeting.transcribe ? (
                      <Chip className="text-accent">распознавание</Chip>
                    ) : (
                      <Chip>без распознавания</Chip>
                    )}
                    {meeting.stopping && <Chip className="text-err">останавливается</Chip>}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="Нет активных сессий"
              hint="Live-записи и Jitsi-встречи появятся здесь."
            />
          )}
        </Card>

        <Card title="Очередь и внешние сервисы">
          <div className="flex flex-col gap-3">
            {(
              queue?.active_jobs?.length ? queue.active_jobs : queue?.active ? [queue.active] : []
            ).map((job) => (
              <div key={job.id} className="flex flex-col gap-2">
                <div className="flex items-center justify-between gap-3">
                  <Link
                    to={`/jobs/${job.id}`}
                    className="truncate text-sm font-medium hover:text-accent"
                  >
                    {job.source_name}
                  </Link>
                  <span className="shrink-0 text-xs text-muted">в работе</span>
                </div>
                <ProgressBar value={job.progress} />
              </div>
            ))}
            {!(queue?.active_jobs?.length || queue?.active) && (
              <p className="text-sm text-muted">Воркер свободен.</p>
            )}
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted">Ожидают обработки</span>
              <span className="tabular">{queue?.waiting.length ?? 0}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span
                className="text-muted"
                title="Внешний сервер, где живёт LLM для протоколов и резюме"
              >
                LM Studio (анализ, порт 1234)
              </span>
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
