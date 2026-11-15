import { useState } from "react";

import { api } from "../api";
import { JiraDraftPanel } from "../components/JiraDraftPanel";
import { Badge, ErrorBanner, Pre, Section, Sha, formatTime } from "../components/ui";
import { usePolling } from "../hooks";
import type {
  CodeEvidence,
  CommitAnalysis,
  DebugRunReport,
  GitEvidence,
  Hypothesis,
  RuntimeEvidence,
  StructuredRuntime,
  Validation,
} from "../types";

const ISSUE_STEPS = [
  ["index", "Load index"],
  ["setup_command", "Setup command"],
  ["analysis_command", "Analysis command"],
  ["retrieve", "Retrieve code"],
  ["git_evidence", "Git evidence"],
  ["context", "Context package"],
  ["hypothesis", "LLM hypothesis"],
  ["validation", "Validation"],
] as const;

const COMMIT_STEPS = [
  ["index", "Load index"],
  ["commit_diff", "Commit diff"],
  ["worktree", "Worktree"],
  ["setup_command", "Setup command"],
  ["analysis_command", "Analysis command"],
  ["retrieve", "Retrieve code"],
  ["context", "Context package"],
  ["hypothesis", "LLM hypotheses"],
  ["validation", "Validation"],
] as const;

const ACTIVE = new Set(["queued", "running"]);

export function DebugRunDetailPage({ runId }: { runId: number }) {
  const { data: report, error, setData } = usePolling(
    () => api.report(runId),
    (next) => ACTIVE.has(next.run.status),
  );

  if (!report) return error ? <ErrorBanner message={error} /> : <p className="muted">Loading run #{runId}…</p>;

  const { run } = report;
  const isCommit = run.run_type === "commit_analysis";
  const ranked = report.hypotheses ?? [];
  return (
    <div className="stack">
      <Section
        title={isCommit ? `Commit analysis #${run.id}` : `Debug run #${run.id}`}
        actions={run.overall_result ? <Badge value={run.overall_result} /> : <Badge value={run.status} />}
      >
        <dl className="facts">
          <dt>{isCommit ? "Description" : "Issue"}</dt>
          <dd>{run.issue_description}</dd>
          <dt>Repository</dt>
          <dd>
            {report.repository.owner}/{report.repository.name} ({report.repository.branch}) @ <Sha value={run.commit_sha} />
          </dd>
          <dt>Status</dt>
          <dd>
            <Badge value={run.status} /> {ACTIVE.has(run.status) && <span className="muted">step: {run.current_step}</span>}
          </dd>
          <dt>Started</dt>
          <dd>{formatTime(run.started_at)}</dd>
          <dt>Finished</dt>
          <dd>{formatTime(run.finished_at)}</dd>
        </dl>
        <StepTracker report={report} />
        {report.conclusion && (
          <div className="banner banner-good" role="status" data-testid="run-conclusion">
            {report.conclusion.message}
          </div>
        )}
        <ErrorBanner message={run.error} />
        <ErrorBanner message={error} />
      </Section>

      {isCommit && report.commit_analysis && <CommitAnalysisSection analysis={report.commit_analysis} />}
      {report.runtime_evidence.map((evidence) => (
        <RuntimeSection key={evidence.id} evidence={evidence} />
      ))}
      {report.code_evidence.length > 0 && <CodeEvidenceSection items={report.code_evidence} />}
      {report.git_evidence && <GitSection git={report.git_evidence} />}
      {ranked.length > 1 ? (
        ranked.map(({ hypothesis, validation }) => (
          <div key={hypothesis.id} className="stack" data-testid={`hypothesis-${hypothesis.rank ?? 1}`}>
            <HypothesisSection hypothesis={hypothesis} title={`Hypothesis ${hypothesis.rank ?? 1} of ${ranked.length}`} />
            {validation && <ValidationSection validation={validation} />}
          </div>
        ))
      ) : (
        <>
          {report.hypothesis && <HypothesisSection hypothesis={report.hypothesis} />}
          {report.validation && <ValidationSection validation={report.validation} />}
        </>
      )}
      {run.status === "completed" && report.hypothesis && (
        <JiraDraftPanel
          runId={run.id}
          hypotheses={ranked.length ? ranked : [{ hypothesis: report.hypothesis, validation: report.validation }]}
          draft={report.ticket_draft}
          onCreated={(draft) => setData({ ...report, ticket_draft: draft })}
        />
      )}
    </div>
  );
}

