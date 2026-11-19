// The console's API (the plan's API contract): its types, and one function per route. A refusal or a failure throws
// ApiError, carrying the server's own plain message ({"error": ...}) and its status (401, 403, 404, 409, 422).

export type Lamp = "making" | "ready" | "live" | "attention";
export type Slot = { at: string; local: string; city: string }; // at: ISO UTC; local: "17:00"; city: "New York"
export type VideoCard = {
  id: string; channel: string; title: string; column: "making" | "ready" | "live";
  lamp: Lamp; stage: string | null; step: [number, number] | null; note: string | null; slot: Slot | null;
  posted: { at: string; youtube: string | null; instagram: boolean } | null; views: number | null;
  thumb: string | null; updated: string;
  capped: boolean; // a ready video whose channel already posted today: only Post anyway posts it now
};
export type VideoDetail = VideoCard & {
  stages: { name: string; done_at: string | null; state: "done" | "current" | "todo" | "stopped" }[];
  events: { at: string; kind: "error" | "retry" | "lesson" | "review" | "post" | "drop"; text: string }[];
  script: string | null; media: string | null; can: { post: boolean; anyway: boolean; retry: boolean; drop: boolean };
};
export type Health = {
  running: boolean; last_tick: string | null; being_made: number;
  tick: { channel?: string; skipped_slot?: string; late_slot?: string; refused?: unknown; broken?: string[] } | null;
  quota: { week: number | null; five_hour: number | null; week_resets: string | null }; account: string | null;
};
export type ChannelSummary = {
  name: string; display_name: string;
  color: string | null; // console.color in settings.json; none set: null, drawn in the muted grey
  format: "explainer" | "story"; paused: boolean; slots: string[]; timezone: string; max_per_day: number | null;
  instagram: boolean;
};
export type ChannelDetail = ChannelSummary & {
  playbooks: { key: string; path: string; changed: { by: "you" | "lessons" | "loop" | "other"; at: string; sha: string } | null }[];
  topics: { levers: string[]; min_score: number; ideas_per_pass: number }; recent: VideoCard[];
};
export type Playbook = { key: string; text: string; base: string; history: { sha: string; at: string; by: string; subject: string }[] };
export type Change = {
  sha: string; at: string; by: "lessons" | "loop" | "you"; channel: string | null;
  subject: string; files: string[]; reverted: boolean;
};
export type Topic = { id: number; key: string; lever: string; score: number; status: string; created: string };
export type Stats = {
  videos: { channel: string; video_id: string; title: string; views: number | null; likes: number | null;
    avg_view_pct: number | null; engaged_views: number | null; age_days: number; as_of: string }[];
  channels: Record<string, { date: string; views: number }[]>;
};
// Post now: each leg's outcome ("published", "capped", "dropped", "gave up: ...", or the error it will retry)
export type Publish = { job: string; youtube?: string; instagram?: string };
// ok is false, still HTTP 200, when a leg is left for the tick to retry
export type PostResult = { ok: boolean; publish: Publish };
export type SettingsChange = Partial<Pick<ChannelSummary, "paused" | "slots" | "timezone" | "max_per_day" | "instagram">>
  & { topics?: Partial<ChannelDetail["topics"]> };

export type Choice = { model: string; effort: string };
export type FactorySettings = {
  paused: boolean; videos_at_once: number; auto_retries: number;
  stop_at: { weekly: number; five_hour: number; model_weekly: number };
  fallback: Choice; steps: Record<string, Choice>;
};
export type FactorySettingsChange = Partial<Omit<FactorySettings, "stop_at" | "fallback" | "steps">> & {
  stop_at?: Partial<FactorySettings["stop_at"]>; fallback?: Partial<Choice>; steps?: Record<string, Partial<Choice>>;
};
export type SettingsPage = {
  settings?: FactorySettings; error?: string; // error: the file won't load, the rest is still read
  steps: string[]; models: Record<string, string>; efforts: string[]; counts: Record<string, [number, number]>;
  account: { email: string | null; plan: string | null }; billing: string;
  quota: Record<string, { used_percentage: number; resets_at: string | null }> | null;
};

export class ApiError extends Error {
  status: number; // 0: the server didn't answer

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** What went wrong, for a note: the server's own words, else the error's */
export const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));

// GET without a body; POST as JSON with one. The key rides in the cookie the launcher's link set.
async function call<T>(path: string, body?: object): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, body && {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The console's server isn't answering");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok || data === null) throw new ApiError(res.status, data?.error ?? `The console answered ${res.status} ${res.statusText}`);
  return data as T;
}

const q = encodeURIComponent;

export const api = {
  health: () => call<Health>("/api/health"),
  videos: () => call<{ videos: VideoCard[] }>("/api/videos"),
  video: (id: string) => call<VideoDetail>(`/api/videos/${q(id)}`),
  thumbUrl: (id: string) => `/api/videos/${q(id)}/thumb`, // image/jpeg, for an <img>
  mediaUrl: (id: string) => `/api/videos/${q(id)}/media`, // video/mp4 with ranges, for a <video>
  post: (id: string, anyway: boolean) => call<PostResult>(`/api/videos/${q(id)}/post`, { anyway }),
  retry: (id: string) => call<{ ok: true; state: string }>(`/api/videos/${q(id)}/retry`, {}),
  drop: (id: string) => call<{ ok: true; state: "dropped" }>(`/api/videos/${q(id)}/drop`, {}),
  channels: () => call<{ channels: ChannelSummary[] }>("/api/channels"),
  channel: (name: string) => call<ChannelDetail>(`/api/channels/${q(name)}`),
  saveSettings: (name: string, change: SettingsChange) =>
    call<{ ok: true; commit: string }>(`/api/channels/${q(name)}/settings`, change),
  factorySettings: () => call<SettingsPage>("/api/settings"),
  saveFactorySettings: (change: FactorySettingsChange) => call<{ ok: true; commit: string }>("/api/settings", change),
  playbook: (name: string, key: string) => call<Playbook>(`/api/channels/${q(name)}/playbooks/${q(key)}`),
  savePlaybook: (name: string, key: string, text: string, base: string) =>
    call<{ ok: true; commit: string }>(`/api/channels/${q(name)}/playbooks/${q(key)}`, { text, base }),
  topics: (name: string) => call<{ topics: Topic[] }>(`/api/channels/${q(name)}/topics`),
  removeTopic: (id: number) => call<{ ok: true }>(`/api/topics/${id}/remove`, {}),
  changes: () => call<{ changes: Change[] }>("/api/changes"),
  diff: (sha: string) => call<{ diff: string }>(`/api/changes/${q(sha)}/diff`),
  undo: (sha: string) => call<{ ok: true; commit: string }>(`/api/changes/${q(sha)}/undo`, {}),
  stats: () => call<Stats>("/api/stats"),
};
