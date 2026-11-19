import {
  ArrowLeft, BookOpen, CalendarClock, CircleAlert, Clapperboard, ClipboardCheck, RotateCcw, Send, Trash2,
} from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, errText, type ChannelSummary, type VideoDetail } from "../api";
import { Button } from "../components/Button";
import { ChannelChip } from "../components/ChannelChip";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Lamp } from "../components/Lamp";
import { toast } from "../components/Toasts";
import { SlotTime, stageName } from "../components/VideoCard";
import { post, useAnswered, usePosting } from "../post";
import { Link, navigate } from "../router";
import { at } from "../time";
import { usePoll } from "../usePoll";

const KIND = { error: CircleAlert, retry: RotateCcw, lesson: BookOpen, review: ClipboardCheck, post: Send, drop: Trash2 };
const Mono = ({ children }: { children: ReactNode }) => <span className="font-mono">{children}</span>;
const when = (iso: string) => <Mono>{at(new Date(iso))}</Mono>;

/** The video, asked for with the time it was, so a post answered after that still reads as posting */
const fetched = async (id: string) => {
  const asked = Date.now();
  return { video: await api.video(id), asked };
};

/** A panel with its own heading, the heading naming it for screen readers */
function Panel({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="rounded-xl border border-line bg-panel px-4 py-3">
      <h2 id={id} className="mb-2 text-sm font-medium text-secondary">{title}</h2>
      {children}
    </section>
  );
}

/** A stage's mark on the timeline: lit when done, ringed when it is the one being worked on, amber and hollow when it
 * stopped, dim when still to come */
const Mark = ({ state }: { state: VideoDetail["stages"][number]["state"] }) =>
  state === "done" ? <Lamp kind="ready" />
  : state === "stopped" ? <Lamp kind="attention" />
  : state === "current" ? <span aria-hidden className="inline-block size-2.5 shrink-0 rounded-full border-2 border-primary" />
  : <Lamp kind="making" />;
const MARKED = { done: "", current: "now", stopped: "stopped", todo: "" };

/** A few lines of the script, cut at a word */
function excerpt(script: string): string {
  const text = script.trim().replace(/\s+/g, " ");
  return text.length <= 280 ? text : `${text.slice(0, 280).replace(/\s+\S*$/, "")}…`;
}

/** The player, 9:16: the video from the console's /media (which plays and seeks), or, with no final.mp4 yet, its
 * contact sheet or a plain placeholder. A video that won't load (/media failed) gets the placeholder and says so,
 * rather than a dead player. */
function Player({ video }: { video: VideoDetail }) {
  const thumb = video.thumb && `${video.thumb}?v=${encodeURIComponent(video.updated)}`;
  const [failed, setFailed] = useState<string>(); // the media address that failed: a new one gets its own try
  return (
    <div data-testid="player" className="relative grid aspect-9/16 w-full place-items-center overflow-hidden rounded-xl border border-line bg-panel">
      {video.media && failed !== video.media ? (
        <video
          controls playsInline preload="metadata" src={video.media} aria-label={video.title}
          poster={thumb || undefined} onError={() => setFailed(video.media ?? undefined)} className="size-full object-contain"
        />
      ) : thumb && !failed ? (
        <img src={thumb} alt="" className="size-full object-cover" />
      ) : (
        <p className="flex flex-col items-center gap-3 text-sm text-secondary">
          <Clapperboard aria-hidden size={32} strokeWidth={1.5} className="text-muted" />
          {failed && "The video didn't load"}
        </p>
      )}
    </div>
  );
}

/** A video's page: the player on the left; on the right its stages with when each finished, the script's start, its
 * slot, Post now, Retry and Drop, and what happened to it. Polled every 5 s while the factory is on it. */
