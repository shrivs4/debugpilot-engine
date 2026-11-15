import { useEffect, useState, type FormEvent } from "react";

import { api } from "../api";
import { CommandApproval } from "../components/CommandApproval";
import { ErrorBanner, Section } from "../components/ui";
import { navigate } from "../hooks";
import type { Commit, DebugRun, Repository } from "../types";

type Mode = "commit" | "range";

function commitLabel(commit: Commit) {
  return `${commit.short} ${commit.subject}`;
}

export function CommitAnalysisPage() {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [repositoryId, setRepositoryId] = useState("");
  const [commits, setCommits] = useState<Commit[]>([]);
  const [mode, setMode] = useState<Mode>("commit");
  const [commitSha, setCommitSha] = useState("");
  const [baseSha, setBaseSha] = useState("");
  const [headSha, setHeadSha] = useState("");
  const [setupCommand, setSetupCommand] = useState("");
  const [analysisCommand, setAnalysisCommand] = useState("");
  const [notes, setNotes] = useState("");
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

  useEffect(() => {
    if (!repositoryId) return;
    setCommits([]);
    api
      .listCommits(Number(repositoryId))
      .then((items) => {
        setCommits(items);
        setCommitSha(items[0]?.sha ?? "");
        setHeadSha(items[0]?.sha ?? "");
        setBaseSha(items[1]?.sha ?? "");
      })
      .catch((caught: Error) => setError(caught.message));
  }, [repositoryId]);

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
      const selection = mode === "commit" ? { commit_sha: commitSha } : { base_sha: baseSha, head_sha: headSha };
      setRun(
        await api.createCommitAnalysis({
          repository_id: Number(repositoryId),
          ...selection,
          setup_command: setupCommand.trim() || null,
          analysis_command: analysisCommand.trim() || null,
          notes: notes.trim() || null,
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
    const range = run.base_commit ? `${run.base_commit.slice(0, 8)}..${run.target_commit?.slice(0, 8)}` : run.target_commit?.slice(0, 8);
    return (
      <div className="stack">
        <Section title={`Review commit analysis · run #${run.id}`}>
          <p>
            Analyzing <code>{range}</code> in <strong>{run.repository_name}</strong>. Commands run in a temporary worktree
            checked out at the analyzed commit, and only after you approve the exact text below.
          </p>
          {!run.setup_command && !run.analysis_command && (
            <p className="hint">
              No commands were given: hypotheses will come from the diff and code only, so validation cannot go beyond
              “needs review”.
            </p>
          )}
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
          {run.analysis_command && (
            <CommandApproval
              label="Analysis command"
              command={run.analysis_command}
              approvedAt={run.analysis_command_approved_at}
              repositoryName={run.repository_name}
              disabled={busy}
              onApprove={() => approve("analysis", run.analysis_command!)}
            />
          )}
          <div className="button-row">
            <button type="button" onClick={start} disabled={!run.ready_to_run || busy}>
              Run commit analysis
            </button>
            {!run.ready_to_run && <span className="muted">Approve every command to enable the run.</span>}
          </div>
          <ErrorBanner message={error} />
        </Section>
      </div>
    );
  }

  const commitOptions = commits.map((commit) => (
    <option key={commit.sha} value={commit.sha}>
      {commitLabel(commit)}
    </option>
  ));
  const selectionReady = mode === "commit" ? Boolean(commitSha) : Boolean(baseSha && headSha && baseSha !== headSha);

  return (
    <div className="stack">
      <Section title="Commit analysis">
        <form className="form-grid" onSubmit={onReview}>
          <label>
            Repository
            <select value={repositoryId} onChange={(event) => setRepositoryId(event.target.value)} required>
              <option value="" disabled>
                {repositories.length ? "Select a repository" : "No ready repositories — add one first"}
              </option>
              {repositories.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.owner}/{item.name} ({item.branch})
                </option>
              ))}
            </select>
          </label>
          <fieldset className="mode">
            <legend>Analyze</legend>
            <label className="inline">
              <input type="radio" name="mode" checked={mode === "commit"} onChange={() => setMode("commit")} /> Single
              commit
            </label>
            <label className="inline">
              <input type="radio" name="mode" checked={mode === "range"} onChange={() => setMode("range")} /> Commit range
            </label>
          </fieldset>
          {mode === "commit" ? (
            <label>
              Commit
              <select value={commitSha} onChange={(event) => setCommitSha(event.target.value)} required>
                {commitOptions}
              </select>
            </label>
          ) : (
            <>
              <label>
                Base commit (excluded)
                <select value={baseSha} onChange={(event) => setBaseSha(event.target.value)} required>
                  {commitOptions}
                </select>
              </label>
              <label>
                Head commit (included)
                <select value={headSha} onChange={(event) => setHeadSha(event.target.value)} required>
                  {commitOptions}
                </select>
              </label>
            </>
          )}
          <label>
            Setup command (optional)
            <input value={setupCommand} onChange={(event) => setSetupCommand(event.target.value)} placeholder="npm install" />
          </label>
          <label>
            Analysis / reproduction command (optional)
            <input
              value={analysisCommand}
              onChange={(event) => setAnalysisCommand(event.target.value)}
              placeholder="npm test"
            />
          </label>
          <label>
            Notes (optional)
            <textarea rows={2} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="What should the review focus on?" />
          </label>
          <p className="hint">
            Commands run without a shell in a temporary checkout of the analyzed commit. You will confirm each command
            before anything executes.
          </p>
          <div className="button-row">
            <button type="submit" disabled={busy || !repositoryId || !selectionReady}>
              Review analysis
            </button>
          </div>
        </form>
        <ErrorBanner message={error} />
      </Section>
    </div>
  );
}