function StepTracker({ report }: { report: DebugRunReport }) {
  const { run } = report;
  const steps = (run.run_type === "commit_analysis" ? COMMIT_STEPS : ISSUE_STEPS).filter(
    ([key]) => (key !== "setup_command" || run.setup_command) && (key !== "analysis_command" || run.analysis_command),
  );
  const currentIndex = steps.findIndex(([key]) => key === run.current_step);
  // A run that found no failing behavior stops after the commands.
  const lastRun = report.conclusion ? steps.findIndex(([key]) => key === "analysis_command") : steps.length - 1;
  return (
    <ol className="steps">
      {steps.map(([key, label], index) => {
        let state = "pending";
        if (index > lastRun) state = "skipped";
        else if (run.status === "completed" || index < currentIndex) state = "done";
        else if (index === currentIndex) state = run.status === "failed" ? "failed" : "active";
        return (
          <li key={key} className={`step step-${state}`}>
            {label}
          </li>
        );
      })}
    </ol>
  );
}

function RuntimeSection({ evidence }: { evidence: RuntimeEvidence }) {
  const trace = evidence.stack_trace;
  return (
    <Section
      title={`Runtime evidence · ${evidence.phase} command`}
      actions={<Badge value={evidence.success ? "passed" : "failed"} label={evidence.success ? "succeeded" : "failed"} />}
    >
      <dl className="facts">
        <dt>Command</dt>
        <dd>
          <code>{evidence.command}</code>
        </dd>
        <dt>Exit code</dt>
        <dd data-testid={`exit-code-${evidence.phase}`}>{evidence.exit_code ?? "—"}</dd>
        <dt>Duration</dt>
        <dd>{evidence.data.duration_ms !== undefined ? `${(evidence.data.duration_ms / 1000).toFixed(1)}s` : "—"}</dd>
        <dt>Captured</dt>
        <dd>{formatTime(evidence.captured_at)}</dd>
      </dl>
      {evidence.data.error && <ErrorBanner message={evidence.data.error} />}
      {evidence.data.structured && <StructuredSection structured={evidence.data.structured} />}
      {trace && (
        <>
          <h3>Stack trace</h3>
          {trace.error_type && (
            <p className="error-line">
              <strong>{trace.error_type}</strong>: {trace.error_message}
            </p>
          )}
          <ul className="frames">
            {trace.frames.map((frame, index) => (
              <li key={index} className={frame.in_repository ? "in-repo" : "external"}>
                <code>{frame.function}</code> — {frame.relative_path ?? frame.path}:{frame.line}:{frame.column}
              </li>
            ))}
          </ul>
        </>
      )}
      <details>
        <summary>stdout</summary>
        <Pre>{evidence.stdout}</Pre>
      </details>
      <details open={!evidence.success}>
        <summary>stderr</summary>
        <Pre>{evidence.stderr}</Pre>
      </details>
    </Section>
  );
}

