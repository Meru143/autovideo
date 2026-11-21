import * as Switch from "@radix-ui/react-switch";
import { FileText, Film, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { api, errText, type ChannelDetail, type ChannelSummary, type RadarResult, type SettingsChange } from "../api";
import { Button } from "../components/Button";
import { ChannelChip } from "../components/ChannelChip";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PlaybookEditor } from "../components/PlaybookEditor";
import { toast } from "../components/Toasts";
import { stageName } from "../components/VideoCard";
import { guardNavigation, Link } from "../router";
import { at } from "../time";
import { usePoll } from "../usePoll";

const count = new Intl.NumberFormat("en-GB");
const dayOf = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const INPUT = "h-8 rounded-md border border-line bg-page px-2 font-mono text-sm text-primary";
const CARD = "rounded-xl border border-line bg-panel";

/** A value being edited: it follows `saved` until typed over, and goes back to it (reset) on a refusal. `saved` is
 * compared as text, so a poll that brings the same values back doesn't undo the typing. */
function useDraft<T>(saved: T, key: string) {
  const [d, set] = useState({ key, v: saved });
  if (d.key !== key) set({ key, v: saved });
  return [d.v, (v: T) => set({ key: d.key, v }), () => set({ key, v: saved })] as const;
}

/** A time of day in a zone, today, as a Date: 17:00 in "America/New_York" is the instant that wall clock reads it */
function instant(hm: string, zone: string): Date | undefined {
  const [h, m] = hm.split(":").map(Number);
  if (!(h >= 0 && h < 24 && m >= 0 && m < 60)) return;
  try {
    const wall = (d: Date) => {
      const p = Object.fromEntries(
        new Intl.DateTimeFormat("en-US", {
          timeZone: zone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric",
        }).formatToParts(d).map((x) => [x.type, +x.value]),
      );
      return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
    };
    const t = wall(new Date());
    const day = new Date(t);
    const guess = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), h, m);
    return new Date(guess - (wall(new Date(guess)) - guess));
  } catch {
    return; // a zone name this browser doesn't know
  }
}

/** "New York · 03:30 IST": the channel's city (from its time-zone name) and the slot here, the time in mono */
function SlotHere({ slots, zone }: { slots: string[]; zone: string }) {
  const city = zone.split("/").pop()?.replaceAll("_", " ") ?? zone;
  const here = slots.map((s) => instant(s, zone)).filter((d): d is Date => !!d).map((d) => at(d, d));
  return <span className="text-secondary">{city}{here.length > 0 && <> · <span className="font-mono">{here.join(", ")}</span></>}</span>;
}

type Save = (change: SettingsChange) => Promise<boolean>;

/** An on/off setting, saved as it flips. While a save is out it is aria-disabled, not disabled, so the keyboard keeps
 * its place on it. */
function Toggle({ label, on, saving, flip }: { label: string; on: boolean; saving: boolean; flip: (on: boolean) => void }) {
  const id = useId();
  return (
    <div className="flex items-center gap-3">
      <label htmlFor={id} className="text-sm text-secondary">{label}</label>
      <Switch.Root
        id={id}
        checked={on}
        aria-disabled={saving || undefined}
        onCheckedChange={(v) => !saving && flip(v)}
        className="relative h-5 w-9 shrink-0 rounded-full border border-muted bg-raised transition-colors hover:border-secondary data-[state=checked]:bg-primary"
      >
        <Switch.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-secondary transition-transform data-[state=checked]:translate-x-[18px] data-[state=checked]:bg-page" />
      </Switch.Root>
    </div>
  );
}

