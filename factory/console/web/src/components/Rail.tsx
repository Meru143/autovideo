import { ChartNoAxesColumn, GitBranch, House, Settings as Gear, Tag, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { ChannelSummary } from "../api";
import { Link } from "../router";
import { ChannelChip } from "./ChannelChip";

function Item({ to, path, children }: { to: string; path: string; children: ReactNode }) {
  const here = path === to;
  return (
    <li>
      <Link
        to={to}
        aria-current={here ? "page" : undefined}
        className={`flex h-8 items-center gap-2.5 rounded-md px-2 text-sm transition-colors focus-visible:-outline-offset-2 ${
          here ? "bg-raised text-primary" : "text-secondary hover:bg-panel hover:text-primary"
        }`}
      >
        {children}
      </Link>
    </li>
  );
}

const Icon = ({ of: Glyph }: { of: LucideIcon }) => (
  <Glyph aria-hidden size={16} strokeWidth={1.5} className="shrink-0 text-muted" />
);

/** The left rail: the wordmark, Home, each channel by its chip, then the factory-wide pages */
export function Rail({ path, channels }: { path: string; channels: ChannelSummary[] }) {
  return (
    <aside className="row-span-2 flex flex-col border-r border-line">
      <div className="flex h-12 shrink-0 items-center px-4 text-base font-medium">Autovideo</div>
      <nav aria-label="Console" className="flex flex-col gap-3 p-2">
        <ul className="flex flex-col gap-0.5">
          <Item to="/" path={path}>
            <Icon of={House} />
            Home
          </Item>
          {channels.map((c) => (
            <Item key={c.name} to={`/channels/${c.name}`} path={path}>
              <ChannelChip channel={c} />
            </Item>
          ))}
        </ul>
        <ul className="flex flex-col gap-0.5 border-t border-line pt-3">
          <Item to="/topics" path={path}>
            <Icon of={Tag} />
            Topics
          </Item>
          <Item to="/changes" path={path}>
            <Icon of={GitBranch} />
            Changes
          </Item>
          <Item to="/stats" path={path}>
            <Icon of={ChartNoAxesColumn} />
            Stats
          </Item>
          <Item to="/settings" path={path}>
            <Icon of={Gear} />
            Settings
          </Item>
        </ul>
      </nav>
    </aside>
  );
}
