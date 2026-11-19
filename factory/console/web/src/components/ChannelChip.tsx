import type { ChannelSummary } from "../api";

/** A channel's small colour chip and its name. The chip sits in an icon-sized box, so a name beside it lines up with
 * the labels beside icons. A channel with no console.color gets the muted grey. */
export function ChannelChip({ channel }: { channel: Pick<ChannelSummary, "display_name" | "color"> }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-2.5">
      <span className="grid size-4 shrink-0 place-items-center">
        <span
          data-testid="chip"
          className="size-2.5 rounded-xs"
          style={{ backgroundColor: channel.color ?? "var(--color-muted)" }}
        />
      </span>
      <span className="truncate">{channel.display_name}</span>
    </span>
  );
}
