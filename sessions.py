#!/usr/bin/env python3
"""Live status of every Claude Code session on this machine, for the Omarchy bar.

Claude Code keeps one small file per running session in
<config home>/sessions/<pid>.json with a `status` field:

  busy     working
  waiting  needs the user; `waitingFor` says why
  shell    the model has stopped, but a background task it started (a dev
           server, a monitor, a long command) is still running
  idle     idle

A `shell` session is shown as idle (or "your turn" if it just finished while
you were elsewhere) with the reason "background": the model is done
responding, and a dev server left running is not Claude still working.

This helper reads those files, drops the ones whose process is gone, matches
each session to its terminal window through Hyprland, and prints one JSON line
whenever anything changes (and a heartbeat every 30 seconds). It also raises a
desktop notification when a session starts needing the user.

Standard library only. Run with `python3 -I sessions.py [--once] [--notify ...]`.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

MAX_FILE_BYTES = 262144  # Claude Code's own cap for these records.
POLL_SECONDS = 0.75
CLIENTS_EVERY = 2.0
HOMES_EVERY = 60.0
HEARTBEAT_EVERY = 30.0
MAX_SESSIONS = 64
MAX_LABEL = 80
MAX_TEXT = 120
SKIP_KINDS = {"daemon", "daemon-worker"}

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{1,16}$")
PID_FILE_RE = re.compile(r"^[0-9]{1,10}\.json$")
# Spinner and status glyphs Claude Code puts in front of the terminal title,
# plus braille spinner frames other tools use.
LEADING_GLYPHS_RE = re.compile(
    "^[\\s·•*✢✳✶✻✽◐-◓⠀-⣿]+"
)
CONTROL_RE = re.compile("[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]")

# waitingFor -> the reason the bar shows.
REASONS = {
    "permission prompt": "permission",
    "sandbox request": "permission",
    "worker request": "permission",
    "goal proposal": "approval",
    "input needed": "question",
    "dialog open": "dialog",
}
REASON_TEXT = {
    "permission": "Needs permission",
    "approval": "Has a plan to approve",
    "question": "Has a question",
    "dialog": "Has a dialog open",
    "other": "Needs you",
}
STATE_ORDER = {"needs": 0, "done": 1, "working": 2, "watching": 3, "idle": 4}


def clean_text(value, limit=MAX_TEXT):
    """A single line of plain text, with control and bidi characters removed."""
    if not isinstance(value, str):
        return ""
    text = CONTROL_RE.sub("", value.replace("\n", " ").replace("\t", " "))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def title_label(title):
    """The session title from a terminal title such as '◑ Fix the login bug'."""
    if not isinstance(title, str):
        return ""
    return clean_text(LEADING_GLYPHS_RE.sub("", title), MAX_LABEL)


def finite_ms(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0 or value > 4e15:
        return None
    return int(value)


# --------------------------------------------------------------------------
# /proc


class ProcTable:
    """Reads process start times and parents from a /proc-like directory."""

    def __init__(self, root="/proc"):
        self.root = root

    def _stat_fields(self, pid):
        try:
            with open(os.path.join(self.root, str(pid), "stat"), "rb") as handle:
                raw = handle.read(4096).decode("utf-8", "replace")
        except OSError:
            return None
        end = raw.rfind(")")
        if end < 0:
            return None
        # Fields after the command name start at field 3 (state).
        return raw[end + 1 :].split()

    def start_time(self, pid):
        fields = self._stat_fields(pid)
        if not fields or len(fields) < 20:
            return None
        return fields[19]  # field 22: starttime

    def command(self, pid):
        """The process name from /proc/<pid>/stat, e.g. 'claude' or 'foot'."""
        try:
            with open(os.path.join(self.root, str(pid), "stat"), "rb") as handle:
                raw = handle.read(4096).decode("utf-8", "replace")
        except OSError:
            return ""
        start, end = raw.find("("), raw.rfind(")")
        return raw[start + 1 : end] if 0 <= start < end else ""

    def parent(self, pid):
        fields = self._stat_fields(pid)
        if not fields or len(fields) < 2:
            return None
        try:
            return int(fields[1])  # field 4: ppid
        except ValueError:
            return None


# --------------------------------------------------------------------------
# Hyprland


class Hyprland:
    """Talks to Hyprland's request socket directly, so nothing is spawned."""

    def __init__(self):
        self.path = self._socket_path()

    @staticmethod
    def _socket_path():
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        base = os.path.join(runtime, "hypr")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        if signature and "/" not in signature:
            return os.path.join(base, signature, ".socket.sock")
        try:
            candidates = [
                os.path.join(base, name, ".socket.sock")
                for name in os.listdir(base)
                if "/" not in name
            ]
        except OSError:
            return ""
        candidates = [path for path in candidates if os.path.exists(path)]
        if not candidates:
            return ""
        return max(candidates, key=lambda path: os.path.getmtime(os.path.dirname(path)))

    def request(self, command):
        if not self.path:
            self.path = self._socket_path()
            if not self.path:
                return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(self.path)
                sock.sendall(command.encode())
                chunks = []
                size = 0
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        return None
            return json.loads(b"".join(chunks).decode("utf-8", "replace"))
        except (OSError, ValueError):
            self.path = ""
            return None

    def clients(self):
        data = self.request("j/clients")
        if not isinstance(data, list):
            return None
        windows = {}
        for client in data:
            if not isinstance(client, dict):
                continue
            pid = client.get("pid")
            address = client.get("address")
            if not isinstance(pid, int) or not isinstance(address, str):
                continue
            if not ADDRESS_RE.match(address):
                continue
            workspace = client.get("workspace") if isinstance(client.get("workspace"), dict) else {}
            windows[pid] = {
                "address": address,
                "title": client.get("title") if isinstance(client.get("title"), str) else "",
                "workspace": clean_text(str(workspace.get("name", "")), 24),
            }
        return windows

    def active_address(self):
        data = self.request("j/activewindow")
        if isinstance(data, dict):
            address = data.get("address")
            if isinstance(address, str) and ADDRESS_RE.match(address):
                return address
        return ""


