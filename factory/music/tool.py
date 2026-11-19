"""The sound writer's commands (Plan 2c, Plan 2d; design 8.5 items 12 and 21), allowlisted by absolute path and run
from its music folder:
    <venv python> -I <repo>/factory/music/tool.py render | listen
    <venv python> -I <repo>/factory/music/tool.py sfx search <words...> | sfx get <id> | sfx forge <recipe> <target...>
                                                  | sfx match <id> <description...>
Each prints one JSON object for the writer to read; queries are argv data, never shell text."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root: the writer runs this from its job folder

from factory.music.render import RenderFailed, render_bed  # noqa: E402
from factory.sfx.library import SoundRefused  # noqa: E402


def _sfx(sub: str, args: list[str]) -> dict:
    from factory.sfx import library, make
    if sub == "search":
        return library.search(" ".join(args))
    if sub == "get":
        row = library.get(args[0])
        return {k: row.get(k) for k in ("id", "title", "tags", "duration_s", "license", "provider")}
    if sub == "forge" and len(args) > 1:
        return make.forge(args[0], " ".join(args[1:]))
    if sub == "match" and len(args) > 1:
        return make.match(args[0], " ".join(args[1:]))
    raise SoundRefused("usage", "sfx search <words> | sfx get <id> | sfx forge <recipe> <target> | sfx match <id> <desc>")


def main(argv: list[str]) -> int:
    cmd, folder = (argv[1] if len(argv) > 1 else ""), Path.cwd()
    try:
        if cmd == "render":
            out = render_bed(folder)
        elif cmd == "listen":
            from factory.music.listen import listen
            out = listen(folder)
        elif cmd == "sfx" and len(argv) > 3:
            out = _sfx(argv[2], argv[3:])
        else:
            print(json.dumps({"ok": False, "error": "usage: tool.py render | listen | sfx search <words> | sfx get <id> | "
                                                    "sfx forge <recipe> <target> | sfx match <id> <description>"}))
            return 2
    except SoundRefused as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    except RenderFailed as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, **out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
