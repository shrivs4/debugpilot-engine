import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { CommitAnalysisPage } from "../pages/CommitAnalysisPage";
import { DebugRunDetailPage } from "../pages/DebugRunDetailPage";
import { commitReport, commitRun, commits, mockFetch, repository } from "./fixtures";

const routes = {
  "GET /repositories": [repository],
  "GET /repositories/1/commits?limit=50": commits,
};

describe("CommitAnalysisPage", () => {
  it("loads commits and submits a single-commit analysis that still needs approval", async () => {
    const calls = mockFetch({
      ...routes,
      "POST /commit-analyses": commitRun,
      "POST /debug-runs/9/approve-command": { ...commitRun, analysis_command_approved_at: "2026-11-08T10:00:00Z", ready_to_run: true },
    });
    render(<CommitAnalysisPage />);

    await screen.findByRole("option", { name: "c2222222 Simplify line merging" });
    await userEvent.selectOptions(screen.getByLabelText("Commit"), commits[1].sha);
    await userEvent.type(screen.getByLabelText("Analysis / reproduction command (optional)"), "node scripts/check.js");
    await userEvent.click(screen.getByRole("button", { name: "Review analysis" }));

    expect(await screen.findByText("Run this command inside the selected repository?")).toBeInTheDocument();
    expect(calls.find((call) => call.key === "POST /commit-analyses")?.body).toEqual({
      repository_id: 1,
      commit_sha: commits[1].sha,
      setup_command: null,
      analysis_command: "node scripts/check.js",
      notes: null,
    });
    const runButton = screen.getByRole("button", { name: "Run commit analysis" });
    expect(runButton).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Approve analysis command" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Run commit analysis" })).toBeEnabled());
    expect(calls.find((call) => call.key === "POST /debug-runs/9/approve-command")?.body).toEqual({
      kind: "analysis",
      command: "node scripts/check.js",
    });
    expect(calls.some((call) => call.key === "POST /debug-runs/9/run")).toBe(false);
  });

  it("submits a commit range with notes and no commands", async () => {
    const rangeRun = { ...commitRun, analysis_command: null, ready_to_run: true };
    const calls = mockFetch({ ...routes, "POST /commit-analyses": rangeRun, "POST /debug-runs/9/run": rangeRun });
    render(<CommitAnalysisPage />);

    await screen.findByRole("option", { name: "c3333333 Add readme" });
    await userEvent.click(screen.getByLabelText("Commit range"));
    await userEvent.selectOptions(screen.getByLabelText("Base commit (excluded)"), commits[2].sha);
    await userEvent.selectOptions(screen.getByLabelText("Head commit (included)"), commits[0].sha);
    await userEvent.type(screen.getByLabelText("Notes (optional)"), "Totals changed this week");
    await userEvent.click(screen.getByRole("button", { name: "Review analysis" }));

    expect(await screen.findByText(/validation cannot go beyond/)).toBeInTheDocument();
    expect(calls.find((call) => call.key === "POST /commit-analyses")?.body).toEqual({
      repository_id: 1,
      base_sha: commits[2].sha,
      head_sha: commits[0].sha,
      setup_command: null,
      analysis_command: null,
      notes: "Totals changed this week",
    });

    await userEvent.click(screen.getByRole("button", { name: "Run commit analysis" }));
    await waitFor(() => expect(calls.some((call) => call.key === "POST /debug-runs/9/run")).toBe(true));
    expect(window.location.hash).toBe("#/runs/9");
  });

  it("disables review when the range is empty and shows API errors", async () => {
    mockFetch(routes);
    render(<CommitAnalysisPage />);

    await screen.findByRole("option", { name: "c3333333 Add readme" });
    await userEvent.click(screen.getByLabelText("Commit range"));
    await userEvent.selectOptions(screen.getByLabelText("Base commit (excluded)"), commits[0].sha);
    expect(screen.getByRole("button", { name: "Review analysis" })).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText("Base commit (excluded)"), commits[1].sha);
    await userEvent.click(screen.getByRole("button", { name: "Review analysis" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("unmocked POST /commit-analyses");
  });
});

describe("DebugRunDetailPage for a commit analysis", () => {
  it("shows commits, mismatches, retrieval reasons, related evidence and every hypothesis", async () => {
    mockFetch({ "GET /debug-runs/9/report": commitReport });
    render(<DebugRunDetailPage runId={9} />);

    expect(await screen.findByText("Commit analysis #9")).toBeInTheDocument();
    expect(within(screen.getByTestId("analyzed-commits")).getByText("Simplify line merging")).toBeInTheDocument();
    expect(within(screen.getByTestId("changed-files")).getByText(/putLine/)).toBeInTheDocument();

    const mismatches = screen.getByTestId("mismatches");
    expect(within(mismatches).getByText("root")).toBeInTheDocument();
    expect(within(mismatches).getByText("propagated from gross")).toBeInTheDocument();

    expect(screen.getByText(/changed lines 8-8 fall inside this chunk/)).toBeInTheDocument();
    expect(within(screen.getByTestId("related-evidence")).getByText("putLine -[property_read_by:units]-> sumLines")).toBeInTheDocument();

    const first = screen.getByTestId("hypothesis-1");
    expect(within(first).getByText("Hypothesis 1 of 2")).toBeInTheDocument();
    expect(within(first).getAllByText("validated").length).toBeGreaterThan(0);
    const second = screen.getByTestId("hypothesis-2");
    expect(within(second).getByText("The reduction is miscalculated.")).toBeInTheDocument();
    expect(within(second).getAllByText("rejected").length).toBeGreaterThan(0);
    expect(within(second).getByText("(conflict check)", { exact: false })).toBeInTheDocument();
  });
});

describe("DebugRunDetailPage for a commit analysis with no failing behavior", () => {
  it("states that no bug was detected and shows no hypotheses or Jira panel", async () => {
    const message = "No bug detected: the analysis command succeeded and reported no mismatches, errors, or stack traces.";
    mockFetch({
      "GET /debug-runs/9/report": {
        ...commitReport,
        run: { ...commitReport.run, overall_result: "no_bug_detected" },
        runtime_evidence: [{ ...commitReport.runtime_evidence[0], exit_code: 0, success: true, data: { duration_ms: 200 } }],
        code_evidence: [],
        hypothesis: null,
        validation: null,
        hypotheses: [],
        conclusion: { result: "no_bug_detected", message, command: "node scripts/check.js" },
      },
    });
    render(<DebugRunDetailPage runId={9} />);

    expect(await screen.findByText("Commit analysis #9")).toBeInTheDocument();
    expect(screen.getByTestId("run-conclusion")).toHaveTextContent(message);
    expect(within(screen.getByTestId("analyzed-commits")).getByText("Simplify line merging")).toBeInTheDocument();
    expect(screen.getByText("Analysis command")).toHaveClass("step-done");
    expect(screen.getByText("LLM hypotheses")).toHaveClass("step-skipped");
    expect(screen.queryByTestId("hypothesis-1")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Mock Jira ticket" })).not.toBeInTheDocument();
  });
});
