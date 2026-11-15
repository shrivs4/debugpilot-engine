import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DebugRunDetailPage } from "../pages/DebugRunDetailPage";
import type { DebugRunReport, TicketDraft } from "../types";
import { HttpError, commitReport, completedReport, mockFetch } from "./fixtures";

const BLOCKED = "Human review required: hypothesis 1 (calculateDiscount) is needs review. A Jira draft is only created for a validated hypothesis.";

const needsReview: DebugRunReport = {
  ...completedReport,
  run: { ...completedReport.run, overall_result: "needs_review" },
  validation: { ...completedReport.validation!, overall_result: "needs_review" },
};

const noneValidated: DebugRunReport = {
  ...commitReport,
  run: { ...commitReport.run, overall_result: "needs_review" },
  hypotheses: commitReport.hypotheses!.map((item) => ({
    ...item,
    validation: { ...item.validation!, overall_result: item.hypothesis.rank === 1 ? "needs_review" : "rejected" },
  })),
};

const ticket: TicketDraft = {
  id: 1,
  debug_run_id: 9,
  hypothesis_id: 21,
  provider: "mock-jira",
  ticket_key: "MOCK-9",
  title: "[till] Units are overwritten",
  summary: "Units are overwritten when a SKU is added twice.",
  root_cause: "putLine replaces found.units.",
  file_path: "src/basket.js",
  line_number: 8,
  reproduction: { analysis_command: "node scripts/check.js" },
  evidence: [],
  validation_status: "validated",
  status: "draft",
  created_at: "2026-11-08T10:05:00Z",
};

const [validatedItem, rejectedItem] = commitReport.hypotheses!;
const needsReviewItem = {
  hypothesis: { ...validatedItem.hypothesis, id: 23, rank: 3, symbol: "sumLines", summary: "sumLines rounds early." },
  validation: { ...validatedItem.validation!, id: 33, overall_result: "needs_review" as const },
};
const mixed: DebugRunReport = { ...commitReport, hypotheses: [validatedItem, rejectedItem, needsReviewItem] };

function option(name: RegExp) {
  return screen.getByRole("option", { name }) as HTMLOptionElement;
}

describe("Jira safety gate", () => {
  it("lets a validated hypothesis be selected and creates its draft", async () => {
    const calls = mockFetch({ "GET /debug-runs/9/report": mixed, "POST /debug-runs/9/jira-draft": ticket });
    render(<DebugRunDetailPage runId={9} />);

    const select = await screen.findByLabelText("Hypothesis for the ticket");
    const validatedOption = option(/Hypothesis 1: putLine \(validated\)/);
    expect(validatedOption.disabled).toBe(false);
    await userEvent.selectOptions(select, "21");
    expect(select).toHaveValue("21");

    await userEvent.click(screen.getByRole("button", { name: "Create Draft" }));

    const draft = await screen.findByTestId("jira-draft");
    expect(within(draft).getByTestId("jira-hypothesis")).toHaveTextContent("Hypothesis 1: putLine (validated)");
    await waitFor(() =>
      expect(calls.find((call) => call.key === "POST /debug-runs/9/jira-draft")?.body).toEqual({ hypothesis_id: 21 }),
    );
  });

  it("shows needs_review and rejected hypotheses with their status but does not allow selecting them", async () => {
    const calls = mockFetch({ "GET /debug-runs/9/report": mixed });
    render(<DebugRunDetailPage runId={9} />);

    const select = await screen.findByLabelText("Hypothesis for the ticket");
    const needsReviewOption = option(/Hypothesis 3: sumLines \(needs review, not selectable\)/);
    const rejectedOption = option(/Hypothesis 2: rebate \(rejected, not selectable\)/);
    expect(needsReviewOption.disabled).toBe(true);
    expect(rejectedOption.disabled).toBe(true);

    // Even if a disabled option is forced (e.g. via devtools), the selection is ignored.
    for (const id of ["23", "22"]) {
      fireEvent.change(select, { target: { value: id } });
      expect(select).toHaveValue("");
      expect(screen.getByRole("button", { name: "Create Draft" })).toBeDisabled();
    }
    expect(calls.some((call) => call.key === "POST /debug-runs/9/jira-draft")).toBe(false);
  });

  it("blocks creation when no hypothesis is validated", async () => {
    const calls = mockFetch({ "GET /debug-runs/9/report": noneValidated });
    render(<DebugRunDetailPage runId={9} />);

    expect(await screen.findByTestId("jira-blocked")).toHaveTextContent("Human review required");
    expect(screen.getByLabelText("Hypothesis for the ticket")).toHaveValue("");
    expect(option(/Hypothesis 1: putLine \(needs review, not selectable\)/).disabled).toBe(true);
    expect(option(/Hypothesis 2: rebate \(rejected, not selectable\)/).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "Create Draft" })).toBeDisabled();
    expect(calls.some((call) => call.key === "POST /debug-runs/9/jira-draft")).toBe(false);
  });

  it("blocks a single needs_review hypothesis without sending a request", async () => {
    const calls = mockFetch({ "GET /debug-runs/5/report": needsReview });
    render(<DebugRunDetailPage runId={5} />);

    expect(await screen.findByTestId("jira-blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create Draft" })).toBeDisabled();
    expect(calls.some((call) => call.key === "POST /debug-runs/5/jira-draft")).toBe(false);
    expect(screen.queryByTestId("jira-draft")).not.toBeInTheDocument();
  });

  it("shows the API's human-review message if the server refuses a hypothesis the page saw as validated", async () => {
    mockFetch({
      "GET /debug-runs/5/report": completedReport,
      "POST /debug-runs/5/jira-draft": new HttpError(409, BLOCKED),
    });
    render(<DebugRunDetailPage runId={5} />);

    await userEvent.click(await screen.findByRole("button", { name: "Create Draft" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(BLOCKED);
    expect(screen.queryByTestId("jira-draft")).not.toBeInTheDocument();
  });

  it("does not present a stored unvalidated draft as a ticket", async () => {
    mockFetch({
      "GET /debug-runs/5/report": {
        ...needsReview,
        ticket_draft: { ...ticket, debug_run_id: 5, hypothesis_id: 2, validation_status: "needs_review" },
      },
    });
    render(<DebugRunDetailPage runId={5} />);

    expect(await screen.findByTestId("jira-draft-invalid")).toHaveTextContent("not a valid ticket");
    expect(screen.queryByTestId("jira-draft")).not.toBeInTheDocument();
  });
});
