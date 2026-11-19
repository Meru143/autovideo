import * as Tabs from "@radix-ui/react-tabs";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError, errText, type Playbook } from "../api";
import { at } from "../time";
import { Button } from "./Button";
import { ConfirmDialog } from "./ConfirmDialog";
import { Diff } from "./Diff";
import { stageName } from "./VideoCard";
import { toast } from "./Toasts";

// ponytail: a plain textarea; a hidden mirror with the same font and width measures how tall each logical line wraps, and
// the gutter's numbers take those heights. No editor library.
const TEXT = "font-mono text-sm leading-6 py-3";

type Entry = Playbook["history"][number];

/** What the History offers Undo for: the changes feed's own test (the server refuses any other commit) */
const undoable = (h: Entry) => h.by === "lessons" || h.by === "loop" || h.subject.startsWith("console(");

/** A channel's playbook: its text in a monospace editor with line numbers and Save, and its History (each entry's diff,
 * and Undo, after asking, as the Changes page does, on the ones the factory or the console made). A save answered 409
 * (the file moved on since this was loaded) keeps what was typed beside a notice with Reload; nothing is overwritten.
 * `onChanged` tells the page a commit happened. */
export function PlaybookEditor({ channel, pkey, latest, onChanged, onDirty }: {
  channel: string; pkey: string;
  latest?: string; // the sha the page's last poll saw as this file's newest commit
  onChanged: () => void; onDirty: (dirty: boolean) => void;
}) {
  const [pb, setPb] = useState<Playbook>();
  const [text, setText] = useState("");
  const [tab, setTab] = useState("edit");
  const [conflict, setConflict] = useState<string>();
  const [diff, setDiff] = useState<{ sha: string; text: string }>();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string>();
  const [heights, setHeights] = useState<number[]>([]);
  const [ask, setAsk] = useState<Entry>(); // the History entry whose Undo waits for the owner's word
  const gutter = useRef<HTMLDivElement>(null);
  const mirror = useRef<HTMLDivElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const history = useRef<HTMLDivElement>(null);
  const name = stageName(pkey);
  // the newest commit when this text was loaded: a later one that isn't ours means the file moved on behind the editor
  const seen = useRef(latest);
  const latestRef = useRef(latest);
  latestRef.current = latest;

  const load = async () => {
    try {
      const p = await api.playbook(channel, pkey);
      setPb(p);
      setText(p.text);
      setConflict(undefined);
      setFailed(undefined);
      seen.current = latestRef.current;
    } catch (e) {
      setFailed(errText(e));
    }
  };
  useEffect(() => void load(), []); // eslint-disable-line react-hooks/exhaustive-deps -- the page remounts this per playbook

  const dirty = pb !== undefined && text !== pb.text;
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty]); // eslint-disable-line react-hooks/exhaustive-deps -- onDirty is the page's setter
  const moved = pb !== undefined && latest !== undefined && latest !== seen.current && !pb.base.startsWith(latest) && !latest.startsWith(pb.base);

  const measure = useCallback(() => {
    const m = mirror.current, a = area.current;
    if (!m || !a) return;
    m.style.width = `${a.clientWidth}px`;
    const next = [...m.children].map((c) => c.getBoundingClientRect().height);
    setHeights((old) => (old.join() === next.join() ? old : next));
  }, []);
  useLayoutEffect(measure, [text, pb, measure]);
  // The edit tab mounts a new textarea each time it is shown (after History, say), a render after the tab changes; so
  // each one is watched from its own mount, by its ref, and measured as it starts and whenever its width changes.
  const watch = useCallback((el: HTMLTextAreaElement | null) => {
    area.current = el;
    if (!el) return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      ro.disconnect();
      area.current = null;
    };
  }, [measure]);

  const save = async () => {
    if (!pb || busy) return;
    setBusy(true);
    try {
      await api.savePlaybook(channel, pkey, text, pb.base);
      toast({ title: "Saved", text: `${name} playbook saved` });
      await load();
      onChanged();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(e.message);
      else toast({ title: "Not saved", text: errText(e), lamp: "attention" });
    }
    setBusy(false);
  };

  const showDiff = async (sha: string) => {
    if (diff?.sha === sha) return setDiff(undefined);
    try {
      setDiff({ sha, text: (await api.diff(sha)).diff });
    } catch (e) {
      toast({ title: "No changes to show", text: errText(e), lamp: "attention" });
    }
  };

  const undo = async (sha: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await api.undo(sha);
      toast({ title: "Undone", text: `${name} playbook put back as it was before that change` });
      setDiff(undefined);
      await load();
      onChanged();
    } catch (e) {
      toast({ title: "Not undone", text: errText(e), lamp: "attention" });
    }
    setBusy(false);
  };

  // the ring sits inside the tab, as the rail's does, not over the panel's edge
  const tabClass = "h-10 border-b-2 border-transparent px-4 text-sm text-secondary transition-colors hover:text-primary focus-visible:-outline-offset-2 data-[state=active]:border-primary data-[state=active]:text-primary";
  return (
    <Tabs.Root value={tab} onValueChange={setTab} className="flex min-h-[34rem] min-w-0 flex-col rounded-xl border border-line bg-panel">
      <div className="flex items-center justify-between border-b border-line pr-3">
        <Tabs.List
          className="flex"
          // Radix passes a Tab into the list on to the open tab, except just after a click on a tab, when the list
          // keeps the focus itself, with no ring (its inline outline: none). Pass it on every time.
          onFocus={(e) => {
            if (e.target === e.currentTarget) e.currentTarget.querySelector<HTMLElement>('[data-state="active"]')?.focus();
          }}
        >
          <Tabs.Trigger value="edit" className={tabClass}>{name}</Tabs.Trigger>
          <Tabs.Trigger value="history" className={tabClass}>History</Tabs.Trigger>
        </Tabs.List>
        <Button primary disabled={!dirty || busy} onClick={() => void save()}>Save</Button>
      </div>

      {(conflict || moved) && (
        <div role="alert" className="flex items-start gap-3 border-b border-line bg-raised px-4 py-3 text-sm">
          <p className="flex-1">
            {conflict
              ? "This playbook changed since you opened it, so nothing was saved. What you typed is still here."
              : "This playbook changed since you opened it. What you typed stays until you reload."}
            {conflict && <span className="block text-secondary">{conflict}</span>}
          </p>
          <Button onClick={() => void load()}>Reload</Button>
        </div>
      )}
      {failed && <p className="px-4 py-3 text-sm text-secondary">The playbook didn't load: {failed}</p>}

      <Tabs.Content value="edit" className="relative flex min-h-0 flex-1 overflow-hidden">
        {pb && (
          <>
            <div
              ref={gutter}
              aria-hidden
              data-testid="line-numbers"
              className={`${TEXT} w-12 shrink-0 select-none overflow-hidden border-r border-line pr-3 text-right text-secondary`}
            >
              {text.split("\n").map((_, i) => <div key={i} style={{ height: heights[i] }}>{i + 1}</div>)}
            </div>
            <div
              ref={mirror}
              aria-hidden
              className={`${TEXT} invisible pointer-events-none absolute top-0 left-0 px-4 whitespace-pre-wrap break-words`}
            >
              {text.split("\n").map((l, i) => <div key={i}>{l || "​"}</div>)}
            </div>
            <textarea
              aria-label={`${name} playbook`}
              value={text}
              ref={watch}
              readOnly={busy}
              spellCheck={false}
              onChange={(e) => setText(e.target.value)}
              onScroll={(e) => gutter.current && (gutter.current.scrollTop = e.currentTarget.scrollTop)}
              className={`${TEXT} min-w-0 flex-1 resize-none overflow-y-scroll bg-transparent px-4 whitespace-pre-wrap break-words text-primary outline-offset-[-2px]`}
            />
          </>
        )}
      </Tabs.Content>

      <Tabs.Content ref={history} value="history" className="min-h-0 flex-1 overflow-y-auto p-3">
        {dirty && <p className="px-1 pb-3 text-sm text-secondary">Save or reload your edit before undoing a change.</p>}
        {pb && !pb.history.length && <p className="px-1 text-sm text-secondary">Nobody has changed this playbook yet.</p>}
        <ul aria-label="History" className="flex flex-col gap-2">
          {pb?.history.map((h) => (
            <li key={h.sha} className="rounded-sm border border-line bg-raised p-3">
              <p className="text-sm font-medium break-words">{h.subject}</p>
              <p className="mt-1 text-xs text-secondary">
                {h.by} · <span className="font-mono">{at(new Date(h.at))}</span>
              </p>
              <div className="mt-3 flex gap-2">
                <Button onClick={() => void showDiff(h.sha)} aria-expanded={diff?.sha === h.sha}>Show changes</Button>
                {undoable(h) && <Button disabled={busy || dirty} onClick={() => setAsk(h)}>Undo</Button>}
              </div>
              {diff?.sha === h.sha && <Diff text={diff.text} />}
            </li>
          ))}
        </ul>
      </Tabs.Content>
      <ConfirmDialog
        open={!!ask}
        onOpenChange={(o) => !o && setAsk(undefined)}
        title="Undo this change?"
        confirm="Undo"
        returnTo={history}
        onConfirm={() => {
          const h = ask;
          setAsk(undefined);
          if (h) void undo(h.sha);
        }}
      >
        {ask?.subject}. The {name} playbook goes back as it was before this change, as a new change; nothing is deleted.
      </ConfirmDialog>
    </Tabs.Root>
  );
}
