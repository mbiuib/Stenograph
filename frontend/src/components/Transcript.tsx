import { useEffect, useRef, useState } from "react";
import { fmtTimestamp, speakerColor, speakerLabel } from "../format";
import type { Segment } from "../types";

export function Transcript({
  segments,
  speakers,
  live,
  speakerNames,
  activeIndex = null,
  onSeek,
}: {
  segments: Segment[];
  speakers: string[];
  live: boolean;
  speakerNames?: Record<string, string>;
  /** Index of the segment matching the current playback time (player sync). */
  activeIndex?: number | null;
  /** Click on a row: jump the player to that moment. */
  onSeek?: (segment: Segment) => void;
}) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const activeRow = useRef<HTMLDivElement | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  useEffect(() => {
    if (!autoScroll) return;
    if (activeIndex != null && activeRow.current) {
      activeRow.current.scrollIntoView({ block: "nearest" });
      return;
    }
    if (scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [segments.length, autoScroll, activeIndex]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>
          Сегментов: {segments.length}
          {live && <span className="text-accent"> · идёт обработка…</span>}
        </span>
        <label className="flex cursor-pointer items-center gap-2 select-none">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(event) => setAutoScroll(event.target.checked)}
            className="accent-cyan-400"
          />
          Автопрокрутка
        </label>
      </div>
      <div ref={scroller} className="max-h-[62vh] overflow-y-auto rounded-lg border border-edge bg-bg/60">
        {segments.length === 0 && (
          <div className="p-6 text-center text-sm text-muted">Сегменты появятся по мере обработки…</div>
        )}
        {segments.map((segment, index) => {
          const color = segment.speaker ? speakerColor(segment.speaker, speakers) : undefined;
          const isActive = index === activeIndex;
          return (
            <div
              key={`${segment.index}-${index}`}
              ref={isActive ? activeRow : undefined}
              onClick={onSeek ? () => onSeek(segment) : undefined}
              title={onSeek ? "Перейти к этому моменту записи" : undefined}
              className={`flex gap-3 border-b border-edge/60 px-3 py-2 last:border-b-0 ${
                isActive
                  ? "bg-accent/10"
                  : live && index === segments.length - 1
                    ? "seg-enter bg-surface2/40"
                    : ""
              } ${onSeek ? (isActive ? "cursor-pointer" : "cursor-pointer hover:bg-surface2/30") : ""}`}
            >
              <span className="tabular w-16 shrink-0 pt-0.5 text-right text-xs text-muted">
                {fmtTimestamp(segment.start)}
              </span>
              {segment.speaker && (
                <span
                  className="mt-0.5 h-fit shrink-0 rounded-md border px-1.5 py-0.5 text-[11px]"
                  style={{ borderColor: color, color }}
                  title={segment.speaker}
                >
                  {speakerLabel(segment.speaker, speakerNames)}
                </span>
              )}
              <p className="min-w-0 text-sm leading-relaxed">{segment.text}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
