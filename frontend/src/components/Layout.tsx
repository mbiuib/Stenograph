import { useSyncExternalStore } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { useNow } from "../hooks";
import { getLiveSession, subscribeLiveSession } from "../live/session";
import { IconDashboard, IconJobs, IconMic, IconPlus } from "./Icons";

export function Layout() {
  const live = useSyncExternalStore(subscribeLiveSession, getLiveSession);
  const now = useNow(1000);
  const recordSeconds = live.active
    ? Math.max(0, Math.floor(now / 1000 - live.active.startedAt))
    : 0;
  const recordClock = `${String(Math.floor(recordSeconds / 60)).padStart(2, "0")}:${String(
    recordSeconds % 60,
  ).padStart(2, "0")}`;
  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
      isActive ? "bg-surface2 text-ink" : "text-muted hover:bg-surface2/60 hover:text-ink"
    }`;

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-10 flex w-16 flex-col border-r border-edge bg-surface px-2 py-4 md:w-60 md:px-3">
        <Link to="/" className="mb-6 flex items-center gap-3 px-1">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-accent2 to-accent text-bg">
            <IconMic className="size-5" />
          </span>
          <span className="hidden text-base font-semibold tracking-wide md:block">Стенограф</span>
        </Link>
        <nav className="flex flex-col gap-1">
          <NavLink to="/" end className={linkClass}>
            <IconDashboard className="size-5 shrink-0" />
            <span className="hidden md:block">Дашборд</span>
          </NavLink>
          <NavLink to="/jobs" end className={linkClass}>
            <IconJobs className="size-5 shrink-0" />
            <span className="hidden md:block">Задачи</span>
          </NavLink>
          <NavLink to="/jobs/new" className={linkClass}>
            <IconPlus className="size-5 shrink-0" />
            <span className="hidden md:block">Новая задача</span>
          </NavLink>
          <NavLink to="/live" className={linkClass}>
            <IconMic className="size-5 shrink-0" />
            <span className="hidden md:block">Live</span>
          </NavLink>
          {live.active && (
            <Link
              to="/live"
              title="Идёт запись — открыть Live"
              className="mt-1 flex items-center gap-3 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err"
            >
              <span className="relative flex size-5 shrink-0 items-center justify-center">
                <span className="absolute inline-flex size-2.5 animate-ping rounded-full bg-err opacity-60" />
                <span className="relative inline-flex size-2.5 rounded-full bg-err" />
              </span>
              <span className="hidden flex-1 md:block">Идёт запись</span>
              <span className="tabular hidden md:block">{recordClock}</span>
            </Link>
          )}
        </nav>
        <div className="mt-auto hidden px-1 text-xs text-muted md:block">
          <a className="hover:text-ink" href="/docs" target="_blank" rel="noreferrer">
            API (Swagger) ↗
          </a>
        </div>
      </aside>
      <main className="ml-16 w-full px-4 py-6 md:ml-60 md:px-8">
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
