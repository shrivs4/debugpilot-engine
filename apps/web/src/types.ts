export type RepositoryStatus = "pending" | "cloning" | "syncing" | "indexing" | "ready" | "error";
export type RunStatus = "draft" | "queued" | "running" | "completed" | "failed";
export type CheckStatus = "passed" | "failed" | "skipped";
export type OverallResult = "validated" | "needs_review" | "rejected";
export type RunResult = OverallResult | "no_bug_detected";
export type RunType = "issue" | "commit_analysis";

export interface RepositorySync {
  id: number;
  kind: "clone" | "sync";
  status: "running" | "completed" | "failed";
  previous_commit_sha: string | null;
  new_commit_sha: string | null;
  commit_changed: boolean | null;
  changed_files: string[];
  index_strategy: string | null;
  index_plan: Record<string, number>;
  files_indexed: number | null;
  chunks_indexed: number | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Repository {
  id: number;
  url: string;
  owner: string;
  name: string;
  branch: string | null;
  status: RepositoryStatus;
  status_message: string | null;
  current_commit_sha: string | null;
  indexed_commit_sha: string | null;
  last_synced_at: string | null;
  last_indexed_at: string | null;
  created_at: string;
  file_count: number;
  chunk_count: number;
  latest_sync: RepositorySync | null;
}

export interface DebugRun {
  id: number;
  repository_id: number;
  repository_name: string | null;
  issue_description: string;
  setup_command: string | null;
  setup_command_approved_at: string | null;
  analysis_command: string | null;
  analysis_command_approved_at: string | null;
  run_type?: RunType;
  base_commit?: string | null;
  target_commit?: string | null;
  status: RunStatus;
  current_step: string | null;
  error: string | null;
  commit_sha: string | null;
  overall_result: RunResult | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  ready_to_run: boolean;
}

export interface StackFrame {
  function: string;
  path: string;
  relative_path: string | null;
  line: number;
  column: number;
  in_repository: boolean;
}

export interface StackTrace {
  error_type: string | null;
  error_message: string | null;
  frames: StackFrame[];
  top_repository_frame: StackFrame | null;
  raw: string;
}

export interface Mismatch {
  field: string;
  label: string;
  expected: string;
  actual: string;
  numeric_delta: number | null;
  ratio: number | null;
  source_line: string;
}

export interface MismatchAnalysis {
  proportional_groups: { ratio: number; fields: string[] }[];
  earliest_field: string | null;
  root_fields: string[];
  propagated_fields: string[];
  dependencies: Record<string, string[]>;
  method: "output_order" | "code_dependencies";
  notes: string[];
}

export interface StructuredRuntime {
  extracted: boolean;
  exit_code: number | null;
  timed_out: boolean;
  expected: Record<string, string>;
  actual: Record<string, string>;
  mismatches: Mismatch[];
  mismatched_fields: string[];
  errors: string[];
  locations: { file_path: string; line: number; function: string | null; source: string }[];
  structured_logs: { format: "json" | "logfmt"; data: Record<string, unknown> }[];
  mismatch_analysis: MismatchAnalysis;
}

export interface RuntimeEvidence {
  id: number;
  phase: "setup" | "analysis" | null;
  command: string | null;
  exit_code: number | null;
  success: boolean | null;
  stdout: string | null;
  stderr: string | null;
  stack_trace: StackTrace | null;
  data: {
    duration_ms?: number;
    timed_out?: boolean;
    error?: string | null;
    started_at?: string;
    structured?: StructuredRuntime | null;
  };
  captured_at: string;
}

export interface CodeEvidence {
  chunk_id: number;
  rank: number;
  file_path: string;
  symbol: string;
  chunk_type: string;
  language: string;
  start_line: number;
  end_line: number;
  semantic_score: number;
  lexical_score: number;
  combined_score: number;
  matched_keywords: string[];
  sources: string[];
  role?: "primary" | "related";
  reasons?: RetrievalReason[];
  relation?: Relation | null;
}

export interface RetrievalReason {
  signal: string;
  score: number;
  weight: number | null;
  contribution: number;
  detail: string;
}

export interface Relation {
  type: string;
  via: string;
  depth: number;
  from_chunk_id: number;
  from_symbol: string;
  from_file: string;
  path: string;
}

export interface GitEvidence {
  head_commit: string;
  head_summary: string | null;
  target: { file_path: string; line: number; source: string } | null;
  blame: string | null;
  suspect_commit: { commit: string; short: string; author?: string; summary?: string; line_content?: string } | null;
  suspect_commit_summary: string | null;
  suspect_diff: string | null;
  file_history: { commit: string; author: string; date: string; subject: string }[];
  errors: string[];
}

export interface Hypothesis {
  id: number;
  model: string;
  summary: string | null;
  root_cause: string | null;
  file_path: string | null;
  line_number: number | null;
  symbol: string | null;
  introducing_commit: string | null;
  confidence: string | null;
  execution_path: string[];
  missing_evidence: string[];
  affected_fields?: string[];
  rank?: number;
  parse_ok: boolean;
  raw_response: string;
  duration_ms: number | null;
}

export interface ValidationCheck {
  name: string;
  status: CheckStatus;
  details: string;
  required: boolean;
  category?: "runtime" | "behavior" | "evidence" | "conflict";
}

export interface Validation {
  id: number;
  overall_result: OverallResult;
  checks: ValidationCheck[];
}

export interface TicketDraft {
  id: number;
  debug_run_id: number;
  hypothesis_id?: number | null;
  provider: string;
  ticket_key: string;
  title: string;
  summary: string;
  root_cause: string | null;
  file_path: string | null;
  line_number: number | null;
  reproduction: Record<string, string | number | null>;
  evidence: { type: string; label: string; similarity?: number; combined_score?: number }[];
  validation_status: string | null;
  status: string;
  created_at: string;
}

export interface DebugRunReport {
  run: DebugRun;
  repository: Repository;
  runtime_evidence: RuntimeEvidence[];
  code_evidence: CodeEvidence[];
  git_evidence: GitEvidence | null;
  hypothesis: Hypothesis | null;
  validation: Validation | null;
  ticket_draft: TicketDraft | null;
  hypotheses?: { hypothesis: Hypothesis; validation: Validation | null }[];
  commit_analysis?: CommitAnalysis | null;
  conclusion?: { result: "no_bug_detected"; message: string; command?: string | null } | null;
}

export interface Commit {
  sha: string;
  short: string;
  parents: string[];
  author: string;
  date: string;
  subject: string;
}

export interface CommitAnalysis {
  base?: string | null;
  head?: string;
  mode?: "commit" | "range";
  notes?: string | null;
  commits?: { sha: string; short: string; author: string; date: string; subject: string }[];
  changed_files?: { status: string; file_path: string; symbols: string[]; line_ranges: [number, number][] }[];
  diff_truncated?: boolean;
}

export interface CommitAnalysisCreate {
  repository_id: number;
  commit_sha?: string;
  base_sha?: string;
  head_sha?: string;
  setup_command: string | null;
  analysis_command: string | null;
  notes: string | null;
}

export interface Health {
  status: "ok" | "degraded" | "error";
  database: { ok: boolean; pgvector: string | null; error: string | null };
  ollama: { ok: boolean; base_url: string; version: string | null; models: Record<string, boolean>; error: string | null };
  models: { embedding: string; generation: string };
}
