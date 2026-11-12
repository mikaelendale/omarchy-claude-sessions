#!/usr/bin/env python3
"""Every coding agent's sessions: Claude Code, Codex, and the other CLIs.

Prints one JSON object per line whenever something changes:

  {"sessions": [{"id", "agent", "agentName", "label", "project", "cwd",
                 "state", "reason", "detail", "since", "window", "workspace",
                 "focused", "tmux", "pid"}...],
   "counts": {"needs", "done", "working", "watching", "idle", "total"}}

state is "needs" (waiting on you), "done" (finished while you were looking
elsewhere: your turn), "working" or "idle".

Claude Code says exactly what it's doing in ~/.claude/sessions, read by
sessions.py (claude_sessions.py where it's bundled).

Codex logs every turn of a session (~/.codex/sessions/.../rollout-*.jsonl), so
it's read from there just as exactly: working from the turn starting until it
ends, waiting on you while it asks to run something.

The other agents (OpenCode, Crush, Amp, Cursor, Copilot, Kimi, Qwen, Gemini,
Grok, Devin, Kilo...) don't say, so they're found as processes in the
foreground of a terminal and called "working" while they draw to it. A
working agent streams text or animates a spinner; an idle one sits still. What
reaches the screen is measured as what the terminal window's own process reads
(its rchar), which is exact when that process has one window, as Omarchy's
foot windows do. Where it can't be (a terminal process with several windows,
tmux, no window found), the agent's CPU is used instead, and only counts when
it stays up for a few seconds: idle CLIs run short update checks.

When an agent rings the bell in a window you're not looking at, a bell hook
(the notch plugin installs one for foot) notes the terminal; that session is
"needs you" until you go to it or it gets busy again.

  agents.py                      stream
  agents.py --once               print one snapshot and exit
  agents.py --notify unfocused   also notify when a session starts needing you
"""

import argparse
import collections
import importlib.util
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.realpath(__file__))
# Claude Code's collector: claude_sessions.py where the notch bundles it,
# sessions.py in the claude-sessions widget.
SESSIONS_PY = next((path for path in (os.path.join(HERE, "claude_sessions.py"),
                                      os.path.join(HERE, "sessions.py")) if os.path.exists(path)),
                   os.path.join(HERE, "sessions.py"))

POLL_SECONDS = 1.0
HEARTBEAT_EVERY = 30.0
# What a terminal reads from a working agent: measured at 4-34 KB/s for
# OpenCode streaming and about 1 KB/s for Claude Code's spinner alone. Idle
# agents send nothing, or a cursor blink (~170 bytes every few seconds).
OUTPUT_BYTES_PER_SECOND = 400
# CPU fallback: working while the agent's terminal processes stay above this
# for most of the last few seconds. Idle CLIs measured 0-3%, with one- or
# two-second bursts to 15-30% (update checks); streaming 8-60%.
CPU_PERCENT = 6.0
CPU_WINDOW = 4                # seconds looked at
CPU_NEEDED = 3                # of which this many must be busy
HOLD_SECONDS = 3.0            # stay "working" this long after the last sign of it

