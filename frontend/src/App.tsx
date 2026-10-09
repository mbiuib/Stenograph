import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { DashboardPage } from "./pages/DashboardPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { JobsPage } from "./pages/JobsPage";
import { LivePage } from "./pages/LivePage";
import { MonitorPage } from "./pages/MonitorPage";
import { NewJobPage } from "./pages/NewJobPage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/live" element={<LivePage />} />
        <Route path="/monitor" element={<MonitorPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/jobs/new" element={<NewJobPage />} />
        <Route path="/jobs/:id" element={<JobDetailPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

function NotFound() {
  return (
    <div className="py-24 text-center">
      <p className="text-4xl font-semibold">404</p>
      <p className="mt-2 text-sm text-muted">Такой страницы нет.</p>
    </div>
  );
}
