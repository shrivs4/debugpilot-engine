import { vi } from "vitest";

import type { Commit, DebugRun, DebugRunReport, Hypothesis, Repository, Validation } from "../types";

export const repository: Repository = {
  id: 1,
  url: "https://github.com/shrivs4/debugpilot-checkout-fixture",
  owner: "shrivs4",
  name: "debugpilot-checkout-fixture",
  branch: "main",
  status: "ready",
  status_message: "Indexed 37 chunks from 14 files",
  current_commit_sha: "6a872565cbdc3aa55449eb220658152b18355505",
  indexed_commit_sha: "6a872565cbdc3aa55449eb220658152b18355505",
  last_synced_at: "2026-11-07T10:00:00Z",
  last_indexed_at: "2026-11-07T10:00:05Z",
  created_at: "2026-11-07T10:00:00Z",
  file_count: 14,
  chunk_count: 37,
  latest_sync: null,
};

export const draftRun: DebugRun = {
  id: 5,
  repository_id: 1,
  repository_name: "debugpilot-checkout-fixture",
  issue_description: "Checkout fails for legacy coupons",
  setup_command: "npm install",
  setup_command_approved_at: null,
  analysis_command: "npm run repro:legacy",
  analysis_command_approved_at: null,
  status: "draft",
  current_step: null,
  error: null,
  commit_sha: null,
  overall_result: null,
  created_at: "2026-11-07T10:01:00Z",
  started_at: null,
  finished_at: null,
  ready_to_run: false,
};

export const completedReport: DebugRunReport = {
  run: {
    ...draftRun,
    status: "completed",
    current_step: "done",
    commit_sha: repository.current_commit_sha,
    overall_result: "validated",
    setup_command_approved_at: "2026-11-07T10:01:10Z",
    analysis_command_approved_at: "2026-11-07T10:01:12Z",
    started_at: "2026-11-07T10:01:13Z",
    finished_at: "2026-11-07T10:02:00Z",
  },
  repository,
  runtime_evidence: [
    {
      id: 11,
      phase: "analysis",
      command: "npm run repro:legacy",
      exit_code: 1,
      success: false,
      stdout: "> repro:legacy",
      stderr: "TypeError: Cannot read properties of undefined (reading 'toUpperCase')",
      stack_trace: {
        error_type: "TypeError",
        error_message: "Cannot read properties of undefined (reading 'toUpperCase')",
        frames: [
          {
            function: "calculateDiscount",
            path: "/workspace/repos/1/src/checkout/discountService.ts",
            relative_path: "src/checkout/discountService.ts",
            line: 16,
            column: 46,
            in_repository: true,
          },
        ],
        top_repository_frame: null,
        raw: "",
      },
      data: { duration_ms: 1200 },
      captured_at: "2026-11-07T10:01:40Z",
    },
  ],
  code_evidence: [
    {
      chunk_id: 3,
      rank: 1,
      file_path: "src/checkout/discountService.ts",
      symbol: "calculateDiscount",
      chunk_type: "function",
      language: "typescript",
      start_line: 4,
      end_line: 27,
      semantic_score: 0.71,
      lexical_score: 0.6,
      combined_score: 0.677,
      matched_keywords: ["discountType"],
      sources: ["semantic", "stack_trace"],
    },
  ],
  git_evidence: null,
  hypothesis: {
    id: 2,
    model: "qwen2.5-coder:7b",
    summary: "Legacy coupons crash checkout.",
    root_cause: "calculateDiscount reads coupon.discountType, which legacy coupons do not have.",
    file_path: "src/checkout/discountService.ts",
    line_number: 16,
    symbol: "calculateDiscount",
    introducing_commit: repository.current_commit_sha,
    confidence: "high",
    execution_path: [],
    missing_evidence: [],
    parse_ok: true,
    raw_response: "{}",
    duration_ms: 9000,
  },
  validation: {
    id: 3,
    overall_result: "validated",
    checks: [
      { name: "reproduction_failed", status: "passed", details: "Exit code: 1", required: true },
      { name: "hypothesis_line_matches_runtime", status: "passed", details: "16 vs 16", required: true },
    ],
  },
  ticket_draft: null,
};

export const commits: Commit[] = [
  { sha: "c3".padEnd(40, "3"), short: "c3333333", parents: ["c2".padEnd(40, "2")], author: "Ana", date: "2026-11-08T09:00:00Z", subject: "Add readme" },
  { sha: "c2".padEnd(40, "2"), short: "c2222222", parents: ["c1".padEnd(40, "1")], author: "Ana", date: "2026-11-07T09:00:00Z", subject: "Simplify line merging" },
  { sha: "c1".padEnd(40, "1"), short: "c1111111", parents: [], author: "Ana", date: "2026-11-06T09:00:00Z", subject: "Basket and pricing" },
];

export const commitRun: DebugRun = {
  ...draftRun,
  id: 9,
  repository_name: "till",
  issue_description: "Regression analysis of commit c2222222",
  setup_command: null,
  analysis_command: "node scripts/check.js",
  run_type: "commit_analysis",
  base_commit: commits[2].sha,
  target_commit: commits[1].sha,
};