# Agent id -> executable or script names it runs as. The id names the mark in
# /usr/share/omarchy/shell/plugins/agents/assets/<id>.svg where there is one.
AGENTS = {
    "codex": {"codex"},
    "opencode": {"opencode"},
    "agy": {"agy", "antigravity"},
    "crush": {"crush"},
    "amp": {"amp"},
    "cursor": {"cursor-agent"},
    "copilot": {"copilot"},
    "kimi": {"kimi", "kimi-code"},
    "qwen": {"qwen", "qwen-code"},
    "gemini": {"gemini", "gemini-cli"},
    "grok": {"grok"},
    "devin": {"devin"},
    "kilo": {"kilo", "kilocode"},
    "hermes": {"hermes"},
    "muse": {"muse"},
    "goose": {"goose"},
    "aider": {"aider"},
    "omp": {"omp"},
    "pi": {"pi"},
}
NAMES = {name: agent for agent, names in AGENTS.items() for name in names}
INTERPRETERS = {"node", "bun", "deno", "python", "python3", "uv", "uvx", "npx", "bash", "sh"}
DISPLAY = {
    "claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "agy": "Antigravity",
    "crush": "Crush", "amp": "Amp", "cursor": "Cursor", "copilot": "Copilot", "kimi": "Kimi",
    "qwen": "Qwen", "gemini": "Gemini", "grok": "Grok", "devin": "Devin", "kilo": "Kilo",
    "hermes": "Hermes", "muse": "Muse", "goose": "Goose", "aider": "Aider",
    "omp": "Oh My Pi", "pi": "Pi",
}
BELLS = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), "mike-notch/bells")
BELL_MAX_AGE = 3600.0
SPINNER = re.compile(r"^[\s⠀-⣿◐-◓✳✶✢✻✽·•*|/\\-]+")
CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
ORDER = {"needs": 0, "done": 1, "working": 2, "watching": 3, "idle": 4}

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_SESSIONS = os.path.join(CODEX_HOME, "sessions")
CODEX_INDEX = os.path.join(CODEX_HOME, "session_index.jsonl")
# Events in a Codex session log that say where the turn is.
TURN_STARTS = {"task_started", "turn_started"}
TURN_ENDS = {"task_complete", "turn_complete", "turn_aborted"}
ASKS = {"exec_approval_request", "apply_patch_approval_request", "request_user_input",
        "elicitation_request", "request_permissions"}
QUIET = {"token_count"}       # bookkeeping that says nothing about the turn


