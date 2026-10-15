from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import session_migrate as sm

CLAUDE_ID = "cb902122-94e5-49ca-81a4-02d5378ef05b"
CODEX_ID = "01a10148-5c4c-7383-a1d5-7d3ffc2dad1b"


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def claude_home(home: Path, rows: list[dict[str, object]] | None = None) -> Path:
    path = home / ".claude/projects/-work" / f"{CLAUDE_ID}.jsonl"
    write_jsonl(
        path,
        rows
        or [
            {
                "type": "user",
                "cwd": "/work/demo",
                "gitBranch": "main",
                "isSidechain": True,
                "message": {"role": "user", "content": "sidechain secret"},
            },
            {
                "type": "user",
                "cwd": "/work/demo",
                "gitBranch": "main",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "SECRET_TOOL_OUTPUT"}],
                },
            },
            {
                "type": "user",
                "cwd": "/work/demo",
                "gitBranch": "main",
                "message": {
                    "role": "user",
                    "content": "finish the parser and leave the tests green",
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hidden chain"},
                        {"type": "text", "text": "I will edit parser.py next."},
                        {"type": "tool_use", "name": "Edit", "input": {}},
                    ],
                },
            },
            {"type": "ai-title", "aiTitle": "Finish the parser"},
            {"type": "permission-mode", "permissionMode": "bypassPermissions"},
        ],
    )
    return path


def test_handoff_keeps_prose_and_drops_tool_bodies(tmp_path: Path) -> None:
    path = claude_home(tmp_path)
    transcript = sm.read_claude(path)
    assert transcript is not None
    text = sm.render_handoff(transcript, "codex", "paused")
    assert "finish the parser" in text
    assert "I will edit parser.py next." in text
    assert "Tools used: Edit" in text
    assert "SECRET_TOOL_OUTPUT" not in text
    assert "sidechain secret" not in text
    assert "hidden chain" not in text
    assert "Finish the parser" in text


def test_size_cap_keeps_the_latest_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "HANDOFF_MAX", 3_500)
    monkeypatch.setattr(sm, "TURNS_KEPT", 8)
    path = claude_home(tmp_path, [])
    rows: list[dict[str, object]] = []
    for index in range(6):
        rows.append(
            {
                "type": "user",
                "cwd": "/work/demo",
                "gitBranch": "main",
                "message": {"role": "user", "content": f"old request {index} " + ("x" * 800)},
            }
        )
    rows.append(
        {
            "type": "user",
            "cwd": "/work/demo",
            "gitBranch": "main",
            "message": {"role": "user", "content": "LATEST_REQUEST keep this"},
        }
    )
    rows.append({"type": "ai-title", "aiTitle": "Size cap"})
    write_jsonl(path, rows)
    transcript = sm.read_claude(path)
    assert transcript is not None
    text = sm.render_handoff(transcript, "codex", "paused")
    assert "LATEST_REQUEST" in text
    assert "old request 0" not in text
    assert "Older turns omitted:" in text


def test_round_trip_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claude_home(tmp_path)
    assert sm.main(["--home", str(tmp_path), "to-codex", CLAUDE_ID[:8]]) == 0
    out = capsys.readouterr().out
    assert "codex --yolo" in out
    handoff = next(tmp_path.joinpath(".local/state/session-migrate/handoffs").glob("*-to-codex.md"))
    assert handoff.is_file()
    codex_path = (
        tmp_path / ".codex/sessions/2026/10/08" / f"rollout-2026-10-08T00-00-00-{CODEX_ID}.jsonl"
    )
    write_jsonl(
        codex_path,
        [
            {
                "type": "session_meta",
                "payload": {"id": CODEX_ID, "cwd": "/work/demo", "thread_source": "user"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": f"Read the handoff at {handoff}"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Parser tests are green."}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "shell", "arguments": "{}"},
            },
        ],
    )
    assert sm.main(["--home", str(tmp_path), "to-claude", CODEX_ID[:8]]) == 0
    back = capsys.readouterr().out
    assert "claude --dangerously-skip-permissions --remote-control --resume" in back
    assert CLAUDE_ID in back
    returned = (
        tmp_path / ".local/state/session-migrate/handoffs" / f"{CLAUDE_ID}-to-claude.md"
    ).read_text()
    assert "Parser tests are green." in returned
    assert "Tools used: shell" in returned
    assert str(handoff) not in returned
    with pytest.raises(SystemExit, match="already linked"):
        sm.main(["--home", str(tmp_path), "to-codex", CLAUDE_ID])


