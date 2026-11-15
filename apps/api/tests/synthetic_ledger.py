"""A larger invented JavaScript project for vague "wrong totals" reports.

Timesheet entries are recorded through a service object; recording the same
code twice should accumulate hours, but the regression overwrites them. The
invoice base is computed from hours, the allowance from the base, and the
amount due from both, so all three are wrong by the same factor and only the
base is a root mismatch. Hours are read and written in many places (reports,
validators, tests), like a real codebase, and several chunks mention
"allowance" or "totals" to attract keyword and semantic retrieval.

None of these names exist in DebugPilot's code: the pipeline must find the
writer generically.
"""

PACKAGE_JSON = '{"type": "module", "scripts": {"repro:scenario": "node scripts/scenario.js", "repro:plain": "node scripts/plain.js", "test": "node tests/run.js"}}\n'

ENTRY_STORE_CORRECT = """import { auditTrail } from '../audit/auditTrail.js';

export const entryStore = {
  createSheet(owner) {
    return { owner, entries: [] };
  },

  record(sheet, code, rate, hours) {
    const existing = sheet.entries.find((entry) => entry.code === code);
    if (existing) {
      existing.hours += hours;
      auditTrail.record('update', code);
      return sheet;
    }
    sheet.entries.push({ code, rate, hours });
    auditTrail.record('insert', code);
    return sheet;
  },

  remove(sheet, code) {
    sheet.entries = sheet.entries.filter((entry) => entry.code !== code);
    return sheet;
  },
};
"""
ENTRY_STORE_REGRESSION = ENTRY_STORE_CORRECT.replace("existing.hours += hours;", "existing.hours = hours;")
BUG_FILE = "src/ledger/entryStore.js"
BUG_LINE = 11

AUDIT_TRAIL = """export const auditTrail = {
  events: [],
  record(kind, code) {
    this.events.push({ kind, code, at: Date.now() });
  },
};
"""

TOTALS = """export function computeBase(entries) {
  return entries.reduce((sum, entry) => sum + entry.rate * entry.hours, 0);
}
"""

ADJUSTMENTS = """import { allowanceRules } from './allowanceRules.js';

export function computeAllowance(base, policy) {
  const rule = allowanceRules[policy.tier] ?? allowanceRules.standard;
  return Math.round(base * rule.percent) / 100;
}
"""

ALLOWANCE_RULES = """// Allowance rules: the allowance reduces invoice totals by a percentage of the base.
export const allowanceRules = {
  standard: { percent: 10, label: 'Standard allowance' },
  partner: { percent: 15, label: 'Partner allowance' },
  internal: { percent: 0, label: 'No allowance' },
};

export function describeAllowance(tier) {
  const rule = allowanceRules[tier] ?? allowanceRules.standard;
  return `${rule.label}: ${rule.percent}% allowance on invoice totals`;
}
"""

INVOICE_SERVICE = """import { computeBase } from './totals.js';
import { computeAllowance } from './adjustments.js';

export function buildInvoice(sheet, policy) {
  const base = computeBase(sheet.entries);
  const allowance = computeAllowance(base, policy);
  const due = base - allowance;
  return { base, allowance, due };
}

export function formatInvoice(invoice) {
  return `base ${invoice.base}, allowance ${invoice.allowance}, due ${invoice.due}`;
}
"""

HOURS_REPORT = """export function totalHours(sheet) {
  return sheet.entries.reduce((sum, entry) => sum + entry.hours, 0);
}

export function overtime(sheet, limit) {
  return sheet.entries.filter((entry) => entry.hours > limit).map((entry) => entry.code);
}

export function hoursByCode(sheet) {
  return Object.fromEntries(sheet.entries.map((entry) => [entry.code, entry.hours]));
}
"""

VALIDATORS = """export function validateEntry(entry) {
  if (entry.hours < 0) throw new Error('hours must be positive');
  if (entry.rate <= 0) throw new Error('rate must be positive');
  return true;
}

export function clampHours(entry, max) {
  entry.hours = Math.min(entry.hours, max);
  return entry;
}
"""

