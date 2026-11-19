"""The console's local HTTP server (console Task 2): the standard library's ThreadingHTTPServer, bound to 127.0.0.1
only. Every request is checked before any route runs (the plan's "Access and security"): its Host must name this
server, else 403 (DNS rebinding); an Origin, when sent, must be this console's own, else 403 (another site's page);
and it must carry the per-launch key, as the console_key cookie or as ?key=, else 401. The launcher's link carries
?key=: a page request with it gets the cookie (HttpOnly, SameSite=Strict) and a redirect to the same address without
it. It only routes: read.py answers, and the built app is served from dist, any other path falling back to its
index.html."""
from __future__ import annotations

import hmac
import json
import mimetypes
import re
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from . import act, read

WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"
COOKIE = "console_key"
TYPES = mimetypes.MimeTypes()  # the standard table: Windows' registry can map .js wrongly, and a module script fails
TYPES.add_type("font/woff2", ".woff2")
CHUNK = 1 << 16


def _file(path: Path) -> Path | None:
    return path if path.is_file() else None


BODY_MAX = 1 << 20  # a playbook is a few KB: a megabyte is no editor's save
ERRORS = {act.Conflict: 409, act.Invalid: 422, act.NotFound: 404}
# (method, path pattern, answer): a dict is JSON, a Path a file, None a 404; a POST's answer also takes its JSON body
ROUTES = [(method, re.compile(pattern), fn) for method, pattern, fn in (
    ("GET", r"/api/health", lambda s: read.health(s.root)),
    ("GET", r"/api/videos", lambda s: read.videos(s.root)),
    ("GET", r"/api/videos/(?P<jid>[\w-]+)", lambda s, jid: read.video(s.root, jid)),
    ("GET", r"/api/videos/(?P<jid>[\w-]+)/thumb", lambda s, jid: read.thumb(s.root, jid)),
    ("GET", r"/api/videos/(?P<jid>[\w-]+)/media", lambda s, jid: _file(s.root / "jobs" / jid / "final.mp4")),
    ("GET", r"/api/channels", lambda s: read.channels()),
    ("GET", r"/api/channels/(?P<name>[\w-]+)", lambda s, name: read.channel(s.root, s.repo, name)),
    ("GET", r"/api/channels/(?P<name>[\w-]+)/playbooks/(?P<key>[\w-]+)",
     lambda s, name, key: read.playbook(s.repo, name, key)),
    ("GET", r"/api/channels/(?P<name>[\w-]+)/topics", lambda s, name: read.topics(s.root, name)),
    ("GET", r"/api/changes", lambda s: read.changes(s.repo)),
    ("GET", r"/api/changes/(?P<sha>[0-9a-f]{7,40})/diff", lambda s, sha: read.diff(s.repo, sha)),
    ("GET", r"/api/settings", lambda s: read.settings(s.root)),
    ("GET", r"/api/stats", lambda s: read.stats(s.root)),
    ("POST", r"/api/videos/(?P<jid>[\w-]+)/post", lambda s, b, jid: act.post(
        s.root, jid, anyway=b.get("anyway") is True, legs=s.legs)),
    ("POST", r"/api/videos/(?P<jid>[\w-]+)/retry", lambda s, b, jid: {"ok": True, "state": act.retry(s.root, jid)}),
    ("POST", r"/api/videos/(?P<jid>[\w-]+)/drop", lambda s, b, jid: {"ok": True, "state": act.drop(s.root, jid)}),
    ("POST", r"/api/settings", lambda s, b: {"ok": True, "commit": act.save_factory_settings(b, repo=s.repo)}),
    ("POST", r"/api/channels/(?P<name>[\w-]+)/settings",
     lambda s, b, name: {"ok": True, "commit": act.save_settings(name, b, repo=s.repo)}),
    ("POST", r"/api/channels/(?P<name>[\w-]+)/playbooks/(?P<key>[\w-]+)",
     lambda s, b, name, key: {"ok": True, "commit": act.save_playbook(name, key, b.get("text"), b.get("base"),
                                                                       repo=s.repo)}),
    ("POST", r"/api/topics/(?P<tid>\d+)/remove", lambda s, b, tid: act.remove_topic(s.root, int(tid)) or {"ok": True}),
    ("POST", r"/api/changes/(?P<sha>[0-9a-f]{7,40})/undo",
     lambda s, b, sha: {"ok": True, "commit": act.undo(sha, repo=s.repo, root=s.root)}),
)]


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # Windows binds a taken port anyway when this is set: a stale console must fail the launch
    daemon_threads = False  # Ctrl+C (server_close) waits for a request in flight, so a post finishes (final review)


def _same(given: str | None, key: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), key.encode())


