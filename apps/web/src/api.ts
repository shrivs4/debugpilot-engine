import type { DebugRun, DebugRunReport, Health, Repository, TicketDraft } from "./types";

const BASE = "/api";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the DebugPilot API. Is the api container running?");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : response.statusText;
    throw new ApiError(response.status, message || `Request failed (${response.status})`);
  }
  return body as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => request<Health>("/health"),
  listRepositories: () => request<Repository[]>("/repositories"),
  addRepository: (url: string, branch: string | null) => post<Repository>("/repositories", { url, branch }),
  syncRepository: (id: number) => post<Repository>(`/repositories/${id}/sync`),
  listRuns: () => request<DebugRun[]>("/debug-runs"),
  createRun: (body: {
    repository_id: number;
    issue_description: string;
    setup_command: string | null;
    analysis_command: string;
  }) => post<DebugRun>("/debug-runs", body),
  approveCommand: (id: number, kind: "setup" | "analysis", command: string) =>
    post<DebugRun>(`/debug-runs/${id}/approve-command`, { kind, command }),
  startRun: (id: number) => post<DebugRun>(`/debug-runs/${id}/run`),
  report: (id: number) => request<DebugRunReport>(`/debug-runs/${id}/report`),
  createJiraDraft: (id: number) => post<TicketDraft>(`/debug-runs/${id}/jira-draft`),
};
