import { X } from "lucide-react";
import { useRef, useState } from "react";
import { api, errText, type ChannelSummary, type Topic } from "../api";
import { Button } from "../components/Button";
import { ChannelChip } from "../components/ChannelChip";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { toast } from "../components/Toasts";
import { at } from "../time";
import { usePoll } from "../usePoll";

type Ask = (t: Topic, c: ChannelSummary, done: () => void) => void;

/** One channel's queue: the briefs as the server sends them (queued first), each with its score, lever and status.
 * `busy`: a removal is out, so Remove waits. */
function Queue({ channel, ask, busy }: { channel: ChannelSummary; ask: Ask; busy: boolean }) {
  const { data, error, refresh } = usePoll(() => api.topics(channel.name), 10_000);
  const topics = data?.topics.filter((t) => t.status !== "removed") ?? []; // a removed brief is off the list
  return (
    <section aria-label={channel.display_name} className="rounded-xl border border-line bg-panel">
      <h2 className="border-b border-line px-4 py-3 text-base font-medium">
        <ChannelChip channel={channel} />
      </h2>
      <p role="status" className="px-4 py-3 text-sm text-secondary empty:hidden">
        {error && `The queue didn't ${data ? "refresh" : "load"}: ${error.message}`}
      </p>
      {data && !topics.length && <p className="px-4 py-3 text-sm text-secondary">Nothing is queued for this channel.</p>}
      <ul>
        {topics.map((t) => (
          <li key={t.id} className="flex items-center gap-4 border-b border-line px-4 py-3 last:border-b-0">
            <span className="w-8 shrink-0 text-right font-mono text-sm">{t.score}</span>
            <div className="min-w-0 flex-1 text-sm">
              <p className="font-medium break-words">{t.key}</p>
              <p className="mt-0.5 text-xs text-secondary">
                {t.lever} · {t.status} · <span className="font-mono">{at(new Date(t.created))}</span>
              </p>
            </div>
            {t.status === "queued" && (
              <Button disabled={busy} onClick={() => ask(t, channel, refresh)}>
                <X aria-hidden size={14} strokeWidth={1.5} className="mr-1.5 text-muted" />
                Remove
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Each channel's queue of topic briefs. Remove takes one off for good (it is never queued again), after asking. */
export function Topics({ channels }: { channels: ChannelSummary[] }) {
  const [target, setTarget] = useState<{ t: Topic; c: ChannelSummary; done: () => void }>();
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false);
  const back = useRef<HTMLDivElement>(null);

  const remove = async () => {
    if (!target || inflight.current) return;
    inflight.current = true;
    setBusy(true);
    const { t, done } = target;
    setTarget(undefined);
    try {
      await api.removeTopic(t.id);
      toast({ title: "Removed", text: t.key });
      done();
    } catch (e) {
      toast({ title: "Not removed", text: errText(e), lamp: "attention" });
    }
    inflight.current = false;
    setBusy(false);
  };

  return (
    <div ref={back} tabIndex={-1} className="flex max-w-3xl flex-col gap-4 outline-none">
      <h1 className="text-xl font-medium">Topics</h1>
      {channels.map((c) => <Queue key={c.name} channel={c} busy={busy} ask={(t, c, done) => setTarget({ t, c, done })} />)}
      <ConfirmDialog
        open={!!target}
        onOpenChange={(o) => !o && setTarget(undefined)}
        title="Remove this topic?"
        confirm="Remove"
        returnTo={back}
        onConfirm={() => void remove()}
      >
        {target?.t.key} comes off {target?.c.display_name}'s queue and is never queued again.
      </ConfirmDialog>
    </div>
  );
}