class Handler(BaseHTTPRequestHandler):
    # A socket silent this long ends its request (a browser's spare connection, a paused video's stream): closing the
    # console waits for every request, never long on one of those. A post that takes minutes does no socket work then.
    timeout = 10

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def log_message(self, *args) -> None:  # a poll every few seconds: no line per request; errors print themselves
        pass

    def _handle(self, method: str) -> None:
        srv, url = self.server, urlsplit(self.path)
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{srv.server_port}", f"localhost:{srv.server_port}"):
            return self._json(403, {"error": "this console answers only at its own address"})
        if (origin := self.headers.get("Origin")) is not None and origin != f"http://{host}":
            return self._json(403, {"error": "a request from another site"})
        query = parse_qsl(url.query, keep_blank_values=True)
        given = dict(query).get("key")
        # by hand: cookies aren't kept per port, so other local apps' cookies come too, and one the cookie parser
        # dislikes would make it drop the rest
        jar = (c.strip().partition("=") for c in (self.headers.get("Cookie") or "").split(";"))
        if not (_same(given, srv.key) or any(k == COOKIE and _same(v, srv.key) for k, _, v in jar)):
            return self._json(401, {"error": "no key: open the address the console printed when it started"})
        if _same(given, srv.key) and method == "GET" and not url.path.startswith("/api/"):  # the launcher's link
            rest = urlencode([(k, v) for k, v in query if k != "key"])
            self.send_response(303)
            self.send_header("Location", url.path + (f"?{rest}" if rest else ""))
            self.send_header("Set-Cookie", f"{COOKIE}={srv.key}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        args = (srv,)
        if method == "POST":  # a form post (text/plain, urlencoded) is how another site's page would try it
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._json(403, {"error": "a change must be sent as application/json"})
            try:
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size).decode("utf-8")) if 0 < size <= BODY_MAX else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json(422, {"error": "the body must be a JSON object"})
            args = (srv, body)
        for m, rx, fn in ROUTES:
            if m == method and (hit := rx.fullmatch(url.path)):
                try:
                    out = fn(*args, **hit.groupdict())
                except tuple(ERRORS) as e:  # a refusal, in plain words
                    return self._json(ERRORS[type(e)], {"error": str(e)})
                except Exception as e:  # the one request fails, with its reason; the server goes on
                    traceback.print_exc()
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
                if out is None:
                    return self._json(404, {"error": f"not found: {url.path}"})
                return self._send(out) if isinstance(out, Path) else self._json(200, out)
        if url.path.startswith("/api/"):
            return self._json(404, {"error": f"not found: {url.path}"})
        if method != "GET":
            return self._json(404, {"error": f"not found: {url.path}"})
        self._app(url.path)

    def _app(self, path: str) -> None:
        """The built app: a file under dist, else its index.html (the app routes itself); never a file outside it."""
        dist = self.server.dist.resolve()
        try:
            want = (dist / unquote(path).lstrip("/")).resolve()
        except (OSError, ValueError):  # a name no file can have (%00)
            want = dist
        if want.is_relative_to(dist) and want.is_file():
            return self._send(want)
        if (dist / "index.html").is_file():
            return self._send(dist / "index.html")
        self._json(404, {"error": "the console's app isn't built: run npm run build in factory/console/web"})

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, path: Path) -> None:
        """A file, with one byte range when asked (the video player seeks by them); a list of ranges gets it whole."""
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        if (m := re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())) and any(m.groups()):
            a, b = m.groups()
            start, end, code = (int(a), min(int(b), size - 1) if b else size - 1, 206) if a else (
                max(0, size - int(b)), size - 1, 206)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(code)
        self.send_header("Content-Type", TYPES.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0 and (chunk := f.read(min(CHUNK, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except ConnectionError:  # the player moved on: a seek, a closed tab
                pass


def make(root: Path, *, repo: Path, key: str, port: int = 0, legs: dict | None = None,
         dist: Path = WEB_DIST) -> ThreadingHTTPServer:
    """The console's server on 127.0.0.1 (port 0: the OS picks a free one), ready for serve_forever. root is the factory
    root, repo the git repo holding the channels, key the launch's secret; legs, the publish legs Post now runs (None:
    the tick's own)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):  # what secrets.token_urlsafe makes: safe in a URL and a cookie as is
        raise ValueError("the console's key must be letters, digits, - and _")
    srv = _Server(("127.0.0.1", port), Handler)
    srv.root, srv.repo, srv.key, srv.legs, srv.dist = Path(root), Path(repo), key, legs, Path(dist)
    return srv


def url(srv: ThreadingHTTPServer) -> str:
    """The address the launcher opens: the key rides along once, then lives in the cookie."""
    return f"http://127.0.0.1:{srv.server_port}/?key={srv.key}"


def serve(root: Path, *, repo: Path, key: str, port: int = 0, legs: dict | None = None, open_browser: bool = True) -> None:
    """Run the console until Ctrl+C: print its address, open it. A taken port raises OSError. Ctrl+C waits for the
    requests in progress, a post among them."""
    srv = make(root, repo=repo, key=key, port=port, legs=legs)
    print(f"Console: {url(srv)}", flush=True)
    if open_browser:
        webbrowser.open(url(srv))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopping once the requests in progress finish (a post can take minutes; closing this window leaves it "
              "to the next tick).", flush=True)
    finally:
        srv.server_close()
