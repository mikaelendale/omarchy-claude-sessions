import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents  # noqa: E402

CLK = agents.CLK


class Names(unittest.TestCase):
    # Command lines as they really appear in /proc for the installed CLIs.
    CASES = [
        (["/home/u/.local/share/mise/installs/codex/latest/bin/codex"], "codex"),
        (["/home/u/.local/share/mise/installs/cursor-agent/latest/bin/cursor-agent", "--use-system-ca",
          "/home/u/…/dist-package/index.js"], "cursor"),
        (["opencode"], "opencode"),
        (["/home/u/.local/share/mise/installs/grok/latest/grok"], "grok"),
        (["node", "/home/u/…/node_modules/@qwen-code/qwen-code/cli-entry.js"], "qwen"),
        (["/home/u/…/node_modules/@ampcode/cli/bin/amp.exe"], "amp"),
        (["/home/u/.local/share/mise/installs/copilot/latest/copilot"], "copilot"),
        (["/home/u/…/crush_0.100.0_Linux_x86_64/crush"], "crush"),
        (["kimi-code"], "kimi"),
        (["/home/u/.local/share/mise/installs/http-devin/latest/bin/devin"], "devin"),
        (["node", "/home/u/…/node_modules/@kilocode/cli/bin/kilo"], "kilo"),
        (["node", "/usr/lib/node_modules/@google/gemini-cli/dist/index.js", "gemini"], "gemini"),
        (["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"], "codex"),
    ]

    def test_installed_clis(self):
        for args, agent in self.CASES:
            with self.subTest(args=args[0]):
                self.assertEqual(agents.agent_from_args(args), agent)

    def test_not_agents(self):
        for args in (["bash"], ["node", "server.js"], ["vim", "codex-notes.md"], ["python3", "-m", "http.server"], [], [""]):
            with self.subTest(args=args):
                self.assertIsNone(agents.agent_from_args(args))


class ActivityFromOutput(unittest.TestCase):
    """Terminal output is the signal when the window's terminal is measurable."""

    def run_samples(self, per_second_bytes, cpu_percent=0.0):
        activity = agents.Activity()
        cpu, read, states = 0, 0, []
        for t, b in enumerate(per_second_bytes):
            cpu += int(cpu_percent * CLK / 100)
            read += b
            states.append(activity.update("k", float(t), cpu, read))
        return states

    def test_streaming_is_working(self):
        # OpenCode streaming: 4-34 KB/s.
        self.assertEqual(self.run_samples([0, 9000, 20000, 15000])[1:], ["working"] * 3)

    def test_spinner_alone_is_working(self):
        # Claude Code's spinner: about 1 KB/s.
        self.assertEqual(self.run_samples([0, 1000, 1100, 900])[-1], "working")

    def test_cursor_blink_is_idle(self):
        self.assertEqual(set(self.run_samples([0, 0, 170, 0, 0, 170, 0, 0, 0, 0])), {"idle"})

    def test_quiet_terminal_beats_busy_cpu(self):
        # OpenCode's start screen used 16-33% CPU while drawing nothing.
        self.assertEqual(set(self.run_samples([0] * 10, cpu_percent=25)), {"idle"})

    def test_holds_briefly_then_idles(self):
        states = self.run_samples([0, 9000, 0, 0, 0, 0, 0])
        self.assertEqual(states[1:4], ["working"] * 3)
        self.assertEqual(states[-1], "idle")


class ActivityFromCpu(unittest.TestCase):
    """No measurable terminal: CPU counts only when it stays up."""

    def run_cpu(self, percents):
        activity = agents.Activity()
        cpu, states = 0, []
        for t, p in enumerate(percents):
            cpu += int(p * CLK / 100)
            states.append(activity.update("k", float(t), cpu, None))
        return states

    def test_update_check_bursts_are_idle(self):
        # Measured idle: Qwen 15%, 16% for two seconds; Kimi 29% for one.
        self.assertEqual(set(self.run_cpu([0, 0, 15, 16, 0, 0, 1, 29, 1, 0, 0])), {"idle"})

    def test_sustained_cpu_is_working(self):
        states = self.run_cpu([0, 20, 30, 25, 40, 30])
        self.assertEqual(states[3:], ["working"] * 3)

    def test_quiet_cli_is_idle(self):
        self.assertEqual(set(self.run_cpu([0, 1, 2, 1, 0, 2, 3, 2])), {"idle"})


