import { Scissors, Sparkles } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { api, errText, type ClipData, type ClipSpec } from "../api";
import { Button } from "./Button";
import { toast } from "./Toasts";

const INPUT = "h-8 rounded-md border border-line bg-page px-2 font-mono text-sm text-primary";

function fmt(s: number): string {
  const sec = Math.max(0, Number.isFinite(s) ? s : 0);
  const m = Math.floor(sec / 60);
  const r = (sec % 60).toFixed(1).padStart(4, "0");
  return `${m}:${r}`;
}

export function ClipEditor({ videoId, onRendered }: { videoId: string; onRendered: () => void }) {
  const [loaded, setLoaded] = useState<ClipData>();
  const [spec, setSpec] = useState<ClipSpec>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const sourceRef = useRef<HTMLVideoElement>(null);
  const headingId = useId();

  useEffect(() => {
    let live = true;
    api.clip(videoId)
      .then((d) => {
        if (live) {
          setLoaded(d);
          setSpec(d.spec);
          setErr(undefined);
        }
      })
      .catch((e) => live && setErr(errText(e)));
    return () => {
      live = false;
    };
  }, [videoId]);

  if (err) {
    return (
      <section aria-labelledby={headingId} className="rounded-xl border border-line bg-panel px-4 py-3">
        <h2 id={headingId} className="mb-2 text-sm font-medium text-secondary">Clip editor</h2>
        <p className="text-sm text-secondary">Could not load clip settings: {err}</p>
      </section>
    );
  }
  if (!loaded || !spec) return null;

  const duration = Math.max(0, spec.end_s - spec.start_s);

  const saveAndRender = async () => {
    setBusy(true);
    try {
      await api.saveClip(videoId, spec);
      await api.rerenderClip(videoId);
      toast({ title: "Clip re-rendered", text: `${spec.layout} · ${spec.caption_style} · ${duration.toFixed(1)}s`, lamp: "ready" });
      onRendered();
    } catch (e) {
      toast({ title: "Clip render failed", text: errText(e), lamp: "attention" });
    }
    setBusy(false);
  };

  const setFromPlayhead = (edge: "start_s" | "end_s") => {
    const t = sourceRef.current?.currentTime;
    if (typeof t === "number" && Number.isFinite(t)) {
      setSpec({ ...spec, [edge]: Math.round(t * 100) / 100 });
    }
  };

  return (
    <section aria-labelledby={headingId} className="rounded-xl border border-line bg-panel px-4 py-3">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id={headingId} className="flex items-center gap-2 text-sm font-medium text-secondary">
          <Scissors aria-hidden size={15} strokeWidth={1.5} className="text-muted" />
          Clip editor
        </h2>
        <span className="font-mono text-xs text-secondary">
          {fmt(spec.start_s)} – {fmt(spec.end_s)} ({duration.toFixed(1)}s)
        </span>
      </div>

      {loaded.source_available && (
        <div className="mb-3 flex flex-col gap-2">
          <video
            ref={sourceRef}
            controls
            playsInline
            preload="metadata"
            src={loaded.source_url}
            onLoadedMetadata={() => {
              if (sourceRef.current) sourceRef.current.currentTime = spec.start_s;
            }}
            className="aspect-video w-full rounded-lg border border-line bg-page object-contain"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button disabled={busy} onClick={() => setFromPlayhead("start_s")}>Set start at playhead</Button>
            <Button disabled={busy} onClick={() => setFromPlayhead("end_s")}>Set end at playhead</Button>
            <Button
              disabled={busy}
              onClick={() => {
                if (sourceRef.current) {
                  sourceRef.current.currentTime = spec.start_s;
                  void sourceRef.current.play();
                }
              }}
            >
              Jump to start
            </Button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-secondary">Start (s)</span>
          <input type="number" step="0.1" min={0} value={spec.start_s}
            onChange={(e) => setSpec({ ...spec, start_s: Number(e.target.value) })} className={INPUT} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-secondary">End (s)</span>
          <input type="number" step="0.1" min={0} value={spec.end_s}
            onChange={(e) => setSpec({ ...spec, end_s: Number(e.target.value) })} className={INPUT} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-secondary">Framing</span>
          <select value={spec.layout} onChange={(e) => setSpec({ ...spec, layout: e.target.value as ClipSpec["layout"] })} className={INPUT}>
            {loaded.layouts.map((l) => <option key={l} value={l}>{l === "auto" ? "auto (Smart)" : l}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-secondary">Caption preset</span>
          <select value={spec.caption_style} onChange={(e) => setSpec({ ...spec, caption_style: e.target.value })} className={INPUT}>
            {loaded.caption_styles.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-4 text-sm">
        <label className="flex items-center gap-2 text-secondary">
          Pacing
          <select value={spec.pacing} onChange={(e) => setSpec({ ...spec, pacing: e.target.value as ClipSpec["pacing"] })} className={`${INPUT} w-28`}>
            <option value="tight">tight (cut dead air)</option>
            <option value="natural">natural</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-secondary">
          Speed
          <select value={spec.speed} onChange={(e) => setSpec({ ...spec, speed: Number(e.target.value) })} className={`${INPUT} w-20`}>
            {[1.0, 1.1, 1.2, 1.35, 1.5].map((x) => <option key={x} value={x}>{x}x</option>)}
          </select>
        </label>
        <label className="flex items-center gap-2 text-secondary">
          <input type="checkbox" checked={spec.include_captions}
            onChange={(e) => setSpec({ ...spec, include_captions: e.target.checked })} />
          Burn captions
        </label>
      </div>

      {spec.captions.length > 0 && (
        <div className="mt-3 border-t border-line pt-3">
          <p className="mb-2 text-xs font-medium text-secondary">Captions ({spec.captions.length} segments)</p>
          <div className="flex max-h-44 flex-col gap-1.5 overflow-y-auto pr-1">
            {spec.captions.map((c, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <span className="w-14 shrink-0 font-mono text-xs text-secondary">{c.start.toFixed(1)}s</span>
                <input type="text" value={c.text}
                  onChange={(e) => {
                    const next = spec.captions.map((item, j) => j === idx ? { ...item, text: e.target.value, words: [] } : item);
                    setSpec({ ...spec, captions: next });
                  }}
                  className="h-7 min-w-0 flex-1 rounded-md border border-line bg-page px-2 text-xs text-primary" />
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="mt-4 flex items-center gap-2">
        <Button primary busy={busy} onClick={() => void saveAndRender()}>
          <Sparkles aria-hidden size={14} strokeWidth={1.5} className="mr-1 inline" />
          {busy ? "Rendering…" : "Save & re-render"}
        </Button>
      </div>
    </section>
  );
}
