/** Audio player + transcript synced by playback time (job detail page). */
import { useEffect, useRef, useState } from "react";
import { fmtClock, speakerLabel } from "../format";
import type { Job, Segment } from "../types";
import { IconDownload, IconPause, IconPlay } from "./Icons";
import { Transcript } from "./Transcript";

const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
/** Громкость: 1 = как в файле, до 3 — тихие записи слышно заметно лучше. */
const MAX_VOLUME = 3;
const VOLUME_KEY = "stenograph.player.volume";

const AUDIO_FILE_EXTS = ["wav", "mp3", "flac", "m4a", "aac", "ogg", "opus", "wma"];

/** true, когда браузер проигрывает исходник как есть (аудио-контейнер). */
function isAudioFile(name: string): boolean {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return AUDIO_FILE_EXTS.includes(ext);
}

/** Имя скачиваемого файла: аудио — как есть, звук из видео — .mp3, микс — .mp3, дорожка — .wav. */
function downloadName(job: Job, track: TrackOption | null): string | undefined {
  if (!track) return undefined;
  const kind = job.kind === "reprocess" ? String(job.meta.source_kind ?? "live") : job.kind;
  if (kind === "file") {
    if (isAudioFile(job.source_name)) return job.source_name;
    return `${job.source_name.replace(/\.[^.]+$/, "")}.mp3`;
  }
  const base = job.source_name.replace(/[\\/:*?"<>|]+/g, "·").trim() || job.id;
  if (track.key === "") return `${base}.mp3`;
  return `${base} — ${track.label}.wav`;
}

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
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [volume, setVolume] = useState(() => {
    const stored = Number(localStorage.getItem(VOLUME_KEY));
    return Number.isFinite(stored) && stored > 0 ? Math.min(stored, MAX_VOLUME) : 1;
  });
  const [audioError, setAudioError] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const pendingSeek = useRef<number | null>(null);
  /** Играть ли после догрузки новой дорожки (бесшовное переключение). */
  const pendingPlay = useRef(false);
  const audioCtx = useRef<AudioContext | null>(null);
  const gainNode = useRef<GainNode | null>(null);

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

  /** Обновляем позицию не чаще раза в секунду (подсветка живёт отдельно). */
  const tickPosition = () => {
    const audio = audioRef.current;
    if (!audio) return;
    const time = audio.currentTime;
    setPosition((previous) => (Math.floor(previous) === Math.floor(time) ? previous : time));
  };

  /**
   * Громкость через Web Audio: элемент отдаёт звук в GainNode, поэтому звук
   * можно УСИЛИТЬ выше 100% (нативная громкость <audio> умеет только до 1.0,
   * а тихие записи хочется слышать лучше). Граф создаётся лениво по первому
   * жесту пользователя (политика автозапуска) и один раз на элемент.
   */
  const ensureAudioGraph = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!gainNode.current) {
      try {
        const ctx = new AudioContext();
        const source = ctx.createMediaElementSource(audio);
        const gain = ctx.createGain();
        gain.gain.value = volume;
        source.connect(gain);
        gain.connect(ctx.destination);
        audioCtx.current = ctx;
        gainNode.current = gain;
      } catch {
        // Граф недоступен — падаем на нативную громкость (не выше 100%).
        audio.volume = Math.min(1, volume);
      }
    }
    void audioCtx.current?.resume().catch(() => {});
  };

  const changeVolume = (next: number) => {
    setVolume(next);
    try {
      localStorage.setItem(VOLUME_KEY, String(next));
    } catch {
      // приватный режим — просто не сохраняем
    }
    if (gainNode.current) gainNode.current.gain.value = next;
    else if (audioRef.current) audioRef.current.volume = Math.min(1, next);
  };

  const play = () => {
    ensureAudioGraph();
    const audio = audioRef.current;
    if (audio) void audio.play().catch(() => {});
  };

  const togglePlay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) play();
    else audio.pause();
  };

  useEffect(
    () => () => {
      void audioCtx.current?.close().catch(() => {});
    },
    [],
  );

  /**
   * Бесшовная смена дорожки: позиция и состояние воспроизведения
   * сохраняются — запись продолжается с того же места, не с нуля.
   */
  const switchTrack = (nextKey: string, seekTo?: number | null, forcePlay?: boolean) => {
    const audio = audioRef.current;
    const target = seekTo ?? (audio ? audio.currentTime : 0);
    pendingSeek.current = target;
    pendingPlay.current = forcePlay ?? (audio ? !audio.paused : false);
    setPosition(target);
    setAudioError(false);
    setSelectedKey(nextKey);
  };

  const seek = (time: number, speaker?: string | null) => {
    const audio = audioRef.current;
    if (current?.speaker && speaker && tracks.some((track) => track.key === speaker)) {
      if (speaker !== current.key) {
        switchTrack(speaker, time, true);
        return;
      }
    }
    if (!audio) return;
    audio.currentTime = Math.max(0, time);
    play();
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
                  switchTrack(event.target.value);
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
          ref={audioRef}
          src={current?.url}
          preload="metadata"
          className="hidden"
          onLoadedMetadata={() => {
            const audio = audioRef.current;
            if (!audio) return;
            setAudioError(false);
            setDuration(audio.duration || 0);
            audio.playbackRate = rate; // скорость не сбрасывается при смене дорожки
            if (pendingSeek.current != null) {
              // Бесшовная смена дорожки: продолжаем с той же секунды.
              audio.currentTime = pendingSeek.current;
              pendingSeek.current = null;
              setPosition(audio.currentTime);
            }
            const resumePlayback = pendingPlay.current;
            pendingPlay.current = false;
            if (resumePlayback) play();
            syncActive();
          }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={() => setAudioError(true)}
          onTimeUpdate={() => {
            syncActive();
            tickPosition();
          }}
          onSeeked={syncActive}
        />
        {audioError ? (
          <p className="text-xs text-err">Аудио недоступно — файл записи не найден на сервере.</p>
        ) : (
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={togglePlay}
              title={playing ? "Пауза" : "Слушать"}
              aria-label={playing ? "Пауза" : "Слушать"}
              className="grid size-9 shrink-0 place-items-center rounded-full border border-edge bg-surface2 text-accent transition-colors hover:border-accent/40 hover:bg-accent/10"
            >
              {playing ? <IconPause className="size-4" /> : <IconPlay className="size-4" />}
            </button>
            <span className="tabular w-24 shrink-0 text-center text-xs text-muted">
              {fmtClock(position)} / {fmtClock(duration)}
            </span>
            <input
              type="range"
              min={0}
              max={duration > 0 ? duration : 1}
              step={0.05}
              value={duration > 0 ? Math.min(position, duration) : 0}
              onChange={(event) => {
                const next = Number(event.target.value);
                const audio = audioRef.current;
                setPosition(next);
                if (audio) audio.currentTime = next;
              }}
              aria-label="Позиция воспроизведения"
              className="player-range h-1.5 min-w-0 flex-1 cursor-pointer appearance-none rounded-full"
              style={{
                background: `linear-gradient(to right, var(--color-accent) ${
                  duration > 0 ? (position / duration) * 100 : 0
                }%, var(--color-edge) ${duration > 0 ? (position / duration) * 100 : 0}%)`,
              }}
            />
            <span
              className="flex shrink-0 items-center gap-2"
              title="Громкость: тихие записи можно усилить до 300%"
            >
              <input
                type="range"
                min={0}
                max={MAX_VOLUME}
                step={0.05}
                value={volume}
                onChange={(event) => changeVolume(Number(event.target.value))}
                aria-label="Громкость"
                className="player-range h-1.5 w-20 cursor-pointer appearance-none rounded-full"
                style={{
                  background: `linear-gradient(to right, var(--color-accent) ${
                    (volume / MAX_VOLUME) * 100
                  }%, var(--color-edge) ${(volume / MAX_VOLUME) * 100}%)`,
                }}
              />
              <span className="tabular w-9 shrink-0 text-right text-xs text-muted">
                {Math.round(volume * 100)}%
              </span>
            </span>
            <select
              value={rate}
              onChange={(event) => {
                const next = Number(event.target.value);
                setRate(next);
                const audio = audioRef.current;
                if (audio) audio.playbackRate = next;
              }}
              title="Скорость воспроизведения"
              aria-label="Скорость воспроизведения"
              className="shrink-0 rounded-md border border-edge bg-surface px-2 py-1 text-xs text-ink outline-none focus:border-accent/50"
            >
              {SPEEDS.map((speed) => (
                <option key={speed} value={speed}>
                  {speed}×
                </option>
              ))}
            </select>
            <a
              href={current?.url}
              download={downloadName(job, current)}
              title="Скачать запись"
              aria-label="Скачать запись"
              className="grid size-9 shrink-0 place-items-center rounded-full border border-edge bg-surface2 text-muted transition-colors hover:border-accent/40 hover:text-ink"
            >
              <IconDownload className="size-4" />
            </a>
          </div>
        )}
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
