"""Mock Jira ticket drafts, built deterministically from stored evidence (no LLM, no network)."""

from app.models import DebugRun, EvidenceRecord, Hypothesis, Repository, ValidationResult


def build_ticket_fields(
    run: DebugRun,
    repository: Repository,
    hypothesis: Hypothesis,
    validation: ValidationResult | None,
    evidence: list[EvidenceRecord],
) -> dict:
    analysis = next((item for item in evidence if item.kind == "runtime" and item.phase == "analysis"), None)
    setup = next((item for item in evidence if item.kind == "runtime" and item.phase == "setup"), None)
    git = next((item for item in evidence if item.kind == "git"), None)
    chunks = [item for item in evidence if item.kind == "code_chunk"]
    stack = (analysis.stack_trace if analysis else None) or {}

    if stack.get("error_type"):
        headline = f"{stack['error_type']}: {stack.get('error_message') or ''}".strip()
    else:
        headline = run.issue_description.splitlines()[0]
    location = f" in {hypothesis.file_path}:{hypothesis.line_number}" if hypothesis.file_path else ""
    title = f"[{repository.name}] {headline}{location}"[:240]

    evidence_items: list[dict] = []
    frame = stack.get("top_repository_frame")
    if frame:
        evidence_items.append(
            {
                "type": "stack_trace",
                "label": f"{frame['relative_path']}:{frame['line']}:{frame['column']} in {frame['function']}",
            }
        )
    suspect = (git.data if git else {}).get("suspect_commit")
    if suspect:
        evidence_items.append(
            {"type": "git_commit", "label": f"{suspect['short']} {suspect.get('summary') or ''}".strip()}
        )
    for chunk in chunks[:5]:
        data = chunk.data
        evidence_items.append(
            {
                "type": "code",
                "label": f"{data['file_path']}:{data['start_line']}-{data['end_line']} ({data['symbol']})",
                "similarity": data["semantic_score"],
                "combined_score": data["combined_score"],
            }
        )

    return {
        "title": title,
        "summary": hypothesis.summary or run.issue_description,
        "root_cause": hypothesis.root_cause,
        "file_path": hypothesis.file_path,
        "line_number": hypothesis.line_number,
        "reproduction": {
            "repository": repository.url,
            "branch": repository.branch,
            "commit": run.commit_sha,
            "setup_command": setup.command if setup else None,
            "analysis_command": analysis.command if analysis else run.analysis_command,
            "exit_code": analysis.exit_code if analysis else None,
            "issue_description": run.issue_description,
        },
        "evidence": evidence_items,
        "validation_status": validation.overall_result if validation else None,
    }
