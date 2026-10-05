/**
 * cartStockState.ts — the ONE place that decides how a cart line's stock reads.
 *
 * Extracted because two features need the same judgement and must not disagree:
 * the label beside the stepper, and the quantity input's clamp behaviour. If
 * they each re-derived "is this over the limit?" they would drift, and the UI
 * would say "Max reached" while the input accepted the value.
 *
 * THRESHOLD
 * ---------
 * A constant, not the drug's `reorder_level`.
 *
 * `Drug.reorder_level` exists and would have been the obvious choice, but it is
 * the level at which the drug gets REORDERED, not the level at which a cashier
 * should be warned. In this org the real values are Gebedol 300, Amoxicilin 500,
 * Paracetamol 200 — so `available <= reorder_level` is true for almost every
 * stocked line (Gebedol has 113 against a threshold of 300) and every row would
 * permanently read "Only 113 left" in amber. Warning on the majority of healthy
 * lines trains staff to ignore the warning. LOW_STOCK_THRESHOLD = 5 keeps the
 * amber states for genuinely scarce lines.
 */

export type LineStockState = "unknown" | "over" | "max" | "low" | "ok";

export const LOW_STOCK_THRESHOLD = 5;

/**
 * Classify one cart line.
 *
 * Precedence, highest first:
 *   unknown  available is undefined — we do not know yet, so say so rather than
 *            implying anything. Never treat a missing number as zero: that would
 *            claim a drug is unavailable when it was merely not loaded.
 *   over     quantity > available. The existing red band explains the shortfall;
 *            this state only labels it.
 *   max      quantity === available, and available > 0. The line is exactly full,
 *            which is worth saying before the cashier hits "+" and nothing moves.
 *   low      available is scarce (<= threshold).
 *   ok       normal.
 *
 * `quantity === available && available > 0` is checked before `low` so a scarce
 * line that is exactly full reads "Max reached", not "Only 2 left".
 *
 * `quantity === available && available === 0` deliberately does NOT become
 * "max": a zero-stock line at quantity 0 is not full, it is empty.
 */
export function getLineStockState(
    quantity: number,
    available: number | undefined,
    threshold: number = LOW_STOCK_THRESHOLD
): LineStockState {
    if (available === undefined || Number.isNaN(available)) return "unknown";
    if (quantity > available) return "over";
    if (quantity === available && available > 0) return "max";
    if (available <= threshold) return "low";
    return "ok";
}

/** CSS classes for the label, using the tokens already in the design system. */
export function stockLabelClass(state: LineStockState): string {
    switch (state) {
        case "over":
            return "text-red-600";
        case "max":
        case "low":
            return "text-amber-600";
        case "unknown":
            return "text-ink-muted";
        case "ok":
        default:
            return "text-ink-muted";
    }
}

/** The single-line label text for a state. */
export function stockLabelText(
    state: LineStockState,
    available: number | undefined
): string {
    const n = available ?? 0;
    switch (state) {
        case "unknown":
            return "Checking stock…";
        case "over":
            return `${n} available`;
        case "max":
            return `Max reached (${n})`;
        case "low":
            return `Only ${n} left`;
        case "ok":
        default:
            return `${n} available`;
    }
}
