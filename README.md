# session-migrate

Move a paused Claude Code session onto Codex, then move the new work back into the same Claude session.

Claude Code and Codex do not share a transcript. A usage-limit stall leaves the Claude conversation on disk and the process sitting in Konsole. This writes a handoff the other agent reads, records the pair, and can open the other CLI in a new tab. The original transcript is not rewritten.

## Install

```sh
ln -s "$PWD/session_migrate.py" ~/.local/bin/session-migrate
```

Needs `uv`. `--launch` also needs `konsole`. The window matches the existing Claude Code windows: `konsole --new-tab --workdir <dir> -e env -C <dir> codex ...`.

## Usage

```sh
session-migrate list --running
session-migrate schema
session-migrate -o json list --limit 20 --offset 0 --fields sid,state,cwd,title,codex_id
session-migrate to-codex cb902122 --launch --stop
session-migrate to-claude cb902122 --launch --stop
```

`list` prints each Claude session id, whether a `claude` or `codex` process still has that id, the directory, the title, and the Codex id once one has read the handoff.

`to-codex` writes `~/.local/state/session-migrate/handoffs/<claude-id>-to-codex.md` and prints the `codex` command. `--launch` opens it in a Konsole tab in the session directory. If that Claude process is still running, `--launch` stops unless you also pass `--stop`, which sends SIGTERM to the live `claude` or `codex` process for that id.

`to-claude` finds the Codex rollout whose first user message contains that handoff path, writes `<claude-id>-to-claude.md` with the Codex turns after the migration prompt, and prints a `claude --remote-control --resume` command. `--launch --stop` closes the local Codex process and resumes the original Claude session with Remote Control on. Leave gateway env unset on that Claude process, or Remote Control stays off.

`yield` closes the local Codex window for a migrated session and the CLI app-server that still has its rollout open. The ChatGPT desktop process is left running, so the phone can open the same thread. A local Konsole Codex and the phone cannot hold one thread at the same time.

A session id may be a unique prefix of at least 8 characters. `--again` on `to-codex` drops the stored Codex id and writes a fresh handoff. The old Codex rollout is left on disk.

Permission mode is copied from the Claude session. `bypassPermissions` becomes `codex --yolo` on the way over and `claude --dangerously-skip-permissions` on the way back.

## What is copied

User and assistant prose, plus tool names. Tool results, thinking blocks, and sidechain transcripts are left out. Assistant and user prose is kept up to 10,000 turns or 1,500,000 characters, whichever comes first. Each turn is cut at 12,000 characters. Older turns past that are counted, not copied. The prompt passed on the command line is only the path to the handoff.

## Limits

- The other agent starts a new turn from the handoff. It does not inherit Claude's in-memory tool state, todos, or unsent queue.
- `to-claude` has nothing to import until Codex has written a reply after the migration prompt.
- Two agents in one working tree will edit the same files. `--launch` refuses while the session process is still alive unless `--stop` is set.
- A usage-limit message is not a field in the transcript, so `list` cannot tell a limit stall from a session you closed. `--running` shows the ones whose process is still up.

## Development

```sh
uv sync
uv run black --check . && uv run ruff check . && uv run mypy session_migrate.py tests && uv run pytest
```
