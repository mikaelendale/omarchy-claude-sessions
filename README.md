# Claude Sessions

An Omarchy bar widget that shows every coding-agent session on this machine: Claude Code, Codex, Cursor, OpenCode and the other agent CLIs. At a glance you can see whether a session is working, waiting on you, or done and waiting for your next message.

## What the icon shows

| Icon | Meaning |
|---|---|
| `✳` dimmed | No sessions running |
| `✳` | Sessions open, all idle |
| `✳` turning slowly | Something is working |
| `2 ✳` in the urgent colour, still | Two sessions need you (permission, a question, a plan to approve) |
| `1 ✳` | One session finished while you were elsewhere, and it's your turn |

The mark is drawn with Noto Sans Symbols 2, falling back to Adwaita Mono or DejaVu Sans. The bar's own font has no ✳, and Qt would otherwise draw it as the green colour emoji. When animations are turned off (`omarchy toggle animations`), the mark doesn't turn.

Hover over the icon for a summary such as "1 needs you · 2 working".

## Agents

| Agent | How it's read | Working | Needs you | Your turn |
|---|---|---|---|---|
| Claude Code | its own session files | exact | exact, with the reason | yes |
| Codex | its session log | exact | when it asks to run a command or apply a patch | yes |
| Cursor, OpenCode, Crush, Amp, Copilot, Kimi, Qwen, Gemini, Grok, Devin, Kilo, Antigravity, Goose, Aider, Hermes, Pi | the terminal they run in | from what they draw | if they ring the terminal bell and a bell hook is set up | yes |

Tested against the real CLIs: Codex 0.167, Cursor, OpenCode, Crush, Amp, Copilot, Kimi, Qwen, Grok, Devin and Kilo. Gemini, Antigravity, Goose, Aider, Hermes and Pi are recognised by name but untested.

Agents that don't report their state are found as processes in the foreground of a terminal. They count as working while text or a spinner reaches the screen, which is measured as what the window's terminal reads: more than 400 bytes a second. On this machine, working windows read about 1 KB a second (median) and idle ones nothing.

That is exact when the terminal process has one window, as Omarchy's foot windows do. Where it can't be measured (a terminal with several windows in one process, such as `foot --server` or Ghostty, or an agent inside tmux), CPU is used instead, and only counts when it stays up for three of four seconds. That fallback is a couple of seconds slow, and an agent waiting on a slow model with an idle screen can read as idle.

## Clicks

- **Left:** open the panel, with sessions grouped as Needs you, Your turn, Working and Idle. Pick a session (mouse, or arrows and Enter) to jump to its terminal window.
- **Right:** jump straight to the session that has waited on you longest.
- **Middle:** start a new session with `omarchy agent`.

In the panel, `n` also starts a new session.

The panel never scrolls. While there's room, each session gets two lines. When there are more sessions than fit, the rest drop to one line, except the ones that need you, which keep the line saying why. If even that won't fit, the least pressing sessions fold into one "N more · show all" row, which opens the Super+Shift+C conversations picker from [omarchy-notch](https://github.com/mikaelendale/omarchy-notch), if you have it.

## Notifications

When a session starts needing you and you aren't looking at its window, a notification appears. Clicking it jumps to the session, and it goes away by itself once you've answered. To change this, set `notify` in the widget's settings to `Unfocused` (the default), `Always` or `Off`.

## Keyboard shortcut

The widget answers on IPC, so you can bind a key to "take me to what needs me":

```sh
omarchy-shell mike.claude-sessions jump     # focus the session waiting longest
omarchy-shell mike.claude-sessions toggle   # open or close the panel
omarchy-shell mike.claude-sessions status   # print the summary
omarchy-shell mike.claude-sessions preview 14  # a minute of made-up sessions, to see the panel busy
```

## How it works

A session that has finished its reply but left something running, such as a dev server, counts as your turn (or idle), not as working. Its row says "background task running".

Claude Code keeps a small file for each running session in `~/.claude/sessions/<pid>.json`. That file holds a live `status`:

- `busy`
- `waiting`, plus the reason
- `shell`, meaning idle with a background task still running
- `idle`

`sessions.py` reads these files, and `agents.py` adds Codex and the other agents, about once a second. It skips sessions whose process has ended, and finds each session's window through Hyprland's socket. Whenever something changes, it prints one JSON line, which the widget reads.

It also reads any extra Claude accounts set up with `omarchy agent account`. It uses only the Python standard library and makes no network requests.

This file format belongs to Claude Code and isn't a documented API. If an update changes it, the widget shows unknown states as idle rather than breaking.

## Requirements

- [Omarchy](https://omarchy.org) with its Quickshell shell (the `omarchy plugin` commands)
- [Claude Code](https://claude.com/claude-code). Tested with 2.1.322.
- `python3`, standard library only
- Noto Sans Symbols 2 (in `noto-fonts`) for the ✳ mark

## Install

```sh
omarchy plugin add https://github.com/mikaelendale/omarchy-claude-sessions --enable
```

Or clone it into `~/.config/omarchy/plugins/claude-sessions` and run `omarchy plugin enable mike.claude-sessions --section right`.

## Tests

```sh
python3 -I -B tests/test_sessions.py
python3 -I -B tests/test_agents.py
python3 -I agents.py --once     # print what it sees right now
```

After editing `Panel.qml`, run `omarchy restart shell`. The shell's live reload can keep running the previous version of the widget.