const upstream: Hypothesis = {
  ...completedReport.hypothesis!,
  id: 21,
  rank: 1,
  summary: "Units are overwritten when a SKU is added twice.",
  root_cause: "putLine replaces found.units instead of adding to it.",
  file_path: "src/basket.js",
  line_number: 8,
  symbol: "putLine",
  introducing_commit: commits[1].sha,
  affected_fields: ["gross", "reduction", "net"],
};
const downstream: Hypothesis = {
  ...upstream,
  id: 22,
  rank: 2,
  summary: "The reduction is miscalculated.",
  root_cause: "rebate uses the wrong rate.",
  file_path: "src/pricing.js",
  line_number: 6,
  symbol: "rebate",
  affected_fields: ["reduction"],
};
const validated: Validation = {
  id: 31,
  overall_result: "validated",
  checks: [
    { name: "affects_mismatched_field", status: "passed", details: "sumLines -[shared_property:units]-> putLine (feeds gross)", required: true, category: "behavior" },
  ],
};
const rejected: Validation = {
  id: 32,
  overall_result: "rejected",
  checks: [
    { name: "consistent_with_stronger_evidence", status: "failed", details: "rebate only affects reduction, net", required: true, category: "conflict" },
  ],
};

export const commitReport: DebugRunReport = {
  ...completedReport,
  run: { ...completedReport.run, ...commitRun, status: "completed", current_step: "done", overall_result: "validated" },
  runtime_evidence: [
    {
      ...completedReport.runtime_evidence[0],
      command: "node scripts/check.js",
      stack_trace: null,
      stderr: "",
      stdout: "expected gross: 55\nactual gross: 35",
      data: {
        duration_ms: 300,
        structured: {
          extracted: true,
          exit_code: 1,
          timed_out: false,
          expected: { gross: "55", reduction: "5.5" },
          actual: { gross: "35", reduction: "3.5" },
          mismatches: [
            { field: "gross", label: "gross", expected: "55", actual: "35", numeric_delta: -20, ratio: 0.636364, source_line: "actual gross: 35" },
            { field: "reduction", label: "reduction", expected: "5.5", actual: "3.5", numeric_delta: -2, ratio: 0.636364, source_line: "actual reduction: 3.5" },
          ],
          mismatched_fields: ["gross", "reduction"],
          errors: [],
          locations: [],
          structured_logs: [],
          mismatch_analysis: {
            proportional_groups: [{ ratio: 0.636364, fields: ["gross", "reduction"] }],
            earliest_field: "gross",
            root_fields: ["gross"],
            propagated_fields: ["reduction"],
            dependencies: { gross: [], reduction: ["gross"] },
            method: "code_dependencies",
            notes: ["Fields gross, reduction all deviate from the expected values by the same factor (0.6364)."],
          },
        },
      },
    },
  ],
  code_evidence: [
    {
      chunk_id: -1,
      rank: 1,
      file_path: "src/basket.js",
      symbol: "putLine",
      chunk_type: "function",
      language: "javascript",
      start_line: 5,
      end_line: 13,
      semantic_score: 0,
      lexical_score: 0,
      combined_score: 0.31,
      matched_keywords: [],
      sources: ["changed_in_commit", "git_change"],
      role: "primary",
      reasons: [{ signal: "git_change", score: 1, weight: 0.1, contribution: 0.1, detail: "changed lines 8-8 fall inside this chunk" }],
    },
    {
      chunk_id: 7,
      rank: 2,
      file_path: "src/pricing.js",
      symbol: "sumLines",
      chunk_type: "function",
      language: "javascript",
      start_line: 1,
      end_line: 3,
      semantic_score: 0,
      lexical_score: 0,
      combined_score: 0.25,
      matched_keywords: [],
      sources: ["relationship"],
      role: "related",
      relation: { type: "property_read_by", via: "units", depth: 1, from_chunk_id: -1, from_symbol: "putLine", from_file: "src/basket.js", path: "putLine -[property_read_by:units]-> sumLines" },
    },
  ],
  hypothesis: upstream,
  validation: validated,
  hypotheses: [
    { hypothesis: upstream, validation: validated },
    { hypothesis: downstream, validation: rejected },
  ],
  commit_analysis: {
    base: commits[2].sha,
    head: commits[1].sha,
    mode: "commit",
    notes: null,
    commits: [{ sha: commits[1].sha, short: commits[1].short, author: "Ana", date: commits[1].date, subject: "Simplify line merging" }],
    changed_files: [{ status: "M", file_path: "src/basket.js", symbols: ["putLine"], line_ranges: [[8, 8]] }],
    diff_truncated: false,
  },
};

/** A mocked non-2xx API response: `{detail}` with the given status. */
export class HttpError {
  constructor(public status: number, public detail: string) {}
}

/** Route fetch calls by "METHOD /path" to canned JSON responses. */
export function mockFetch(routes: Record<string, unknown | ((body: unknown) => unknown)>) {
  const calls: { key: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(/^\/api/, "");
    const key = `${init?.method ?? "GET"} ${path}`;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ key, body });
    if (!(key in routes)) return new Response(JSON.stringify({ detail: `unmocked ${key}` }), { status: 404 });
    const value = routes[key];
    const payload = typeof value === "function" ? (value as (body: unknown) => unknown)(body) : value;
    if (payload instanceof HttpError) {
      return new Response(JSON.stringify({ detail: payload.detail }), { status: payload.status });
    }
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}
