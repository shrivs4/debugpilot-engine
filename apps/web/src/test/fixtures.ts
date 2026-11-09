import { vi } from "vitest";

import type { DebugRun, DebugRunReport, Repository } from "../types";

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
    return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}
