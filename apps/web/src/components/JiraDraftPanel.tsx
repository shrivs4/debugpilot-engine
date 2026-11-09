import { useState } from "react";

import { api } from "../api";
import type { TicketDraft } from "../types";
import { Badge, ErrorBanner, Section, formatTime } from "./ui";

export function JiraDraftPanel({
  runId,
  draft,
  onCreated,
}: {
  runId: number;
  draft: TicketDraft | null;
  onCreated: (draft: TicketDraft) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createJiraDraft(runId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title="Mock Jira ticket"
      actions={
        <button type="button" onClick={create} disabled={busy}>
          {draft ? "Regenerate draft" : "Create Draft"}
        </button>
      }
    >
      <p className="hint">Mocked for Phase 1: drafts are stored locally and never sent to Jira.</p>
      <ErrorBanner message={error} />
      {draft ? (
        <div className="ticket" data-testid="jira-draft">
          <div className="ticket-header">
            <span className="ticket-key">{draft.ticket_key}</span>
            <Badge value={draft.status} />
            <span className="muted small">{formatTime(draft.created_at)}</span>
          </div>
          <h3>{draft.title}</h3>
          <dl className="facts">
            <dt>Summary</dt>
            <dd>{draft.summary}</dd>
            <dt>Root cause</dt>
            <dd>{draft.root_cause ?? "—"}</dd>
            <dt>File and line</dt>
            <dd>
              <code>
                {draft.file_path ?? "?"}:{draft.line_number ?? "?"}
              </code>
            </dd>
            <dt>Reproduction</dt>
            <dd>
              <ul className="plain">
                {Object.entries(draft.reproduction)
                  .filter(([, value]) => value !== null && value !== "")
                  .map(([key, value]) => (
                    <li key={key}>
                      <span className="muted">{key.replace(/_/g, " ")}:</span> <code>{String(value)}</code>
                    </li>
                  ))}
              </ul>
            </dd>
            <dt>Evidence</dt>
            <dd>
              <ul className="plain">
                {draft.evidence.map((item, index) => (
                  <li key={index}>
                    <span className="muted">{item.type.replace(/_/g, " ")}:</span> {item.label}
                    {item.similarity !== undefined && <span className="muted small"> (similarity {item.similarity.toFixed(3)})</span>}
                  </li>
                ))}
              </ul>
            </dd>
            <dt>Validation status</dt>
            <dd>{draft.validation_status ? <Badge value={draft.validation_status} /> : "—"}</dd>
          </dl>
        </div>
      ) : (
        <p className="muted">No draft yet. Create one from this run's hypothesis, evidence and validation result.</p>
      )}
    </Section>
  );
}