def focus_window(address):
    if not ADDRESS_RE.match(address or ""):
        return
    try:
        # Hyprland's Lua config takes dispatchers as Lua calls.
        subprocess.run(
            ["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:%s" })' % address],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------
# Notifications


class Notifier:
    """One desktop notification per waiting episode; clicking it focuses the
    session, and it is withdrawn once the session no longer needs the user."""

    def __init__(self):
        self.ids = {}
        self.lock = threading.Lock()

    def show(self, key, summary, body, address):
        thread = threading.Thread(target=self._run, args=(key, summary, body, address), daemon=True)
        thread.start()

    def _run(self, key, summary, body, address):
        command = [
            "notify-send",
            "--app-name=Claude Code",
            "--icon=dialog-question",
            "--print-id",
        ]
        if address:
            command += ["--action=default=Open"]
        command += ["--", summary, body]
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return
        first = proc.stdout.readline().strip()
        if first.isdigit():
            with self.lock:
                self.ids[key] = first
        if not address:
            proc.wait()
            return
        action = proc.stdout.readline().strip()
        proc.wait()
        with self.lock:
            if self.ids.get(key) == first:
                del self.ids[key]
        if action == "default":
            focus_window(address)

    def withdraw(self, key):
        with self.lock:
            notification_id = self.ids.pop(key, None)
        if not notification_id:
            return
        thread = threading.Thread(target=self._close, args=(notification_id,), daemon=True)
        thread.start()

    @staticmethod
    def _close(notification_id):
        try:
            subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.freedesktop.Notifications",
                    "--object-path", "/org/freedesktop/Notifications",
                    "--method", "org.freedesktop.Notifications.CloseNotification",
                    notification_id,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


# --------------------------------------------------------------------------
# Config homes


