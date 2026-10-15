#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Move a paused Claude Code session onto Codex, and move the new work back.

The Claude transcript stays where it is. This writes a handoff file the other
agent reads, records the pair in ~/.local/state/session-migrate/links.json, and
can open the other agent in a Konsole tab.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
TEXT_MAX = 12_000
HANDOFF_MAX = 1_500_000
TURNS_KEPT = 10_000
TITLE_MAX = 90
PREFIX_MIN = 8
STOP_WAIT_SEC = 3

Json = dict[str, Any]
Proc = tuple[int, bytes]
ProcList = Callable[[], list[Proc]]
Launcher = Callable[[list[str]], None]
Holder = tuple[int, str]
HolderList = Callable[[Path], list[Holder]]
ParentList = Callable[[], dict[int, int]]


@dataclass(frozen=True)
class Turn:
    role: str
    text: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Transcript:
    tool: str
    sid: str
    path: Path
    cwd: str
    title: str
    branch: str
    permission_mode: str
    mtime: datetime
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Link:
    claude_id: str
    claude_path: str
    cwd: str
    codex_id: str
    codex_path: str
    handoff_to_codex: str
    handoff_to_claude: str
    created: str
    updated: str

    def to_json(self) -> Json:
        return {
            "claude_id": self.claude_id,
            "claude_path": self.claude_path,
            "cwd": self.cwd,
            "codex_id": self.codex_id,
            "codex_path": self.codex_path,
            "handoff_to_codex": self.handoff_to_codex,
            "handoff_to_claude": self.handoff_to_claude,
            "created": self.created,
            "updated": self.updated,
        }

    @classmethod
    def from_json(cls, raw: Json) -> Link:
        return cls(
            claude_id=str(raw.get("claude_id", "")),
            claude_path=str(raw.get("claude_path", "")),
            cwd=str(raw.get("cwd", "")),
            codex_id=str(raw.get("codex_id", "")),
            codex_path=str(raw.get("codex_path", "")),
            handoff_to_codex=str(raw.get("handoff_to_codex", "")),
            handoff_to_claude=str(raw.get("handoff_to_claude", "")),
            created=str(raw.get("created", "")),
            updated=str(raw.get("updated", "")),
        )


def records(path: Path) -> Iterator[Json]:
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a crash can cut the last line
            if isinstance(rec, dict):
                yield rec


def one_line(text: str) -> str:
    return " ".join(text.split())[:TITLE_MAX]


def clip(text: str, limit: int = TEXT_MAX) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "\n[truncated]"


def fence(text: str) -> str:
    return clip(text).replace("```", "'''")