SCENARIO = """import { entryStore } from '../src/ledger/entryStore.js';
import { buildInvoice } from '../src/billing/invoiceService.js';

const sheet = entryStore.createSheet('ada');
entryStore.record(sheet, 'dev', 50, 2);
entryStore.record(sheet, 'dev', 50, 3);
entryStore.record(sheet, 'review', 40, 1);

const invoice = buildInvoice(sheet, { tier: 'standard' });
const expected = { base: 290, allowance: 29, due: 261 };
let failed = false;
for (const key of Object.keys(expected)) {
  console.log(`expected ${key}: ${expected[key]}`);
  console.log(`actual ${key}: ${invoice[key]}`);
  if (invoice[key] !== expected[key]) failed = true;
}
process.exit(failed ? 1 : 0);
"""

# Fails with plain text only: no expected/actual fields to extract.
PLAIN_SCENARIO = """import { entryStore } from '../src/ledger/entryStore.js';
import { buildInvoice } from '../src/billing/invoiceService.js';

const sheet = entryStore.createSheet('ada');
entryStore.record(sheet, 'dev', 50, 2);
entryStore.record(sheet, 'dev', 50, 3);
const invoice = buildInvoice(sheet, { tier: 'standard' });
if (invoice.due !== 225) {
  console.error('Invoice check did not match the reference run');
  process.exit(1);
}
"""

SCENARIO_OUTPUT = """expected base: 290
actual base: 190
expected allowance: 29
actual allowance: 19
expected due: 261
actual due: 171
"""

RATE_CARD = """export const rateCard = { dev: { rate: 50 }, review: { rate: 40 }, ops: { rate: 45 } };

export function rateFor(code) {
  return rateCard[code]?.rate ?? 0;
}

export function applyRates(entries) {
  return entries.map((entry) => ({ ...entry, rate: entry.rate || rateFor(entry.code) }));
}

export function averageRate(entries) {
  return entries.reduce((sum, entry) => sum + entry.rate, 0) / Math.max(entries.length, 1);
}
"""

TEST_FIXTURES = """export const sampleEntries = [
  { code: 'dev', rate: 50, hours: 2 },
  { code: 'ops', rate: 45, hours: 4 },
];

export function makeEntry(code, hours) {
  return { code, rate: 10, hours };
}
"""

README = """# Ledger

Invoices are built from timesheet entries. Totals: the base is the sum of
rate times hours, the allowance is a percentage of the base, and the amount
due is the base minus the allowance. Incorrect totals usually mean a wrong
allowance tier.
"""

FILES_CORRECT = {
    "package.json": PACKAGE_JSON,
    "README.md": README,
    BUG_FILE: ENTRY_STORE_CORRECT,
    "src/audit/auditTrail.js": AUDIT_TRAIL,
    "src/billing/totals.js": TOTALS,
    "src/billing/adjustments.js": ADJUSTMENTS,
    "src/billing/allowanceRules.js": ALLOWANCE_RULES,
    "src/billing/invoiceService.js": INVOICE_SERVICE,
    "src/reports/hoursReport.js": HOURS_REPORT,
    "src/ledger/validators.js": VALIDATORS,
    "src/billing/rateCard.js": RATE_CARD,
    "scripts/scenario.js": SCENARIO,
    "scripts/plain.js": PLAIN_SCENARIO,
    "tests/fixtures.js": TEST_FIXTURES,
}

ISSUE = "Invoices produce incorrect totals"
COMMAND = "npm run repro:scenario"


def upstream_hypothesis(commit: str) -> dict:
    return {
        "summary": "Base, allowance and due are too low because recording a code twice loses hours.",
        "root_cause": "entryStore.record overwrites existing.hours instead of adding the new hours, so the base is "
        "computed from too few hours and the allowance and due inherit the error.",
        "file_path": BUG_FILE,
        "line": BUG_LINE,
        "symbol": "entryStore",
        "execution_path": ["scripts/scenario.js:6 <module>", f"{BUG_FILE}:11 record", "src/billing/totals.js:2 computeBase"],
        "introducing_commit": commit,
        "confidence": "medium",
        "missing_evidence": [],
        "affected_fields": ["base", "allowance", "due"],
    }


def downstream_hypothesis(commit: str) -> dict:
    return {
        "summary": "The allowance is computed incorrectly.",
        "root_cause": "computeAllowance applies the wrong allowance percentage, so allowance and due are wrong.",
        "file_path": "src/billing/adjustments.js",
        "line": 5,
        "symbol": "computeAllowance",
        "execution_path": ["src/billing/invoiceService.js:6 buildInvoice", "src/billing/adjustments.js:5 computeAllowance"],
        "introducing_commit": commit,
        "confidence": "high",
        "missing_evidence": [],
        "affected_fields": ["allowance", "due"],
    }
