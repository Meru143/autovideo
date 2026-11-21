import * as Switch from "@radix-ui/react-switch";
import { useEffect, useId, useState, type ReactNode } from "react";
import { api, errText, type Choice, type FactorySettings, type FactorySettingsChange, type LlmProviders, type SettingsPage } from "../api";
import { Button } from "../components/Button";
import { toast } from "../components/Toasts";

const INPUT = "h-8 rounded-md border border-line bg-page px-2 font-mono text-sm text-primary";
const CARD = "rounded-xl border border-line bg-panel";

// The numbers are kept as typed (text), so a value the server refuses stays on screen beside its reason.
type Draft = {
  paused: boolean; videos_at_once: string; auto_retries: string;
  stop_at: Record<keyof FactorySettings["stop_at"], string>; fallback: Choice; steps: Record<string, Choice>;
};
const draftOf = (s: FactorySettings): Draft => ({
  paused: s.paused, videos_at_once: String(s.videos_at_once), auto_retries: String(s.auto_retries),
  stop_at: { weekly: String(s.stop_at.weekly), five_hour: String(s.stop_at.five_hour), model_weekly: String(s.stop_at.model_weekly) },
  fallback: { ...s.fallback }, steps: Object.fromEntries(Object.entries(s.steps).map(([k, v]) => [k, { ...v }])),
});
// a typed number: a whole or decimal number as a number, anything else as the text (the server refuses it, in words)
const num = (t: string): number | string => (t.trim() !== "" && Number.isFinite(Number(t)) ? Number(t) : t);

/** Only what differs from the saved settings, in the shape POST /api/settings takes */
function changesOf(saved: FactorySettings, d: Draft): FactorySettingsChange {
  const out: FactorySettingsChange = {};
  if (d.paused !== saved.paused) out.paused = d.paused;
  for (const k of ["videos_at_once", "auto_retries"] as const) {
    if (d[k] !== String(saved[k])) (out as Record<string, unknown>)[k] = num(d[k]);
  }
  const stop: Record<string, unknown> = {};
  for (const k of Object.keys(saved.stop_at) as (keyof FactorySettings["stop_at"])[]) {
    if (d.stop_at[k] !== String(saved.stop_at[k])) stop[k] = num(d.stop_at[k]);
  }
  if (Object.keys(stop).length) out.stop_at = stop;
  const fb: Partial<Choice> = {};
  for (const k of ["model", "effort"] as const) if (d.fallback[k] !== saved.fallback[k]) fb[k] = d.fallback[k];
  if (Object.keys(fb).length) out.fallback = fb;
  const steps: Record<string, Partial<Choice>> = {};
  for (const [name, c] of Object.entries(d.steps)) {
    const part: Partial<Choice> = {};
    for (const k of ["model", "effort"] as const) if (c[k] !== saved.steps[name][k]) part[k] = c[k];
    if (Object.keys(part).length) steps[name] = part;
  }
  if (Object.keys(steps).length) out.steps = steps;
  return out;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className={CARD}>
      <h2 className="border-b border-line px-4 py-3 text-base font-medium">{title}</h2>
      <div className="flex flex-col gap-3 px-4 py-3">{children}</div>
    </section>
  );
}

function Row({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  const id = useId();
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label htmlFor={id} className="w-56 shrink-0 text-secondary">{label}</label>
      <div id={id} className="contents">{children}</div>
      {hint && <span className="text-xs text-secondary">{hint}</span>}
    </div>
  );
}

function Pick({ label, value, options, onChange }: {
  label: string; value: string; options: [string, string][]; onChange: (v: string) => void;
}) {
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className={`${INPUT} font-sans`}>
      {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
    </select>
  );
}

function Num({ label, value, onChange, min, max }: {
  label: string; value: string; onChange: (v: string) => void; min?: number; max?: number;
}) {
  return (
    <input aria-label={label} type="number" min={min} max={max} value={value} autoComplete="off"
      onChange={(e) => onChange(e.target.value)} className={`${INPUT} w-20`} />
  );
}

