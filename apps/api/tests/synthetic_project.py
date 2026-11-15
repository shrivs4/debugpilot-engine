"""A small invented JavaScript project with a wrong-totals regression.

`putLine` should add units when the same SKU is added twice; the regression
commit replaces the units instead. Every derived value (gross, reduction, net)
is then wrong by the same factor, and the reduction / net code is correct, so
a hypothesis blaming them describes a symptom, not the cause.
"""

PACKAGE_JSON = '{"type": "module", "scripts": {"check": "node scripts/check.js"}}\n'

BASKET_CORRECT = """export function createBasket() {
  return { lines: [] };
}

export function putLine(basket, sku, unitCost, units) {
  const found = basket.lines.find((line) => line.sku === sku);
  if (found) {
    found.units += units;
    return basket;
  }
  basket.lines.push({ sku, unitCost, units });
  return basket;
}
"""

BASKET_REGRESSION = BASKET_CORRECT.replace("found.units += units;", "found.units = units;")
BUG_LINE = 8  # `found.units = units;` in src/basket.js

PRICING = """export function sumLines(basket) {
  return basket.lines.reduce((acc, line) => acc + line.unitCost * line.units, 0);
}

export function rebate(gross, rate) {
  return Math.round(gross * rate * 100) / 100;
}

export function settle(basket, rate) {
  const gross = sumLines(basket);
  const reduction = rebate(gross, rate);
  const net = gross - reduction;
  return { gross, reduction, net };
}
"""
REBATE_LINE = 6

CHECK_SCRIPT = """import { createBasket, putLine } from '../src/basket.js';
import { settle } from '../src/pricing.js';

const basket = createBasket();
putLine(basket, 'pen', 10, 2);
putLine(basket, 'pen', 10, 3);
putLine(basket, 'pad', 5, 1);
const result = settle(basket, 0.1);
const expected = { gross: 55, reduction: 5.5, net: 49.5 };
let failed = false;
for (const key of Object.keys(expected)) {
  console.log(`expected ${key}: ${expected[key]}`);
  console.log(`actual ${key}: ${result[key]}`);
  if (result[key] !== expected[key]) failed = true;
}
process.exit(failed ? 1 : 0);
"""

CHECK_OUTPUT = """expected gross: 55
actual gross: 35
expected reduction: 5.5
actual reduction: 3.5
expected net: 49.5
actual net: 31.5
"""

FILES_CORRECT = {
    "package.json": PACKAGE_JSON,
    "src/basket.js": BASKET_CORRECT,
    "src/pricing.js": PRICING,
    "scripts/check.js": CHECK_SCRIPT,
}


def upstream_hypothesis(commit: str) -> dict:
    return {
        "summary": "Gross, reduction and net are all too low because units are lost when a SKU is added twice.",
        "root_cause": "putLine overwrites found.units with the new units instead of adding them, so gross is computed "
        "from too few units and reduction and net inherit the error.",
        "file_path": "src/basket.js",
        "line": BUG_LINE,
        "symbol": "putLine",
        "execution_path": ["scripts/check.js:6 <module>", "src/basket.js:8 putLine", "src/pricing.js:2 sumLines"],
        "introducing_commit": commit,
        "confidence": "medium",
        "missing_evidence": [],
        "affected_fields": ["gross", "reduction", "net"],
    }


def downstream_hypothesis(commit: str) -> dict:
    return {
        "summary": "The reduction is miscalculated.",
        "root_cause": "rebate computes the reduction with the wrong rate, so reduction and net are wrong.",
        "file_path": "src/pricing.js",
        "line": REBATE_LINE,
        "symbol": "rebate",
        "execution_path": ["scripts/check.js:8 <module>", "src/pricing.js:11 settle", "src/pricing.js:6 rebate"],
        "introducing_commit": commit,
        "confidence": "high",
        "missing_evidence": [],
        "affected_fields": ["reduction", "net"],
    }
