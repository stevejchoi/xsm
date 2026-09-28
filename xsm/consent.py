"""A command the person typed into their own session counts as their consent.

User decision, 2026-09-28: a tester could not connect two repositories, because
that took an MCP form on both sides and Codex under approval_policy "never"
declines a form without showing it. Typing `/xsm link <folder>` (Codex:
`$xsm link <folder>`) is already the person saying yes; asking them again in a
form only gave that decline a chance to overrule them.

Where it is read differs by runtime, because a peer message can look exactly
like typing (2026-09-28, Claude Code 2.1.283 and its hooks documentation):

- Claude: only the UserPromptExpansion hook, which fires when the person types
  a slash command and never for a peer message (peer text arrives as plain
  text, and commands in it do not run). UserPromptSubmit is not used: a peer
  message written to the inbox socket without the wrapper reaches it with the
  same fields as the person's own "/xsm link x".
- Codex: UserPromptSubmit, for a prompt starting with `$xsm <verb>` that is not
  an xsm envelope or header. A `codex queue` item, which is how xsm itself
  delivers to Codex, is also raw user input there, so any process of the same
  OS user that can run `codex queue` could write one. That is inside xsm's
  trust boundary, the uid (ADR-0009): consent records are not security.

xsm's own workers are typed into through tmux, so a worker never records
consent. Consent is one file per session, ~/.xsm/asked/<ref>.json, good for
TTL seconds and for one use by the same verb on the same target.
"""
from __future__ import annotations

import os
import re
import shlex
import time

from . import envelope, paths

ASKED = "asked"
TTL = 600
VERBS = ("link", "join", "leave", "reach")
# A Codex prompt as typed: `$xsm link <folder>`.
CODEX_RE = re.compile(r"\A\s*\$xsm[ \t]+(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The arguments of a Claude slash command: `link <folder>`.
ARGS_RE = re.compile(r"\A\s*(%s)(?![^ \t\n])[ \t]*([^\n]*)" % "|".join(VERBS))
# The plugin's skill may be named with or without its plugin prefix.
CLAUDE_COMMANDS = ("xsm", "xsm:xsm")


def _path(ref: str) -> str:
    return paths.path(ASKED, "%s.json" % ref)


def _target(args: str) -> str | None:
    """The first word of the typed arguments that is not an option."""
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    return next((w for w in words if not w.startswith("-")), None)


def resolve(verb: str, target: str, cwd: str) -> str:
    """A folder as its canonical project root, relative to the session's
    folder; a project name (join, leave) as it is."""
    if verb not in ("link", "reach"):
        return target
    from . import config            # lazy: config is heavier than this module needs
    return config.project_root(os.path.join(cwd or "/", os.path.expanduser(target)))


def _write(me: dict | None, verb: str, args: str) -> dict | None:
    if not me or not me.get("ref") or os.environ.get("XSM_WORKER") or not _target(args):
        return None
    entry = {"verb": verb, "args": args.strip(), "cwd": me.get("cwd") or "", "t": time.time(),
             "session_id": str(me.get("session_id") or ""), "runtime": me.get("runtime")}
    paths.write_json(_path(me["ref"]), entry, mode=0o600)
    return entry


def record(me: dict | None, data: dict) -> dict | None:
    """Keep the person's typed xsm command, from one hook call, as consent for
    this session. Returns what was written, or None when there is none."""
    event = data.get("hook_event_name")
    if event == "UserPromptExpansion":
        if data.get("expansion_type") != "slash_command" or \
                data.get("command_name") not in CLAUDE_COMMANDS:
            return None
        m = ARGS_RE.match(data.get("command_args") or "")
    elif event == "UserPromptSubmit" and (me or {}).get("runtime") == "codex":
        text = data.get("prompt") or ""
        if envelope.parse(text).peer or envelope.looks_like_peer(text):
            return None
        m = CODEX_RE.match(text)
    else:
        return None
    return _write(me, m.group(1), m.group(2)) if m else None


def take(me: dict | None, verb: str, target: str, here: str | None = None) -> bool:
    """Use up this session's consent for `verb` on `target` (a folder relative
    to the session's folder, or a project name). True only for a fresh,
    matching consent, which is then gone. For link, `here` is the folder being
    linked from, and it must be the session's own. Anything that does not
    match is left as it was."""
    if not me or not me.get("ref") or not target:
        return False
    p = _path(me["ref"])
    entry = paths.read_json(p)
    if not isinstance(entry, dict) or entry.get("verb") != verb:
        return False
    if time.time() - float(entry.get("t") or 0) > TTL:
        return False
    if entry.get("session_id") and me.get("session_id") and \
            entry["session_id"] != str(me["session_id"]):
        return False                    # a session sharing the ref (24 bits) typed it
    typed = _target(entry.get("args") or "")
    cwd = entry.get("cwd") or me.get("cwd") or ""
    if not typed or resolve(verb, typed, cwd) != resolve(verb, target, me.get("cwd") or cwd):
        return False
    if verb == "link" and here is not None and \
            resolve("link", here, cwd) != resolve("link", ".", cwd):
        return False
    try:
        os.unlink(p)
    except OSError:
        return False                    # someone else used it first
    return True
