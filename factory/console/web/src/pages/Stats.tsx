import { api, type ChannelSummary, type Stats as Data } from "../api";
import { ChannelChip } from "../components/ChannelChip";
import { usePoll } from "../usePoll";

const count = new Intl.NumberFormat("en-GB");
// 1500000 as "1.5M", so an axis label fits; "en", as en-GB writes "1.5m", which reads as a length
const short = new Intl.NumberFormat("en", { notation: "compact" });
const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const n = (v: number | null) => (v === null ? "—" : count.format(v));
/** "2026-11-09" as "9 Nov": the series' dates are calendar days already, so no zone moves them */
const label = (date: string) => day.format(new Date(`${date}T00:00:00Z`));
const time = (date: string) => Date.parse(`${date}T00:00:00Z`);

const W = 720, H = 280, L = 64, R = 24, T = 36, B = 44; // the chart's box and its margins

/** The views axis, in about four steps of 1, 2 or 5 times a power of ten, never under 1, so every tick is a whole
 * round number: 31 views gives 0, 10, 20, 30, 40 */
function ticks(most: number) {
  const rough = Math.max(most, 1) / 4;
  const p = 10 ** Math.floor(Math.log10(rough));
  const step = Math.max(1, ([1, 2, 5, 10].find((m) => m * p >= rough) ?? 10) * p);
  return Array.from({ length: Math.max(1, Math.ceil(most / step)) + 1 }, (_, i) => i * step);
}

/** Cumulative views per channel, a line each in the channel's colour and its name in the legend. Plain SVG; the same
 * numbers are in the table under it. */
