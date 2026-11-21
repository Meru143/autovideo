import { useEffect, useRef, useState } from "react";
import { api, errText, type ChannelSummary, type IngestStatus, type LiveWatcherStatus, type VideoCard as Card } from "../api";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { toast } from "../components/Toasts";
import { ChannelChip } from "../components/ChannelChip";
import { Lamp } from "../components/Lamp";
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

function IngestLogTail({ ingestId }: { ingestId: string }) {
  const { data } = usePoll(() => api.ingestLog(ingestId), 4_000);
  const lines = data?.lines ?? [];
  return (
    <pre className="mt-2 max-h-40 overflow-auto rounded border border-line bg-panel p-2 font-mono text-[11px] leading-snug text-secondary whitespace-pre-wrap break-words">
      {lines.length ? lines.join("\n") : "No log lines yet…"}
    </pre>
  );
}

export function Home({ channels }: { channels: ChannelSummary[] }) {
  const { data, error, refresh } = usePoll(board, 10_000);
  const answered = useAnswered();
  const seen = useRef(answered);
  useEffect(() => {
    if (answered === seen.current) return; // the mount's own poll is fresh already
    seen.current = answered;
    refresh();
  }, [answered, refresh]);
  const [openLog, setOpenLog] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const clearBtn = useRef<HTMLButtonElement>(null);
  const videos = data?.videos;
  const ingests: IngestStatus[] = data?.ingests ?? [];
  const watchers: LiveWatcherStatus[] = data?.live_watchers ?? [];
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
              <div className="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
                <h2 id={`column-${key}`} className="text-base font-medium">
                  {title}
                  {count && here && (
                    <span className="text-secondary">
                      {" "}(<span className="font-mono">{here.length}</span>)
                    </span>
                  )}
                </h2>
                {key === "making" && (here?.some((v) => !v.running) ?? false) && (
                  <Button ref={clearBtn} busy={clearing} onClick={() => setConfirmClear(true)}>
                    {clearing ? "Clearing…" : "Clear queued"}
                  </Button>
                )}
              </div>
              {key === "making" && (watchers.length > 0 || ingests.length > 0) && (
                <ul className="flex flex-col gap-2 border-b border-line p-2">
                  {watchers.map((w) => {
                    const active = w.stage === "starting" || w.stage === "watching";
                    return (
                      <li key={w.id} className="rounded-lg border border-line bg-page px-3 py-2 text-sm">
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex items-center gap-2 font-medium">
                            <Lamp kind={w.stage === "error" ? "attention" : active ? "making" : "ready"} />
                            <ChannelChip channel={channels.find((c) => c.name === w.channel) ?? { display_name: w.channel, color: null }} />
                          </span>
                          <span className="flex items-center gap-2">
                            <span className="font-mono text-xs text-secondary">live · {w.stage}</span>
                            {active && (
                              <Button
                                onClick={async () => {
                                  try {
                                    await api.stopLiveWatch(w.id);
                                    toast({ title: "Stopping live watcher", text: "Flushing any pending spikes…", lamp: "ready" });
                                    refresh();
                                  } catch (e) {
                                    toast({ title: "Stop failed", text: errText(e), lamp: "attention" });
                                  }
                                }}
                              >
                                Stop
                              </Button>
                            )}
                            {!active && (
                              <Button
                                onClick={async () => {
                                  try {
                                    await api.dismissLiveWatch(w.id);
                                    refresh();
                                  } catch (e) {
                                    toast({ title: "Dismiss failed", text: errText(e), lamp: "attention" });
                                  }
                                }}
                              >
                                Dismiss
                              </Button>
                            )}
                          </span>
                        </div>
                        <p className="mt-1 truncate text-xs text-secondary">{w.detail || w.url}</p>
                        <p className="mt-0.5 truncate font-mono text-xs text-secondary">
                          {w.spikes} spike{w.spikes === 1 ? "" : "s"} · {w.url}
                        </p>
                        {w.error && <p className="mt-1 text-xs text-lamp-attention">{w.error}</p>}
                      </li>
                    );
                  })}
                  {ingests.map((ing) => (
                    <li key={ing.id} className="rounded-lg border border-line bg-page px-3 py-2 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-2 font-medium">
                          <Lamp kind={ing.stage === "error" ? "attention" : ing.stage === "done" ? "ready" : "making"} />
                          <ChannelChip channel={channels.find((c) => c.name === ing.channel) ?? { display_name: ing.channel, color: null }} />
                        </span>
                        <span className="flex items-center gap-2">
                          <span className="font-mono text-xs text-secondary">{ing.stage}</span>
                          <Button onClick={() => setOpenLog((cur) => (cur === ing.id ? null : ing.id))}>
                            {openLog === ing.id ? "Hide log" : "Log"}
                          </Button>
                          {ing.stage === "error" && (
                            <Button
                              onClick={async () => {
                                try {
                                  await api.retryIngest(ing.id);
                                  toast({ title: "Ingest resumed", text: "Reusing cached audio on disk.", lamp: "ready" });
                                  refresh();
                                } catch (e) {
                                  toast({ title: "Retry failed", text: errText(e), lamp: "attention" });
                                }
                              }}
                            >
                              Retry
                            </Button>
                          )}
                          {(ing.stage === "error" || ing.stage === "done") && (
                            <Button
                              onClick={async () => {
                                try {
                                  await api.dismissIngest(ing.id);
                                  refresh();
                                } catch (e) {
                                  toast({ title: "Dismiss failed", text: errText(e), lamp: "attention" });
                                }
                              }}
                            >
                              Dismiss
                            </Button>
                          )}
                        </span>
                      </div>
                      {ing.title && <p className="mt-1 truncate text-xs font-medium text-primary" title={ing.title}>{ing.title}</p>}
                      <p className="mt-0.5 truncate text-xs text-secondary">{ing.detail || ing.url}</p>
                      {typeof ing.progress === "number" && (
                        <span aria-hidden className="mt-2 block h-1 overflow-hidden rounded-full bg-line">
                          <span
                            className="block h-full transition-all"
                            style={{
                              width: `${Math.max(4, Math.min(100, ing.progress))}%`,
                              backgroundColor: channels.find((c) => c.name === ing.channel)?.color ?? "var(--color-muted)",
                            }}
                          />
                        </span>
                      )}
                      {ing.error && <p className="mt-1 text-xs text-lamp-attention">{ing.error}</p>}
                      {openLog === ing.id && <IngestLogTail ingestId={ing.id} />}
                    </li>
                  ))}
                </ul>
              )}
              {here?.length ? (
                <ul className="flex flex-col gap-2 p-2">
                  {here.map((v) => (
                    <VideoCard
                      key={v.id}
                      card={v}
                      channel={channels.find((c) => c.name === v.channel)}
                      most={most}
                      asked={asked}
                      onRefresh={refresh}
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
      <ConfirmDialog
        returnTo={clearBtn}
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title="Clear all queued videos?"
        confirm="Clear queued"
        onConfirm={() => {
          setConfirmClear(false);
          setClearing(true);
          void (async () => {
            try {
              const res = await api.dropQueued();
              toast({ title: "Queue cleared", text: `Dropped ${res.count} queued video(s).`, lamp: "ready" });
              refresh();
            } catch (e) {
              toast({ title: "Clear failed", text: errText(e), lamp: "attention" });
            }
            setClearing(false);
          })();
        }}
      >
        Drops every idle queued or stopped video in Being made. Actively running jobs are left alone.
      </ConfirmDialog>
    </div>
  );
}