class TurnsTest(unittest.TestCase):
    def test_finishing_while_away_is_your_turn_until_looked_at(self):
        turns = agents.Turns()
        self.assertEqual(turns.update("k", "working", False, 1000)[0], "working")
        shown, since = turns.update("k", "idle", False, 2000)
        self.assertEqual((shown, since), ("done", 2000))
        self.assertEqual(turns.update("k", "idle", False, 3000), ("done", 2000))
        self.assertEqual(turns.update("k", "idle", True, 4000)[0], "idle")

    def test_finishing_while_looking_is_idle(self):
        turns = agents.Turns()
        turns.update("k", "working", True, 1000)
        self.assertEqual(turns.update("k", "idle", True, 2000)[0], "idle")

    def test_first_sighting_idle_is_not_done(self):
        self.assertEqual(agents.Turns().update("k", "idle", False, 1000)[0], "idle")


class FakeNotifier:
    def __init__(self):
        self.shown, self.withdrawn = [], []

    def show(self, key, summary, body, address):
        self.shown.append((key, summary, body, address))

    def withdraw(self, key):
        self.withdrawn.append(key)


class Alerts(unittest.TestCase):
    def session(self, state, focused=False, agent="codex"):
        return {"id": f"{agent}:1", "agent": agent, "agentName": "Codex", "label": "Fix tests",
                "project": "api", "state": state, "reason": "permission", "focused": focused, "window": "0xa"}

    def test_once_then_withdrawn(self):
        notifier = FakeNotifier()
        alerts = agents.OtherAlerts(notifier, "unfocused")
        alerts.update([self.session("working")])
        alerts.update([self.session("needs")])
        alerts.update([self.session("needs")])
        self.assertEqual(notifier.shown, [("codex:1", "Fix tests", "Codex: Needs permission · api", "0xa")])
        alerts.update([self.session("working")])
        self.assertEqual(notifier.withdrawn, ["codex:1"])

    def test_not_while_looking(self):
        notifier = FakeNotifier()
        agents.OtherAlerts(notifier, "unfocused").update([self.session("needs", focused=True)])
        self.assertEqual(notifier.shown, [])

    def test_claude_left_to_its_own_collector(self):
        notifier = FakeNotifier()
        agents.OtherAlerts(notifier, "always").update([self.session("needs", agent="claude")])
        self.assertEqual(notifier.shown, [])


class Combine(unittest.TestCase):
    def test_order_and_counts(self):
        claude = {"sessions": [{"id": "c", "state": "working", "since": 5}]}
        others = [{"id": "x", "agent": "codex", "agentName": "Codex", "state": "needs", "since": 9},
                  {"id": "y", "agent": "crush", "agentName": "Crush", "state": "idle", "since": 1}]
        out = agents.combine(claude, others)
        self.assertEqual([s["id"] for s in out["sessions"]], ["x", "c", "y"])
        self.assertEqual(out["sessions"][1]["agentName"], "Claude Code")
        self.assertEqual(out["counts"], {"needs": 1, "done": 0, "working": 1, "watching": 0, "idle": 1, "total": 3})


class CodexLogReplay(unittest.TestCase):
    """A Codex 0.167 session log, line shapes as written on this machine."""

    def test_turn_states(self):
        import json
        import tempfile
        lines = [
            {"type": "session_meta", "payload": {"id": "abc", "cwd": "/w", "timestamp": "2026-11-12T10:00:00Z"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "thread_settings_applied"}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))
            path = handle.name
        try:
            log = agents.CodexLog(path)
            log.update()
            self.assertEqual((log.id, log.cwd, log.state), ("abc", "/w", "working"))
            with open(path, "a") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "exec_approval_request"}}) + "\n")
            log.update()
            self.assertEqual(log.state, "needs")
            with open(path, "a") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
            log.update()
            self.assertEqual(log.state, "needs")
            with open(path, "a") as handle:
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed"}}) + "\n")
                handle.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            log.update()
            self.assertEqual(log.state, "idle")
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
