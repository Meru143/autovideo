import { useRef, useState } from "react";
import { api, errText, type Change, type ChannelSummary } from "../api";
import { Button } from "../components/Button";
import { ChannelChip } from "../components/ChannelChip";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Diff } from "../components/Diff";
import { toast } from "../components/Toasts";
import { at } from "../time";
import { usePoll } from "../usePoll";

const list = new Intl.ListFormat("en-GB", { type: "conjunction" });

/** "settings.json goes back as it was", "music.md and scene.md go back as they were": what an undo puts back */
const goesBack = (files: string[]) =>
  `${list.format(files.map((f) => f.split("/").pop() ?? f)) || "It"} ${files.length > 1 ? "go back as they were" : "goes back as it was"}`;

/** What the factory's lessons and learning loop, and the owner's own edits, changed in the channels' playbooks and
 * settings, newest first. Each can be read as a diff, and Undo puts it back (a new commit, after asking). One that
 * is undone already says so and offers nothing. While one Undo is out, the others wait. */
export function Changes({ channels }: { channels: ChannelSummary[] }) {
  const { data, error, refresh } = usePoll(api.changes, 10_000);
  const [diff, setDiff] = useState<{ sha: string; text: string }>();
  const [ask, setAsk] = useState<Change>();
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false);
  const back = useRef<HTMLDivElement>(null);

  const show = async (sha: string) => {
    if (diff?.sha === sha) return setDiff(undefined);
    try {
      setDiff({ sha, text: (await api.diff(sha)).diff });
    } catch (e) {
      toast({ title: "No changes to show", text: errText(e), lamp: "attention" });
    }
  };
  const undo = async (c: Change) => {
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setAsk(undefined);
    try {
      await api.undo(c.sha);
      toast({ title: "Undone", text: c.subject });
      setDiff(undefined);
      refresh();
    } catch (e) {
      toast({ title: "Not undone", text: errText(e), lamp: "attention" });
    }
    inflight.current = false;
    setBusy(false);
  };

  return (
    <div ref={back} tabIndex={-1} className="flex max-w-3xl flex-col gap-4 outline-none">
      <h1 className="text-xl font-medium">Changes</h1>
      <p role="status" className="text-sm text-secondary empty:hidden">
        {error && `The changes didn't ${data ? "refresh" : "load"}: ${error.message}`}
      </p>
      {data && !data.changes.length && <p className="text-sm text-secondary">Nothing has changed a channel's playbooks or settings yet.</p>}
      <ul aria-label="Changes" className="flex flex-col gap-2">
        {data?.changes.map((c) => {
          const chan = channels.find((x) => x.name === c.channel);
          return (
            <li key={c.sha} data-sha={c.sha} className="rounded-xl border border-line bg-panel p-4">
              <p className="text-sm font-medium break-words">{c.subject}</p>
              <p className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-secondary">
                <span>{c.by}</span>
                {c.channel && <ChannelChip channel={chan ?? { display_name: c.channel, color: null }} />}
                <span className="font-mono">{at(new Date(c.at))}</span>
                <span className="font-mono">{c.files.join(", ")}</span>
              </p>
              <div className="mt-3 flex items-center gap-2">
                <Button onClick={() => void show(c.sha)} aria-expanded={diff?.sha === c.sha}>Show changes</Button>
                {c.reverted ? <span className="text-sm text-secondary">Undone</span>
                  : <Button disabled={busy} onClick={() => setAsk(c)}>Undo</Button>}
              </div>
              {diff?.sha === c.sha && <Diff text={diff.text} />}
            </li>
          );
        })}
      </ul>
      <ConfirmDialog
        open={!!ask}
        onOpenChange={(o) => !o && setAsk(undefined)}
        title="Undo this change?"
        confirm="Undo"
        returnTo={back}
        onConfirm={() => ask && void undo(ask)}
      >
        {ask?.subject}. {ask && goesBack(ask.files)} before this change, as a new change; nothing is deleted.
      </ConfirmDialog>
    </div>
  );
}
