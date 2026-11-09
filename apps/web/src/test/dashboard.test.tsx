import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DebugRunDetailPage } from "../pages/DebugRunDetailPage";
import { NewDebugRunPage } from "../pages/NewDebugRunPage";
import { RepositoriesPage } from "../pages/RepositoriesPage";
import { completedReport, draftRun, mockFetch, repository } from "./fixtures";

describe("RepositoriesPage", () => {
  it("lists repositories with status, commit and index size", async () => {
    mockFetch({ "GET /repositories": [repository] });
    render(<RepositoriesPage />);

    const row = await screen.findByTestId("repository-1");
    expect(within(row).getByText("shrivs4/debugpilot-checkout-fixture")).toBeInTheDocument();
    expect(within(row).getByText("ready")).toBeInTheDocument();
    expect(within(row).getByText("6a872565")).toBeInTheDocument();
    expect(within(row).getByText("37 chunks / 14 files")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Sync" })).toBeEnabled();
  });

  it("submits a repository URL and branch", async () => {
    const calls = mockFetch({ "GET /repositories": [], "POST /repositories": repository });
    render(<RepositoriesPage />);

    await userEvent.type(screen.getByLabelText("Public GitHub URL"), repository.url);
    await userEvent.type(screen.getByLabelText("Branch (optional)"), "main");
    await userEvent.click(screen.getByRole("button", { name: "Add repository" }));

    await waitFor(() => expect(calls.some((call) => call.key === "POST /repositories")).toBe(true));
    expect(calls.find((call) => call.key === "POST /repositories")?.body).toEqual({ url: repository.url, branch: "main" });
  });

  it("shows API validation errors", async () => {
    mockFetch({ "GET /repositories": [] });
    render(<RepositoriesPage />);

    await userEvent.type(screen.getByLabelText("Public GitHub URL"), "https://gitlab.com/a/b");
    await userEvent.click(screen.getByRole("button", { name: "Add repository" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("unmocked POST /repositories");
  });
});

describe("NewDebugRunPage", () => {
  it("requires approving every command before the run button is enabled", async () => {
    const calls = mockFetch({
      "GET /repositories": [repository],
      "POST /debug-runs": draftRun,
      "POST /debug-runs/5/approve-command": (body: unknown) => {
        const { kind } = body as { kind: string };
        return kind === "setup"
          ? { ...draftRun, setup_command_approved_at: "2026-11-07T10:01:10Z" }
          : {
              ...draftRun,
              setup_command_approved_at: "2026-11-07T10:01:10Z",
              analysis_command_approved_at: "2026-11-07T10:01:12Z",
              ready_to_run: true,
            };
      },
    });
    render(<NewDebugRunPage />);

    await screen.findByRole("option", { name: /debugpilot-checkout-fixture/ });
    await userEvent.click(screen.getByRole("button", { name: "Fill checkout-fixture example" }));
    await userEvent.click(screen.getByRole("button", { name: "Review commands" }));

    expect(await screen.findAllByText("Run this command inside the selected repository?")).toHaveLength(2);
    const runButton = screen.getByRole("button", { name: "Run debug analysis" });
    expect(runButton).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Approve setup command" }));
    await screen.findByText(/Approved/);
    expect(runButton).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Approve analysis command" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Run debug analysis" })).toBeEnabled());

    const approvals = calls.filter((call) => call.key === "POST /debug-runs/5/approve-command").map((call) => call.body);
    expect(approvals).toEqual([
      { kind: "setup", command: "npm install" },
      { kind: "analysis", command: "npm run repro:legacy" },
    ]);
    expect(calls.some((call) => call.key === "POST /debug-runs/5/run")).toBe(false);
  });
});

describe("DebugRunDetailPage", () => {
  it("shows runtime evidence, citations, hypothesis and validation", async () => {
    mockFetch({ "GET /debug-runs/5/report": completedReport });
    render(<DebugRunDetailPage runId={5} />);

    expect(await screen.findByText("Debug run #5")).toBeInTheDocument();
    expect(screen.getByTestId("exit-code-analysis")).toHaveTextContent("1");
    expect(screen.getAllByText(/Cannot read properties of undefined/).length).toBeGreaterThan(0);
    expect(screen.getByText("0.677")).toBeInTheDocument();
    expect(screen.getByText("Legacy coupons crash checkout.")).toBeInTheDocument();
    expect(screen.getByText("reproduction failed")).toBeInTheDocument();
    expect(screen.getByText(/Overall result/)).toBeInTheDocument();
  });

  it("creates a mock Jira draft", async () => {
    mockFetch({
      "GET /debug-runs/5/report": completedReport,
      "POST /debug-runs/5/jira-draft": {
        id: 1,
        debug_run_id: 5,
        provider: "mock-jira",
        ticket_key: "MOCK-5",
        title: "[debugpilot-checkout-fixture] TypeError in src/checkout/discountService.ts:16",
        summary: "Legacy coupons crash checkout.",
        root_cause: "calculateDiscount reads coupon.discountType.",
        file_path: "src/checkout/discountService.ts",
        line_number: 16,
        reproduction: { analysis_command: "npm run repro:legacy", exit_code: 1 },
        evidence: [{ type: "git_commit", label: "6a872565 Add discountType" }],
        validation_status: "validated",
        status: "draft",
        created_at: "2026-11-07T10:03:00Z",
      },
    });
    render(<DebugRunDetailPage runId={5} />);

    await userEvent.click(await screen.findByRole("button", { name: "Create Draft" }));

    const ticket = await screen.findByTestId("jira-draft");
    expect(within(ticket).getByText("MOCK-5")).toBeInTheDocument();
    expect(within(ticket).getByText("src/checkout/discountService.ts:16")).toBeInTheDocument();
    expect(within(ticket).getByText(/6a872565 Add discountType/)).toBeInTheDocument();
  });
});