function StructuredSection({ structured }: { structured: StructuredRuntime }) {
  const analysis = structured.mismatch_analysis;
  if (!structured.mismatches.length) {
    return structured.extracted ? null : <p className="hint">No structured values could be extracted from this output.</p>;
  }
  return (
    <>
      <h3>Observed mismatches</h3>
      <div className="table-wrap">
        <table data-testid="mismatches">
          <thead>
            <tr>
              <th>Field</th>
              <th>Expected</th>
              <th>Actual</th>
              <th>Actual / expected</th>
              <th>Role</th>
            </tr>
          </thead>
          <tbody>
            {structured.mismatches.map((item) => (
              <tr key={item.field}>
                <td>{item.label}</td>
                <td>{item.expected}</td>
                <td className="mismatch-actual">{item.actual}</td>
                <td>{item.ratio ?? "—"}</td>
                <td className="small">
                  {analysis.root_fields.includes(item.field)
                    ? "root"
                    : analysis.propagated_fields.includes(item.field)
                      ? `propagated from ${(analysis.dependencies[item.field] ?? []).join(", ")}`
                      : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {analysis.notes.map((note) => (
        <p key={note} className="hint">
          {note}
        </p>
      ))}
    </>
  );
}

function CommitAnalysisSection({ analysis }: { analysis: CommitAnalysis }) {
  return (
    <Section title="Analyzed commits">
      <dl className="facts">
        <dt>Range</dt>
        <dd>
          {analysis.base ? <Sha value={analysis.base} /> : <span className="muted">(root)</span>} ..{" "}
          <Sha value={analysis.head} />
        </dd>
        {analysis.notes && (
          <>
            <dt>Notes</dt>
            <dd>{analysis.notes}</dd>
          </>
        )}
      </dl>
      {analysis.commits && (
        <ul className="plain" data-testid="analyzed-commits">
          {analysis.commits.map((commit) => (
            <li key={commit.sha}>
              <Sha value={commit.sha} /> {commit.subject} <span className="muted small">{commit.author}</span>
            </li>
          ))}
        </ul>
      )}
      {analysis.changed_files && (
        <>
          <h3>Changed files and symbols</h3>
          <ul className="plain" data-testid="changed-files">
            {analysis.changed_files.map((file) => (
              <li key={file.file_path}>
                <code className="sha" title="git status">{file.status}</code> <code>{file.file_path}</code>
                {file.symbols.length > 0 && <span className="muted small"> — {file.symbols.join(", ")}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
      {analysis.diff_truncated && <p className="hint">The diff was truncated for the LLM context.</p>}
    </Section>
  );
}

function Reasons({ item }: { item: CodeEvidence }) {
  if (!item.reasons?.length) return <span className="small">{item.sources.join(", ")}</span>;
  return (
    <ul className="reasons">
      {item.reasons.map((reason) => (
        <li key={reason.signal} title={`score ${reason.score} × weight ${reason.weight ?? "—"}`}>
          <strong>{reason.signal.replace(/_/g, " ")}</strong> +{reason.contribution.toFixed(3)}: {reason.detail}
        </li>
      ))}
    </ul>
  );
}

function CodeEvidenceSection({ items }: { items: CodeEvidence[] }) {
  const primary = items.filter((item) => item.role !== "related");
  const related = items.filter((item) => item.role === "related");
  return (
    <Section title="Retrieved code evidence">
      <p className="hint">
        Combined score = weighted sum of semantic similarity, exact keyword, symbol and file-name matches, runtime
        locations and Git changes. Each row lists the signals that selected it.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th>Symbol</th>
              <th>Lines</th>
              <th>Semantic</th>
              <th>Lexical</th>
              <th>Combined</th>
              <th>Why selected</th>
            </tr>
          </thead>
          <tbody>
            {primary.map((item) => (
              <tr key={item.chunk_id}>
                <td>
                  <code>{item.file_path}</code>
                </td>
                <td>
                  {item.symbol} <span className="muted small">{item.chunk_type}</span>
                </td>
                <td>
                  {item.start_line}–{item.end_line}
                </td>
                <td>{item.semantic_score.toFixed(3)}</td>
                <td title={item.matched_keywords.join(", ")}>{item.lexical_score.toFixed(3)}</td>
                <td>
                  <strong>{item.combined_score.toFixed(3)}</strong>
                </td>
                <td>
                  <Reasons item={item} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {related.length > 0 && (
        <>
          <h3>Related evidence</h3>
          <p className="hint">Code connected to the primary evidence through calls, imports, types or shared data.</p>
          <div className="table-wrap">
            <table data-testid="related-evidence">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Symbol</th>
                  <th>Lines</th>
                  <th>Score</th>
                  <th>Relationship</th>
                </tr>
              </thead>
              <tbody>
                {related.map((item) => (
                  <tr key={item.chunk_id}>
                    <td>
                      <code>{item.file_path}</code>
                    </td>
                    <td>{item.symbol}</td>
                    <td>
                      {item.start_line}–{item.end_line}
                    </td>
                    <td>{item.combined_score.toFixed(3)}</td>
                    <td className="relation">{item.relation?.path ?? item.sources.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Section>
  );
}

function GitSection({ git }: { git: GitEvidence }) {
  return (
    <Section title="Git evidence">
      <dl className="facts">
        <dt>Current commit</dt>
        <dd>
          <Sha value={git.head_commit} />
        </dd>
        <dt>Blame target</dt>
        <dd>{git.target ? `${git.target.file_path}:${git.target.line} (from ${git.target.source.replace("_", " ")})` : "—"}</dd>
        <dt>Line last changed by</dt>
        <dd>
          {git.suspect_commit ? (
            <>
              <Sha value={git.suspect_commit.commit} /> {git.suspect_commit.summary}{" "}
              <span className="muted">({git.suspect_commit.author})</span>
            </>
          ) : (
            "—"
          )}
        </dd>
      </dl>
      {git.errors.map((message) => (
        <ErrorBanner key={message} message={message} />
      ))}
      <details open>
        <summary>git blame</summary>
        <Pre>{git.blame}</Pre>
      </details>
      <details>
        <summary>Commit diff for the target file</summary>
        <Pre>{git.suspect_diff}</Pre>
      </details>
      {git.file_history.length > 0 && (
        <details>
          <summary>File history</summary>
          <ul className="plain">
            {git.file_history.map((entry) => (
              <li key={entry.commit}>
                <Sha value={entry.commit} /> {entry.subject} <span className="muted small">{entry.author}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Section>
  );
}

function HypothesisSection({ hypothesis, title = "LLM hypothesis" }: { hypothesis: Hypothesis; title?: string }) {
  const [showRaw, setShowRaw] = useState(false);
  return (
    <Section title={title} actions={<span className="muted small">{hypothesis.model}</span>}>
      {!hypothesis.parse_ok && (
        <div className="banner banner-warn">The model did not return valid JSON; fields were extracted from free text.</div>
      )}
      {hypothesis.summary && <p className="lead">{hypothesis.summary}</p>}
      <dl className="facts">
        <dt>Root cause</dt>
        <dd>{hypothesis.root_cause ?? "—"}</dd>
        <dt>Location</dt>
        <dd>
          <code>
            {hypothesis.file_path ?? "?"}:{hypothesis.line_number ?? "?"}
          </code>{" "}
          {hypothesis.symbol && <span className="muted">in {hypothesis.symbol}</span>}
        </dd>
        <dt>Introducing commit</dt>
        <dd>
          <Sha value={hypothesis.introducing_commit} />
        </dd>
        <dt>Confidence</dt>
        <dd>{hypothesis.confidence ?? "—"}</dd>
        {hypothesis.affected_fields && hypothesis.affected_fields.length > 0 && (
          <>
            <dt>Explains fields</dt>
            <dd>{hypothesis.affected_fields.join(", ")}</dd>
          </>
        )}
      </dl>
      {hypothesis.execution_path.length > 0 && (
        <>
          <h3>Execution path</h3>
          <ol>
            {hypothesis.execution_path.map((step, index) => (
              <li key={index}>
                <code>{step}</code>
              </li>
            ))}
          </ol>
        </>
      )}
      {hypothesis.missing_evidence.length > 0 && (
        <>
          <h3>Missing evidence</h3>
          <ul>
            {hypothesis.missing_evidence.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </>
      )}
      <button type="button" className="link" onClick={() => setShowRaw(!showRaw)}>
        {showRaw ? "Hide" : "Show"} raw model response
      </button>
      {showRaw && <Pre>{hypothesis.raw_response}</Pre>}
    </Section>
  );
}

function ValidationSection({ validation }: { validation: Validation }) {
  return (
    <Section title="Validation checks" actions={<Badge value={validation.overall_result} />}>
      <ul className="checks">
        {validation.checks.map((check) => (
          <li key={check.name} className={`check check-${check.status}`}>
            <Badge value={check.status} />
            <div>
              <div>
                <strong>{check.name.replace(/_/g, " ")}</strong>
                {!check.required && <span className="muted small"> (advisory)</span>}
                {check.category === "conflict" && <span className="muted small"> (conflict check)</span>}
              </div>
              <div className="muted small">{check.details}</div>
            </div>
          </li>
        ))}
      </ul>
      <p className="overall">
        Overall result: <Badge value={validation.overall_result} />
      </p>
    </Section>
  );
}
