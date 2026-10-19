import { useState } from "react";

export interface BarDatum {
  label: string;
  value: number | null | undefined;
  /** Extra tooltip lines (e.g. sample counts, caveats). */
  detail?: string[];
}

/**
 * Horizontal single-series bar chart (magnitude → one hue). Bars grow from a shared
 * baseline, are capped at 14px, have a 4px rounded data-end and show the value at the
 * tip in text ink. The whole row is the hover target; a tooltip repeats the value.
 * Pages pair every chart with a table view of the same numbers.
 */
export function BarChart({
  data,
  format,
  ariaLabel,
}: {
  data: BarDatum[];
  format: (v: number) => string;
  ariaLabel: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const values = data.map((d) => d.value ?? 0);
  const max = Math.max(...values, 0) || 1;
  if (data.length === 0) return <p className="text-xs text-muted-foreground">No data yet.</p>;
  return (
    <div role="img" aria-label={ariaLabel} className="relative flex flex-col gap-1.5">
      {data.map((d, i) => {
        const width = d.value ? Math.max((d.value / max) * 100, 0.5) : 0;
        return (
          <div
            key={d.label}
            className="group grid cursor-default grid-cols-[minmax(6rem,11rem)_1fr] items-center gap-2 rounded px-1 py-0.5 hover:bg-muted/60"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <span className="truncate text-xs text-muted-foreground" title={d.label}>
              {d.label}
            </span>
            <div className="relative flex items-center gap-2">
              <div className="h-3.5 flex-1 border-l border-[var(--viz-grid)]">
                {d.value !== null && d.value !== undefined ? (
                  <div
                    className="h-full rounded-r-[4px]"
                    style={{ width: `${width}%`, background: "var(--viz-series-1)" }}
                  />
                ) : null}
              </div>
              <span className="w-16 shrink-0 text-right font-mono text-xs tabular-nums">
                {d.value !== null && d.value !== undefined ? format(d.value) : "—"}
              </span>
            </div>
            {hover === i && (
              <div
                role="tooltip"
                className="pointer-events-none absolute right-0 z-10 -translate-y-full rounded-md border bg-card px-2 py-1 text-xs shadow-md"
              >
                <div className="font-medium">{d.label}</div>
                <div className="font-mono">
                  {d.value !== null && d.value !== undefined ? format(d.value) : "no data"}
                </div>
                {d.detail?.map((line) => (
                  <div key={line} className="text-muted-foreground">
                    {line}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
