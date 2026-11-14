import json

from pluto_factory.claude_worker import handle_hook, hook_settings
from pluto_factory.cli import main
from pluto_factory.config import load_config
from pluto_factory.demo import prepare_demo
from pluto_factory.engine import Engine
from pluto_factory.models import DebugPolicy, FactoryConfig, Repository
from pluto_factory.trace import (
    Tracer,
    _annotate,
    attribute_files,
    read_trace,
    trace_path,
    truncate,
)


def enable_debug(home):
    path = home / "factory.toml"
    text = path.read_text()
    assert "[debug]\nenabled = false" in text
    path.write_text(
        text.replace("[debug]\nenabled = false", "[debug]\nenabled = true")
    )
    assert load_config(home).debug.enabled


def test_truncate_bounds_nested_strings():
    value = truncate({"a": ["x" * 50], "b": 3}, 10)
    assert value["a"][0].startswith("x" * 10)
    assert "40 bytes truncated" in value["a"][0]
    assert value["b"] == 3


def test_redaction_scrubs_known_and_pattern_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_API_TOKEN", "supersecretvalue123")
    config = FactoryConfig(
        name="t",
        repositories=[Repository(name="a", path=str(tmp_path))],
        debug=DebugPolicy(enabled=True),
    )
    tracer = Tracer(tmp_path, config)
    redacted = tracer.redact(
        {
            "command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234' "
            "-d supersecretvalue123",
            "nested": ["key ghp_" + "a" * 30],
        }
    )
    text = json.dumps(redacted)
    assert "supersecretvalue123" not in text
    assert "abcdefghijklmnop1234" not in text
    assert "ghp_" not in text
    assert text.count("[REDACTED]") == 3


def test_annotate_flags_generator_commands_and_paths():
    shell = _annotate(
        {"tool": "bash", "input": {"command": "cd web && pluto add endpoint"}}
    )
    assert shell["generator"] is True
    assert shell["command"].startswith("cd web")
    other = _annotate({"tool": "bash", "input": {"command": "plutonium go"}})
    assert other["generator"] is False
    edit = _annotate({"tool": "Edit", "input": {"file_path": "/w/app.go"}})
    assert edit["path"] == "/w/app.go"


def test_attribution_prefers_edits_then_generator_then_shell():
    records = [
        {"kind": "tool", "run_id": "r", "tool": "Edit", "path": "/w/a.go"},
        {
            "kind": "tool",
            "run_id": "r",
            "tool": "bash",
            "command": "pluto add job",
            "generator": True,
        },
        {
            "kind": "phase.files",
            "run_id": "r",
            "role": "implementer",
            "repository": "app",
            "changes": [
                {"status": "M", "path": "a.go"},
                {"status": "A", "path": "jobs/new.go"},
            ],
        },
    ]
    rows = {row["path"]: row for row in attribute_files(records)}
    assert rows["a.go"]["source"] == "edit"
    assert rows["jobs/new.go"]["source"] == "generator"
    assert rows["jobs/new.go"]["evidence"] == ["pluto add job"]


def test_claude_hooks_record_tool_calls(tmp_path):
    raw = tmp_path / "trace.raw.jsonl"
    assignment = {
        "report_path": str(tmp_path / "report.json"),
        "trace": {"path": str(raw), "max_output_bytes": 64},
    }
    handle_hook(
        assignment,
        "PostToolUse",
        {
            "tool_name": "Write",
            "tool_use_id": "t1",
            "tool_input": {"file_path": "/w/main.go"},
            "tool_response": {"ok": True, "text": "y" * 200},
        },
    )
    event = json.loads(raw.read_text())
    assert event["kind"] == "tool"
    assert event["tool"] == "Write"
    assert "truncated" in event["output"]
    assert "PostToolUse" in hook_settings("python", trace=True)["hooks"]
    assert "PostToolUse" not in hook_settings("python")["hooks"]


def test_hooks_write_nothing_when_disabled(tmp_path):
    assignment = {"report_path": str(tmp_path / "report.json")}
    handle_hook(assignment, "PostToolUse", {"tool_name": "Write"})
    assert not list(tmp_path.iterdir())


def test_disabled_debug_writes_no_trace(tmp_path):
    home, _ = prepare_demo(tmp_path / "off")
    Engine(home).run(max_seconds=30)
    assert not (home / "logs" / "trace").exists()


def test_fixture_run_produces_full_trace(tmp_path, capsys):
    home, prepared = prepare_demo(tmp_path / "on")
    enable_debug(home)
    Engine(home).run(max_seconds=30)
    work_id = prepared["submitted"]["id"]
    path = trace_path(home, work_id)
    assert path.stat().st_mode & 0o777 == 0o600
    records = read_trace(home, work_id)
    kinds = {record["kind"] for record in records}
    assert {
        "run.provenance",
        "worker.dispatched",
        "tool",
        "phase.files",
        "evidence",
    } <= kinds
    assert "worker.observed" not in kinds
    provenance = next(r for r in records if r["kind"] == "run.provenance")
    assert provenance["tools"]["pluto_factory"]
    assert provenance["harness"] == "fixture"
    implemented = [
        r
        for r in records
        if r["kind"] == "phase.files" and r["role"] == "implementer"
    ]
    assert any(
        change["path"] == "calculator.py"
        for record in implemented
        for change in record["changes"]
    )
    rows = [row for row in attribute_files(records) if row["status"] != "?"]
    edited = [row for row in rows if row["path"] == "calculator.py"]
    # Both repositories have calculator.py; each maps to its own edit only.
    assert {row["repository"] for row in edited} == {"core", "cli"}
    assert all(
        row["source"] == "edit" and row["evidence"] == ["Edit"]
        for row in edited
    )

    assert main(["trace", "--home", str(home), "--files"]) == 0
    output = capsys.readouterr().out
    assert "phase.files" in output
    assert "calculator.py" in output
    assert main(["trace", "--home", str(home), work_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["work_id"] == work_id


def test_trace_without_debug_is_actionable(tmp_path, capsys):
    home, _ = prepare_demo(tmp_path / "none")
    assert main(["trace", "--home", str(home)]) == 1
    assert "[debug]" in capsys.readouterr().err


def test_ingest_namespaces_worker_fields_that_collide(tmp_path):
    config = FactoryConfig(
        name="t",
        repositories=[Repository(name="a", path=str(tmp_path))],
        debug=DebugPolicy(enabled=True),
    )
    tracer = Tracer(tmp_path, config)
    raw = tmp_path / "trace.raw.jsonl"
    events = [
        {"ts": 1.0, "kind": "session.start", "source": "startup"},
        {"ts": 2.0, "kind": "tool", "tool": "Bash", "role": "x", "run_id": "y"},
    ]
    raw.write_text("".join(json.dumps(event) + "\n" for event in events))
    run = {
        "id": "run-1",
        "work_id": "work-1",
        "role": "implementer",
        "assignment": {"trace": {"path": str(raw)}},
    }
    tracer.ingest(run)
    records = [
        json.loads(line)
        for line in trace_path(tmp_path, "work-1").read_text().splitlines()
    ]
    start = next(r for r in records if r["kind"] == "session.start")
    assert start["source"] == "worker"
    assert start["event_source"] == "startup"
    tool = next(r for r in records if r["kind"] == "tool")
    assert (tool["role"], tool["run_id"]) == ("implementer", "run-1")
    assert (tool["event_role"], tool["event_run_id"]) == ("x", "y")
    tracer.ingest(run)
    assert len(trace_path(tmp_path, "work-1").read_text().splitlines()) == 2
