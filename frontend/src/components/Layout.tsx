import { Link, NavLink, Outlet } from "react-router-dom";
import { IconDashboard, IconJobs, IconMic, IconPlus } from "./Icons";

export function Layout() {
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