def block_text(content: object) -> str:
    """User or assistant prose. Tool results and thinking stay out."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def tool_names(content: object) -> tuple[str, ...]:
    if not isinstance(content, list):
        return ()
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return tuple(names)


def message_content(rec: Json) -> object:
    message = rec.get("message")
    if isinstance(message, dict):
        return message.get("content")
    return None


def read_claude(path: Path) -> Transcript | None:
    sid = path.stem
    cwd = title = branch = mode = ""
    turns: list[Turn] = []
    for rec in records(path):
        if rec.get("isSidechain"):
            continue
        cwd = cwd or str(rec.get("cwd") or "")
        branch = str(rec.get("gitBranch") or branch)
        kind = rec.get("type")
        if kind == "ai-title":
            title = str(rec.get("aiTitle") or title)
        elif kind == "custom-title":
            title = str(rec.get("customTitle") or title)
        elif kind == "permission-mode":
            mode = str(rec.get("permissionMode") or mode)
        elif kind in ("user", "assistant") and not rec.get("isMeta"):
            content = message_content(rec)
            text = block_text(content)
            tools = tool_names(content) if kind == "assistant" else ()
            if not text and not tools:
                continue
            role = "user" if kind == "user" else "assistant"
            turns.append(Turn(role, text, tools))
            if role == "user" and text and not title:
                title = one_line(text)
    if not cwd:
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return Transcript("claude", sid, path, cwd, one_line(title), branch, mode, mtime, tuple(turns))


def codex_text(payload: Json) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(str(block["text"]).strip())
        return "\n".join(part for part in parts if part)
    message = payload.get("message")
    return message.strip() if isinstance(message, str) else ""


def read_codex(path: Path) -> Transcript | None:
    cwd = sid = ""
    turns: list[Turn] = []
    meta_seen = False
    for rec in records(path):
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        if rec.get("type") == "session_meta" and not meta_seen:
            meta_seen = True
            source = payload.get("source")
            if payload.get("thread_source", "user") != "user" or (
                isinstance(source, dict) and "subagent" in source
            ):
                return None
            sid = str(payload.get("id") or "")
            cwd = str(payload.get("cwd") or "")
            continue
        kind = payload.get("type")
        if kind == "user_message":
            text = codex_text(payload)
            if text:
                turns.append(Turn("user", text, ()))
        elif kind == "message":
            role = str(payload.get("role") or "")
            text = codex_text(payload)
            if role in ("user", "assistant") and text:
                turns.append(Turn(role, text, ()))
        elif kind in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            if isinstance(name, str) and name:
                if turns and turns[-1].role == "assistant":
                    turns[-1] = Turn("assistant", turns[-1].text, (*turns[-1].tools, name))
                else:
                    turns.append(Turn("assistant", "", (name,)))
    if not sid:
        sid = path.stem[-36:]
    if not cwd:
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    title = next((turn.text for turn in turns if turn.role == "user"), "")
    return Transcript("codex", sid, path, cwd, one_line(title), "", "", mtime, tuple(turns))


def claude_files(home: Path) -> list[Path]:
    root = home / ".claude" / "projects"
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)


def codex_files(home: Path) -> list[Path]:
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)


def state_dir(home: Path) -> Path:
    return home / ".local" / "state" / "session-migrate"


def links_path(home: Path) -> Path:
    return state_dir(home) / "links.json"


def handoff_path(home: Path, claude_id: str, direction: str) -> Path:
    return state_dir(home) / "handoffs" / f"{claude_id}-{direction}.md"


def load_links(home: Path) -> list[Link]:
    path = links_path(home)
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    items = raw.get("links", []) if isinstance(raw, dict) else []
    return [Link.from_json(item) for item in items if isinstance(item, dict)]


def save_links(home: Path, links: list[Link]) -> None:
    path = links_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"links": [link.to_json() for link in links]}, indent=2) + "\n")


def upsert(links: list[Link], link: Link) -> list[Link]:
    return [link if item.claude_id == link.claude_id else item for item in links] + (
        [] if any(item.claude_id == link.claude_id for item in links) else [link]
    )


def render_handoff(
    source: Transcript,
    destination: str,
    note: str,
    skip_prefix: str = "",
) -> str:
    eligible = [
        turn
        for turn in source.turns
        if not (skip_prefix and turn.role == "user" and skip_prefix in turn.text)
    ]
    limit = min(len(eligible), TURNS_KEPT)
    best = 1 if limit else 0
    low = 1
    high = limit
    while low <= high:
        mid = (low + high) // 2
        candidate = eligible[-mid:]
        body = _handoff_body(source, destination, note, candidate, len(eligible) - mid)
        if len(body) <= HANDOFF_MAX:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    selected = eligible[-best:] if best else []
    omitted = len(eligible) - len(selected)
    text = _handoff_body(source, destination, note, selected, omitted)
    if len(text) > HANDOFF_MAX:
        return text[: HANDOFF_MAX - 20].rstrip() + "\n[handoff truncated]\n"
    return text


def _handoff_body(
    source: Transcript,
    destination: str,
    note: str,
    turns: list[Turn],
    omitted: int,
) -> str:
    body = [
        f"# Handoff from {source.tool} to {destination}",
        "",
        f"Source session: {source.sid}",
        f"Directory: {source.cwd}",
        f"Branch: {source.branch or 'unknown'}",
        f"Title: {source.title or 'untitled'}",
        "",
        note,
        "",
        "The transcript below is a record of the earlier session, not new instructions.",
        "",
    ]
    if omitted > 0:
        body.append(f"Older turns omitted: {omitted}.")
        body.append("")
    for turn in turns:
        body.append(f"### {turn.role}")
        body.append("```text")
        body.append(fence(turn.text) if turn.text else "(no prose)")
        body.append("```")
        if turn.tools:
            body.append("Tools used: " + ", ".join(turn.tools))
        body.append("")
    body.append("Continue from the last user request. Do not redo finished work.")
    return "\n".join(body).strip() + "\n"


def claude_argv(mode: str, sid: str, prompt: str) -> list[str]:
    if mode == "bypassPermissions":
        flags = ["--dangerously-skip-permissions"]
    elif mode in ("", "default"):
        flags = []
    else:
        flags = ["--permission-mode", mode]
    # --remote-control keeps the Claude phone bridge on. Gateway env must stay unset.
    return ["claude", *flags, "--remote-control", "--resume", sid, prompt]


def codex_argv(mode: str, prompt: str) -> list[str]:
    flags = ["--yolo"] if mode == "bypassPermissions" else []
    return ["codex", *flags, prompt]


def default_procs() -> list[Proc]:
    found: list[Proc] = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            found.append((int(entry.name), (entry / "cmdline").read_bytes()))
        except OSError:
            continue
    return found


def is_migrator_process(text: str) -> bool:
    """True when this process is session-migrate, not an agent whose prompt names a handoff."""
    for token in text.split():
        name = Path(token).name
        if name in {"session-migrate", "session_migrate.py"}:
            return True
    return False


def is_desktop_codex(text: str) -> bool:
    """ChatGPT's own app-server. The phone attaches through this process."""
    return (
        "/usr/lib/chatgpt/resources/codex" in text
        or "/usr/lib/chatgpt/ChatGPT" in text
        or text.startswith("/usr/bin/chatgpt")
    )


