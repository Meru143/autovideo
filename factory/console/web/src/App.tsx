import { useEffect, useRef } from "react";
import { api, type ChannelSummary } from "./api";
import { HealthBar } from "./components/HealthBar";
import { Rail } from "./components/Rail";
import { Toasts } from "./components/Toasts";
import { Changes } from "./pages/Changes";
import { Channel } from "./pages/Channel";
import { Home } from "./pages/Home";
import { Settings } from "./pages/Settings";
import { Stats } from "./pages/Stats";
import { Topics } from "./pages/Topics";
import { Video } from "./pages/Video";
import { Link, usePath } from "./router";
import { usePoll } from "./usePoll";

const PAGES: Record<string, string> = { topics: "Topics", changes: "Changes", stats: "Stats", settings: "Settings" };

/** The page's name, for its heading and the tab; none for a path that is no page */
function titleOf(path: string, channels: ChannelSummary[]): string | undefined {
  const [, section, id] = path.split("/");
  return section === "" ? "Home"
    : section === "videos" && id ? "Video"
    : section === "channels" && id ? (channels.find((c) => c.name === id)?.display_name ?? id)
    : PAGES[section];
}

function Page({ path, title, channels }: { path: string; title?: string; channels: ChannelSummary[] }) {
  if (!title) {
    return (
      <p className="text-sm text-secondary">
        No page here. <Link to="/" className="text-primary underline underline-offset-4">Back to Home</Link>
      </p>
    );
  }
  if (path === "/") return <Home channels={channels} />;
  const [, section, id] = path.split("/");
  if (section === "videos") return <Video key={id} id={id} channels={channels} />;
  if (section === "channels") return <Channel key={id} name={id} summary={channels.find((c) => c.name === id)} />;
  if (section === "topics") return <Topics channels={channels} />;
  if (section === "changes") return <Changes channels={channels} />;
  if (section === "settings") return <Settings />;
  return <Stats channels={channels} />;
}

/** Focus on the page's heading, where a page load would start a screen reader */
function toHeading() {
  const h1 = document.querySelector<HTMLElement>("main h1");
  if (h1) {
    h1.tabIndex = -1;
    h1.focus();
  }
}

/** The app frame: the rail down the left, the health bar across the top, the page below it, notes in the corner */
export default function App() {
  const path = usePath();
  const channels = usePoll(api.channels, 60_000).data?.channels ?? [];
  const title = titleOf(path, channels);
  useEffect(() => {
    document.title = `${title ?? "No page here"} · Autovideo`;
  }, [title]);
  // a move to another page takes focus to its heading, as a page load would start there; the first load leaves it
  const moved = useRef(false);
  useEffect(() => {
    if (!moved.current) {
      moved.current = true;
      return;
    }
    toHeading();
  }, [path]);
  return (
    <>
      {/* the keyboard's first stop, out of sight until then: past the rail's links to the page */}
      <a
        href="#main"
        onClick={(e) => {
          e.preventDefault();
          toHeading();
        }}
        className="fixed top-2 left-4 z-30 -translate-y-16 rounded-md border border-line bg-raised px-3 py-1.5 text-sm text-primary focus:translate-y-0"
      >
        Skip to content
      </a>
      <div className="grid h-dvh grid-cols-[224px_minmax(0,1fr)] grid-rows-[48px_minmax(0,1fr)]">
        <Rail path={path} channels={channels} />
        <HealthBar channels={channels} />
        <main id="main" key={path} className="overflow-y-auto px-6 py-5">
          <Page path={path} title={title} channels={channels} />
        </main>
      </div>
      <Toasts />
    </>
  );
}
