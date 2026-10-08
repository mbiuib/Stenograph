/**
 * Live-capture store: owns the running capture independently of the Live page.
 *
 * Navigating to another tab unmounts the Live page — but the recording must
 * not be lost with it, and going back must re-attach to the same session.
 * The capture (WebSocket + MediaStreams) lives here, at module scope; the
 * page only subscribes to the snapshot (``useSyncExternalStore``).
 */
import { startLiveCapture, type LiveCapture, type LiveTrack } from "./capture";

export interface ActiveLiveSession {
  jobId: string;
  tracks: LiveTrack[];
  startedAt: number; // epoch seconds
  levels: Record<string, number>;
}

export interface LiveSessionSnapshot {
  /** The session being recorded right now, if any. */
  active: ActiveLiveSession | null;
  /** The last finished session — for the "session finished" screen. */
  lastJobId: string | null;
  /** Why the capture closed unexpectedly (server stop, lost connection). */
  closedReason: string | null;
}

const EMPTY: LiveSessionSnapshot = { active: null, lastJobId: null, closedReason: null };

let snapshot: LiveSessionSnapshot = EMPTY;
let capture: LiveCapture | null = null;
const listeners = new Set<() => void>();

function emit(next: Partial<LiveSessionSnapshot>): void {
  snapshot = { ...snapshot, ...next };
  for (const listener of listeners) listener();
}

export function subscribeLiveSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getLiveSession(): LiveSessionSnapshot {
  return snapshot;
}

/** Start a recording owned by the store; rejects when one is already running. */
export async function startLiveSession(options: {
  tracks: LiveTrack[];
  language?: string | null;
  title?: string | null;
}): Promise<ActiveLiveSession> {
  if (capture) throw new Error("запись уже идёт");

  const started = await startLiveCapture({
    tracks: options.tracks,
    language: options.language,
    title: options.title,
    onLevel: (track, rms) => {
      const active = snapshot.active;
      if (!active) return;
      emit({ active: { ...active, levels: { ...active.levels, [track]: rms } } });
    },
    onClosed: (reason) => {
      const jobId = capture?.jobId ?? snapshot.active?.jobId ?? null;
      capture = null;
      emit({ active: null, lastJobId: jobId, closedReason: reason });
    },
  });

  capture = started;
  const active: ActiveLiveSession = {
    jobId: started.jobId,
    tracks: started.tracks,
    startedAt: Date.now() / 1000,
    levels: Object.fromEntries(started.tracks.map((track) => [track, 0])),
  };
  emit({ active, lastJobId: null, closedReason: null });
  return active;
}

/** Stop the running recording and move it to the "finished" state. */
export async function stopLiveSession(): Promise<string | null> {
  const current = capture;
  if (!current) return snapshot.lastJobId;
  const jobId = current.jobId;
  await current.stop();
  capture = null;
  emit({ active: null, lastJobId: jobId, closedReason: null });
  return jobId;
}

/** Leave the "session finished" screen and show the start panel again. */
export function clearFinishedLiveSession(): void {
  if (snapshot.lastJobId !== null || snapshot.closedReason !== null) {
    emit({ lastJobId: null, closedReason: null });
  }
}
