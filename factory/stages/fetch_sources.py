"""fetch_sources: our code saves dated copies of the researcher's sources; the script may quote only these copies
(design 5.5.2 step 4, 5.5.4, 8.5 item 10; Plan 2b Task 4; FAILURE_MODES.md, fetch_sources)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from ..jobs import Job, Quarantined

MAX_BYTES, TIMEOUT_S, MIN_CHARS = 10 * 1024 * 1024, 30, 500
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")  # a copy's file name; research numbers them s1..sN
USER_AGENT = "Mozilla/5.0 (compatible; autovideo-factory/0.1; +https://autovideo-factory.github.io/)"
SKIP = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside", "form", "button",
        "iframe", "select"}
BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "tr", "blockquote", "section", "article",
         "br", "dd", "dt", "figcaption", "pre"}
NOISE = re.compile(r"cookie|consent|banner|gdpr|newsletter|subscribe|advert", re.I)
VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "track"}


class _Text(HTMLParser):
    """Visible text only: skipped subtrees (scripts, menus, cookie banners) leave nothing behind."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[bool] = []  # one entry per open element: is it (or an ancestor) skipped?

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            if tag == "br":
                self.out.append("\n")
            return
        names = " ".join(v or "" for k, v in attrs if k in ("id", "class", "role", "aria-label"))
        skipped = (self.stack[-1] if self.stack else False) or tag in SKIP or bool(NOISE.search(names))
        self.stack.append(skipped)
        if tag in BLOCK and not skipped:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.stack:
            self.stack.pop()
        if tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not (self.stack and self.stack[-1]):
            self.out.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.out).splitlines())
        return "\n".join(line for line in lines if line)


def _allowed(url: str) -> bool:
    u = urlparse(url)
    loopback = u.hostname in ("127.0.0.1", "localhost") and os.environ.get("FACTORY_FETCH_LOOPBACK") == "1"
    return u.scheme == "https" or (u.scheme == "http" and loopback)


def snapshot(src: dict, out_dir: Path) -> dict:
    """Fetch one source and save <id>.txt; return its index record. Raises ValueError with the reason to drop it."""
    url = src["url"]
    if not _allowed(url):
        raise ValueError(f"not https: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            final, ctype = r.geturl(), r.headers.get("Content-Type", "")
            raw = r.read(MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
        raise ValueError(f"fetch failed: {str(e)[:150]}") from None
    if not _allowed(final):
        raise ValueError(f"redirected off https to {final}")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"over {MAX_BYTES // (1024 * 1024)} MB")
    if "html" not in ctype and "text/plain" not in ctype:
        raise ValueError(f"unsupported content type {ctype or 'unknown'}")
    charset = (re.search(r"charset=([\w-]+)", ctype) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I))
    enc = charset.group(1) if charset else "utf-8"
    enc = enc.decode() if isinstance(enc, bytes) else enc
    try:
        html = raw.decode(enc, "replace")
    except LookupError:
        html = raw.decode("utf-8", "replace")
    if "html" in ctype:
        parser = _Text()
        parser.feed(html)
        text = parser.text()
    else:
        text = html
    if len(text) < MIN_CHARS:
        raise ValueError(f"visible text is {len(text)} chars, under {MIN_CHARS}")
    (out_dir / f"{src['id']}.txt").write_text(text, encoding="utf-8")
    return {"id": src["id"], "url": url, "final_url": final, "group": src.get("group", ""),
            "attribution": src.get("group", ""), "why": src.get("why", ""), "published": src.get("published", ""),
            "licence": src.get("licence", "facts and short attributed quotes only"),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(raw).hexdigest(), "chars": len(text)}


def fetch_sources(job: Job) -> dict:
    sources = json.loads((job.dir / "research.json").read_text(encoding="utf-8"))["sources"]
    out = job.dir / "sources"
    out.mkdir(exist_ok=True)
    kept, dropped, seen = [], [], set()
    for src in sources:
        sid = str(src.get("id", ""))
        why = ("unsafe id: letters, digits, _ and - only (1-32), never a path" if not SAFE_ID.fullmatch(sid)
               else "duplicate id" if sid in seen else None)
        if why:  # a research.json from before the renumbering
            dropped.append({"id": sid[:80], "url": src.get("url"), "why": why})
            continue
        seen.add(sid)
        try:
            kept.append(snapshot(src, out))
        except (ValueError, OSError) as e:
            dropped.append({"id": sid, "url": src.get("url"), "why": str(e)})
    (out / "index.json").write_text(json.dumps({"sources": kept, "dropped": dropped}, indent=2), encoding="utf-8")
    groups = {" ".join(s["group"].lower().split()) for s in kept}
    if len(groups) < 2:
        raise Quarantined(f"{len(kept)} usable source copies in {len(groups)} independent group(s); need 2 (5.4.2)")
    return {"kept": len(kept), "dropped": len(dropped), "groups": len(groups)}