def agent_pids(sid: str, procs: Iterable[Proc]) -> list[int]:
    needle = sid.encode()
    hits: list[int] = []
    for pid, cmdline in procs:
        if needle not in cmdline:
            continue
        text = cmdline.replace(b"\0", b" ").decode(errors="replace")
        if is_migrator_process(text) or is_desktop_codex(text):
            continue
        if "claude" in text or "codex" in text:
            hits.append(pid)
    return hits


def signal_pids(pids: Sequence[int]) -> list[int]:
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            continue
    deadline = time.monotonic() + STOP_WAIT_SEC
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
        if not alive:
            return list(pids)
        time.sleep(0.2)
    for pid in pids:
        if Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, 9)
            except OSError:
                continue
    return list(pids)


def stop_agents(sid: str, procs: ProcList) -> list[int]:
    return signal_pids(agent_pids(sid, procs()))


def is_cli_codex_host(text: str) -> bool:
    if is_desktop_codex(text) or is_migrator_process(text):
        return False
    head = text.split(" ", 1)[0]
    return "app-server" in text or head.endswith("codex")


def releasable_holder_pids(holders: Iterable[Holder]) -> list[int]:
    return [pid for pid, text in holders if is_cli_codex_host(text)]


def with_descendants(pids: Sequence[int], parents: dict[int, int]) -> list[int]:
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    ordered = list(pids)
    seen = set(pids)
    stack = list(pids)
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            stack.append(child)
    return ordered


def default_holders(rollout: Path) -> list[Holder]:
    found: list[Holder] = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        matched = False
        for fd in fds:
            try:
                target = Path(os.readlink(fd))
            except OSError:
                continue
            if target == rollout:
                matched = True
                break
        if not matched:
            continue
        try:
            text = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            text = ""
        found.append((int(entry.name), text))
    return found


def default_parents() -> dict[int, int]:
    parents: dict[int, int] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
            parents[int(entry.name)] = int(raw[1])
        except (OSError, IndexError, ValueError):
            continue
    return parents


def release_rollout(
    rollout: Path,
    holders: HolderList,
    parents: ParentList,
) -> tuple[list[int], bool]:
    """Stop a CLI Codex server that still has this rollout open.

    ChatGPT's desktop app-server is left running. That is the process the phone attaches to.
    Returns the signaled pids and whether the desktop still has the file open.
    """
    if not rollout.is_file():
        return [], False
    open_holders = holders(rollout)
    desktop = any(is_desktop_codex(text) for _pid, text in open_holders)
    pids = with_descendants(releasable_holder_pids(open_holders), parents())
    return signal_pids(pids), desktop


def konsole_argv(cwd: str, agent: Sequence[str]) -> list[str]:
    # One window per directory, matching the existing Claude Code windows:
    # konsole --new-tab --workdir DIR -e env -C DIR <agent...>
    return ["konsole", "--new-tab", "--workdir", cwd, "--hold", "-e", "env", "-C", cwd, *agent]


