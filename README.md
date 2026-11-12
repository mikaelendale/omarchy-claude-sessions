# Claude Sessions

An Omarchy bar widget that shows every Claude Code session on this machine. At a glance you can see whether a session is working, watching a background task, or waiting on you.

## What the icon shows

| Icon | Meaning |
|---|---|
| `✳` dimmed | No sessions running |
| `✳` | Sessions open, all idle |
| `◐` `◑` alternating | Something is working |
| `✳ 2` in the urgent colour | Two sessions need you (permission, a question, a plan to approve) |
| `✳ 1` | One session finished while you were elsewhere, and it's your turn |

Hover over the icon for a summary such as "1 needs you · 2 working".

## Clicks

- **Left:** open the panel, with sessions grouped as Needs you, Your turn, Working, Watching and Idle. Pick a session (mouse, or arrows and Enter) to jump to its terminal window.
- **Right:** jump straight to the session that has waited on you longest.
- **Middle:** start a new session with `omarchy agent`.

In the panel, `n` also starts a new session.

## Notifications

When a session starts needing you and you aren't looking at its window, a notification appears. Clicking it jumps to the session, and it goes away by itself once you've answered. To change this, set `notify` in the widget's settings to `Unfocused` (the default), `Always` or `Off`.

## Keyboard shortcut

The widget answers on IPC, so you can bind a key to "take me to what needs me":

```sh
omarchy-shell mike.claude-sessions jump     # focus the session waiting longest
omarchy-shell mike.claude-sessions toggle   # open or close the panel
omarchy-shell mike.claude-sessions status   # print the summary
```

## How it works

Claude Code keeps a small file for each running session in `~/.claude/sessions/<pid>.json`. That file holds a live `status`:

- `busy`
- `waiting`, plus the reason
- `shell`, meaning idle with a background task still running
- `idle`

`sessions.py` reads these files about once a second. It skips sessions whose process has ended, and finds each session's window through Hyprland's socket. Whenever something changes, it prints one JSON line, which the widget reads.

It also reads any extra Claude accounts set up with `omarchy agent account`. It uses only the Python standard library and makes no network requests.

This file format belongs to Claude Code and isn't a documented API. If an update changes it, the widget shows unknown states as idle rather than breaking.

## Install

```sh
omarchy plugin add https://github.com/mikaelendale/omarchy-claude-sessions --enable
```

Or clone it into `~/.config/omarchy/plugins/claude-sessions` and run `omarchy plugin enable mike.claude-sessions --section right`.

## Tests

```sh
python3 -I -B tests/test_sessions.py
python3 -I sessions.py --once   # print what it sees right now
```

After editing `Panel.qml`, run `omarchy restart shell`. The shell's live reload can keep running the previous version of the widget.