export function Video({ id, channels }: { id: string; channels: ChannelSummary[] }) {
  const [making, setMaking] = useState(true);
  const { data, error, refresh } = usePoll(() => fetched(id), making ? 5_000 : 60_000);
  const video = data?.video;
  useEffect(() => {
    if (video) setMaking(video.column === "making");
  }, [video]);

  const answered = useAnswered();
  const seen = useRef(answered);
  useEffect(() => {
    if (answered === seen.current) return; // the mount's own poll is fresh already
    seen.current = answered;
    refresh();
  }, [answered, refresh]);

  const posting = usePosting(id, data?.asked ?? 0);
  const [dropping, setDropping] = useState(false);
  const [busy, setBusy] = useState(false);
  const reason = useId();
  const dropButton = useRef<HTMLButtonElement>(null);

  if (!video) {
    return (
      <div className="flex flex-col gap-3">
        <h1 className="text-xl font-medium">Video</h1>
        <p className="text-sm text-secondary">
          {error ? (error.status === 404 ? "No video has this id." : `The video didn't load: ${error.message}`) : "Loading…"}
        </p>
        <Link to="/" className="w-fit text-sm text-primary underline underline-offset-4">Back to Home</Link>
      </div>
    );
  }

  const channel = channels.find((c) => c.name === video.channel);
  const { can, posted } = video;
  const act = async (run: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await run();
    } catch (e) {
      toast({ title: video.title, text: errText(e), lamp: "attention" });
    }
    setBusy(false);
    refresh();
  };
  const dropped = video.events.some((e) => e.kind === "drop"); // the factory keeps its page, and what became of it
  const why = dropped ? null : video.column === "making" ? (video.lamp === "attention" ? "Stopped, so it can't be posted yet" : "Still being made") : null;

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-start gap-3">
        <Link to="/" aria-label="Back to Home" className="mt-1 grid size-6 shrink-0 place-items-center rounded-sm hover:bg-line">
          <ArrowLeft aria-hidden size={20} strokeWidth={1.5} className="text-muted" />
        </Link>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-3">
            <h1 className="text-xl font-medium break-words">{video.title}</h1>
            <span className="text-sm text-secondary">
              <ChannelChip channel={channel ?? { display_name: video.channel, color: null }} />
            </span>
          </div>
          <p data-testid="state" className="mt-1 flex items-center gap-2 text-sm text-secondary">
            <span className="flex h-5 items-center"><Lamp kind={video.lamp} /></span>
            <span>
              {dropped ? "Dropped" : video.note ?? (video.column === "making" ? "Being made" : video.column === "ready" ? "Ready to post" : "On air")}
              {posted?.at && <> · Posted {when(posted.at)}</>}
            </span>
          </p>
        </div>
      </header>
      <p role="status" className="text-sm text-secondary empty:hidden">
        {error && `The video didn't refresh: ${error.message}`}
      </p>

      <div className="grid grid-cols-[minmax(0,22rem)_minmax(0,1fr)] items-start gap-5">
        <Player video={video} />
        <div className="flex max-w-2xl flex-col gap-3">
          <Panel title="Stages">
            <ol aria-label="Stages" className="flex flex-col">
              {video.stages.map((s) => (
                <li key={s.name} className="flex items-center gap-3 border-b border-line py-1.5 text-sm last:border-b-0">
                  <span className="flex h-5 items-center"><Mark state={s.state} /></span>
                  <span className={`min-w-0 flex-1 ${s.state === "todo" ? "text-secondary" : ""}`}>
                    {stageName(s.name)}
                    {MARKED[s.state] && <span className="text-secondary">: {MARKED[s.state]}</span>}
                  </span>
                  <span className="text-xs text-secondary">{s.done_at && when(s.done_at)}</span>
                </li>
              ))}
            </ol>
          </Panel>

          {video.script && (
            <Panel title="Script excerpt">
              <p className="text-sm">{excerpt(video.script)}</p>
            </Panel>
          )}

          {video.slot && (
            <Panel title={posted ? "Slot" : "Planned slot"}>
              <p className="flex items-center gap-3 text-sm">
                <CalendarClock aria-hidden size={20} strokeWidth={1.5} className="shrink-0 text-muted" />
                <span><SlotTime slot={video.slot} /></span>
              </p>
            </Panel>
          )}

          {video.column !== "live" && !dropped && (
            <div className="flex flex-wrap items-center gap-2">
              {video.capped && (
                <p className="w-full text-sm text-secondary">
                  Already posted today{video.slot && <> · posts at <SlotTime slot={video.slot} /></>}
                </p>
              )}
              {video.capped ? (
                <Button busy={posting} disabled={!can.anyway} onClick={() => void post(video, true)}>
                  {posting ? "Posting…" : "Post anyway"}
                </Button>
              ) : (
                <Button primary busy={posting} disabled={!can.post} aria-describedby={why ? reason : undefined} onClick={() => void post(video, false)}>
                  {posting ? "Posting…" : "Post now"}
                </Button>
              )}
              {can.retry && <Button disabled={busy || posting} onClick={() => void act(() => api.retry(id))}>Retry</Button>}
              {can.drop && <Button ref={dropButton} disabled={busy || posting} onClick={() => setDropping(true)}>Drop</Button>}
              {why && <p id={reason} className="w-full text-sm text-secondary">{why}</p>}
            </div>
          )}

          <Panel title="What happened">
            {video.events.length ? (
              <ul className="flex flex-col">
                {video.events.map((e, i) => {
                  const Icon = KIND[e.kind];
                  return (
                    <li key={i} className="flex items-start gap-3 border-b border-line py-1.5 text-sm last:border-b-0">
                      <span className="flex h-5 items-center">
                        <Icon aria-hidden size={16} strokeWidth={1.5} className="text-muted" />
                      </span>
                      <span className="min-w-0 flex-1 break-words">{e.text}</span>
                      <span className="shrink-0 text-xs text-secondary">{when(e.at)}</span>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-sm text-secondary">Nothing has gone wrong, or been changed, on this video.</p>
            )}
          </Panel>
        </div>
      </div>

      <ConfirmDialog
        returnTo={dropButton}
        open={dropping}
        onOpenChange={setDropping}
        title={`Drop “${video.title}”?`}
        confirm="Drop video"
        onConfirm={() => {
          setDropping(false);
          void act(async () => {
            await api.drop(id);
            navigate("/");
          });
        }}
      >
        It leaves the board, and the factory stops working on it, or posting it. This can't be undone from the console.
      </ConfirmDialog>
    </div>
  );
}