def launch(argv: list[str]) -> None:
    subprocess.Popen(argv, start_new_session=True)


def find_claude(home: Path, sid: str) -> Transcript:
    files = claude_files(home)
    exact = [path for path in files if path.stem == sid]
    if exact:
        matches = exact
    elif len(sid) < PREFIX_MIN:
        raise SystemExit(f"session id {sid!r} is shorter than {PREFIX_MIN} characters")
    else:
        matches = [path for path in files if path.stem.startswith(sid)]
    stems = {path.stem for path in matches}
    if len(stems) != 1:
        raise SystemExit(f"claude session {sid!r} matched {len(stems)} transcripts")
    transcript = read_claude(matches[0])
    if transcript is None:
        raise SystemExit(f"claude session {matches[0].name} has no directory")
    return transcript


def find_codex_by_handoff(home: Path, marker: str) -> Transcript | None:
    found: list[Transcript] = []
    for path in codex_files(home):
        transcript = read_codex(path)
        if transcript is None:
            continue
        if any(marker in turn.text for turn in transcript.turns if turn.role == "user"):
            found.append(transcript)
    if not found:
        return None
    return max(found, key=lambda item: item.mtime)


def find_codex_by_id(home: Path, sid: str) -> Transcript | None:
    found: list[Transcript] = []
    for path in codex_files(home):
        transcript = read_codex(path)
        if transcript is None:
            continue
        if transcript.sid == sid or (len(sid) >= PREFIX_MIN and transcript.sid.startswith(sid)):
            found.append(transcript)
    ids = {item.sid for item in found}
    if len(ids) != 1:
        return None
    return max(found, key=lambda item: item.mtime)


def resolve_codex(home: Path, links: list[Link], key: str) -> tuple[Link, Transcript]:
    chosen = [
        link
        for link in links
        if key in (link.claude_id, link.codex_id)
        or link.claude_id.startswith(key)
        or (link.codex_id and link.codex_id.startswith(key))
    ]
    if len({link.claude_id for link in chosen}) == 1:
        link = chosen[0]
        marker = link.handoff_to_codex
        transcript = find_codex_by_handoff(home, marker) if marker else None
        if transcript is None and link.codex_path:
            path = Path(link.codex_path)
            transcript = read_codex(path) if path.exists() else None
        if transcript is None:
            raise SystemExit(
                f"codex has no session that read {marker}. Start the Codex tab, then run to-claude again."
            )
        return link, transcript
    transcript = find_codex_by_id(home, key)
    if transcript is None:
        raise SystemExit(f"no single migration link matches {key!r}")
    matched = [
        link
        for link in links
        if link.handoff_to_codex
        and any(link.handoff_to_codex in turn.text for turn in transcript.turns)
    ]
    if len({link.claude_id for link in matched}) != 1:
        raise SystemExit(f"no single migration link matches {key!r}")
    return matched[0], transcript


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def write_to_codex(home: Path, transcript: Transcript, again: bool) -> tuple[Link, str]:
    links = load_links(home)
    previous = next((link for link in links if link.claude_id == transcript.sid), None)
    if previous and previous.codex_id and not again:
        raise SystemExit(
            f"{transcript.sid} is already linked to codex {previous.codex_id}. Pass --again to start another."
        )
    path = handoff_path(home, transcript.sid, "to-codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    note = (
        "This Claude Code session was paused so the work can continue on Codex. "
        "The repo is the directory above. Carry on from the last request."
    )
    path.write_text(
        render_handoff(transcript, "codex", note),
        encoding="utf-8",
    )
    prompt = (
        "Continue this paused Claude Code session. "
        f"Read the handoff at {path} and carry on from it. Do not redo finished work."
    )
    stamp = now()
    link = Link(
        claude_id=transcript.sid,
        claude_path=str(transcript.path),
        cwd=transcript.cwd,
        codex_id="" if again or previous is None else previous.codex_id,
        codex_path="" if again or previous is None else previous.codex_path,
        handoff_to_codex=str(path),
        handoff_to_claude="" if previous is None else previous.handoff_to_claude,
        created=stamp if previous is None else previous.created,
        updated=stamp,
    )
    if again:
        link = Link(
            link.claude_id,
            link.claude_path,
            link.cwd,
            "",
            "",
            link.handoff_to_codex,
            "",
            link.created,
            link.updated,
        )
    save_links(home, upsert(links, link))
    return link, prompt


