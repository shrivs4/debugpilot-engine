import { useState } from "react";

import { api } from "../api";
import { JiraDraftPanel } from "../components/JiraDraftPanel";
import { Badge, ErrorBanner, Pre, Section, Sha, formatTime } from "../components/ui";
import { usePolling } from "../hooks";
import type { CodeEvidence, DebugRunReport, GitEvidence, Hypothesis, RuntimeEvidence, Validation } from "../types";

const STEPS = [
  ["index", "Load index"],
  ["retrieve", "Retrieve code"],
  ["setup_command", "Setup command"],
  ["analysis_command", "Analysis command"],
  ["git_evidence", "Git evidence"],
  ["context", "Context package"],
  ["hypothesis", "LLM hypothesis"],
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
  return (
    <div className="stack">
      <Section
        title={`Debug run #${run.id}`}
        actions={run.overall_result ? <Badge value={run.overall_result} /> : <Badge value={run.status} />}
      >
        <dl className="facts">
          <dt>Issue</dt>
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
        <ErrorBanner message={run.error} />
        <ErrorBanner message={error} />
      </Section>

      {report.runtime_evidence.map((evidence) => (
        <RuntimeSection key={evidence.id} evidence={evidence} />
      ))}
      {report.code_evidence.length > 0 && <CodeEvidenceSection items={report.code_evidence} />}
      {report.git_evidence && <GitSection git={report.git_evidence} />}
      {report.hypothesis && <HypothesisSection hypothesis={report.hypothesis} />}
      {report.validation && <ValidationSection validation={report.validation} />}
      {run.status === "completed" && report.hypothesis && (
        <JiraDraftPanel
          runId={run.id}
          draft={report.ticket_draft}
          onCreated={(draft) => setData({ ...report, ticket_draft: draft })}
        />
      )}
    </div>
  );
}

function StepTracker({ report }: { report: DebugRunReport }) {
  const { run } = report;
  const steps = STEPS.filter(([key]) => key !== "setup_command" || run.setup_command);
  const currentIndex = steps.findIndex(([key]) => key === run.current_step);
  return (
    <ol className="steps">
      {steps.map(([key, label], index) => {
        let state = "pending";
        if (run.status === "completed" || index < currentIndex) state = "done";
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

function CodeEvidenceSection({ items }: { items: CodeEvidence[] }) {
  return (
    <Section title="Retrieved code evidence">
      <p className="hint">Combined score = semantic weight × cosine similarity (pgvector) + lexical weight × keyword match ratio.</p>
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
              <th>Via</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
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
                <td className="small">{item.sources.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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

function HypothesisSection({ hypothesis }: { hypothesis: Hypothesis }) {
  const [showRaw, setShowRaw] = useState(false);
  return (
    <Section title="LLM hypothesis" actions={<span className="muted small">{hypothesis.model}</span>}>
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
