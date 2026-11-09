import type { ReactNode } from "react";

const TONE: Record<string, string> = {
  ready: "good",
  completed: "good",
  validated: "good",
  passed: "good",
  ok: "good",
  error: "bad",
  failed: "bad",
  degraded: "warn",
  needs_review: "warn",
  draft: "neutral",
  skipped: "neutral",
};

export function Badge({ value, label }: { value: string; label?: string }) {
  const tone = TONE[value] ?? "busy";
  return <span className={`badge badge-${tone}`}>{label ?? value.replace(/_/g, " ")}</span>;
}

export function Sha({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">—</span>;
  return (
    <code className="sha" title={value}>
      {value.slice(0, 8)}
    </code>
  );
}

export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <header className="card-header">
        <h2>{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="banner banner-error" role="alert">
      {message}
    </div>
  );
}

export function Pre({ children, empty = "(empty)" }: { children: string | null | undefined; empty?: string }) {
  return <pre className="log">{children && children.trim() ? children : empty}</pre>;
}

export function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}
