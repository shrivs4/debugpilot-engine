import { useEffect, useState, type FormEvent } from "react";

import { api } from "../api";
import { ErrorBanner, Section } from "../components/ui";
import { navigate } from "../hooks";
import type { DebugRun, Repository } from "../types";

const FIXTURE_EXAMPLE = {
  issue: "Checkout fails for legacy coupons because discountType is missing",
  setup: "npm install",
  analysis: "npm run repro:legacy",
};

function initialRepositoryId(): string {
  const query = window.location.hash.split("?")[1] ?? "";
  return new URLSearchParams(query).get("repo") ?? "";
}

export function NewDebugRunPage() {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [repositoryId, setRepositoryId] = useState(initialRepositoryId);
  const [issue, setIssue] = useState("");
  const [setupCommand, setSetupCommand] = useState("");
  const [analysisCommand, setAnalysisCommand] = useState("");
  const [run, setRun] = useState<DebugRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listRepositories()
      .then((items) => {
        const ready = items.filter((item) => item.status === "ready");
        setRepositories(ready);
        setRepositoryId((current) => current || (ready[0] ? String(ready[0].id) : ""));
      })
      .catch((caught: Error) => setError(caught.message));
  }, []);

  const repository = repositories.find((item) => String(item.id) === repositoryId);

  const act = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const onReview = (event: FormEvent) => {
    event.preventDefault();
    void act(async () => {
      setRun(
        await api.createRun({
          repository_id: Number(repositoryId),
          issue_description: issue,
          setup_command: setupCommand.trim() || null,
          analysis_command: analysisCommand,
        }),
      );
    });
  };

  const approve = (kind: "setup" | "analysis", command: string) =>
    act(async () => setRun(await api.approveCommand(run!.id, kind, command)));

  const start = () =>
    act(async () => {
      await api.startRun(run!.id);
      navigate(`/runs/${run!.id}`);
    });

  if (run) {
    return (
      <div className="stack">
        <Section title={`Review commands · run #${run.id}`}>
          <p>
            Nothing has run yet. Each command executes inside <strong>{run.repository_name}</strong> (managed clone,
            no shell) only after you approve the exact text below.
          </p>
          {run.setup_command && (
            <CommandApproval
              label="Setup command"
              command={run.setup_command}
              approvedAt={run.setup_command_approved_at}
              repositoryName={run.repository_name}
              disabled={busy}
              onApprove={() => approve("setup", run.setup_command!)}
            />
          )}
          <CommandApproval
            label="Analysis command"
            command={run.analysis_command}
            approvedAt={run.analysis_command_approved_at}
            repositoryName={run.repository_name}
            disabled={busy}
            onApprove={() => approve("analysis", run.analysis_command)}
          />
          <div className="button-row">
            <button type="button" onClick={start} disabled={!run.ready_to_run || busy}>
              Run debug analysis
            </button>
            {!run.ready_to_run && <span className="muted">Approve every command to enable the run.</span>}
          </div>
          <ErrorBanner message={error} />
        </Section>
      </div>
    );
  }

  return (
    <div className="stack">
      <Section title="New debug run">
        <form className="form-grid" onSubmit={onReview}>
          <label>
            Repository
            <select value={repositoryId} onChange={(event) => setRepositoryId(event.target.value)} required>
              <option value="" disabled>
                {repositories.length ? "Select a repository" : "No ready repositories — add one first"}
              </option>
              {repositories.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.owner}/{item.name} ({item.branch}) @ {item.current_commit_sha?.slice(0, 8)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Issue description
            <textarea
              rows={3}
              value={issue}
              onChange={(event) => setIssue(event.target.value)}
              placeholder="What is going wrong? e.g. Checkout fails for legacy coupons"
              required
            />
          </label>
          <label>
            Setup command (optional)
            <input value={setupCommand} onChange={(event) => setSetupCommand(event.target.value)} placeholder="npm install" />
          </label>
          <label>
            Analysis / reproduction command
            <input
              value={analysisCommand}
              onChange={(event) => setAnalysisCommand(event.target.value)}
              placeholder="npm run repro:legacy"
              required
            />
          </label>
          <p className="hint">
            Commands run without a shell, so <code>&amp;&amp;</code>, pipes and redirects are not supported. You will
            confirm each command before anything executes.{" "}
            <button
              type="button"
              className="link"
              onClick={() => {
                setIssue(FIXTURE_EXAMPLE.issue);
                setSetupCommand(FIXTURE_EXAMPLE.setup);
                setAnalysisCommand(FIXTURE_EXAMPLE.analysis);
              }}
            >
              Fill checkout-fixture example
            </button>
          </p>
          <div className="button-row">
            <button type="submit" disabled={busy || !repository || !issue.trim() || !analysisCommand.trim()}>
              Review commands
            </button>
          </div>
        </form>
        <ErrorBanner message={error} />
      </Section>
    </div>
  );
}

function CommandApproval(props: {
  label: string;
  command: string;
  approvedAt: string | null;
  repositoryName: string | null;
  disabled: boolean;
  onApprove: () => void;
}) {
  return (
    <div className={`approval ${props.approvedAt ? "approved" : ""}`}>
      <div className="approval-label">{props.label}</div>
      <p className="approval-question">Run this command inside the selected repository?</p>
      <pre className="command">$ {props.command}</pre>
      <div className="muted small">Repository: {props.repositoryName}</div>
      {props.approvedAt ? (
        <div className="approved-text">✓ Approved {new Date(props.approvedAt).toLocaleTimeString()}</div>
      ) : (
        <button type="button" onClick={props.onApprove} disabled={props.disabled}>
          Approve {props.label.toLowerCase()}
        </button>
      )}
    </div>
  );
}
