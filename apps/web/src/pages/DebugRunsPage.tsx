import { api } from "../api";
import { Badge, ErrorBanner, Section, Sha, formatTime } from "../components/ui";
import { usePolling } from "../hooks";

export function DebugRunsPage() {
  const { data: runs, error } = usePolling(api.listRuns, (items) =>
    items.some((item) => item.status === "queued" || item.status === "running"),
  );

  return (
    <Section title="Debug runs" actions={<a className="button" href="#/runs/new">New debug run</a>}>
      <ErrorBanner message={error} />
      {runs && runs.length === 0 && <p className="muted">No debug runs yet.</p>}
      {runs && runs.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Issue</th>
                <th>Repository</th>
                <th>Commit</th>
                <th>Status</th>
                <th>Result</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td>
                    <a href={`#/runs/${run.id}`}>#{run.id}</a>
                  </td>
                  <td>
                    <a href={`#/runs/${run.id}`}>{run.issue_description}</a>
                  </td>
                  <td>{run.repository_name}</td>
                  <td>
                    <Sha value={run.commit_sha} />
                  </td>
                  <td>
                    <Badge value={run.status} />
                  </td>
                  <td>{run.overall_result ? <Badge value={run.overall_result} /> : <span className="muted">—</span>}</td>
                  <td className="small">{formatTime(run.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
