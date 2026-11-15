export function CommandApproval(props: {
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