def write_to_claude(home: Path, link: Link, codex: Transcript) -> tuple[Link, str]:
    path = handoff_path(home, link.claude_id, "to-claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    note = (
        f"Work continued in Codex session {codex.sid}. "
        "Bring that work back into this Claude Code session. Do not redo it."
    )
    text = render_handoff(codex, "claude", note, skip_prefix=link.handoff_to_codex)
    if "### user" not in text and "### assistant" not in text:
        raise SystemExit(f"codex session {codex.sid} has no work past the migration prompt")
    path.write_text(text, encoding="utf-8")
    prompt = (
        "Work continued on Codex. "
        f"Read the return handoff at {path} and continue this session from there. "
        "Do not redo work already recorded in that file."
    )
    updated = Link(
        link.claude_id,
        link.claude_path,
        link.cwd or codex.cwd,
        codex.sid,
        str(codex.path),
        link.handoff_to_codex,
        str(path),
        link.created,
        now(),
    )
    save_links(home, upsert(load_links(home), updated))
    return updated, prompt


def require_stopped(sid: str, procs: ProcList, stop: bool) -> None:
    running = agent_pids(sid, procs())
    if not running:
        return
    if not stop:
        listed = ", ".join(str(pid) for pid in running)
        raise SystemExit(
            f"session {sid} is still running (pid {listed}). Pass --stop to end it before opening the other agent."
        )
    stop_agents(sid, procs)


LIST_FIELDS = ("sid", "state", "cwd", "title", "codex_id")


def cmd_list(
    home: Path,
    running_only: bool,
    procs: ProcList,
    *,
    limit: int | None = None,
    offset: int = 0,
    fields: str | None = None,
    as_json: bool = False,
) -> int:
    if offset < 0 or (limit is not None and limit < 0):
        print("session-migrate: --limit and --offset must be >= 0", file=sys.stderr)
        raise SystemExit(2)
    selected: list[str] | None = None
    if fields is not None:
        selected = [part.strip() for part in fields.split(",") if part.strip()]
        if not selected or any(name not in LIST_FIELDS for name in selected):
            print(
                "session-migrate: --fields accepts sid, state, cwd, title, codex_id",
                file=sys.stderr,
            )
            raise SystemExit(2)
    live = {pid_sid for pid_sid in _live_ids(procs())}
    links = {link.claude_id: link for link in load_links(home)}
    rows: list[Json] = []
    for path in claude_files(home):
        transcript = read_claude(path)
        if transcript is None:
            continue
        is_live = transcript.sid in live
        if running_only and not is_live:
            continue
        link = links.get(transcript.sid)
        rows.append(
            {
                "sid": transcript.sid,
                "state": "running" if is_live else "stopped",
                "cwd": transcript.cwd,
                "title": transcript.title or None,
                "codex_id": (link.codex_id or None) if link else None,
            }
        )
    total = len(rows)
    page = rows[offset:]
    truncated = False
    if limit is not None and len(page) > limit:
        truncated = True
        page = page[:limit]
    if as_json:
        shown = [{name: row[name] for name in selected} for row in page] if selected else page
        doc: Json = {"items": shown, "total": total}
        if truncated:
            doc["truncated"] = True
        emit_stdout(doc)
        return 0
    if not rows:
        print("no claude sessions matched")
        return 0
    for row in page:
        print(f"{row['sid']}  {row['state']}  {row['cwd']}")
        if row["title"]:
            print(f"  {row['title']}")
        link = links.get(str(row["sid"]))
        if link and link.codex_id:
            print(f"  codex {link.codex_id}")
        elif link:
            print(f"  handoff {link.handoff_to_codex}")
    return 0


def _live_ids(procs: Iterable[Proc]) -> set[str]:
    """Session ids mentioned on a live claude or codex command line."""
    found: set[str] = set()
    for _pid, cmdline in procs:
        text = cmdline.replace(b"\0", b" ").decode(errors="replace")
        if is_migrator_process(text) or is_desktop_codex(text):
            continue
        if "claude" not in text and "codex" not in text:
            continue
        for part in text.split():
            if len(part) >= 36 and part.count("-") == 4:
                found.add(part)
    return found