def config_homes(home_dir=None, state_dir=None, env=None):
    """~/.claude, CLAUDE_CONFIG_DIR, and any extra Omarchy agent accounts."""
    env = os.environ if env is None else env
    home_dir = home_dir or os.path.expanduser("~")
    homes = [os.path.join(home_dir, ".claude")]
    extra = env.get("CLAUDE_CONFIG_DIR", "")
    if extra:
        homes.append(extra)
    state_dir = state_dir or env.get("XDG_STATE_HOME") or os.path.join(home_dir, ".local", "state")
    registry = os.path.join(state_dir, "omarchy", "agents", "accounts", "claude.json")
    try:
        if os.path.getsize(registry) <= MAX_FILE_BYTES:
            with open(registry, encoding="utf-8") as handle:
                homes.extend(_find_homes(json.load(handle)))
    except (OSError, ValueError):
        pass
    seen = []
    for home in homes:
        if isinstance(home, str) and os.path.isabs(home):
            home = os.path.normpath(home)
            if home not in seen:
                seen.append(home)
    return seen


def _find_homes(data, depth=0):
    if depth > 6:
        return []
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "home" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_find_homes(value, depth + 1))
    elif isinstance(data, list):
        for value in data[:64]:
            found.extend(_find_homes(value, depth + 1))
    return found


# --------------------------------------------------------------------------
# Collector


