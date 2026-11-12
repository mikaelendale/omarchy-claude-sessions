import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions  # noqa: E402


class FakeHypr:
    def __init__(self, clients=None, active=""):
        self.windows = clients or {}
        self.active = active

    def clients(self):
        return dict(self.windows)

    def active_address(self):
        return self.active


class FakeNotifier:
    def __init__(self):
        self.shown = []
        self.withdrawn = []

    def show(self, key, summary, body, address):
        self.shown.append((key, summary, body, address))

    def withdraw(self, key):
        self.withdrawn.append(key)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "claude")
        self.proc_root = os.path.join(self.tmp.name, "proc")
        os.makedirs(os.path.join(self.home, "sessions"))
        os.makedirs(self.proc_root)
        self.hypr = FakeHypr()
        self.notifier = FakeNotifier()
        self.collector = sessions.Collector(
            [self.home],
            proc=sessions.ProcTable(self.proc_root),
            hypr=self.hypr,
            notifier=self.notifier,
        )
        self.clock = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def process(self, pid, ppid=1, start="1000", comm="claude"):
        os.makedirs(os.path.join(self.proc_root, str(pid)), exist_ok=True)
        fields = ["S", str(ppid)] + ["0"] * 17 + [start] + ["0"] * 5
        with open(os.path.join(self.proc_root, str(pid), "stat"), "w") as handle:
            handle.write("%d (%s) %s\n" % (pid, comm, " ".join(fields)))

    def session(self, pid, status="idle", waiting_for=None, start="1000", **extra):
        record = {
            "pid": pid,
            "sessionId": "session-%d" % pid,
            "cwd": "/home/me/Work/project-%d" % pid,
            "procStart": start,
            "kind": "interactive",
            "name": "work-%d" % pid,
            "status": status,
            "statusUpdatedAt": 1700000000000 + pid,
        }
        if waiting_for is not None:
            record["waitingFor"] = waiting_for
        record.update(extra)
        with open(os.path.join(self.home, "sessions", "%d.json" % pid), "w") as handle:
            json.dump(record, handle)

    def tick(self):
        self.clock += 5.0  # past CLIENTS_EVERY so windows refresh every tick
        return self.collector.snapshot(self.clock)

    def only(self):
        result = self.tick()
        self.assertEqual(len(result["sessions"]), 1)
        return result["sessions"][0]


class StatusMapping(Fixture):
    def test_each_status(self):
        cases = [
            ("busy", None, "working", ""),
            ("shell", None, "watching", ""),
            ("idle", None, "idle", ""),
            ("waiting", "permission prompt", "needs", "permission"),
            ("waiting", "sandbox request", "needs", "permission"),
            ("waiting", "worker request", "needs", "permission"),
            ("waiting", "goal proposal", "needs", "approval"),
            ("waiting", "input needed", "needs", "question"),
            ("waiting", "dialog open", "needs", "dialog"),
        ]
        self.process(10)
        for status, waiting_for, state, reason in cases:
            with self.subTest(status=status, waiting_for=waiting_for):
                self.collector.memory.clear()
                self.session(10, status, waiting_for)
                row = self.only()
                self.assertEqual(row["state"], state)
                self.assertEqual(row["reason"], reason)

    def test_unknown_waiting_reason_keeps_text(self):
        self.process(10)
        self.session(10, "waiting", "something new")
        row = self.only()
        self.assertEqual((row["state"], row["reason"], row["detail"]), ("needs", "other", "something new"))

    def test_unknown_status_shows_idle_with_word(self):
        self.process(10)
        self.session(10, "pondering")
        row = self.only()
        self.assertEqual((row["state"], row["detail"]), ("idle", "pondering"))

    def test_sorted_by_urgency(self):
        for pid, status in ((10, "idle"), (11, "busy"), (12, "waiting"), (13, "shell")):
            self.process(pid)
            self.session(pid, status, "permission prompt" if status == "waiting" else None)
        states = [row["state"] for row in self.tick()["sessions"]]
        self.assertEqual(states, ["needs", "working", "watching", "idle"])
        self.assertEqual(self.tick()["counts"], {"needs": 1, "done": 0, "working": 1, "watching": 1, "idle": 1})


