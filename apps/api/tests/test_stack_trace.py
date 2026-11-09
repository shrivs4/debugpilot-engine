from pathlib import Path

from app.services.stack_trace import parse_stack_trace


ROOT = Path("/workspace/repos/1-shrivs4__debugpilot-checkout-fixture")

# Shape of the real `npm run repro:legacy` output captured by the POC.
NODE_STDERR = f"""{ROOT}/src/checkout/discountService.ts:16
  const normalizedType = coupon.discountType.toUpperCase();
                                             ^


TypeError: Cannot read properties of undefined (reading 'toUpperCase')
    at calculateDiscount ({ROOT}/src/checkout/discountService.ts:16:46)
    at checkout ({ROOT}/src/checkout/checkoutService.ts:38:16)
    at <anonymous> ({ROOT}/src/reproduceLegacyBug.ts:34:16)
    at ModuleJob.run (node:internal/modules/esm/module_job:345:25)
    at async asyncRunEntryPointWithESMLoader (node:internal/modules/run_main:117:5)

Node.js v22.20.0
"""


def test_parses_node_stack_trace_into_repository_relative_frames():
    trace = parse_stack_trace(NODE_STDERR, ROOT)

    assert trace["error_type"] == "TypeError"
    assert trace["error_message"] == "Cannot read properties of undefined (reading 'toUpperCase')"
    assert trace["header"]["line"] == 16
    assert [frame["relative_path"] for frame in trace["frames"][:3]] == [
        "src/checkout/discountService.ts",
        "src/checkout/checkoutService.ts",
        "src/reproduceLegacyBug.ts",
    ]
    assert trace["frames"][3]["in_repository"] is False
    top = trace["top_repository_frame"]
    assert (top["function"], top["line"], top["column"]) == ("calculateDiscount", 16, 46)


def test_parses_file_urls_and_vitest_frames():
    esm = parse_stack_trace(f"Error: boom\n    at file://{ROOT}/src/a.js:3:7\n", ROOT)
    vitest = parse_stack_trace("TypeError: x is undefined\n ❯ src/checkout/discountService.ts:16:46\n", ROOT)

    assert esm["top_repository_frame"]["relative_path"] == "src/a.js"
    assert vitest["top_repository_frame"]["relative_path"] == "src/checkout/discountService.ts"


def test_returns_none_without_error_or_frames():
    assert parse_stack_trace("all good\n", ROOT) is None
    assert parse_stack_trace("", ROOT) is None