/** The factory's own settings: every change is a commit the server makes after the factory's own checker accepts it. */
export function Settings() {
  const [page, setPage] = useState<SettingsPage>();
  const [loadError, setLoadError] = useState<string>();
  const [draft, setDraft] = useState<Draft>();
  const [refused, setRefused] = useState<string>();
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      const p = await api.factorySettings();
      setPage(p);
      setDraft(p.settings && draftOf(p.settings));
      setLoadError(undefined);
    } catch (e) {
      setLoadError(errText(e));
    }
  };
  useEffect(() => void load(), []);

  const saved = page?.settings;
  const change = saved && draft ? changesOf(saved, draft) : {};
  const changed = Object.keys(change).length > 0;
  const set = (patch: Partial<Draft>) => setDraft((d) => d && { ...d, ...patch });

  const save = async () => {
    setSaving(true);
    setRefused(undefined);
    try {
      await api.saveFactorySettings(change);
      toast({ title: "Settings saved", text: "The factory uses them from its next step.", lamp: "ready" });
      await load();
    } catch (e) {
      setRefused(errText(e));
      toast({ title: "Not saved", text: errText(e), lamp: "attention" });
    } finally {
      setSaving(false);
    }
  };

  // The pause switch acts the moment it is flipped (operator, 12 Nov: flipping it and seeing nothing happen reads as
  // broken); the other fields keep their unsaved edits and wait for Save.
  const pause = async (paused: boolean) => {
    setSaving(true);
    setRefused(undefined);
    try {
      await api.saveFactorySettings({ paused });
      setPage((p) => p && p.settings && { ...p, settings: { ...p.settings, paused } });
      set({ paused });
      toast(paused
        ? { title: "Factory paused", text: "It starts nothing new and posts nothing until you turn this off.", lamp: "attention" }
        : { title: "Factory running", text: "It picks up again at its next check, within 15 minutes.", lamp: "ready" });
    } catch (e) {
      setRefused(errText(e));
      toast({ title: "Not saved", text: errText(e), lamp: "attention" });
    } finally {
      setSaving(false);
    }
  };

  const models = Object.entries(page?.models ?? {}) as [string, string][];
  const efforts = (page?.efforts ?? []).map((e) => [e, e] as [string, string]);
  const reading = (key: string) => {
    const q = page?.quota?.[key];
    return q ? `now ${Math.round(q.used_percentage)}%` : undefined;
  };
  const fable = page?.quota && Object.keys(page.quota).find((k) => k.startsWith("seven_day_"));
  const [lo1, hi1] = page?.counts.videos_at_once ?? [1, 3];
  const [lo2, hi2] = page?.counts.auto_retries ?? [0, 3];

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Settings</h1>
      <p role="status" className="text-sm text-secondary empty:hidden">
        {loadError && `The settings didn't load: ${loadError}`}
        {page?.error && `The factory's settings file won't load, so it is waiting: ${page.error}`}
      </p>
      {draft && (
        <>
          <Section title="Running">
            <Row label="Pause everything" hint="Saves at once. While it's on, the factory starts nothing new and posts nothing. A video already uploading finishes.">
              <Switch.Root
                aria-label="Pause everything"
                checked={draft.paused}
                disabled={saving}
                onCheckedChange={(paused) => void pause(paused)}
                className="relative h-5 w-9 shrink-0 rounded-full border border-muted bg-raised transition-colors hover:border-secondary data-[state=checked]:bg-primary"
              >
                <Switch.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-secondary transition-transform data-[state=checked]:translate-x-[18px] data-[state=checked]:bg-page" />
              </Switch.Root>
            </Row>
            <Row label="Videos at once" hint={`${lo1} to ${hi1}`}>
              <Num label="Videos at once" min={lo1} max={hi1} value={draft.videos_at_once} onChange={(v) => set({ videos_at_once: v })} />
            </Row>
            <Row label="Automatic retries" hint={`${lo2} to ${hi2}: how often a failed video is tried again before you look`}>
              <Num label="Automatic retries" min={lo2} max={hi2} value={draft.auto_retries} onChange={(v) => set({ auto_retries: v })} />
            </Row>
          </Section>

          <Section title="Quota stop points">
            <p className="text-xs text-secondary">The factory waits once this much of the plan is used.</p>
            {([["weekly", "Weekly limit %"], ["five_hour", "5-hour limit %"], ["model_weekly", "Fable's own weekly limit %"]] as const).map(([k, label]) => (
              <Row key={k} label={label} hint={reading(k === "weekly" ? "seven_day" : k === "five_hour" ? "five_hour" : fable ?? "")}>
                <Num label={label} min={1} max={100} value={draft.stop_at[k]}
                  onChange={(v) => set({ stop_at: { ...draft.stop_at, [k]: v } })} />
              </Row>
            ))}
          </Section>

          {page?.llm && <ProvidersSection llm={page.llm} steps={page.steps} onSaved={load} />}

          <Section title="Claude steps">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-secondary">
                  <th className="py-1 font-normal">Step</th>
                  <th className="py-1 font-normal">Model</th>
                  <th className="py-1 font-normal">Effort</th>
                </tr>
              </thead>
              <tbody>
                {page!.steps.map((name) => (
                  <tr key={name} className="border-t border-line">
                    <td className="py-1.5 pr-3">{name}</td>
                    <td className="py-1.5 pr-3">
                      <Pick label={`${name} model`} value={draft.steps[name].model} options={models}
                        onChange={(model) => set({ steps: { ...draft.steps, [name]: { ...draft.steps[name], model } } })} />
                    </td>
                    <td className="py-1.5">
                      <Pick label={`${name} effort`} value={draft.steps[name].effort} options={efforts}
                        onChange={(effort) => set({ steps: { ...draft.steps, [name]: { ...draft.steps[name], effort } } })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="flex flex-wrap items-center gap-3 border-t border-line pt-3 text-sm">
              <span className="w-56 shrink-0 text-secondary">When Fable's own limit fills, run on</span>
              <Pick label="Fallback model" value={draft.fallback.model} options={models}
                onChange={(model) => set({ fallback: { ...draft.fallback, model } })} />
              <Pick label="Fallback effort" value={draft.fallback.effort} options={efforts}
                onChange={(effort) => set({ fallback: { ...draft.fallback, effort } })} />
            </div>
          </Section>

          <div className="flex items-center gap-4">
            <Button primary disabled={!changed} busy={saving} onClick={() => void save()}>
              {saving ? "Saving…" : "Save"}
            </Button>
            {changed && <span className="text-xs text-secondary">{Object.keys(change).length} unsaved</span>}
            <p role="alert" className="text-sm text-lamp-attention empty:hidden">{refused}</p>
          </div>
        </>
      )}

      {page && (
        <Section title="Account">
          <Row label="Signed in as"><span>{page.account.email ?? "nobody"}</span></Row>
          <Row label="Plan"><span>{page.account.plan ?? "unknown"}</span></Row>
          <Row label="Billing"><span>{page.billing}</span></Row>
          <p className="text-xs text-secondary">Signing in is a step in a terminal, not here.</p>
        </Section>
      )}
    </div>
  );
}


function ProvidersSection({ llm, steps, onSaved }: { llm: LlmProviders; steps: string[]; onSaved: () => Promise<void> }) {
  const [active, setActive] = useState(llm.active_provider);
  const [fallbacks, setFallbacks] = useState(llm.fallback_providers.join(", "));
  const [models, setModels] = useState<Record<string, string>>(
    () => Object.fromEntries(llm.providers.map((p) => [p.name, p.model])),
  );
  const [stepMap, setStepMap] = useState<Record<string, string>>(() => ({ ...llm.steps }));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setActive(llm.active_provider);
    setFallbacks(llm.fallback_providers.join(", "));
    setModels(Object.fromEntries(llm.providers.map((p) => [p.name, p.model])));
    setStepMap({ ...llm.steps });
  }, [llm]);

  const providerOptions: [string, string][] = llm.providers.map((p) => [p.name, p.name]);
  const stepOptions: [string, string][] = [["", `default (${active})`], ...providerOptions];

  const save = async () => {
    setBusy(true);
    try {
      const fb = fallbacks.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      await api.saveProviders({ active_provider: active, fallback_providers: fb, models, steps: stepMap });
      toast({ title: "Providers saved", text: "Updated secrets/providers.json for the multi-provider router.", lamp: "ready" });
      await onSaved();
    } catch (e) {
      toast({ title: "Providers not saved", text: errText(e), lamp: "attention" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="LLM providers">
      <p className="text-xs text-secondary">
        Multi-provider routing from <span className="font-mono">secrets/providers.json</span> (API keys stay on disk and are never sent to the browser).
      </p>
      <Row label="Default provider">
        <Pick label="Default provider" value={active} options={providerOptions} onChange={setActive} />
      </Row>
      <Row label="Fallback order" hint="comma-separated">
        <input
          aria-label="Fallback order"
          type="text"
          value={fallbacks}
          onChange={(e) => setFallbacks(e.target.value)}
          className={`${INPUT} w-64`}
        />
      </Row>
      <div className="border-t border-line pt-3">
        <p className="mb-2 text-xs text-secondary">Default model per provider</p>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {llm.providers.map((p) => (
            <label key={p.name} className="flex items-center gap-2 text-sm">
              <span className="w-24 shrink-0 font-mono text-xs text-secondary">{p.name}</span>
              <input
                type="text"
                placeholder="default"
                value={models[p.name] ?? ""}
                onChange={(e) => setModels({ ...models, [p.name]: e.target.value })}
                className={`${INPUT} min-w-0 flex-1`}
              />
            </label>
          ))}
        </div>
      </div>
      <div className="border-t border-line pt-3">
        <p className="mb-2 text-xs text-secondary">Per-step provider override</p>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {steps.map((step) => (
            <label key={step} className="flex items-center gap-2 text-sm">
              <span className="w-24 shrink-0 text-xs text-secondary">{step}</span>
              <Pick
                label={`${step} provider`}
                value={stepMap[step] ?? ""}
                options={stepOptions}
                onChange={(v) => setStepMap({ ...stepMap, [step]: v })}
              />
            </label>
          ))}
        </div>
      </div>
      <div>
        <Button primary busy={busy} onClick={() => void save()}>
          {busy ? "Saving…" : "Save providers"}
        </Button>
      </div>
    </Section>
  );
}