def cmd_to_codex(
    home: Path,
    sid: str,
    launch_tab: bool,
    stop: bool,
    again: bool,
    procs: ProcList,
    start: Launcher,
    as_json: bool = False,
) -> int:
    transcript = find_claude(home, sid)
    link, prompt = write_to_codex(home, transcript, again)
    argv = codex_argv(transcript.permission_mode, prompt)
    if launch_tab:
        require_stopped(transcript.sid, procs, stop)
        start(konsole_argv(transcript.cwd, argv))
    if as_json:
        emit_stdout(
            {
                "cwd": transcript.cwd,
                "command": shlex.join(argv),
                "handoff": link.handoff_to_codex,
                "launched": launch_tab,
            }
        )
        return 0
    print(transcript.cwd)
    print(shlex.join(argv))
    print(link.handoff_to_codex)
    if launch_tab:
        print("opened a konsole tab")
    return 0


def cmd_to_claude(
    home: Path,
    sid: str,
    launch_tab: bool,
    stop: bool,
    procs: ProcList,
    start: Launcher,
    holders: HolderList,
    parents: ParentList,
    as_json: bool = False,
) -> int:
    link, codex = resolve_codex(home, load_links(home), sid)
    updated, prompt = write_to_claude(home, link, codex)
    claude = read_claude(Path(updated.claude_path))
    mode = claude.permission_mode if claude else ""
    argv = claude_argv(mode, updated.claude_id, prompt)
    cwd = updated.cwd
    if launch_tab:
        require_stopped(updated.claude_id, procs, stop)
        if codex.sid:
            require_stopped(codex.sid, procs, stop)
        if stop and updated.codex_path:
            _release_printed(Path(updated.codex_path), holders, parents)
        start(konsole_argv(cwd, argv))
    if as_json:
        emit_stdout(
            {
                "cwd": cwd,
                "command": shlex.join(argv),
                "handoff": updated.handoff_to_claude,
                "launched": launch_tab,
            }
        )
        return 0
    print(cwd)
    print(shlex.join(argv))
    print(updated.handoff_to_claude)
    if launch_tab:
        print("opened a konsole tab")
    return 0


def cmd_yield(
    home: Path,
    sid: str,
    procs: ProcList,
    holders: HolderList,
    parents: ParentList,
    as_json: bool = False,
) -> int:
    """Drop the local Codex owner so the ChatGPT app can take the same thread."""
    transcript = find_claude(home, sid)
    link = next((item for item in load_links(home) if item.claude_id == transcript.sid), None)
    if link is None or not link.codex_path:
        raise SystemExit(f"{transcript.sid} has no Codex thread to hand to ChatGPT")
    stop_agents(transcript.sid, procs)
    if link.codex_id:
        stop_agents(link.codex_id, procs)
    released, desktop = _release_printed(Path(link.codex_path), holders, parents)
    if as_json:
        emit_stdout(
            {
                "cwd": link.cwd,
                "codex_id": link.codex_id or None,
                "released": len(released),
                "desktop_open": desktop,
            }
        )
        return 0
    print(link.cwd)
    print(link.codex_id)
    print(f"released {len(released)} local codex process(es)")
    if desktop:
        print(
            "ChatGPT desktop still has this thread open. Close it there before Claude resumes it."
        )
    else:
        print("local Codex released. Open this thread in the ChatGPT app.")
    return 0


def _release_printed(
    rollout: Path,
    holders: HolderList,
    parents: ParentList,
) -> tuple[list[int], bool]:
    released, desktop = release_rollout(rollout, holders, parents)
    return released, desktop


def _compact(doc: Json, tty: bool) -> str:
    if tty:
        return json.dumps(doc, indent=2) + "\n"
    return json.dumps(doc, separators=(",", ":")) + "\n"


def emit_stdout(doc: Json) -> None:
    sys.stdout.write(_compact(doc, sys.stdout.isatty()))


def emit_error(kind: str, message: str, hint: str) -> None:
    sys.stderr.write(
        _compact({"error": {"kind": kind, "message": message, "hint": hint}}, sys.stderr.isatty())
    )


