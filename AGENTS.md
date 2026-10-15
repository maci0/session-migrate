# session-migrate

Moves a Claude Code transcript onto Codex, then moves the new Codex work back onto the same Claude session.

The parent rules are `/home/maci/Desktop/Projects/maci0/AGENTS.md`.

## Contract

- A migration writes a handoff file under `~/.local/state/session-migrate/handoffs/` and a link in `links.json`. The agent prompt contains the file path, not the transcript.
- `to-codex` does not delete or rewrite the Claude transcript. `to-claude` does not rewrite the Codex rollout.
- `--launch` opens one Konsole tab. It refuses while that session's agent is still running unless `--stop` is also set. `--stop` sends SIGTERM to processes whose command line is `claude` or `codex` with that session id. The migrator process itself is ignored. A handoff path that contains `session-migrate` does not protect the agent.
- `yield` stops the local Codex owner and a CLI app-server that still has that rollout open, including its children. It does not signal `/usr/lib/chatgpt/resources/codex`, because the phone attaches through that process.
- The Claude launch command includes `--remote-control`. Do not set `ANTHROPIC_BASE_URL` on that process.
- Tool results and thinking blocks stay out of the handoff. Turns are copied as text, newest first, up to the size cap.
- Tests use `--home` and never read the real `~/.claude` or `~/.codex`.
- `schema` prints the clispec 0.3 document and does not read a home directory. `-o json` selects JSON; the default stays text on a terminal and when piped. `list` JSON is `{"items":[...],"total":N}` with `--limit`, `--offset`, and `--fields`.