def read_record(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def session_name(record):
    """The session's own name, unless it is a bare id prefix ('c578b3cb')."""
    name = clean_text(record.get("name"), MAX_LABEL)
    return "" if re.fullmatch(r"[0-9a-f]{8}", name) else name


def map_status(status, waiting_for):
    """(state, reason, detail) for a raw status."""
    if status == "busy":
        return "working", "", ""
    if status == "shell":
        # The turn is over; only something it started is still running.
        return "idle", "background", ""
    if status == "waiting":
        raw = waiting_for if isinstance(waiting_for, str) else ""
        reason = REASONS.get(raw)
        if reason is None:
            return "needs", "other", clean_text(raw, 60)
        return "needs", reason, ""
    if status == "idle":
        return "idle", "", ""
    # Unknown: show it as idle, with the raw word, instead of guessing.
    return "idle", "", clean_text(status if isinstance(status, str) else "", 40)


class Collector:
    def __init__(self, homes, proc=None, hypr=None, notifier=None, notify_mode="unfocused"):
        self.homes = homes
        self.proc = proc or ProcTable()
        self.hypr = hypr
        self.notifier = notifier
        self.notify_mode = notify_mode
        self.windows = {}
        self.windows_at = 0.0
        self.active = ""
        self.memory = {}  # session key -> {"state", "done"}

    def refresh_windows(self, now):
        if self.hypr is None:
            return
        if now - self.windows_at >= CLIENTS_EVERY:
            windows = self.hypr.clients()
            if windows is not None:
                self.windows = windows
            self.windows_at = now
        self.active = self.hypr.active_address()

    def window_for(self, pid):
        current = pid
        for _ in range(8):
            if current in self.windows:
                return self.windows[current]
            parent = self.proc.parent(current)
            if not parent or parent <= 1 or parent == current:
                return None
            # Background sessions run under Claude's daemon, which the session
            # that started it spawned. Walking past another Claude process
            # would hand them that session's window and title.
            if self.proc.command(parent) == "claude":
                return None
            current = parent
        return None

    def records(self):
        for home in self.homes:
            directory = os.path.join(home, "sessions")
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not PID_FILE_RE.match(name):
                    continue
                pid = int(name[:-5])
                if str(pid) + ".json" != name:
                    continue
                record = read_record(os.path.join(directory, name))
                if record is None:
                    continue
                yield pid, record

    def alive(self, pid, record):
        started = self.proc.start_time(pid)
        if started is None:
            return False
        expected = record.get("procStart")
        if isinstance(expected, str) and expected and expected != started:
            return False
        return True

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        self.refresh_windows(now)
        sessions = []
        seen = set()
        for pid, record in self.records():
            if len(sessions) >= MAX_SESSIONS:
                break
            kind = record.get("kind")
            if isinstance(kind, str) and kind in SKIP_KINDS:
                continue
            if record.get("pid") not in (None, pid):
                continue
            if not self.alive(pid, record):
                continue
            session_id = record.get("sessionId")
            key = clean_text(session_id, 64) if isinstance(session_id, str) and session_id else str(pid)
            key = key + ":" + str(pid)
            if key in seen:
                continue
            seen.add(key)

            state, reason, detail = map_status(record.get("status"), record.get("waitingFor"))
            window = self.window_for(pid)
            address = window["address"] if window else ""
            focused = bool(address) and address == self.active

            memory = self.memory.get(key, {"state": None, "done": False})
            previous = memory["state"]
            done = memory["done"]
            if state == "idle":
                if previous == "working" and not focused:
                    done = True
                if focused:
                    done = False
            else:
                done = False
            shown = "done" if done else state

            if self.notifier is not None and previous is not None:
                if state == "needs" and previous != "needs":
                    if self.notify_mode == "always" or (self.notify_mode == "unfocused" and not focused):
                        self.notifier.show(key, *self._notification(record, window, reason, detail), address)
                elif previous == "needs" and state != "needs":
                    self.notifier.withdraw(key)
            self.memory[key] = {"state": state, "done": done}

            cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else ""
            project = clean_text(os.path.basename(cwd.rstrip("/")) or cwd, 40)
            label = title_label(window["title"]) if window else ""
            if not label:
                label = session_name(record) or project or str(pid)

            sessions.append({
                "id": key,
                "pid": pid,
                "label": label,
                "project": project,
                "cwd": clean_text(cwd, 200),
                "state": shown,
                "reason": reason,
                "detail": detail,
                "since": finite_ms(record.get("statusUpdatedAt")) or finite_ms(record.get("startedAt")) or 0,
                "window": address,
                "workspace": window["workspace"] if window else "",
                "focused": focused,
                "tmux": isinstance(record.get("tmux"), str) and record.get("tmux") != "",
                "kind": clean_text(kind, 20) if isinstance(kind, str) else "interactive",
            })

        for key in list(self.memory):
            if key not in seen:
                if self.notifier is not None and self.memory[key]["state"] == "needs":
                    self.notifier.withdraw(key)
                del self.memory[key]

        sessions.sort(key=lambda s: (STATE_ORDER.get(s["state"], 9), s["since"] or 0, s["pid"]))
        counts = {state: 0 for state in STATE_ORDER}
        for session in sessions:
            counts[session["state"]] += 1
        return {"sessions": sessions, "counts": counts}

    @staticmethod
    def _notification(record, window, reason, detail):
        cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else ""
        project = clean_text(os.path.basename(cwd.rstrip("/")), 40)
        label = title_label(window["title"]) if window else ""
        label = label or session_name(record) or project or "Claude Code"
        what = detail or REASON_TEXT.get(reason, REASON_TEXT["other"])
        body = what + (" · " + project if project else "")
        return label, body


# --------------------------------------------------------------------------
# Main


def emit(payload):
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        os._exit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--notify", choices=["unfocused", "always", "off"], default="unfocused")
    args = parser.parse_args(argv)

    hypr = Hyprland()
    if args.once:
        collector = Collector(config_homes(), hypr=hypr)
        print(json.dumps(collector.snapshot(), ensure_ascii=False, indent=2))
        return 0

    notifier = Notifier() if args.notify != "off" else None
    collector = Collector(config_homes(), hypr=hypr, notifier=notifier, notify_mode=args.notify)
    parent = os.getppid()
    last_text = None
    last_emit = 0.0
    homes_at = time.monotonic()
    while True:
        now = time.monotonic()
        if now - homes_at >= HOMES_EVERY:
            collector.homes = config_homes()
            homes_at = now
        payload = collector.snapshot(now)
        text = json.dumps(payload, sort_keys=True)
        if text != last_text or now - last_emit >= HEARTBEAT_EVERY:
            emit(payload)
            last_text = text
            last_emit = now
        if os.getppid() != parent:
            return 0  # the shell went away
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
