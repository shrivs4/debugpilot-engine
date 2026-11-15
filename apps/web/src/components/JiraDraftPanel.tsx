import { useState } from "react";

import { api } from "../api";
import type { Hypothesis, TicketDraft, Validation } from "../types";
import { Badge, ErrorBanner, Section, formatTime } from "./ui";

type Ranked = { hypothesis: Hypothesis; validation: Validation | null };

const isValidated = (item: Ranked) => item.validation?.overall_result === "validated";

function label(item: Ranked) {
  const { hypothesis, validation } = item;
  const status = (validation?.overall_result ?? "not validated").replace(/_/g, " ");
  const name = `Hypothesis ${hypothesis.rank ?? 1}: ${hypothesis.symbol ?? hypothesis.file_path ?? "unnamed"}`;
  return isValidated(item) ? `${name} (validated)` : `${name} (${status}, not selectable)`;
}

/**
 * Mock Jira draft. Only a validated hypothesis can be selected (the API
 * enforces this too). Non-validated hypotheses are listed with their status
 * but disabled; with several validated ones the user picks one explicitly.
 */
export function JiraDraftPanel({
  runId,
  hypotheses,
  draft,
  onCreated,
}: {
  runId: number;
  hypotheses: Ranked[];
  draft: TicketDraft | null;
  onCreated: (draft: TicketDraft) => void;
}) {
  const validated = hypotheses.filter(isValidated);
  const validatedIds = new Set(validated.map((item) => String(item.hypothesis.id)));
  const multiple = hypotheses.length > 1;
  const [selectedId, setSelectedId] = useState<string>(() =>
    validated.length === 1 ? String(validated[0].hypothesis.id) : "",
  );
  const canCreate = validatedIds.has(selectedId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const draftHypothesis = hypotheses.find((item) => item.hypothesis.id === draft?.hypothesis_id);
  const draftIsValid = draft?.validation_status === "validated";

  const create = async () => {
    if (!canCreate) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createJiraDraft(runId, Number(selectedId)));
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
        <button type="button" onClick={create} disabled={busy || !canCreate}>
          {draft && draftIsValid ? "Regenerate draft" : "Create Draft"}
        </button>
      }
    >
      <p className="hint">Mocked for Phase 1: drafts are stored locally and never sent to Jira.</p>
      {validated.length === 0 && (
        <div className="banner banner-warn" data-testid="jira-blocked">
          Human review required: no hypothesis is validated, so a Jira draft cannot be created.
        </div>
      )}
      {multiple && (
        <label>
          Hypothesis for the ticket
          <select
            value={selectedId}
            onChange={(event) => setSelectedId(validatedIds.has(event.target.value) ? event.target.value : "")}
          >
            <option value="">{validated.length ? "Select a validated hypothesis" : "No validated hypothesis"}</option>
            {hypotheses.map((item) => (
              <option key={item.hypothesis.id} value={item.hypothesis.id} disabled={!isValidated(item)}>
                {label(item)}
              </option>
            ))}
          </select>
        </label>
      )}
      <ErrorBanner message={error} />
      {draft && !draftIsValid ? (
        <div className="banner banner-warn" data-testid="jira-draft-invalid">
          A stored draft ({draft.ticket_key}) was created from a hypothesis that is not validated. It is not a valid
          ticket; review the hypotheses instead.
        </div>
      ) : draft ? (
        <div className="ticket" data-testid="jira-draft">
          <div className="ticket-header">
            <span className="ticket-key">{draft.ticket_key}</span>
            <Badge value={draft.status} />
            <span className="muted small">{formatTime(draft.created_at)}</span>
          </div>
          <h3>{draft.title}</h3>
          <dl className="facts">
            {draftHypothesis && (
              <>
                <dt>Hypothesis</dt>
                <dd data-testid="jira-hypothesis">{label(draftHypothesis)}</dd>
              </>
            )}
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
