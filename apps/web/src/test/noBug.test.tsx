import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DebugRunDetailPage } from "../pages/DebugRunDetailPage";
import type { DebugRunReport } from "../types";
import { completedReport, mockFetch } from "./fixtures";

const MESSAGE =
  "No bug detected: the reproduction command succeeded (exit code 0) and reported no mismatched values, errors or stack traces, so there is no failing behavior to diagnose.";

const noBug: DebugRunReport = {
  ...completedReport,
  run: { ...completedReport.run, overall_result: "no_bug_detected" },
  runtime_evidence: [{ ...completedReport.runtime_evidence[0], exit_code: 0, success: true, stack_trace: null, stderr: "" }],
  code_evidence: [],
  hypothesis: null,
  validation: null,
  hypotheses: [],
  conclusion: { result: "no_bug_detected", message: MESSAGE, command: "npm run repro:json" },
};

describe("a run with no failing behavior", () => {
  it("states that no bug was detected and offers no hypothesis or Jira draft", async () => {
    mockFetch({ "GET /debug-runs/5/report": noBug });
    render(<DebugRunDetailPage runId={5} />);

    expect(await screen.findByTestId("run-conclusion")).toHaveTextContent(MESSAGE);
    expect(screen.getAllByText("no bug detected").length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { name: "LLM hypothesis" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Mock Jira ticket" })).not.toBeInTheDocument();
    expect(screen.getByText("LLM hypothesis")).toHaveClass("step-skipped");
    expect(screen.getByText("Analysis command")).toHaveClass("step-done");
  });
});