class Liveness(Fixture):
    def test_dead_pid_is_dropped(self):
        self.session(10, "busy")
        self.assertEqual(self.tick()["sessions"], [])

    def test_reused_pid_is_dropped(self):
        self.process(10, start="2000")
        self.session(10, "busy", start="1000")
        self.assertEqual(self.tick()["sessions"], [])

    def test_oversized_file_is_skipped(self):
        self.process(10)
        self.session(10, "busy", padding="x" * (sessions.MAX_FILE_BYTES + 10))
        self.assertEqual(self.tick()["sessions"], [])

    def test_other_files_are_ignored(self):
        self.process(10)
        directory = os.path.join(self.home, "sessions")
        for name in ("abc.json", "010.json", "10.abc.key"):
            with open(os.path.join(directory, name), "w") as handle:
                handle.write('{"pid": 10, "status": "busy", "procStart": "1000"}')
        self.assertEqual(self.tick()["sessions"], [])

    def test_broken_json_is_skipped(self):
        self.process(10)
        with open(os.path.join(self.home, "sessions", "10.json"), "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.tick()["sessions"], [])

    def test_daemon_kinds_are_skipped(self):
        self.process(10)
        self.session(10, "busy", kind="daemon")
        self.assertEqual(self.tick()["sessions"], [])

    def test_pid_field_must_match_file(self):
        self.process(10)
        self.session(10, "busy")
        path = os.path.join(self.home, "sessions", "10.json")
        with open(path) as handle:
            record = json.load(handle)
        record["pid"] = 11
        with open(path, "w") as handle:
            json.dump(record, handle)
        self.assertEqual(self.tick()["sessions"], [])


class Windows(Fixture):
    def setUp(self):
        super().setUp()
        # claude (100) <- shell (90) <- foot (80), which owns the window.
        self.process(80, ppid=1, comm="foot")
        self.process(90, ppid=80, comm="bash")
        self.process(100, ppid=90)
        self.hypr.windows = {80: {"address": "0xabc", "title": "◑ Fix the login bug", "workspace": "2"}}

    def test_window_found_through_parents(self):
        self.session(100, "busy")
        row = self.only()
        self.assertEqual((row["window"], row["workspace"]), ("0xabc", "2"))
        self.assertEqual(row["label"], "Fix the login bug")

    def test_no_window_falls_back_to_name(self):
        self.hypr.windows = {}
        self.session(100, "busy")
        row = self.only()
        self.assertEqual((row["window"], row["label"]), ("", "work-100"))

    def test_markup_shaped_title_stays_plain_text(self):
        self.hypr.windows[80]["title"] = "✳ <img src=\"https://example.invalid/t\"> hi"
        self.session(100, "busy")
        self.assertEqual(self.only()["label"], "<img src=\"https://example.invalid/t\"> hi")

    def test_control_and_bidi_characters_removed(self):
        self.hypr.windows[80]["title"] = "safe‮ txt\u0007\nnext"
        self.session(100, "busy")
        self.assertEqual(self.only()["label"], "safe txt next")

    def test_long_title_is_clipped(self):
        self.hypr.windows[80]["title"] = "x" * 500
        self.session(100, "busy")
        self.assertEqual(len(self.only()["label"]), sessions.MAX_LABEL)


class DoneState(Fixture):
    def setUp(self):
        super().setUp()
        self.process(80, comm="foot")
        self.process(100, ppid=80)
        self.hypr.windows = {80: {"address": "0xabc", "title": "✳ Task", "workspace": "1"}}

    def test_finished_while_away_is_your_turn_until_focused(self):
        self.session(100, "busy")
        self.assertEqual(self.only()["state"], "working")
        self.session(100, "idle")
        self.assertEqual(self.only()["state"], "done")
        self.assertEqual(self.only()["state"], "done")
        self.hypr.active = "0xabc"
        self.assertEqual(self.only()["state"], "idle")
        self.hypr.active = ""
        self.assertEqual(self.only()["state"], "idle")

    def test_finished_while_watching_is_plain_idle(self):
        self.hypr.active = "0xabc"
        self.session(100, "busy")
        self.only()
        self.session(100, "idle")
        self.assertEqual(self.only()["state"], "idle")

    def test_new_work_clears_done(self):
        self.session(100, "busy")
        self.only()
        self.session(100, "idle")
        self.assertEqual(self.only()["state"], "done")
        self.session(100, "busy")
        self.assertEqual(self.only()["state"], "working")


class Notifications(Fixture):
    def setUp(self):
        super().setUp()
        self.process(80, comm="foot")
        self.process(100, ppid=80)
        self.hypr.windows = {80: {"address": "0xabc", "title": "✳ Task", "workspace": "1"}}

    def test_notifies_once_per_episode_and_withdraws(self):
        self.session(100, "busy")
        self.tick()
        self.session(100, "waiting", "permission prompt")
        self.tick()
        self.tick()
        self.assertEqual(len(self.notifier.shown), 1)
        key, summary, body, address = self.notifier.shown[0]
        self.assertEqual((summary, body, address), ("Task", "Needs permission · project-100", "0xabc"))
        self.session(100, "busy")
        self.tick()
        self.assertEqual(self.notifier.withdrawn, [key])

    def test_no_notification_when_looking_at_it(self):
        self.hypr.active = "0xabc"
        self.session(100, "busy")
        self.tick()
        self.session(100, "waiting", "input needed")
        self.tick()
        self.assertEqual(self.notifier.shown, [])

    def test_always_mode_notifies_when_looking(self):
        self.collector.notify_mode = "always"
        self.hypr.active = "0xabc"
        self.session(100, "busy")
        self.tick()
        self.session(100, "waiting", "input needed")
        self.tick()
        self.assertEqual(len(self.notifier.shown), 1)

    def test_already_waiting_at_start_does_not_notify(self):
        self.session(100, "waiting", "permission prompt")
        self.tick()
        self.assertEqual(self.notifier.shown, [])

    def test_session_ending_while_waiting_withdraws(self):
        self.session(100, "busy")
        self.tick()
        self.session(100, "waiting", "permission prompt")
        self.tick()
        os.remove(os.path.join(self.home, "sessions", "100.json"))
        self.tick()
        self.assertEqual(len(self.notifier.withdrawn), 1)


class Homes(unittest.TestCase):
    def test_registry_homes_are_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "state", "omarchy", "agents", "accounts")
            os.makedirs(registry)
            with open(os.path.join(registry, "claude.json"), "w") as handle:
                json.dump({"accounts": [{"id": "work", "home": "/srv/claude-work"}, {"home": "relative/path"}]}, handle)
            homes = sessions.config_homes(home_dir=tmp, state_dir=os.path.join(tmp, "state"), env={})
            self.assertEqual(homes, [os.path.join(tmp, ".claude"), "/srv/claude-work"])

    def test_missing_registry_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sessions.config_homes(home_dir=tmp, state_dir=tmp, env={}), [os.path.join(tmp, ".claude")])


if __name__ == "__main__":
    unittest.main()
