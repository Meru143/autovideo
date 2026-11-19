import { useEffect, useRef } from "react";
import { api, type ChannelSummary, type VideoCard as Card } from "../api";
import { VideoCard } from "../components/VideoCard";
import { useAnswered } from "../post";
import { usePoll } from "../usePoll";

// The board's columns in the contract's order; "empty" is what a column says with no video in it
const COLUMNS: { key: Card["column"]; title: string; count: boolean; empty: string }[] = [
  { key: "making", title: "Being made", count: true, empty: "Nothing is being made" },
  { key: "ready", title: "Ready to post", count: true, empty: "Finished videos wait here with a Post now button" },
  { key: "live", title: "On air", count: false, empty: "Nothing has gone on air in the last 7 days" },
];

/** The board, with when it was asked for: a card's post answered after that still reads as posting */
const board = async () => {
  const asked = Date.now();
  return { ...(await api.videos()), asked };
};

/** Home: every video the tick made, on a board of three columns, polled every 10 s, and at once when a post is
 * answered */
export function Home({ channels }: { channels: ChannelSummary[] }) {
  const { data, error, refresh } = usePoll(board, 10_000);
  const answered = useAnswered();
  const seen = useRef(answered);
  useEffect(() => {
    if (answered === seen.current) return; // the mount's own poll is fresh already
    seen.current = answered;
    refresh();
  }, [answered, refresh]);
  const videos = data?.videos;
  const asked = data?.asked ?? 0;
  const most = Math.max(0, ...(videos ?? []).map((v) => (v.column === "live" && v.views) || 0));
  return (
    <div className="flex min-h-full flex-col gap-5">
      <h1 className="text-xl font-medium">Home</h1>
      {/* a failed poll says so, and the board keeps what it had: "didn't refresh" means it may be out of date */}
      <p role="status" className="text-sm text-secondary empty:hidden">
        {error && `The board didn't ${data ? "refresh" : "load"}: ${error.message}`}
      </p>
      <div className="grid flex-1 grid-cols-3 gap-4">
        {COLUMNS.map(({ key, title, count, empty }) => {
          const here = videos?.filter((v) => v.column === key);
          return (
            <section key={key} aria-labelledby={`column-${key}`} className="flex flex-col rounded-xl border border-line bg-panel">
              <h2 id={`column-${key}`} className="border-b border-line px-4 py-3 text-base font-medium">
                {title}
                {count && here && (
                  <span className="text-secondary">
                    {" "}(<span className="font-mono">{here.length}</span>)
                  </span>
                )}
              </h2>
              {here?.length ? (
                <ul className="flex flex-col gap-2 p-2">
                  {here.map((v) => (
                    <VideoCard
                      key={v.id}
                      card={v}
                      channel={channels.find((c) => c.name === v.channel)}
                      most={most}
                      asked={asked}
                    />
                  ))}
                </ul>
              ) : (
                here && <p className="px-4 py-3 text-sm text-secondary">{empty}</p>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