function Chart({ series, channels }: { series: Data["channels"]; channels: ChannelSummary[] }) {
  const lines = Object.entries(series).filter(([, pts]) => pts.length);
  const dates = [...new Set(lines.flatMap(([, pts]) => pts.map((p) => p.date)))].sort();
  if (!lines.length) return <p className="px-4 py-3 text-sm text-secondary">No views recorded yet.</p>;
  const [t0, t1] = [time(dates[0]), time(dates[dates.length - 1])];
  const yTicks = ticks(Math.max(...lines.flatMap(([, pts]) => pts.map((p) => p.views))));
  const top = yTicks[yTicks.length - 1];
  const x = (date: string) => L + (t1 === t0 ? (W - L - R) / 2 : ((time(date) - t0) / (t1 - t0)) * (W - L - R));
  const y = (v: number) => H - B - (v / top) * (H - T - B);
  const every = Math.ceil(dates.length / 7);
  const info = (name: string) => channels.find((c) => c.name === name);
  return (
    <svg
      role="img"
      aria-label={`Views by channel, ${label(dates[0])} to ${label(dates[dates.length - 1])}. The table below has the numbers.`}
      viewBox={`0 0 ${W} ${H}`}
      className="w-full text-secondary"
    >
      {yTicks.map((v) => (
        <g key={v}>
          <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="currentColor" strokeOpacity={0.15} />
          <text x={L - 10} y={y(v)} dy="0.32em" textAnchor="end" fill="currentColor" fontSize={12} className="font-mono">
            {short.format(v)}
          </text>
        </g>
      ))}
      {dates.map((d, i) => i % every === 0 && (
        <text key={d} data-testid="x-tick" x={x(d)} y={H - B + 20} textAnchor="middle" fill="currentColor" fontSize={12} className="font-mono">
          {label(d)}
        </text>
      ))}
      <text x={(L + W - R) / 2} y={H - 6} textAnchor="middle" fill="currentColor" fontSize={12}>Day (IST)</text>
      {lines.map(([name, pts], i) => {
        const color = info(name)?.color ?? "#75726D";
        return (
          <g key={name}>
            <polyline
              data-series={name}
              fill="none"
              stroke={color}
              strokeWidth={2}
              strokeLinejoin="round"
              points={pts.map((p) => `${x(p.date)},${y(p.views)}`).join(" ")}
            />
            {pts.map((p) => <circle key={p.date} cx={x(p.date)} cy={y(p.views)} r={3} fill={color} />)}
            <line x1={L + i * 190} x2={L + i * 190 + 18} y1={14} y2={14} stroke={color} strokeWidth={2} />
            <text x={L + i * 190 + 26} y={14} dy="0.32em" fill="currentColor" fontSize={13} className="fill-primary">
              {info(name)?.display_name ?? name}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

const TH = "px-4 py-2 text-left text-xs font-medium text-secondary";
const NUM = "px-4 py-2.5 text-right font-mono tabular-nums";

/** What the published videos have drawn, and each channel's views over the days. A video's % watched and engaged
 * views arrive with YouTube's analytics, a day or two after; until then they are "—". */
export function Stats({ channels }: { channels: ChannelSummary[] }) {
  const { data, error } = usePoll(api.stats, 60_000);
  const info = (name: string) => channels.find((c) => c.name === name) ?? { display_name: name, color: null };
  const names = data ? Object.keys(data.channels).filter((k) => data.channels[k].length) : [];
  const dates = data ? [...new Set(names.flatMap((k) => data.channels[k].map((p) => p.date)))].sort().reverse() : [];
  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <h1 className="text-xl font-medium">Stats</h1>
      <p role="status" className="text-sm text-secondary empty:hidden">
        {error && `The stats didn't ${data ? "refresh" : "load"}: ${error.message}`}
      </p>
      {data && (
        <>
          <section aria-labelledby="chart-h" className="rounded-xl border border-line bg-panel">
            <h2 id="chart-h" className="border-b border-line px-4 py-3 text-base font-medium">Views by channel</h2>
            <div className="p-4"><Chart series={data.channels} channels={channels} /></div>
            {names.length > 0 && (
              <table aria-label="Views by day" className="w-full border-t border-line text-sm">
                <thead>
                  <tr className="border-b border-line">
                    <th scope="col" className={TH}>Day (IST)</th>
                    {names.map((k) => <th key={k} scope="col" className={`${TH} text-right`}>{info(k).display_name}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {dates.map((d) => (
                    <tr key={d} className="border-b border-line last:border-b-0">
                      <th scope="row" className="px-4 py-2.5 text-left font-mono font-normal">{label(d)}</th>
                      {names.map((k) => (
                        <td key={k} className={NUM}>{n(data.channels[k].find((p) => p.date === d)?.views ?? null)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section aria-labelledby="videos-h" className="rounded-xl border border-line bg-panel">
            <h2 id="videos-h" className="border-b border-line px-4 py-3 text-base font-medium">Videos</h2>
            {data.videos.length ? (
              <table aria-label="Videos" className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line">
                    <th scope="col" className={TH}>Video</th>
                    <th scope="col" className={`${TH} text-right`}>Views</th>
                    <th scope="col" className={`${TH} text-right`}>Likes</th>
                    <th scope="col" className={`${TH} text-right`}>% watched</th>
                    <th scope="col" className={`${TH} text-right`}>Engaged views</th>
                    <th scope="col" className={`${TH} text-right`}>Age</th>
                  </tr>
                </thead>
                <tbody>
                  {data.videos.map((v) => (
                    <tr key={`${v.channel}/${v.video_id}`} className="border-b border-line last:border-b-0">
                      <td className="px-4 py-2.5">
                        <p className="font-medium">{v.title}</p>
                        <p className="mt-0.5 text-xs text-secondary"><ChannelChip channel={info(v.channel)} /></p>
                      </td>
                      <td className={NUM}>{n(v.views)}</td>
                      <td className={NUM}>{n(v.likes)}</td>
                      <td className={NUM}>{v.avg_view_pct === null ? "—" : `${Math.round(v.avg_view_pct)}%`}</td>
                      <td className={NUM}>{n(v.engaged_views)}</td>
                      <td className={NUM}>{v.age_days} {v.age_days === 1 ? "day" : "days"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="px-4 py-3 text-sm text-secondary">No video has been posted yet.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
