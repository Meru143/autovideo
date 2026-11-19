import { useSyncExternalStore } from "react";
import { api, errText, type Publish, type VideoCard } from "./api";
import { toast } from "./components/Toasts";

// Each video's post: 0 while it is in flight, then the time its answer came. A board asked for before that answer
// still shows the video where it was, so the video reads as posting until a board asked for after it is on screen;
// a post can take minutes and is never timed out, so this lives here, not in a card, and a page opened meanwhile
// shows the same.
const posts = new Map<string, number>();
let answered = 0; // when the last post was answered: a board on screen asks again then
const subs = new Set<() => void>();
const changed = () => subs.forEach((f) => f());
const subscribe = (f: () => void) => {
  subs.add(f);
  return () => void subs.delete(f);
};

/** Whether the video still reads as posting, on a board asked for at `asked` (ms) */
export const usePosting = (id: string, asked: number) =>
  useSyncExternalStore(subscribe, () => {
    const at = posts.get(id);
    return at === 0 || (at !== undefined && asked < at);
  });

/** When the last post was answered (0: none yet) */
export const useAnswered = () => useSyncExternalStore(subscribe, () => answered);

const PLATFORM = { youtube: "YouTube", instagram: "Instagram" } as const;
// How scheduler._publish closes a leg without posting it. Any other outcome but "published" is the error the leg is
// left with, and the next tick retries it.
const CLOSED = /^(capped|dropped|off \(|token refused|gave up)/;

/** What a post did, a line per outcome, a leg that didn't land in the factory's own words: "Posted on YouTube",
 * "Instagram will retry at the next tick: RuntimeError: …", "Instagram won't post: off (a private channel)". The legs
 * say it, not `ok`, which is YouTube's alone. */
function said(p: Publish | undefined): { text: string; posted: boolean } {
  const legs = (["youtube", "instagram"] as const).flatMap((k) => {
    const v = p?.[k];
    return v ? [[PLATFORM[k], v] as const] : [];
  });
  if (!legs.length) return { text: "The factory didn't say how the post went: the board shows where the video is", posted: false };
  const posted = legs.filter(([, v]) => v === "published").map(([name]) => name);
  const rest = legs.filter(([, v]) => v !== "published").map(([name, v]) =>
    CLOSED.test(v) ? `${name} won't post: ${v}` : `${name} will retry at the next tick: ${v}`);
  return { text: [...(posted.length ? [`Posted on ${posted.join(" and ")}`] : []), ...rest].join("\n"), posted: posted.length > 0 };
}

/** Post now, or Post anyway; a second call while one is in flight sends nothing. The toast says how each platform
 * went, or the server's own reason it refused (409: already posted, busy, already posted today, not ready). */
export async function post(video: Pick<VideoCard, "id" | "title">, anyway: boolean): Promise<void> {
  if (posts.get(video.id) === 0) return;
  posts.set(video.id, 0);
  changed();
  try {
    const { publish } = await api.post(video.id, anyway);
    const out = said(publish);
    toast({ title: video.title, text: out.text, lamp: out.posted ? "live" : undefined });
  } catch (e) {
    toast({ title: video.title, text: errText(e), lamp: "attention" });
  } finally {
    answered = Date.now();
    posts.set(video.id, answered);
    changed();
  }
}