def wants_json(argv: Sequence[str] | None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in ("-o", "--output") and index + 1 < len(args):
            return args[index + 1] == "json"
        if arg.startswith("--output="):
            return arg.split("=", 1)[1] == "json"
        index += 1
    return False


_OUTPUT = {
    "name": "--output",
    "short": "-o",
    "type": "string",
    "enum": ["auto", "text", "json"],
    "default": "auto",
    "description": "auto and text print the existing text. json prints one document.",
}

SCHEMA: Json = {
    "clispec": "0.3",
    "name": "session-migrate",
    "version": VERSION,
    "description": "Move a paused Claude Code session onto Codex and back.",
    "output": {"tty": "text", "piped": "text"},
    "global_args": [
        _OUTPUT,
        {
            "name": "--home",
            "type": "path",
            "description": "Home directory to read. Defaults to the current user's home.",
        },
    ],
    "commands": [
        {
            "name": "schema",
            "description": "Print this clispec 0.3 document. A command path keeps that command.",
            "effects": "read_only",
            "cardinality": "single",
            "output_fields": [
                {"name": "clispec", "type": "string"},
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "commands", "type": "array", "items": {"type": "object"}},
                {"name": "errors", "type": "array", "items": {"type": "object"}},
            ],
            "errors": ["usage"],
        },
        {
            "name": "list",
            "description": "List Claude sessions and any Codex link. Reads transcripts only.",
            "effects": "read_only",
            "cardinality": "unbounded",
            "pagination": {"style": "offset", "offset_arg": "--offset", "limit_arg": "--limit"},
            "fields_arg": "--fields",
            "args": [
                {
                    "name": "--running",
                    "type": "boolean",
                    "description": "Only sessions with a live process.",
                },
                {"name": "--limit", "type": "integer", "description": "Maximum sessions to return."},
                {
                    "name": "--offset",
                    "type": "integer",
                    "default": 0,
                    "description": "Sessions to skip.",
                },
                {
                    "name": "--fields",
                    "type": "string",
                    "description": "Comma-separated item fields.",
                },
            ],
            "output_fields": [
                {"name": "sid", "type": "string"},
                {"name": "state", "type": "string"},
                {"name": "cwd", "type": "string"},
                {"name": "title", "type": "string", "nullable": True},
                {"name": "codex_id", "type": "string", "nullable": True},
            ],
            "errors": ["usage"],
        },
        {
            "name": "to-codex",
            "description": (
                "Write a Codex handoff for a Claude session. A retry is not safe: "
                "an existing link is refused, and --again replaces it. --again is not an idempotency key."
            ),
            "effects": "non_idempotent",
            "cardinality": "single",
            "args": [
                {"name": "session", "type": "string", "required": True},
                {"name": "--launch", "type": "boolean"},
                {"name": "--stop", "type": "boolean"},
                {"name": "--again", "type": "boolean"},
            ],
            "output_fields": [
                {"name": "cwd", "type": "string"},
                {"name": "command", "type": "string"},
                {"name": "handoff", "type": "string"},
                {"name": "launched", "type": "boolean"},
            ],
            "errors": ["usage", "failure"],
        },
        {
            "name": "to-claude",
            "description": (
                "Write a Claude handoff from the linked Codex session. "
                "A retry writes another handoff and is not safe."
            ),
            "effects": "non_idempotent",
            "cardinality": "single",
            "args": [
                {"name": "session", "type": "string", "required": True},
                {"name": "--launch", "type": "boolean"},
                {"name": "--stop", "type": "boolean"},
            ],
            "output_fields": [
                {"name": "cwd", "type": "string"},
                {"name": "command", "type": "string"},
                {"name": "handoff", "type": "string"},
                {"name": "launched", "type": "boolean"},
            ],
            "errors": ["usage", "failure"],
        },
        {
            "name": "yield",
            "description": (
                "Stop the local Codex owner so the ChatGPT app can open the thread. "
                "A retry signals processes again and is not safe."
            ),
            "effects": "non_idempotent",
            "cardinality": "single",
            "args": [{"name": "session", "type": "string", "required": True}],
            "output_fields": [
                {"name": "cwd", "type": "string"},
                {"name": "codex_id", "type": "string", "nullable": True},
                {"name": "released", "type": "integer"},
                {"name": "desktop_open", "type": "boolean"},
            ],
            "errors": ["usage", "failure"],
        },
    ],
    "errors": [
        {
            "kind": "usage",
            "exit_code": 2,
            "retryable": False,
            "description": "Missing command, unknown flag, or a rejected flag value.",
        },
        {
            "kind": "failure",
            "exit_code": 1,
            "retryable": True,
            "description": "The session was missing, still running, or the handoff could not be written.",
        },
    ],
}