def load_claude_collector():
    """The Claude sessions collector module, or None if it fails to load."""
    try:
        spec = importlib.util.spec_from_file_location("claude_sessions", SESSIONS_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


# ------------------------------------------------------------------ /proc

def read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as handle:
            return handle.read()
    except OSError:
        return b"" if binary else ""


def stat_fields(pid):
    raw = read(f"/proc/{pid}/stat")
    end = raw.rfind(")")
    return raw[end + 2:].split() if end >= 0 else None


def boot_time():
    for line in read("/proc/stat").splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (ValueError, IndexError):
                break
    return 0.0


BOOT = boot_time()


def started_at(pid):
    """Wall-clock time the process started."""
    fields = stat_fields(pid)
    try:
        return BOOT + int(fields[19]) / CLK
    except (TypeError, ValueError, IndexError):
        return 0.0


def program_name(arg):
    """'amp' from '/…/@ampcode/cli/bin/amp.exe', 'qwen' from 'qwen.js'."""
    base = os.path.basename(arg)
    return re.sub(r"\.(js|mjs|cjs|py|exe)$", "", base)


def agent_from_args(args):
    """The agent a command line runs, by executable or script name, or None."""
    args = [a for a in args if a]
    if not args:
        return None
    first = program_name(args[0])
    if first in NAMES:
        return NAMES[first]
    if first in INTERPRETERS:
        for arg in args[1:3]:
            name = program_name(arg)
            if name in NAMES:
                return NAMES[name]
        # node …/node_modules/@openai/codex/bin/cli.js, …/@qwen-code/qwen-code/cli-entry.js
        for arg in args[1:3]:
            for part in arg.split("/"):
                part = part.lstrip("@")
                if part in NAMES:
                    return NAMES[part]
    return None


def agent_of(pid):
    raw = read(f"/proc/{pid}/cmdline", binary=True).split(b"\0")[:3]
    return agent_from_args([a.decode("utf-8", "replace") for a in raw])


def children_map():
    kids = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fields = stat_fields(int(name))
        if not fields:
            continue
        try:
            kids.setdefault(int(fields[1]), []).append(int(name))
        except (ValueError, IndexError):
            pass
    return kids


def subtree(pid, kids):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


def on_tty(pids, tty):
    """The ones running on this terminal: the agent and the commands it runs,
    not background helpers (Codex keeps an app server going, for one)."""
    out = []
    for p in pids:
        fields = stat_fields(p)
        try:
            if fields and int(fields[4]) == tty:
                out.append(p)
        except (ValueError, IndexError):
            pass
    return out


def cpu_jiffies(pids):
    total = 0
    for p in pids:
        fields = stat_fields(p)
        if fields and len(fields) > 13:
            try:
                total += int(fields[11]) + int(fields[12])
            except ValueError:
                pass
    return total


def bytes_read(pid):
    """Everything the process has read (rchar): for a terminal, mostly what
    its programs printed."""
    for line in read(f"/proc/{pid}/io").splitlines():
        if line.startswith("rchar:"):
            try:
                return int(line.split()[1])
            except ValueError:
                return None
    return None


# ------------------------------------------------------------------ deciding

class Activity:
    """Working or idle for agents that don't say, from samples of what their
    terminal read and their CPU. Pure, so it can be tested with numbers."""

    def __init__(self):
        self.last = {}        # key -> (time, cpu jiffies, terminal bytes or None)
        self.cpu = {}         # key -> recent busy/not busy, one per sample
        self.until = {}       # key -> time it counts as working until
        self.seen = {}        # key -> wall-clock time it was last working

    def update(self, key, now, cpu, terminal_bytes):
        before = self.last.get(key)
        self.last[key] = (now, cpu, terminal_bytes)
        history = self.cpu.setdefault(key, collections.deque(maxlen=CPU_WINDOW))
        if before:
            dt = max(0.05, now - before[0])
            busy_cpu = 100.0 * (cpu - before[1]) / CLK / dt >= CPU_PERCENT
            history.append(busy_cpu)
            if terminal_bytes is not None and before[2] is not None:
                active = (terminal_bytes - before[2]) / dt >= OUTPUT_BYTES_PER_SECOND
            else:
                active = sum(history) >= CPU_NEEDED
            if active:
                self.until[key] = now + HOLD_SECONDS
                self.seen[key] = time.time()
        return "working" if self.until.get(key, 0) > now else "idle"

    def forget(self, keep):
        for table in (self.last, self.cpu, self.until, self.seen):
            for key in list(table):
                if key not in keep:
                    del table[key]


class Turns:
    """Turns working -> idle into "done" (your turn) when it happens in a window
    you aren't looking at, until you look; and keeps when each state began."""

    def __init__(self):
        self.memory = {}      # key -> {"state", "done", "since"}

    def update(self, key, state, focused, now_ms):
        memory = self.memory.get(key)
        if memory is None:
            memory = {"state": state, "done": False, "since": now_ms}
        done = memory["done"]
        if state == "idle":
            if memory["state"] == "working" and not focused:
                done = True
            if focused:
                done = False
        else:
            done = False
        shown = "done" if done else state
        since = memory["since"] if memory.get("shown") == shown else now_ms
        self.memory[key] = {"state": state, "done": done, "since": since, "shown": shown}
        return shown, since

    def forget(self, keep):
        for key in list(self.memory):
            if key not in keep:
                del self.memory[key]


# ------------------------------------------------------------------ Codex

class CodexLog:
    """One Codex session log, read as it grows: the session's id, its folder,
    and where the turn is (idle, working, or waiting on you)."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.id = ""
        self.cwd = ""
        self.created = 0.0
        self.state = "idle"

    def update(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:            # rewritten: read it again
            self.offset, self.partial, self.state = 0, b"", "idle"
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read(size - self.offset)
        except OSError:
            return
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()        # a line still being written waits for the rest
        for raw in lines:
            self.feed(raw)

    def feed(self, raw):
        head = raw[:160]
        # Most lines are the conversation itself; only these two kinds matter.
        if b'"session_meta"' not in head and b'"event_msg"' not in head:
            return
        try:
            entry = json.loads(raw)
        except ValueError:
            return
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            return
        if entry.get("type") == "session_meta":
            self.id = str(payload.get("id") or "")
            self.cwd = str(payload.get("cwd") or "")
            self.created = parse_time(payload.get("timestamp") or entry.get("timestamp"))
            return
        event = payload.get("type")
        if event in TURN_STARTS:
            self.state = "working"
        elif event in TURN_ENDS:
            self.state = "idle"
        elif event in ASKS:
            self.state = "needs"
        elif self.state == "needs" and event not in QUIET:
            self.state = "working"        # whatever comes next means it was answered


def parse_time(value):
    """Seconds since the epoch for an ISO 8601 time like 2026-11-12T09:30:10.808Z."""
    if not isinstance(value, str) or len(value) < 19:
        return 0.0
    try:
        import calendar
        base = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        return float(calendar.timegm(base)) if value.endswith("Z") or "+00:00" in value else time.mktime(base)
    except ValueError:
        return 0.0


class Codex:
    """Finds each running Codex's session log and reads its state."""

    def __init__(self):
        self.logs = {}            # path -> CodexLog
        self.titles = {}          # session id -> the name Codex gave the thread
        self.index_seen = None

    def log(self, path):
        log = self.logs.get(path)
        if log is None:
            log = self.logs[path] = CodexLog(path)
        log.update()
        return log

    def held_open(self, pids):
        """The session log one of these processes has open, if any."""
        for p in pids:
            try:
                fds = os.listdir(f"/proc/{p}/fd")
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(f"/proc/{p}/fd/{fd}")
                except OSError:
                    continue
                if (target.startswith(CODEX_SESSIONS + "/") and target.endswith(".jsonl")
                        and os.path.basename(target).startswith("rollout-")):
                    return target
        return None

    def recent(self, since):
        """Session logs written to since then, from the last two days' folders."""
        out = []
        for days in (0, 1):
            day = time.localtime(time.time() - days * 86400)
            folder = os.path.join(CODEX_SESSIONS, time.strftime("%Y/%m/%d", day))
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                path = os.path.join(folder, name)
                try:
                    if os.path.getmtime(path) >= since - 2:
                        out.append(path)
                except OSError:
                    pass
        return out

    def assign(self, tuis):
        """{pid: CodexLog or None} for running Codex TUIs [(pid, tree, cwd)].

        A log one of them holds open is its own. Otherwise each takes the newest
        log started in its folder after it started (newest Codex first), so two
        Codexes in one folder each get their own; one that hasn't been asked
        anything yet has no log, and is idle."""
        out, taken = {}, set()
        seen = set()
        waiting = []
        for pid, tree, cwd in tuis:
            path = self.held_open(tree)
            if path:
                out[pid] = self.log(path)
                taken.add(path)
                seen.add(path)
            else:
                waiting.append((started_at(pid), pid, cwd))
        if waiting:
            candidates = []
            for path in self.recent(min(start for start, _, _ in waiting)):
                if path in taken:
                    continue
                seen.add(path)
                candidates.append(self.log(path))
            for start, pid, cwd in sorted(waiting, reverse=True):
                best = None
                for log in candidates:
                    if log.path in taken or log.cwd != cwd or log.created < start - 2:
                        continue
                    if best is None or log.created > best.created:
                        best = log
                if best:
                    taken.add(best.path)
                out[pid] = best
        # Forget logs that aren't in play any more.
        for path in list(self.logs):
            if path not in seen:
                self.logs.pop(path, None)
        return out

    def title(self, session_id):
        """The name Codex gave a thread (in session_index.jsonl), or ""."""
        try:
            seen = os.stat(CODEX_INDEX).st_mtime
        except OSError:
            return ""
        if seen != self.index_seen:
            self.index_seen = seen
            titles = {}
            for line in read(CODEX_INDEX).splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    titles[str(entry["id"])] = str(entry.get("thread_name") or "")
            self.titles = titles
        return self.titles.get(session_id, "")


# ------------------------------------------------------------------ others

class Others:
    """Every agent but Claude, in terminals."""

    def __init__(self, hypr):
        self.hypr = hypr
        self.codex = Codex()
        self.activity = Activity()
        self.turns = Turns()

    def bells(self):
        """terminal pid -> wall-clock time its bell rang."""
        out = {}
        try:
            names = os.listdir(BELLS)
        except OSError:
            return out
        now = time.time()
        for name in names:
            path = os.path.join(BELLS, name)
            try:
                rang = float(read(path).strip() or 0)
            except ValueError:
                rang = 0
            if not name.isdigit() or now - rang > BELL_MAX_AGE or not os.path.exists(f"/proc/{name}"):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            out[int(name)] = rang
        return out

    def clear_bell(self, term_pid):
        try:
            os.remove(os.path.join(BELLS, str(term_pid)))
        except OSError:
            pass

    def window_for(self, pid, windows):
        current = pid
        for _ in range(10):
            if current in windows:
                return windows[current]
            fields = stat_fields(current)
            try:
                parent = int(fields[1]) if fields else 0
            except ValueError:
                parent = 0
            if parent <= 1 or parent == current:
                return None
            current = parent
        return None

    def snapshot(self, now):
        windows = {}
        clients = self.hypr.clients() if self.hypr else None
        if clients:
            windows = clients
        kids = children_map()
        found = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            agent = agent_of(pid)
            if not agent:
                continue
            fields = stat_fields(pid)
            if not fields or len(fields) < 8:
                continue
            try:
                ppid, pgrp, tty, tpgid = int(fields[1]), int(fields[2]), int(fields[4]), int(fields[5])
            except ValueError:
                continue
            # Interactive: on a terminal, in its foreground.
            if tty == 0 or tpgid != pgrp:
                continue
            # The same agent's helper processes count as the one session.
            if agent_of(ppid) == agent:
                continue
            found.append((pid, agent, tty))

        codex_tuis = []
        for pid, agent, tty in found:
            if agent == "codex":
                cwd = os.path.realpath(f"/proc/{pid}/cwd") if os.path.exists(f"/proc/{pid}/cwd") else ""
                codex_tuis.append((pid, subtree(pid, kids), cwd))
        codex_logs = self.codex.assign(codex_tuis) if codex_tuis else {}

        bells = self.bells()
        focused = self.hypr.active_address() if self.hypr else ""
        alive = set()
        sessions = []
        now_ms = int(time.time() * 1000)
        for pid, agent, tty in found:
            alive.add(pid)
            tree = subtree(pid, kids)
            window = self.window_for(pid, windows)
            address = (window or {}).get("address", "")
            is_focused = bool(address) and address == focused
            term = (window or {}).get("pid")
            label = ""
            if agent == "codex":
                # Its own log says exactly; no log yet means nothing asked yet.
                log = codex_logs.get(pid)
                state = log.state if log else "idle"
                label = self.codex.title(log.id) if log else ""
                if state == "working":
                    self.activity.seen[pid] = time.time()
            else:
                # What the window's terminal read, when that's this session alone.
                reads = bytes_read(term) if term and not window.get("shared") else None
                state = self.activity.update(pid, now, cpu_jiffies(on_tty(tree, tty)), reads)

            shown, since = self.turns.update(pid, state, is_focused, now_ms)

            # Its terminal rang the bell: needs you, until you go to it or it
            # gets busy again after the bell.
            # Codex asking to run a command or apply a patch: a permission.
            reason = "permission" if shown == "needs" else ""
            if term in bells:
                rang = bells[term]
                if is_focused or self.activity.seen.get(pid, 0) > rang + HOLD_SECONDS:
                    self.clear_bell(term)
                else:
                    shown, reason = "needs", "bell"

            cwd = os.path.realpath(f"/proc/{pid}/cwd") if os.path.exists(f"/proc/{pid}/cwd") else ""
            if not label:
                label = SPINNER.sub("", (window or {}).get("title", "") or "").strip()
            if not label or label.lower() in ("foot", agent, DISPLAY.get(agent, "").lower(), os.path.basename(cwd).lower()):
                where = cwd.replace(HOME, "~", 1) if cwd.startswith(HOME) else cwd
                label = f"{DISPLAY.get(agent, agent)} in {where or '~'}"
            sessions.append({
                "id": f"{agent}:{pid}",
                "agent": agent,
                "agentName": DISPLAY.get(agent, agent),
                "label": label[:120],
                "project": os.path.basename(cwd),
                "cwd": cwd,
                "state": shown,
                "reason": reason,
                "detail": "",
                "since": since,
                "window": address,
                "workspace": (window or {}).get("workspace", ""),
                "focused": is_focused,
                "tmux": False,
                "pid": pid,
            })
        self.activity.forget(alive)
        self.turns.forget(alive)
        return sessions


class Hypr:
    """Fallback when the Claude collector (and its Hyprland socket client)
    didn't load: pid -> {pid, address, title, workspace, shared} via hyprctl."""

    def active_address(self):
        import subprocess
        try:
            out = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=3).stdout
            return (json.loads(out) or {}).get("address", "") or ""
        except Exception:
            return ""

    def clients(self):
        import subprocess
        try:
            out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=3).stdout
            data = json.loads(out)
        except Exception:
            return {}
        result = {}
        for c in data if isinstance(data, list) else []:
            pid = c.get("pid")
            if isinstance(pid, int):
                result[pid] = {
                    "pid": pid,
                    "address": c.get("address", ""),
                    "title": c.get("title", ""),
                    "workspace": str((c.get("workspace") or {}).get("name", "")),
                    "shared": pid in result,
                }
        return result


