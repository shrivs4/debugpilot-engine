import shlex
import sys

from sqlalchemy import select

from app.models import DebugRun, EvidenceRecord, Repository
from app.services.command_runner import run_command
from app.services.pipeline import record_runtime_evidence
from app.services.stack_trace import parse_stack_trace


def test_runtime_evidence_round_trips_through_postgres(db_session, tmp_path):
    repository = Repository(url="https://github.com/acme/demo", owner="acme", name="demo", status="ready")
    db_session.add(repository)
    db_session.flush()
    run = DebugRun(repository_id=repository.id, issue_description="it fails", analysis_command="python fail.py")
    db_session.add(run)
    db_session.commit()

    script = "import sys; print('TypeError: x is undefined\\n    at f (src/a.js:3:7)', file=sys.stderr); sys.exit(1)"
    result = run_command(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", tmp_path, 30, 10_000)
    record_runtime_evidence(db_session, run, "analysis", result, parse_stack_trace(result.stderr, tmp_path))

    db_session.expire_all()
    stored = db_session.scalars(select(EvidenceRecord).where(EvidenceRecord.debug_run_id == run.id)).one()

    assert stored.kind == "runtime"
    assert stored.phase == "analysis"
    assert stored.command == result.command
    assert stored.exit_code == 1
    assert stored.success is False
    assert "TypeError" in stored.stderr
    assert stored.stack_trace["top_repository_frame"]["relative_path"] == "src/a.js"
    assert stored.data["duration_ms"] >= 0
    assert stored.captured_at is not None