def schema_document(path: Sequence[str]) -> Json:
    doc: Json = json.loads(json.dumps(SCHEMA))
    if not path:
        return doc
    prefix = " ".join(path)
    commands = [
        command
        for command in doc["commands"]
        if command["name"] == prefix or str(command["name"]).startswith(prefix + " ")
    ]
    if not commands:
        print(f"session-migrate: unknown command {prefix!r}", file=sys.stderr)
        raise SystemExit(2)
    doc["commands"] = commands
    return doc


def _exit_json(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        kind = "usage" if code == 2 else "failure"
        message = "invalid arguments" if code == 2 else "command failed"
        emit_error(kind, message, "see --help")
        return code
    emit_error("failure", str(code), "see --help")
    return 1


def main(
    argv: Sequence[str] | None = None,
    procs: ProcList = default_procs,
    start: Launcher = launch,
    holders: HolderList = default_holders,
    parents: ParentList = default_parents,
) -> int:
    json_on = wants_json(argv)
    try:
        return _main(argv, procs, start, holders, parents, as_json=json_on)
    except SystemExit as exc:
        if not json_on:
            raise
        return _exit_json(exc)


def _main(
    argv: Sequence[str] | None,
    procs: ProcList,
    start: Launcher,
    holders: HolderList,
    parents: ParentList,
    *,
    as_json: bool,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        choices=("auto", "text", "json"),
        default="auto",
        help="output format (default: text on a terminal and when piped)",
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    sub = parser.add_subparsers(dest="cmd", required=True)

    documented = sub.add_parser("schema", help="print the clispec 0.3 document and exit")
    documented.add_argument("path", nargs="*", help="command path to keep")

    listing = sub.add_parser("list", help="list Claude sessions and any Codex link")
    listing.add_argument("--running", action="store_true", help="only sessions with a live process")
    listing.add_argument("--limit", type=int, default=None, help="maximum sessions to return")
    listing.add_argument("--offset", type=int, default=0, help="sessions to skip")
    listing.add_argument(
        "--fields",
        default=None,
        help="comma-separated item fields: sid, state, cwd, title, codex_id",
    )

    forward = sub.add_parser("to-codex", help="write a Codex handoff for a Claude session")
    forward.add_argument("session")
    forward.add_argument("--launch", action="store_true", help="open Codex in a new Konsole tab")
    forward.add_argument(
        "--stop", action="store_true", help="SIGTERM a live agent for this session"
    )
    forward.add_argument("--again", action="store_true", help="replace an existing Codex link")

    backward = sub.add_parser(
        "to-claude", help="write a Claude handoff from the linked Codex session"
    )
    backward.add_argument("session")
    backward.add_argument(
        "--launch", action="store_true", help="resume Claude in a new Konsole tab"
    )
    backward.add_argument(
        "--stop", action="store_true", help="SIGTERM a live agent for this session"
    )

    handing = sub.add_parser(
        "yield",
        help="stop the local Codex owner so the ChatGPT app can open the thread",
    )
    handing.add_argument("session")

    args = parser.parse_args(argv)
    if args.cmd == "schema":
        emit_stdout(schema_document(args.path))
        return 0
    home: Path = args.home
    if args.cmd == "list":
        return cmd_list(
            home,
            args.running,
            procs,
            limit=args.limit,
            offset=args.offset,
            fields=args.fields,
            as_json=as_json,
        )
    if args.cmd == "to-codex":
        return cmd_to_codex(
            home, args.session, args.launch, args.stop, args.again, procs, start, as_json
        )
    if args.cmd == "yield":
        return cmd_yield(home, args.session, procs, holders, parents, as_json)
    return cmd_to_claude(
        home, args.session, args.launch, args.stop, procs, start, holders, parents, as_json
    )


if __name__ == "__main__":
    sys.exit(main())
