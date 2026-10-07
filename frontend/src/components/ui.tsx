/** Small shared UI primitives. */
import type { ReactNode } from "react";
import type { JobStatus } from "../types";

export function Card({
  title,
  action,
  children,
  className = "",
  bodyClassName = "p-4",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`rounded-xl border border-edge bg-surface ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-edge px-4 py-3">
          <h2 className="text-[13px] font-medium tracking-wide text-muted uppercase">{title}</h2>
          {action}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

const STATUS_STYLES: Record<JobStatus, { label: string; className: string }> = {
  queued: { label: "В очереди", className: "border-edge text-muted" },
  running: { label: "Обработка", className: "border-accent/40 text-accent pulse-soft" },
  done: { label: "Готово", className: "border-ok/40 text-ok" },
  error: { label: "Ошибка", className: "border-err/40 text-err" },
  cancelled: { label: "Отменено", className: "border-warn/40 text-warn" },
};

export function StatusBadge({ status }: { status: JobStatus }) {
  const item = STATUS_STYLES[status];
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs whitespace-nowrap ${item.className}`}>
      {item.label}
    </span>
  );
}

export function ProgressBar({ value, className = "" }: { value: number; className?: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div className={`h-1.5 w-full overflow-hidden rounded-full bg-surface2 ${className}`}>
      <div
        className="h-full rounded-full bg-gradient-to-r from-accent2 to-accent transition-[width] duration-500"
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

export function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-edge bg-surface px-4 py-3">
      <p className="text-[11px] tracking-wide text-muted uppercase">{label}</p>
      <p className="tabular mt-1 text-2xl font-semibold">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="py-8 text-center">
      <p className="text-sm text-muted">{title}</p>
      {hint && <p className="mt-1 text-xs text-muted/70">{hint}</p>}
    </div>
  );
}

export function Chip({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center rounded-md border border-edge bg-surface2 px-1.5 py-0.5 text-xs text-muted ${className}`}>
      {children}
    </span>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-err/40 bg-err/10 px-4 py-3 text-sm text-err">
      {message}
    </div>
  );
}