def test_launch_refuses_a_live_session(tmp_path: Path) -> None:
    claude_home(tmp_path)
    procs = lambda: [(99, f"claude --resume {CLAUDE_ID}\0".encode())]

    def fail_start(_argv: list[str]) -> None:
        raise AssertionError("should not launch")

    with pytest.raises(SystemExit, match="still running"):
        sm.main(
            ["--home", str(tmp_path), "to-codex", CLAUDE_ID, "--launch"],
            procs=procs,
            start=fail_start,
        )


def test_stop_then_launch(tmp_path: Path) -> None:
    claude_home(tmp_path)
    started: list[list[str]] = []
    procs = lambda: [(4242, b"claude\0--resume\0" + CLAUDE_ID.encode())]
    sm.main(
        ["--home", str(tmp_path), "to-codex", CLAUDE_ID, "--launch", "--stop"],
        procs=procs,
        start=started.append,
    )
    assert started and started[0][0] == "konsole"
    assert "codex" in started[0][-1]


def test_list_running_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claude_home(tmp_path)
    procs = lambda: [(7, f"claude --resume {CLAUDE_ID}".encode())]
    assert sm.main(["--home", str(tmp_path), "list", "--running"], procs=procs) == 0
    listed = capsys.readouterr().out
    assert CLAUDE_ID in listed
    assert "running" in listed
    assert "/work/demo" in listed


def test_subagent_codex_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "sub.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": CODEX_ID,
                    "cwd": "/work/demo",
                    "thread_source": "user",
                    "source": {"subagent": {"thread_spawn": {}}},
                },
            }
        ],
    )
    assert sm.read_codex(path) is None


def test_yield_releases_cli_owner_and_keeps_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_home(tmp_path)
    assert sm.main(["--home", str(tmp_path), "to-codex", CLAUDE_ID]) == 0
    capsys.readouterr()
    links = json.loads(
        (tmp_path / ".local/state/session-migrate/links.json").read_text(encoding="utf-8")
    )
    rollout = tmp_path / ".codex/sessions/thread.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("{}\n", encoding="utf-8")
    links["links"][0]["codex_id"] = CODEX_ID
    links["links"][0]["codex_path"] = str(rollout)
    (tmp_path / ".local/state/session-migrate/links.json").write_text(
        json.dumps(links), encoding="utf-8"
    )
    handoff = links["links"][0]["handoff_to_codex"]
    signaled: list[int] = []

    def record_kill(pid: int, _sig: int) -> None:
        signaled.append(pid)

    monkeypatch.setattr(os, "kill", record_kill)
    procs = lambda: [
        (
            50,
            f"konsole --new-tab codex --yolo Read the handoff at {handoff}".encode(),
        )
    ]
    holders = lambda _path: [
        (70, "/home/maci/.codex/packages/app-server-daemon/bin/codex app-server --managed-daemon"),
        (80, "/usr/lib/chatgpt/resources/codex app-server --listen"),
    ]
    assert (
        sm.main(
            ["--home", str(tmp_path), "yield", CLAUDE_ID],
            procs=procs,
            holders=holders,
            parents=lambda: {71: 70},
        )
        == 0
    )
    assert 50 in signaled
    assert 70 in signaled
    assert 71 in signaled
    assert 80 not in signaled
    text = capsys.readouterr().out
    assert "ChatGPT desktop still has this thread open" in text


def test_short_prefix_is_rejected(tmp_path: Path) -> None:
    claude_home(tmp_path)
    with pytest.raises(SystemExit, match="shorter"):
        sm.main(["--home", str(tmp_path), "to-codex", "abc"])


def test_schema_list_json_and_usage_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sm.main(["schema"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["clispec"] == "0.3"
    assert doc["name"] == "session-migrate"
    assert doc["version"] == "0.2.0"
    assert doc["output"]["piped"] == "text"
    assert {item["kind"] for item in doc["errors"]} >= {"usage", "failure"}
    names = [command["name"] for command in doc["commands"]]
    assert "schema" in names and "list" in names
    assert sm.main(["schema", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [command["name"] for command in listed["commands"]] == ["list"]

    claude_home(tmp_path)
    assert sm.main(["-o", "json", "--home", str(tmp_path), "list", "--limit", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["items"][0]["sid"] == CLAUDE_ID
    assert payload["items"][0]["cwd"] == "/work/demo"
    assert "title" in payload["items"][0]

    assert sm.main(["-o", "text", "--home", str(tmp_path), "list"]) == 0
    text = capsys.readouterr().out
    assert CLAUDE_ID in text
    assert not text.lstrip().startswith("{")

    assert sm.main(["-o", "json"]) == 2
    last = capsys.readouterr().err.strip().splitlines()[-1]
    err = json.loads(last)
    assert err["error"]["kind"] == "usage"
    assert "hint" in err["error"]