function Header({ c, save, saving }: { c: ChannelDetail; save: Save; saving: boolean }) {
  const [slot, setSlot, resetSlot] = useDraft(c.slots.join(", "), c.slots.join(", "));
  const [zone, setZone, resetZone] = useDraft(c.timezone, c.timezone);
  const [max, setMax, resetMax] = useDraft(String(c.max_per_day ?? ""), String(c.max_per_day ?? ""));
  const id = useId();

  const commitSlot = async () => {
    const slots = slot.split(/[\s,]+/).filter(Boolean);
    if (slots.join(", ") === c.slots.join(", ")) return resetSlot();
    if (!slots.length || !(await save({ slots }))) resetSlot();
  };
  const commitZone = async () => {
    const z = zone.trim();
    if (z === c.timezone) return resetZone();
    if (!z || !(await save({ timezone: z }))) resetZone();
  };
  const commitMax = async () => {
    const n = Number(max);
    if (max === String(c.max_per_day ?? "")) return;
    if (!max.trim() || !Number.isInteger(n) || !(await save({ max_per_day: n }))) resetMax();
  };
  /** A typed setting saves on Enter, staying in the field, or on leaving it. While a save is out the fields are
   * read-only, not disabled, so focus stays put, and nothing saves twice. */
  const edits = (name: string, commit: () => Promise<unknown>) => ({
    id: `${id}-${name}`, name, readOnly: saving, autoComplete: "off", spellCheck: false,
    onBlur: () => void (saving || commit()),
    onKeyDown: (e: KeyboardEvent) => void (e.key === "Enter" && !saving && commit()),
  });

  // groups are told apart by space alone: a rule between them would hang at the start of a wrapped line
  return (
    <header className="flex flex-wrap items-center gap-x-8 gap-y-3 border-b border-line pb-4">
      <h1 className="text-xl font-medium">
        <ChannelChip channel={c} />
      </h1>
      <Toggle label="Paused" on={c.paused} saving={saving} flip={(paused) => void save({ paused })} />
      <Toggle label="Auto-chain" on={Boolean(c.auto_chain)} saving={saving} flip={(auto_chain) => void save({ auto_chain })} />
      <div className="flex items-center gap-3 text-sm">
        <label htmlFor={`${id}-slots`} className="text-secondary">Daily slot</label>
        <input {...edits("slots", commitSlot)} value={slot} onChange={(e) => setSlot(e.target.value)} className={`${INPUT} w-28`} />
        <SlotHere slots={c.slots} zone={c.timezone} />
      </div>
      <div className="flex items-center gap-3 text-sm">
        <label htmlFor={`${id}-timezone`} className="text-secondary">Timezone</label>
        <input {...edits("timezone", commitZone)} value={zone} onChange={(e) => setZone(e.target.value)} className={`${INPUT} w-44`} />
      </div>
      <Toggle label="Instagram" on={c.instagram} saving={saving} flip={(instagram) => void save({ instagram })} />
      <div className="flex items-center gap-2 text-sm text-secondary">
        <label htmlFor={`${id}-max_per_day`}>Up to</label>
        <input
          {...edits("max_per_day", commitMax)}
          aria-label="Most new videos a day"
          type="number"
          min={1}
          value={max}
          placeholder="any"
          onChange={(e) => setMax(e.target.value)}
          className={`${INPUT} w-16`}
        />
        new videos a day
      </div>
    </header>
  );
}

function IngestVod({ channel, onQueued }: { channel: string; onQueued: () => void }) {
  const [url, setUrl] = useState("");
  const [numClips, setNumClips] = useState(5);
  const [layout, setLayout] = useState<"auto" | "fill" | "fit">("auto");
  const [busy, setBusy] = useState(false);

  const run = async () => {
    const clean = url.trim();
    if (!clean) return;
    setBusy(true);
    try {
      const res = await api.ingestVod(channel, { url: clean, num_clips: numClips, layout });
      toast({ title: "Ingest started", text: res.ingest_id ? "Running in background — track live progress on Home." : `Queued ${res.queued.length} clip job(s)`, lamp: "ready" });
      setUrl("");
      onQueued();
    } catch (e) {
      toast({ title: "Ingest failed", text: errText(e), lamp: "attention" });
    }
    setBusy(false);
  };

  return (
    <section aria-labelledby="ingest-h" className={CARD}>
      <h2 id="ingest-h" className="border-b border-line px-4 py-3 text-base font-medium">Ingest VOD or stream</h2>
      <div className="flex flex-col gap-3 p-4">
        <input type="text" value={url} placeholder="Twitch VOD URL, YouTube link, or local path…"
          onChange={(e) => setUrl(e.target.value)} className={`${INPUT} font-sans placeholder:text-secondary`} />
        <div className="flex items-center gap-2 text-sm">
          <label className="flex items-center gap-1.5 text-secondary">
            Clips
            <input type="number" min={1} max={15} value={numClips}
              onChange={(e) => setNumClips(Number(e.target.value))} className={`${INPUT} w-14`} />
          </label>
          <select value={layout} onChange={(e) => setLayout(e.target.value as "auto" | "fill" | "fit")} className={`${INPUT} flex-1`}>
            <option value="auto">auto (Smart framing)</option>
            <option value="fill">fill (Full frame)</option>
            <option value="fit">fit (Blurred bg)</option>
          </select>
        </div>
        <Button primary busy={busy} disabled={!url.trim()} onClick={() => void run()}>
          {busy ? "Ingesting…" : "Scan & queue clips"}
        </Button>
      </div>
    </section>
  );
}


