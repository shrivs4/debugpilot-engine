"""A TypeScript order/checkout project shaped like the real checkout fixture.

Written from the reported structure only (the real fixture is not read): a
reproduction module imports order mutation functions and checkout, adds the
same product twice, and prints expected and actual subtotal / discount / total.
The regression is in the order mutation (a repeated SKU's quantity is replaced
instead of accumulated); checkout and discount code are correct. Item
properties (`sku`, `quantity`, `orderId`) are used across many files, and the
README and coupon code are full of the words "checkout", "total" and
"discount" to attract keyword and semantic retrieval.

Runs with `node --experimental-strip-types`, so imports carry `.ts` extensions
and type-only imports use `import type`.
"""

PACKAGE_JSON = (
    '{"name": "shop", "type": "module", "scripts": {'
    '"repro:scenario": "node --experimental-strip-types --no-warnings scripts/reproScenario.ts", '
    '"repro:json": "node --experimental-strip-types --no-warnings scripts/reproJsonLog.ts"}}\n'
)

TYPES = """export interface Product {
  sku: string;
  name: string;
  price: number;
}

export interface OrderItem {
  sku: string;
  name: string;
  unitPrice: number;
  quantity: number;
}

export interface Order {
  orderId: string;
  customerId: string;
  items: OrderItem[];
  couponCode?: string;
}

export interface Coupon {
  code: string;
  discountType: 'PERCENTAGE' | 'FIXED';
  value: number;
}
"""

ORDER_SERVICE_CORRECT = """import type { Order, Product } from './types.ts';

let nextOrderId = 1;

export function createOrder(customerId: string): Order {
  return { orderId: `ord-${nextOrderId++}`, customerId, items: [] };
}

export function addItem(order: Order, product: Product, quantity: number): Order {
  const existing = order.items.find((item) => item.sku === product.sku);
  if (existing) {
    existing.quantity += quantity;
    return order;
  }
  order.items.push({ sku: product.sku, name: product.name, unitPrice: product.price, quantity });
  return order;
}

export function removeItem(order: Order, sku: string): Order {
  order.items = order.items.filter((item) => item.sku !== sku);
  return order;
}

export function applyCoupon(order: Order, code: string): Order {
  order.couponCode = code;
  return order;
}
"""
ORDER_SERVICE_REGRESSION = ORDER_SERVICE_CORRECT.replace("existing.quantity += quantity;", "existing.quantity = quantity;")
BUG_FILE = "src/checkout/orderService.ts"
BUG_LINE = 12

PRICING = """import type { OrderItem } from './types.ts';

export function calculateSubtotal(items: OrderItem[]): number {
  return items.reduce((sum, item) => sum + item.unitPrice * item.quantity, 0);
}

export function roundMoney(amount: number): number {
  return Math.round(amount * 100) / 100;
}
"""

DISCOUNT_SERVICE = """import type { Coupon } from './types.ts';
import { roundMoney } from './pricing.ts';

const COUPONS: Coupon[] = [
  { code: 'SAVE10', discountType: 'PERCENTAGE', value: 10 },
  { code: 'FIVEOFF', discountType: 'FIXED', value: 5 },
];

export function findCoupon(code: string | undefined): Coupon | undefined {
  return COUPONS.find((coupon) => coupon.code === code);
}

// Discount for the checkout total: percentage coupons discount the subtotal, fixed coupons a flat amount.
export function calculateDiscount(subtotal: number, coupon: Coupon | undefined): number {
  if (!coupon) return 0;
  const discount = coupon.discountType === 'PERCENTAGE' ? (subtotal * coupon.value) / 100 : coupon.value;
  return roundMoney(Math.min(discount, subtotal));
}
"""

