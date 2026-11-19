import { Fragment, useEffect, useState, type ReactNode } from "react";
import { api, type ChannelSummary, type Health } from "../api";
import { at, clock } from "../time";
import { usePoll } from "../usePoll";
import { Lamp } from "./Lamp";

const Mono = ({ children }: { children: ReactNode }) => <span className="font-mono">{children}</span>;
const Rule = () => <span aria-hidden className="h-4 w-px shrink-0 bg-line" />;
const pct = (n: number | null) => (n === null ? "—" : `${Math.round(n)}%`);

/** Now, read again as each minute turns, for the clock */
function useMinute() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setTimeout(() => setNow(new Date()), 60_000 - (now.getTime() % 60_000));
    return () => window.clearTimeout(t);
  }, [now]);
  return now;
}

/** What the last tick reported besides its time: a slot it skipped or is late for, refusals, broken channels */
function report(tick: Health["tick"], name: (n: string) => string, now: Date): ReactNode[] {
  if (!tick) return [];
  const who = tick.channel ? name(tick.channel) : "A channel";
  const out: ReactNode[] = [];
  if (tick.skipped_slot) out.push(<>{who} skipped its <Mono>{at(new Date(tick.skipped_slot), now)}</Mono> slot</>);
  if (tick.late_slot) out.push(<>{who} is late for its <Mono>{at(new Date(tick.late_slot), now)}</Mono> slot</>);
  if (Array.isArray(tick.refused) && tick.refused.length) {
    out.push(<><Mono>{tick.refused.length}</Mono> refused by the upload guard</>);
  }
  if (tick.broken?.length) {
    out.push(
      <span title={tick.broken.join("\n")}>
        Settings broken: {tick.broken.map((b) => name(b.split(":")[0])).join(", ")}
      </span>,
    );
  }
  return out;
}

/** The health bar on every page: the tick running or stopped, the videos being made, the last tick and its report,
 * the Claude account the factory signs in as and its quota, and the time here */
export function HealthBar({ channels }: { channels: ChannelSummary[] }) {
  const { data: h, error } = usePoll(api.health, 10_000);
  const now = useMinute();
  const name = (n: string) => channels.find((c) => c.name === n)?.display_name ?? n;
  const said: ReactNode[] = error
    ? [<span className="text-primary">Not connected</span>, error.message]
    : h
      ? [
          <span className="text-primary">{h.running ? "Running" : "Stopped"}</span>,
          <><Mono>{h.being_made}</Mono> being made</>,
          h.last_tick ? <>last tick <Mono>{at(new Date(h.last_tick), now)}</Mono></> : "no tick yet",
          ...report(h.tick, name, now),
        ]
      : [];
  const resets = h?.quota.week_resets && `The week's quota resets ${at(new Date(h.quota.week_resets), now)}`;
  return (
    <header
      aria-label="Factory health"
      className="flex h-12 items-center gap-6 border-b border-line px-6 text-sm text-secondary"
    >
      <p className="flex min-w-0 items-center gap-2.5">
        <Lamp kind={error || (h && !h.running) ? "attention" : h ? "ready" : "making"} />
        <span className="truncate">
          {said.map((part, i) => (
            <Fragment key={i}>
              {i > 0 && " · "}
              {part}
            </Fragment>
          ))}
        </span>
      </p>
      <div className="ml-auto flex shrink-0 items-center gap-4 whitespace-nowrap">
        {h?.account && (
          <>
            <span>{h.account}</span>
            <Rule />
          </>
        )}
        {h && (
          <>
            <span title={resets || undefined}>
              Quota: week <Mono>{pct(h.quota.week)}</Mono> · 5-hour <Mono>{pct(h.quota.five_hour)}</Mono>
            </span>
            <Rule />
          </>
        )}
        <Mono>{clock(now)}</Mono>
      </div>
    </header>
  );
}
