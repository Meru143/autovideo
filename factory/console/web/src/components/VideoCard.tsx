import { Clapperboard } from "lucide-react";
import { useId, type ReactNode } from "react";
import type { ChannelSummary, Slot, VideoCard as Card } from "../api";
import { post, usePosting } from "../post";
import { Link } from "../router";
import { at } from "../time";
import { Button } from "./Button";
import { ChannelChip } from "./ChannelChip";
import { Lamp } from "./Lamp";

const Mono = ({ children }: { children: ReactNode }) => <span className="font-mono">{children}</span>;
const Line = ({ children, strong }: { children: ReactNode; strong?: boolean }) => (
  <p className={`text-xs ${strong ? "text-primary" : "text-secondary"}`}>{children}</p>
);
const count = new Intl.NumberFormat("en-GB");
export const stageName = (s: string) => s[0].toUpperCase() + s.slice(1).replaceAll("_", " "); // story_scene: "Story scene"

/** A slot as the console shows one: the channel's own time and city, then here, "17:00 New York · 03:30 IST" (dated
 * when it isn't today), the times in mono (the plan's Display rules); a line breaks only between the two */
export const SlotTime = ({ slot }: { slot: Slot }) => (
  <>
    <span className="whitespace-nowrap">
      <Mono>{slot.local}</Mono> {slot.city}
    </span>{" "}
    · <span className="whitespace-nowrap font-mono">{at(new Date(slot.at))}</span>
  </>
);

/** The video's own frame, 9:16 (a frame of final.mp4, or its contact sheet before that), asked for again whenever
 * the job moves on; a plain placeholder until it has one */
function Frame({ card }: { card: Card }) {
  return (
    <div data-testid="frame" className="grid aspect-9/16 w-18 shrink-0 place-items-center self-start overflow-hidden rounded-sm bg-panel">
      {card.thumb ? (
        <img src={`${card.thumb}?v=${encodeURIComponent(card.updated)}`} alt="" className="size-full object-cover" />
      ) : (
        <Clapperboard aria-hidden size={16} strokeWidth={1.5} className="text-muted" />
      )}
    </div>
  );
}

/** One video on the board: its frame, its title (a link to its page), its channel and tally lamp, then what it is
 * doing. Being made: why it stopped, and its stage. Ready: when it posts, and Post now; on a channel that has posted
 * today, Post anyway instead; either reads "Posting…" until a board asked for after the post's answer is on screen
 * (`asked`, ms). On air: when and where it posted, and its views against the board's best. */
export function VideoCard({ card, channel, most, asked }: {
  card: Card; channel?: ChannelSummary; most: number; asked: number;
}) {
  const title = useId();
  const posting = usePosting(card.id, asked);
  const send = (anyway: boolean) => void post(card, anyway);
  const { posted, step } = card;
  const where = posted && [posted.youtube && "YouTube", posted.instagram && "Instagram"].filter(Boolean).join(" + ");
  return (
    <li className="relative flex gap-3 rounded-sm border border-line bg-raised p-2">
      <Frame card={card} />
      <div className="flex min-w-0 flex-1 flex-col py-0.5">
        <div className="flex items-start gap-3">
          <h3 id={title} className="min-w-0 flex-1 text-sm font-medium break-words">
            {/* the link's ::after covers the card, so the whole card is its click target, and its focus ring */}
            <Link
              to={`/videos/${card.id}`}
              className="after:absolute after:inset-0 after:rounded-sm hover:underline hover:underline-offset-4 focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-secondary"
            >
              {card.title}
            </Link>
          </h3>
          <span className="flex h-5 items-center">
            <Lamp kind={card.lamp} />
          </span>
        </div>
        <span className="mt-1 text-xs text-secondary">
          <ChannelChip channel={channel ?? { display_name: card.channel, color: null }} />
        </span>

        <div className="mt-3 flex flex-col gap-1 empty:hidden">
          {card.note && <Line strong>{card.note}</Line>}
          {card.stage && step && (
            <Line>
              {stageName(card.stage)} · <Mono>{step[0]}</Mono> of <Mono>{step[1]}</Mono>
            </Line>
          )}
          {card.column === "ready" && !card.capped && card.slot && (
            <Line>
              Posts at <SlotTime slot={card.slot} />
            </Line>
          )}
          {posted && (
            <Line>
              Posted{posted.at && <> <Mono>{at(new Date(posted.at))}</Mono></>}
              {where && <> · <span className="whitespace-nowrap">{where}</span></>}
            </Line>
          )}
        </div>

        {card.column === "ready" && (
          <div className="relative z-10 mt-auto flex flex-wrap items-center gap-x-3 gap-y-2 pt-3">
            {card.capped ? (
              <>
                <Line>
                  Already posted today{card.slot && <> · posts at <SlotTime slot={card.slot} /></>}
                </Line>
                <Button busy={posting} onClick={() => send(true)} aria-describedby={title}>
                  {posting ? "Posting…" : "Post anyway"}
                </Button>
              </>
            ) : (
              <Button primary busy={posting} onClick={() => send(false)} aria-describedby={title}>
                {posting ? "Posting…" : "Post now"}
              </Button>
            )}
          </div>
        )}
        {card.views !== null && (
          <div className="mt-auto flex flex-col gap-2 pt-3">
            <Line>
              <Mono>{count.format(card.views)}</Mono> views
            </Line>
            <span aria-hidden className="h-1 bg-line">
              <span
                data-testid="views"
                className="block h-full"
                style={{
                  width: `${most > 0 ? (card.views / most) * 100 : 0}%`,
                  backgroundColor: channel?.color ?? "var(--color-muted)",
                }}
              />
            </span>
          </div>
        )}
      </div>
    </li>
  );
}
