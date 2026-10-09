/** React hooks: polling, live job stream, ticking clock. */
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { ownsLiveJob, subscribeLiveEvents } from "./live/session";
import type { Job, JobEvent, Segment } from "./types";

/** Poll an async function on an interval; errors are surfaced, data is retained.
 *
 * Hidden tabs skip ticks (Chrome throttles their timers anyway) and refresh
 * immediately when the tab becomes visible again — counters and lists stay
 * actual the moment the user switches back to an older tab.
 */
export function usePolling<T>(
  fn: () => Promise<T>,
  intervalMs: number,
): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let alive = true;
    const tick = () => {
      fnRef.current()
        .then((value) => {
          if (alive) {
            setData(value);
            setError(null);
          }
        })
        .catch((err: Error) => {
          if (alive) setError(err.message);
        });
    };
    const refresh = () => {
      if (!document.hidden) tick();
    };
    tick();
    const timer = window.setInterval(refresh, intervalMs);
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [intervalMs]);

  return { data, error };
}

/** A value that re-renders on a timer (for ETAs and elapsed labels). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** Load a job and follow its live event stream until it reaches a terminal state. */
export function useJobStream(jobId: string | undefined): {
  job: Job | null;
  segments: Segment[];
  partials: Record<string, string>;
  levels: Record<string, number>;
  loading: boolean;
  error: string | null;
} {
  const [job, setJob] = useState<Job | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [partials, setPartials] = useState<Record<string, string>>({});
  const [levels, setLevels] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    setLoading(true);
    setError(null);
    setJob(null);
    setSegments([]);
    setPartials({});
    setLevels({});

    const closeStream = () => {
      sourceRef.current?.close();
      sourceRef.current = null;
    };

    const apply = (event: JobEvent) => {
      switch (event.type) {
        case "snapshot":
          setJob(event.job);
          setSegments(event.job.segments);
          break;
        case "status":
          setJob((prev) =>
            prev ? { ...prev, status: event.status, message: event.message, progress: event.progress } : prev,
          );
          break;
        case "progress":
          setJob((prev) => (prev ? { ...prev, progress: event.value, message: event.message } : prev));
          break;
        case "segment":
          setSegments((prev) => [...prev, event.segment]);
          break;
        case "segments_replaced":
          setSegments(event.segments);
          break;
        case "meta":
          setJob((prev) => (prev ? { ...prev, meta: event.meta } : prev));
          break;
        case "partial":
          setPartials((prev) => ({ ...prev, [event.speaker]: event.text }));
          break;
        case "level":
          setLevels((prev) => ({ ...prev, [event.track]: event.rms }));
          break;
        case "done":
          setPartials({});
          setJob((prev) =>
            prev ? { ...prev, status: "done", progress: 100, text: event.text, meta: event.meta } : prev,
          );
          closeStream();
          void api.getJob(jobId).then((final) => {
            if (alive) {
              setJob(final);
              setSegments(final.segments);
            }
          });
          break;
        case "error":
          setJob((prev) => (prev ? { ...prev, status: "error", error: event.message } : prev));
          closeStream();
          break;
        case "cancelled":
          setJob((prev) => (prev ? { ...prev, status: "cancelled" } : prev));
          closeStream();
          break;
      }
    };

    // A recording tab receives this job's events over its capture WebSocket;
    // an EventSource would take one of Chrome's six HTTP/1.1 sockets per host
    // and stall every further request (status polls included) once several
    // tabs record at once. Subscribe first, buffer until the snapshot lands.
    let ready = false;
    const pending: JobEvent[] = [];
    let unsubscribe: (() => void) | null = null;
    if (ownsLiveJob(jobId)) {
      unsubscribe = subscribeLiveEvents((event) => {
        if (!alive) return;
        if (ready) apply(event);
        else pending.push(event);
      });
    }

    void api
      .getJob(jobId)
      .then((initial) => {
        if (!alive) return;
        setJob(initial);
        setSegments(initial.segments);
        setLoading(false);
        if (initial.status === "queued" || initial.status === "running") {
          if (unsubscribe) {
            ready = true;
            const seen = new Set(initial.segments.map((s) => `${s.start}|${s.text}`));
            for (const event of pending.splice(0)) {
              if (event.type === "segment") {
                const key = `${event.segment.start}|${event.segment.text}`;
                if (seen.has(key)) continue; // already covered by the snapshot
                seen.add(key);
              }
              apply(event);
            }
          } else {
            const source = new EventSource(`/api/jobs/${jobId}/events`);
            sourceRef.current = source;
            source.onmessage = (message) => {
              try {
                apply(JSON.parse(message.data) as JobEvent);
              } catch {
                /* ignore malformed frames */
              }
            };
            // EventSource reconnects automatically on transient errors.
          }
        } else if (unsubscribe) {
          unsubscribe();
          unsubscribe = null;
        }
      })
      .catch((err: Error) => {
        if (alive) {
          setError(err.message);
          setLoading(false);
        }
      });

    return () => {
      alive = false;
      closeStream();
      unsubscribe?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  return { job, segments, partials, levels, loading, error };
}