CHECKOUT_SERVICE = """import type { Order } from './types.ts';
import { calculateSubtotal, roundMoney } from './pricing.ts';
import { calculateDiscount, findCoupon } from './discountService.ts';

export interface CheckoutResult {
  orderId: string;
  subtotal: number;
  discount: number;
  total: number;
}

// Checkout computes the order total: subtotal minus the coupon discount.
export function checkout(order: Order): CheckoutResult {
  const subtotal = calculateSubtotal(order.items);
  const coupon = findCoupon(order.couponCode);
  const discount = calculateDiscount(subtotal, coupon);
  const total = roundMoney(subtotal - discount);
  return { orderId: order.orderId, subtotal, discount, total };
}

export function checkoutSummary(order: Order): string {
  const result = checkout(order);
  return `Checkout ${result.orderId}: total ${result.total} (discount ${result.discount})`;
}
"""

STOCK = """import type { Order, OrderItem } from '../checkout/types.ts';

const stock = new Map<string, number>();

export function reserveStock(order: Order): boolean {
  return order.items.every((item) => (stock.get(item.sku) ?? 0) >= item.quantity);
}

export function restock(item: OrderItem): void {
  stock.set(item.sku, (stock.get(item.sku) ?? 0) + item.quantity);
}

export function clampToStock(item: OrderItem): OrderItem {
  item.quantity = Math.min(item.quantity, stock.get(item.sku) ?? 0);
  return item;
}
"""

SALES_REPORT = """import type { Order } from '../checkout/types.ts';

export function unitsSold(orders: Order[]): number {
  return orders.flatMap((order) => order.items).reduce((sum, item) => sum + item.quantity, 0);
}

export function revenueBySku(orders: Order[]): Record<string, number> {
  const revenue: Record<string, number> = {};
  for (const item of orders.flatMap((order) => order.items)) {
    revenue[item.sku] = (revenue[item.sku] ?? 0) + item.unitPrice * item.quantity;
  }
  return revenue;
}

export function orderIds(orders: Order[]): string[] {
  return orders.map((order) => order.orderId);
}
"""

TEST_HELPERS = """import type { OrderItem } from '../src/checkout/types.ts';

export function makeItem(sku: string, quantity: number): OrderItem {
  return { sku, name: sku, unitPrice: 10, quantity };
}

export const sampleItems: OrderItem[] = [
  { sku: 'A', name: 'A', unitPrice: 10, quantity: 1 },
  { sku: 'B', name: 'B', unitPrice: 5, quantity: 3 },
];
"""

REPRO = """import { createOrder, addItem, applyCoupon } from '../src/checkout/orderService.ts';
import { checkout } from '../src/checkout/checkoutService.ts';

const book = { sku: 'BOOK-1', name: 'Book', price: 20 };
const pen = { sku: 'PEN-1', name: 'Pen', price: 5 };

const order = createOrder('customer-42');
addItem(order, book, 2);
addItem(order, book, 3);
addItem(order, pen, 1);
applyCoupon(order, 'SAVE10');

const result = checkout(order);
const expected = { subtotal: 105, discount: 10.5, total: 94.5 };
let failed = false;
for (const key of ['subtotal', 'discount', 'total'] as const) {
  console.log(`expected ${key}: ${expected[key]}`);
  console.log(`actual ${key}: ${result[key]}`);
  if (result[key] !== expected[key]) failed = true;
}
process.exit(failed ? 1 : 0);
"""

# Same scenario, but it reports only through structured JSON logs (no expected/actual text lines).
REPRO_JSON_LOG = """import { createOrder, addItem, applyCoupon } from '../src/checkout/orderService.ts';
import { checkout } from '../src/checkout/checkoutService.ts';

const book = { sku: 'BOOK-1', name: 'Book', price: 20 };
const pen = { sku: 'PEN-1', name: 'Pen', price: 5 };

const order = createOrder('customer-42');
addItem(order, book, 2);
addItem(order, book, 3);
addItem(order, pen, 1);
applyCoupon(order, 'SAVE10');
console.log(JSON.stringify({ level: 'info', message: 'Scenario checkout started', orderId: order.orderId }));

const result = checkout(order);
const expected = { subtotal: 105, discount: 10.5, total: 94.5 };
const mismatches = (['subtotal', 'discount', 'total'] as const)
  .filter((key) => result[key] !== expected[key])
  .map((key) => ({ field: key, expected: expected[key], actual: result[key] }));
if (mismatches.length > 0) {
  console.log(JSON.stringify({ level: 'error', message: 'Scenario checkout produced unexpected totals', mismatches }));
  process.exit(1);
}
console.log(JSON.stringify({ level: 'info', message: 'Scenario checkout matched expected totals' }));
"""