# ------------------------------------------------------------------ main

def combine(claude, others):
    sessions = []
    for s in (claude or {}).get("sessions", []):
        s = dict(s)
        s.setdefault("agent", "claude")
        s.setdefault("agentName", DISPLAY["claude"])
        sessions.append(s)
    sessions.extend(others)
    sessions.sort(key=lambda s: (ORDER.get(s.get("state"), 9), s.get("since") or 0, s.get("agentName", "")))
    counts = {"needs": 0, "done": 0, "working": 0, "watching": 0, "idle": 0}
    for s in sessions:
        if s.get("state") in counts:
            counts[s["state"]] += 1
    counts["total"] = len(sessions)
    return {"sessions": sessions, "counts": counts}


class OtherAlerts:
    """Notifications for agents other than Claude (whose collector notifies
    for itself): once when a session starts needing you, withdrawn after."""

    def __init__(self, notifier, mode):
        self.notifier = notifier
        self.mode = mode
        self.needing = set()

    def update(self, sessions):
        now = set()
        for s in sessions:
            if s.get("agent") == "claude" or s.get("state") != "needs":
                continue
            now.add(s["id"])
            if s["id"] in self.needing:
                continue
            if self.mode == "always" or (self.mode == "unfocused" and not s.get("focused")):
                why = "Needs permission" if s.get("reason") == "permission" else "Needs you"
                body = f"{s.get('agentName', '')}: {why}" + (f" · {s['project']}" if s.get("project") else "")
                self.notifier.show(s["id"], s.get("label") or s.get("agentName", ""), body, s.get("window", ""))
        for key in self.needing - now:
            self.notifier.withdraw(key)
        self.needing = now


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--notify", choices=["unfocused", "always", "off"], default="off")
    args = parser.parse_args(argv)

    cs = load_claude_collector()
    notifier = cs.Notifier() if cs and args.notify != "off" and not args.once else None
    claude = None
    hypr = None
    if cs:
        try:
            hypr = cs.Hyprland()
            claude = cs.Collector(cs.config_homes(), hypr=hypr, notifier=notifier, notify_mode=args.notify)
        except Exception:
            claude = None
    others = Others(hypr or Hypr())
    alerts = OtherAlerts(notifier, args.notify) if notifier else None

    def snapshot(now):
        c = None
        if claude:
            try:
                c = claude.snapshot(now)
            except Exception:
                c = None
        payload = combine(c, others.snapshot(time.monotonic()))
        if alerts:
            alerts.update(payload["sessions"])
        return payload

    if args.once:
        snapshot(time.monotonic())
        time.sleep(1.0)  # a second look, so working/idle has something to compare
        print(json.dumps(snapshot(time.monotonic()), indent=2))
        return 0

    parent = os.getppid()
    last_text, last_emit = None, 0.0
    while True:
        now = time.monotonic()
        payload = snapshot(now)
        text = json.dumps(payload, sort_keys=True)
        if text != last_text or now - last_emit >= HEARTBEAT_EVERY:
            try:
                sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                return 0
            last_text, last_emit = text, now
        if os.getppid() != parent:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
