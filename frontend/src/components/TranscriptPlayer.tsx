/** Audio player + transcript synced by playback time (job detail page). */
import { useRef, useState } from "react";
import { speakerLabel } from "../format";
import type { Job, Segment } from "../types";
import { Transcript } from "./Transcript";

interface TrackOption {
  key: string;
  label: string;
  url: string;
  /** true for Jitsi participant files: each has its own timeline. */
  speaker: boolean;
}

/** Playable sources of a job: the uploaded file, a live mix + tracks, or speakers. */
function buildTracks(job: Job, speakerNames?: Record<string, string>): TrackOption[] {
  const base = `/api/jobs/${job.id}/audio`;
  const kind = job.kind === "reprocess" ? String(job.meta.source_kind ?? "live") : job.kind;
  const audio = (job.meta.audio ?? {}) as Record<string, string>;
  if (kind === "file") {
    return [{ key: "", label: "Запись", url: base, speaker: false }];
  }
  if (kind === "live") {
    const options: TrackOption[] = [];
    if (Object.keys(audio).length > 0) {
      options.push({ key: "", label: "Микс (оба потока)", url: base, speaker: false });
    }
    if (audio.system) {
      options.push({
        key: "system",
        label: "Система («Они»)",
        url: `${base}/system`,
        speaker: false,
      });
    }
    if (audio.mic) {
      options.push({
        key: "mic",
        label: "Микрофон («Вы»)",
        url: `${base}/mic`,
        speaker: false,
      });
    }
    return options;
  }
  if (kind === "jitsi") {
    const options: TrackOption[] = [];
    if (job.meta.audio_timeline === "realtime") {
      // Дорожки на часах встречи — можно слушать единой записью «как вживую».
      options.push({ key: "", label: "Микс (вся встреча)", url: base, speaker: false });
    }
    for (const name of Object.keys(audio)) {
      options.push({
        key: name,
        label: speakerLabel(name, speakerNames),
        url: `${base}/${encodeURIComponent(name)}`,
        speaker: true,
      });
    }
    return options;
  }
  return [];
}

export function TranscriptPlayer({
  job,
  segments,
  speakers,
  live,
  speakerNames,
}: {
  job: Job;
  segments: Segment[];
  speakers: string[];
  live: boolean;
  speakerNames?: Record<string, string>;
}) {
  const tracks = buildTracks(job, speakerNames);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const current = tracks.find((track) => track.key === selectedKey) ?? tracks[0] ?? null;
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const pendingSeek = useRef<number | null>(null);

  const recording =
    job.status === "done" || job.status === "error" || job.status === "cancelled";

  const syncActive = () => {
    const audio = audioRef.current;
    if (!audio) return;
    const time = audio.currentTime;
    let index: number | null = null;
    for (let i = 0; i < segments.length; i += 1) {
      const segment = segments[i];
      if (current?.speaker && segment.speaker !== current.key) continue;
      if (segment.start <= time + 0.05) index = i;
      else break;
    }
    setActiveIndex(index);
  };

  const seek = (time: number, speaker?: string | null) => {
    const audio = audioRef.current;
    if (current?.speaker && speaker && tracks.some((track) => track.key === speaker)) {
      if (speaker !== current.key) {
        pendingSeek.current = time;
        setSelectedKey(speaker);
        return;
      }
    }
    if (!audio) return;
    audio.currentTime = Math.max(0, time);
    void audio.play().catch(() => {});
  };

  if (!recording || tracks.length === 0) {
    return (
      <Transcript segments={segments} speakers={speakers} live={live} speakerNames={speakerNames} />
    );
  }

  const hint = current?.speaker
    ? "Каждый спикер — отдельная дорожка: реплики подсвечиваются по его файлу; клик по реплике другого спикера переключит дорожку и перемотает."
    : "Клик по реплике — перемотка к этому моменту записи.";

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-lg border border-edge bg-bg/60 p-3">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          {tracks.length > 1 && (
            <label className="flex items-center gap-2 text-xs text-muted">
              Дорожка:
              <select
                value={current?.key ?? ""}
                onChange={(event) => {
                  pendingSeek.current = null;
                  setSelectedKey(event.target.value);
                }}
                className="rounded-md border border-edge bg-surface px-2 py-1 text-xs text-ink outline-none focus:border-accent/50"
              >
                {tracks.map((track) => (
                  <option key={track.key} value={track.key}>
                    {track.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <span className="text-xs text-muted">{hint}</span>
        </div>
        <audio
          key={`${current?.key ?? ""}`}
          ref={audioRef}
          src={current?.url}
          controls
          preload="metadata"
          className="h-10 w-full"
          onLoadedMetadata={() => {
            const audio = audioRef.current;
            if (audio && pendingSeek.current != null) {
              audio.currentTime = pendingSeek.current;
              pendingSeek.current = null;
              void audio.play().catch(() => {});
            }
            syncActive();
          }}
          onTimeUpdate={syncActive}
          onSeeked={syncActive}
        />
      </div>
      {(segments.length > 0 || live) && (
        <Transcript
          segments={segments}
          speakers={speakers}
          live={live}
          speakerNames={speakerNames}
          activeIndex={activeIndex}
          onSeek={(segment) => seek(segment.start, segment.speaker ?? null)}
        />
      )}
    </div>
  );
}