# The exact log record emitted by the real fixture (as reported), pretty-printed.
FIXTURE_JSON_LOG = """{
  "level": "error",
  "message": "Scenario checkout produced unexpected totals",
  "mismatches": [
    { "field": "subtotal", "expected": 105, "actual": 65 },
    { "field": "discount", "expected": 10.5, "actual": 6.5 },
    { "field": "total", "expected": 94.5, "actual": 58.5 }
  ]
}"""

README = """# Shop checkout

Checkout computes the order total from the subtotal and the discount. The
discount comes from the coupon: percentage coupons discount the subtotal and
fixed coupons discount a flat amount. Incorrect checkout totals are usually a
discount or coupon problem; check the discount calculation and the coupon
discountType first.
"""

FILES_CORRECT = {
    "package.json": PACKAGE_JSON,
    "README.md": README,
    "src/checkout/types.ts": TYPES,
    BUG_FILE: ORDER_SERVICE_CORRECT,
    "src/checkout/pricing.ts": PRICING,
    "src/checkout/discountService.ts": DISCOUNT_SERVICE,
    "src/checkout/checkoutService.ts": CHECKOUT_SERVICE,
    "src/inventory/stock.ts": STOCK,
    "src/reports/salesReport.ts": SALES_REPORT,
    "tests/helpers.ts": TEST_HELPERS,
    "scripts/reproScenario.ts": REPRO,
    "scripts/reproJsonLog.ts": REPRO_JSON_LOG,
}

ISSUE = "Checkout produces incorrect totals"
COMMAND = "npm run repro:scenario"
JSON_COMMAND = "npm run repro:json"


def add_item_hypothesis(commit: str) -> dict:
    return {
        "summary": "Subtotal, discount and total are too low because adding the same product twice loses quantity.",
        "root_cause": "addItem replaces existing.quantity with the new quantity instead of adding to it, so the "
        "subtotal is computed from too few items and the discount and total inherit the error.",
        "file_path": BUG_FILE,
        "line": BUG_LINE,
        "symbol": "addItem",
        "execution_path": ["scripts/reproScenario.ts:9 <module>", f"{BUG_FILE}:12 addItem",
                           "src/checkout/pricing.ts:4 calculateSubtotal"],
        "introducing_commit": commit,
        "confidence": "medium",
        "missing_evidence": [],
        "affected_fields": ["subtotal", "discount", "total"],
    }


def discount_hypothesis(commit: str) -> dict:
    return {
        "summary": "The discount is calculated incorrectly.",
        "root_cause": "calculateDiscount applies the percentage coupon incorrectly, so discount and total are wrong.",
        "file_path": "src/checkout/discountService.ts",
        "line": 16,
        "symbol": "calculateDiscount",
        "execution_path": ["src/checkout/checkoutService.ts:16 checkout", "src/checkout/discountService.ts:16 calculateDiscount"],
        "introducing_commit": commit,
        "confidence": "high",
        "missing_evidence": [],
        "affected_fields": ["discount", "total"],
    }


def legacy_coupon_hypothesis(commit: str) -> dict:
    """An unrelated diagnosis that leans on coupon concepts from lower-ranked evidence."""
    return {
        "summary": "Legacy coupons without a discountType produce incorrect checkout totals.",
        "root_cause": "findCoupon returns legacy coupons whose discountType is missing, so the discount and total are "
        "calculated with the wrong coupon rule.",
        "file_path": "src/checkout/discountService.ts",
        "line": 10,
        "symbol": "findCoupon",
        "execution_path": ["src/checkout/checkoutService.ts:15 checkout", "src/checkout/discountService.ts:10 findCoupon"],
        "introducing_commit": commit,
        "confidence": "high",
        "missing_evidence": [],
        "affected_fields": ["discount", "total"],
    }
