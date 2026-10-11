import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Card, Chip, EmptyState, ErrorBanner } from "../components/ui";
import type { SettingItem, SettingsSnapshot } from "../types";

const APPLIES_LABEL: Record<SettingItem["applies"], string> = {
  live: "Применится сразу",
  next: "Применится к новым задачам и сессиям",
  restart: "Применится после перезапуска сервера",
};

function itemText(value: SettingItem["value"]): string {
  if (value == null) return "";
  return String(value);
}

function Toggle({
  on,
  disabled,
  onChange,
}: {
  on: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
        on ? "bg-accent" : "border border-edge bg-surface2"
      }`}
    >
      <span
        className={`absolute top-0.5 size-5 rounded-full bg-bg shadow transition-all ${
          on ? "left-[22px]" : "left-0.5"
        }`}
      />
    </button>
  );
}

const INPUT_CLASS =
  "w-40 rounded-md border border-edge bg-surface2 px-2.5 py-1.5 text-sm text-ink " +
  "outline-none transition-colors focus:border-accent/60";

function SettingControl({
  item,
  busy,
  onApply,
}: {
  item: SettingItem;
  busy: boolean;
  onApply: (value: unknown) => void;
}) {
  const [text, setText] = useState(itemText(item.value));
  useEffect(() => {
    setText(itemText(item.value));
  }, [item.value]);

  if (!item.editable) {
    if (item.kind === "secret") {
      return (
        <span className="text-xs text-muted">{item.is_set ? "значение задано" : "не задано"}</span>
      );
    }
    return <span className="tabular text-sm text-muted">{itemText(item.value) || "—"}</span>;
  }

  if (item.kind === "bool") {
    return <Toggle on={item.value === true} disabled={busy} onChange={(next) => onApply(next)} />;
  }

  if (item.choices && item.choices.length > 0) {
    return (
      <select
        className={INPUT_CLASS}
        value={itemText(item.value)}
        disabled={busy}
        onChange={(event) => onApply(event.target.value)}
      >
        {item.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice === "" ? "— движок по умолчанию —" : choice}
          </option>
        ))}
        {item.choices.every((choice) => choice !== itemText(item.value)) && item.value != null && (
          <option value={itemText(item.value)}>{itemText(item.value)}</option>
        )}
      </select>
    );
  }

  const commit = () => {
    if (text !== itemText(item.value)) onApply(text);
  };

  return (
    <input
      type={item.kind === "int" || item.kind === "float" ? "number" : "text"}
      className={INPUT_CLASS}
      value={text}
      disabled={busy}
      min={item.minimum ?? undefined}
      max={item.maximum ?? undefined}
      step={item.kind === "int" ? 1 : "any"}
      onChange={(event) => setText(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") (event.target as HTMLInputElement).blur();
      }}
    />
  );
}

function SettingRow({
  item,
  busy,
  error,
  onApply,
  onReset,
}: {
  item: SettingItem;
  busy: boolean;
  error?: string;
  onApply: (value: unknown) => void;
  onReset: () => void;
}) {
  return (
    <div className="flex flex-col gap-1 border-b border-edge/60 py-3 first:pt-0 last:border-0 last:pb-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{item.label}</span>
            {item.source === "runtime" && (
              <Chip className="text-accent">изменено</Chip>
            )}
            {item.source === "env" && <Chip>из окружения</Chip>}
            <span className="text-[11px] text-muted/70">{APPLIES_LABEL[item.applies]}</span>
          </div>
          {item.hint && <p className="mt-0.5 text-xs text-muted">{item.hint}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2 pt-0.5">
          <SettingControl item={item} busy={busy} onApply={onApply} />
          {item.source === "runtime" && (
            <button
              type="button"
              title="Вернуть значение из окружения"
              className="rounded-md border border-edge px-2 py-1 text-xs text-muted transition-colors hover:border-accent/60 hover:text-accent"
              onClick={onReset}
            >
              Сбросить
            </button>
          )}
        </div>
      </div>
      {error && <p className="text-xs text-err">{error}</p>}
    </div>
  );
}

type RestartPhase = "idle" | "confirm" | "restarting" | "back" | "error";

async function healthOk(): Promise<boolean> {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

/** Перезапуск сервера: кнопка, ожидание возврата и версия после старта. */
function RestartCard({ onRestarted }: { onRestarted: () => Promise<void> }) {
  const [phase, setPhase] = useState<RestartPhase>("idle");
  const [note, setNote] = useState<string | null>(null);

  const doRestart = useCallback(async () => {
    setPhase("restarting");
    setNote(null);
    // Сервер отвечает ДО остановки; если соединение успело упасть — тоже ок.
    await api.restartServer().catch(() => undefined);
    // Сначала дожидаемся остановки, затем — возвращения.
    const downDeadline = Date.now() + 30_000;
    while (Date.now() < downDeadline && (await healthOk())) {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    const upDeadline = Date.now() + 120_000;
    while (Date.now() < upDeadline) {
      if (await healthOk()) {
        const info = await fetch("/api/health")
          .then((response) => response.json() as Promise<{ version?: string }>)
          .catch(() => null);
        setNote(
          info?.version ? `Сервер перезапущен (версия ${info.version}).` : "Сервер перезапущен.",
        );
        setPhase("back");
        await onRestarted();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    setNote("Сервер не ответил за две минуты — проверьте контейнер (docker compose ps).");
    setPhase("error");
  }, [onRestarted]);

  return (
    <Card title="Сервер">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <p className="text-sm text-muted">
          Перезапуск применяет настройки с пометкой «Применится после перезапуска сервера».
          Идущие задачи прервутся — при старте их поднимет настройка «Поднимать задачи после
          рестарта». В докере сервер поднимается сам (политика рестарта).
        </p>
        {phase === "restarting" ? (
          <Chip className="shrink-0 text-warn">Перезагружаю…</Chip>
        ) : phase === "confirm" ? (
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-bg transition-opacity hover:opacity-90"
              onClick={() => void doRestart()}
            >
              Да, перезапустить
            </button>
            <button
              type="button"
              className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted transition-colors hover:text-ink"
              onClick={() => setPhase("idle")}
            >
              Отмена
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="shrink-0 rounded-md border border-edge px-3 py-1.5 text-sm text-ink transition-colors hover:border-warn/60 hover:text-warn"
            onClick={() => setPhase("confirm")}
          >
            Перезапустить сервер
          </button>
        )}
      </div>
      {phase === "back" && note && <p className="mt-1 text-xs text-ok">{note}</p>}
      {phase === "error" && note && <p className="mt-1 text-xs text-err">{note}</p>}
    </Card>
  );
}

export function SettingsPage() {
  const [data, setData] = useState<SettingsSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setData(await api.settings());
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const apply = useCallback(async (key: string, value: unknown) => {
    setBusyKey(key);
    try {
      const result = await api.updateSettings({ [key]: value });
      setData(result.settings);
      setErrors(result.errors);
    } catch (err) {
      setErrors((prev) => ({ ...prev, [key]: err instanceof Error ? err.message : String(err) }));
    } finally {
      setBusyKey(null);
    }
  }, []);

  const reset = useCallback(async (key: string) => {
    setBusyKey(key);
    try {
      const result = await api.resetSetting(key);
      setData(result.settings);
      setErrors(result.errors);
    } catch (err) {
      setErrors((prev) => ({ ...prev, [key]: err instanceof Error ? err.message : String(err) }));
    } finally {
      setBusyKey(null);
    }
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Настройки</h1>
        <p className="mt-1 text-sm text-muted">
          Все параметры приложения. Изменения применяются без перезагрузки — под каждым пунктом
          сказано, когда именно (к новым задачам, сессиям или после перезапуска сервера).
        </p>
      </header>

      <RestartCard onRestarted={reload} />

      {loadError && <ErrorBanner message={`Не удалось загрузить настройки: ${loadError}`} />}
      {!data && !loadError && <EmptyState title="Загружаю настройки…" />}

      {data?.groups.map((group) => (
        <Card key={group.id} title={group.title}>
          {group.description && <p className="-mt-1 mb-2 text-xs text-muted">{group.description}</p>}
          <div className="flex flex-col">
            {group.items.map((item) => (
              <SettingRow
                key={item.key}
                item={item}
                busy={busyKey === item.key}
                error={errors[item.key]}
                onApply={(value) => {
                  void apply(item.key, value);
                }}
                onReset={() => {
                  void reset(item.key);
                }}
              />
            ))}
          </div>
        </Card>
      ))}
    </div>
  );
}
