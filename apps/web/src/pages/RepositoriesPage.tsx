import { useState, type FormEvent } from "react";

import { api } from "../api";
import { Badge, ErrorBanner, Section, Sha, formatTime } from "../components/ui";
import { usePolling } from "../hooks";
import type { Repository } from "../types";

const BUSY = new Set(["pending", "cloning", "syncing", "indexing"]);
const FIXTURE_URL = "https://github.com/shrivs4/debugpilot-checkout-fixture";

export function RepositoriesPage() {
  const { data: repositories, error: loadError, refresh } = usePolling(
    api.listRepositories,
    (items) => items.some((item) => BUSY.has(item.status)),
  );
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const pollUntilIdle = async () => {
    const next = await refresh();
    if (next?.some((item) => BUSY.has(item.status))) setTimeout(pollUntilIdle, 1500);
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.addRepository(url.trim(), branch.trim() || null);
      setUrl("");
      setBranch("");
      await pollUntilIdle();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const onSync = async (repository: Repository) => {
    setError(null);
    try {
      await api.syncRepository(repository.id);
      await pollUntilIdle();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  return (
    <div className="stack">
      <Section title="Add repository">
        <form className="form-row" onSubmit={onSubmit}>
          <label>
            Public GitHub URL
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder={FIXTURE_URL}
              required
            />
          </label>
          <label className="narrow">
            Branch (optional)
            <input value={branch} onChange={(event) => setBranch(event.target.value)} placeholder="default branch" />
          </label>
          <button type="submit" disabled={submitting || !url.trim()}>
            {submitting ? "Adding…" : "Add repository"}
          </button>
        </form>
        <p className="hint">
          The repository is cloned into the API's managed workspace and indexed. No setup or test commands run
          automatically.{" "}
          <button type="button" className="link" onClick={() => setUrl(FIXTURE_URL)}>
            Use the checkout fixture
          </button>
        </p>
        <ErrorBanner message={error} />
      </Section>

      <Section title="Repositories">
        <ErrorBanner message={loadError} />
        {repositories && repositories.length === 0 && <p className="muted">No repositories yet.</p>}
        {repositories && repositories.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Repository</th>
                  <th>Branch</th>
                  <th>Status</th>
                  <th>Commit</th>
                  <th>Index</th>
                  <th>Last sync</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {repositories.map((repository) => (
                  <RepositoryRow key={repository.id} repository={repository} onSync={() => onSync(repository)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}

function RepositoryRow({ repository, onSync }: { repository: Repository; onSync: () => void }) {
  const busy = BUSY.has(repository.status);
  const sync = repository.latest_sync;
  const stale = repository.indexed_commit_sha && repository.indexed_commit_sha !== repository.current_commit_sha;
  return (
    <tr data-testid={`repository-${repository.id}`}>
      <td>
        <a href={repository.url} target="_blank" rel="noreferrer">
          {repository.owner}/{repository.name}
        </a>
        {repository.status_message && <div className="muted small">{repository.status_message}</div>}
      </td>
      <td>{repository.branch ?? <span className="muted">default</span>}</td>
      <td>
        <Badge value={repository.status} />
      </td>
      <td>
        <Sha value={repository.current_commit_sha} />
        {sync?.kind === "sync" && sync.status === "completed" && (
          <div className="muted small">{sync.commit_changed ? `new commit (${sync.changed_files.length} files changed)` : "no new commit"}</div>
        )}
      </td>
      <td>
        {repository.chunk_count} chunks / {repository.file_count} files
        {stale && <div className="small warn-text">index is behind HEAD</div>}
      </td>
      <td className="small">{formatTime(repository.last_synced_at)}</td>
      <td className="actions">
        <button type="button" onClick={onSync} disabled={busy || !repository.current_commit_sha}>
          {repository.status === "syncing" ? "Syncing…" : "Sync"}
        </button>
        {repository.status === "ready" && (
          <a className="button secondary" href={`#/runs/new?repo=${repository.id}`}>
            Debug
          </a>
        )}
      </td>
    </tr>
  );
}