function RadarAndLive({ channel, autoWatch, defaultQuery, save, saving, onStarted }: { channel: string; autoWatch: string[]; defaultQuery?: string; save: Save; saving: boolean; onStarted: () => void }) {
  const [query, setQuery] = useState(defaultQuery ?? "");
  const [minutes, setMinutes] = useState(60);
  const [liveUrl, setLiveUrl] = useState("");
  const [scanning, setScanning] = useState(false);
  const [watchingUrl, setWatchingUrl] = useState<string | null>(null);
  const [ingestingUrl, setIngestingUrl] = useState<string | null>(null);
  const [radar, setRadar] = useState<RadarResult | null>(null);
  const [watchList, setWatchList, resetWatchList] = useDraft(autoWatch.join(", "), autoWatch.join(", "));
  const commitWatchList = async () => {
    const list = watchList.split(/[\s,]+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
    if (list.join(", ") === autoWatch.join(", ")) return resetWatchList();
    if (!(await save({ auto_watch_streamers: list }))) resetWatchList();
  };

  const scan = async () => {
    const q = query.trim();
    if (!q) return;
    setScanning(true);
    try {
      const res = await api.clipRadar({ query: q, mode: "all", limit: 8, hours: 24 });
      setRadar(res);
    } catch (e) {
      toast({ title: "Radar scan failed", text: errText(e), lamp: "attention" });
    }
    setScanning(false);
  };

  const watch = async (targetUrl: string) => {
    const clean = targetUrl.trim();
    if (!clean) return;
    setWatchingUrl(clean);
    try {
      await api.startLiveWatch(channel, { url: clean, minutes });
      toast({ title: "Live watcher started", text: "Recording in background — track spikes & stop on Home.", lamp: "ready" });
      if (clean === liveUrl.trim()) setLiveUrl("");
      onStarted();
    } catch (e) {
      toast({ title: "Watch Live failed", text: errText(e), lamp: "attention" });
    }
    setWatchingUrl(null);
  };

  const quickIngest = async (targetUrl: string) => {
    setIngestingUrl(targetUrl);
    try {
      await api.ingestVod(channel, { url: targetUrl });
      toast({ title: "Ingest started", text: "Running in background — track live progress on Home.", lamp: "ready" });
      onStarted();
    } catch (e) {
      toast({ title: "Ingest failed", text: errText(e), lamp: "attention" });
    }
    setIngestingUrl(null);
  };

  return (
    <section aria-labelledby="radar-h" className={CARD}>
      <h2 id="radar-h" className="border-b border-line px-4 py-3 text-base font-medium">Radar &amp; Watch Live</h2>
      <div className="flex flex-col gap-3 p-4">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={query}
            placeholder="Game or topic…"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !scanning && void scan()}
            className={`${INPUT} min-w-0 flex-1 font-sans placeholder:text-secondary`}
          />
          <Button primary busy={scanning} disabled={!query.trim()} onClick={() => void scan()}>
            {scanning ? "Scanning…" : "Scan"}
          </Button>
        </div>

        <div className="flex items-center gap-2 border-t border-line pt-3">
          <input
            type="text"
            value={liveUrl}
            placeholder="Live Twitch or YouTube URL…"
            onChange={(e) => setLiveUrl(e.target.value)}
            className={`${INPUT} min-w-0 flex-1 font-sans placeholder:text-secondary`}
          />
          <input
            type="number"
            min={5}
            max={720}
            value={minutes}
            aria-label="Watch minutes"
            title="Watch duration (minutes)"
            onChange={(e) => setMinutes(Number(e.target.value))}
            className={`${INPUT} w-14`}
          />
          <Button busy={watchingUrl === liveUrl.trim() && !!liveUrl.trim()} disabled={!liveUrl.trim()} onClick={() => void watch(liveUrl)}>
            Watch
          </Button>
        </div>

        <div className="flex flex-col gap-1 border-t border-line pt-3">
          <label className="text-xs text-secondary">Auto-watch Twitch streamers (comma-separated, checked each tick)</label>
          <input
            value={watchList}
            readOnly={saving}
            placeholder="streamer1, streamer2"
            onChange={(e) => setWatchList(e.target.value)}
            onBlur={() => void (saving || commitWatchList())}
            onKeyDown={(e) => void (e.key === "Enter" && !saving && commitWatchList())}
            className={INPUT}
          />
        </div>

        {radar && (
          <div className="flex flex-col gap-3 border-t border-line pt-3 text-xs">
            <div>
              <p className="mb-1.5 font-medium text-secondary">Live streams ({radar.streams?.length ?? 0})</p>
              {radar.streams?.length ? (
                <ul className="flex flex-col gap-2">
                  {radar.streams.slice(0, 6).map((st) => (
                    <li key={st.url} className="flex items-center justify-between gap-2 rounded-md border border-line bg-page px-2.5 py-1.5">
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium text-primary">
                          <span className="font-mono text-secondary">[{st.platform}]</span> {st.channel}
                        </p>
                        <p className="truncate text-secondary">
                          <span className="font-mono">{count.format(st.viewers)}</span> viewers{st.live_for ? ` · ${st.live_for}` : ""} · {st.title}
                        </p>
                      </div>
                      <Button busy={watchingUrl === st.url} onClick={() => void watch(st.url)}>Watch</Button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-secondary">No live streams found.</p>
              )}
            </div>

            <div>
              <p className="mb-1.5 font-medium text-secondary">Trending videos ({radar.trends?.length ?? 0})</p>
              {radar.trends?.length ? (
                <ul className="flex flex-col gap-2">
                  {radar.trends.slice(0, 6).map((tr) => (
                    <li key={tr.url} className="flex items-center justify-between gap-2 rounded-md border border-line bg-page px-2.5 py-1.5">
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium text-primary">{tr.title}</p>
                        <p className="truncate text-secondary">
                          <span className="font-mono">{count.format(Math.round(tr.views_per_hour))}/h</span> · {tr.age_h}h · {tr.channel}
                          {tr.has_heatmap ? " · heatmap" : ""}
                        </p>
                      </div>
                      <Button busy={ingestingUrl === tr.url} onClick={() => void quickIngest(tr.url)}>Ingest</Button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-secondary">No trending videos in last 24h.</p>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function Topics({ c, save, saving }: { c: ChannelDetail; save: Save; saving: boolean }) {
  const t = c.topics;
  const [levers, setLevers, resetLevers] = useDraft(t.levers, t.levers.join("\n"));
  const [score, setScore, resetScore] = useDraft(String(t.min_score), String(t.min_score));
  const [ideas, setIdeas, resetIdeas] = useDraft(String(t.ideas_per_pass), String(t.ideas_per_pass));
  const [adding, setAdding] = useState("");

  const topics: NonNullable<SettingsChange["topics"]> = {};
  if (levers.join("\n") !== t.levers.join("\n")) topics.levers = levers;
  if (score !== String(t.min_score)) topics.min_score = Number(score);
  if (ideas !== String(t.ideas_per_pass)) topics.ideas_per_pass = Number(ideas);
  const valid = [score, ideas].every((s) => s.trim() !== "" && Number.isFinite(Number(s)));
  const dirty = Object.keys(topics).length > 0;

  const add = () => {
    const v = adding.trim();
    if (v && !levers.includes(v)) setLevers([...levers, v]);
    setAdding("");
  };
  const field = (label: string, name: string, value: string, set: (v: string) => void) => (
    <label className="flex items-center justify-between gap-3 text-sm">
      {label}
      <input
        aria-label={label} name={name} type="number" autoComplete="off" value={value}
        onChange={(e) => set(e.target.value)} className={`${INPUT} w-24`}
      />
    </label>
  );

  return (
    <section aria-labelledby="topics-h" className={CARD}>
      <h2 id="topics-h" className="border-b border-line px-4 py-3 text-base font-medium">Topic settings</h2>
      <div className="flex flex-col gap-4 p-4">
        <ul aria-label="Levers" className="flex flex-wrap gap-2">
          {levers.map((l) => (
            <li key={l} className="flex items-center gap-1.5 rounded-sm border border-line bg-raised py-1 pr-1 pl-2 text-sm">
              {l}
              <button
                type="button"
                aria-label={`Remove ${l}`}
                onClick={() => setLevers(levers.filter((x) => x !== l))}
                className="grid size-5 place-items-center rounded-sm hover:bg-line"
              >
                <X aria-hidden size={14} strokeWidth={1.5} className="text-muted" />
              </button>
            </li>
          ))}
        </ul>
        <input
          aria-label="Add a lever"
          name="lever"
          autoComplete="off"
          placeholder="Add a lever…"
          value={adding}
          onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          onBlur={add}
          className={`${INPUT} font-sans placeholder:text-secondary`}
        />
        <div className="flex flex-col gap-3 border-t border-line pt-4">
          {field("Min score", "min_score", score, setScore)}
          {field("Ideas per pass", "ideas_per_pass", ideas, setIdeas)}
        </div>
        <div className="flex items-center gap-2">
          <Button
            primary
            disabled={!dirty || !valid || saving}
            onClick={async () => {
              if (!(await save({ topics }))) {
                resetLevers();
                resetScore();
                resetIdeas();
              }
            }}
          >
            Save topics
          </Button>
          {dirty && (
            <Button onClick={() => { resetLevers(); resetScore(); resetIdeas(); setAdding(""); }}>Discard</Button>
          )}
        </div>
      </div>
    </section>
  );
}

function Recent({ c }: { c: ChannelDetail }) {
  const most = Math.max(0, ...c.recent.map((v) => v.views ?? 0));
  return (
    <section aria-labelledby="recent-h" className={CARD}>
      <h2 id="recent-h" className="border-b border-line px-4 py-3 text-base font-medium">Recent videos</h2>
      {c.recent.length ? (
        <ul>
          {c.recent.map((v) => (
            <li key={v.id} className="flex gap-3 border-b border-line px-4 py-3 last:border-b-0">
              <span className="flex h-5 items-center">
                <Film aria-hidden size={16} strokeWidth={1.5} className="text-muted" />
              </span>
              <div className="min-w-0 flex-1 text-sm">
                <Link to={`/videos/${v.id}`} className="font-medium hover:underline hover:underline-offset-4">{v.title}</Link>
                {v.views !== null ? (
                  <>
                    <p className="mt-1 text-xs text-secondary"><span className="font-mono">{count.format(v.views)}</span> views</p>
                    <span aria-hidden className="mt-2 block h-1 bg-line">
                      <span
                        data-testid="views"
                        className="block h-full"
                        style={{ width: `${most > 0 ? (v.views / most) * 100 : 0}%`, backgroundColor: c.color ?? "var(--color-muted)" }}
                      />
                    </span>
                  </>
                ) : (
                  <p className="mt-1 text-xs text-secondary">
                    {v.posted ? <>Posted <span className="font-mono">{at(new Date(v.posted.at))}</span></>
                      : v.note ?? (v.stage && v.step ? <>{stageName(v.stage)} · <span className="font-mono">{v.step[0]}</span> of <span className="font-mono">{v.step[1]}</span></> : "Ready to post")}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-4 py-3 text-sm text-secondary">No videos from this channel yet</p>
      )}
    </section>
  );
}

function Playbooks({ c, open, pick }: { c: ChannelDetail; open: string; pick: (key: string) => void }) {
  return (
    <section aria-labelledby="playbooks-h" className={`${CARD} self-start`}>
      <h2 id="playbooks-h" className="border-b border-line px-4 py-3 text-base font-medium">Playbooks</h2>
      <ul aria-label="Playbooks">
        {c.playbooks.map((p) => {
          const on = p.key === open;
          return (
            <li key={p.key} className="border-b border-line last:border-b-0">
              <button
                type="button"
                onClick={() => pick(p.key)}
                aria-current={on || undefined}
                className={`flex w-full items-center gap-3 border-l-2 px-3 py-2.5 text-left focus-visible:-outline-offset-2 ${on ? "bg-raised" : "border-transparent hover:bg-raised"}`}
                style={on ? { borderLeftColor: c.color ?? "var(--color-muted)" } : undefined}
              >
                <FileText aria-hidden size={16} strokeWidth={1.5} className="shrink-0 text-muted" />
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{stageName(p.key)}</span>
                  <span className="block truncate text-xs text-secondary">
                    {p.changed ? `${p.changed.by} · ${dayOf.format(new Date(p.changed.at))}` : "no changes yet"}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** A channel: its pause, slot and daily limit up top; its playbooks, the open one in the editor, and its topic
 * settings; its recent videos. Every change is a commit the server makes, validated by the factory's own loader, so a
 * refusal comes back as the loader's words in a note and the page shows the saved values again. */
export function Channel({ name, summary }: { name: string; summary?: ChannelSummary }) {
  const { data: c, error, refresh } = usePoll(() => api.channel(name), 10_000);
  const [picked, pick] = useState<string>();
  const [saving, setSaving] = useState(false);
  const inflight = useRef(false);
  const caughtUp = useRef(false);
  const [dirty, setDirty] = useState(false); // the open playbook has text that isn't saved
  const [ask, setAsk] = useState<{ proceed: () => void }>(); // a move that would drop it, waiting for the owner's word
  const back = useRef<HTMLDivElement>(null);

  // a save holds the controls until the poll it asked for has brought the saved values back (or failed)
  useEffect(() => {
    if (!caughtUp.current) return;
    caughtUp.current = false;
    inflight.current = false;
    setSaving(false);
  }, [c, error]);

  // ponytail: one flag for every settings control; a second save waits for the first to land
  const save: Save = async (change) => {
    if (inflight.current) return false;
    inflight.current = true;
    setSaving(true);
    try {
      await api.saveSettings(name, change);
      caughtUp.current = true;
      refresh();
      window.setTimeout(() => { // a hidden tab doesn't poll; don't hold the controls for ever
        if (caughtUp.current) {
          caughtUp.current = false;
          inflight.current = false;
          setSaving(false);
        }
      }, 5000);
      return true;
    } catch (e) {
      toast({ title: "Not saved", text: errText(e), lamp: "attention" });
      inflight.current = false;
      setSaving(false);
      return false;
    }
  };

  // unsaved text asks before a link, or closing the tab, drops it
  useEffect(() => {
    if (!dirty) return;
    const off = guardNavigation((proceed) => setAsk({ proceed }));
    const bu = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", bu);
    return () => {
      off();
      window.removeEventListener("beforeunload", bu);
    };
  }, [dirty]);

  if (!c) {
    return (
      <div className="flex flex-col gap-3">
        <h1 className="text-xl font-medium">{summary?.display_name ?? name}</h1>
        {error && <p className="text-sm text-secondary">The channel didn't load: {error.message}</p>}
      </div>
    );
  }
  const open = picked ?? (c.playbooks.find((p) => p.key === "script") ?? c.playbooks[0])?.key;
  const choose = (key: string) => {
    if (key === open) return;
    if (dirty) setAsk({ proceed: () => pick(key) });
    else pick(key);
  };
  return (
    <div className="flex flex-col gap-5">
      <Header c={c} save={save} saving={saving} />
      <p role="status" className="text-sm text-secondary empty:hidden">
        {error && `The channel didn't refresh: ${error.message}`}
      </p>
      <div className="grid grid-cols-[240px_minmax(0,1fr)_320px] gap-4">
        <Playbooks c={c} open={open ?? ""} pick={choose} />
        <div ref={back} tabIndex={-1} className="flex min-w-0 outline-none *:flex-1">
          {open && (
            <PlaybookEditor
              key={open}
              channel={name}
              pkey={open}
              latest={c.playbooks.find((p) => p.key === open)?.changed?.sha}
              onChanged={refresh}
              onDirty={setDirty}
            />
          )}
        </div>
        <div className="flex flex-col gap-4 self-start">
          {c.format === "clip" && <IngestVod channel={name} onQueued={refresh} />}
          {c.format === "clip" && <RadarAndLive channel={name} autoWatch={c.auto_watch_streamers ?? []} defaultQuery={c.auto_watch_query} save={save} saving={saving} onStarted={refresh} />}
          <Topics c={c} save={save} saving={saving} />
          <Recent c={c} />
        </div>
      </div>
      <ConfirmDialog
        open={!!ask}
        onOpenChange={(o) => !o && setAsk(undefined)}
        title={`Discard your unsaved changes to ${open ? stageName(open) : "this playbook"}?`}
        confirm="Discard"
        returnTo={back}
        onConfirm={() => {
          const go = ask?.proceed;
          setAsk(undefined);
          go?.();
        }}
      >
        What you typed in the playbook hasn't been saved, and goes if you carry on.
      </ConfirmDialog>
    </div>
  );
}
