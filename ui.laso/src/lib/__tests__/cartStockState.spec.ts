/**
 * The cart line's stock label and the "+" button.
 *
 * Before: "/{resolvedStock ?? '?'}" — a bare number behind a slash, which read
 * as "113" with no unit, and "?" for a drug whose stock had not loaded. Nothing
 * distinguished "at the limit" from "plenty left", and "+" stayed clickable at
 * the limit, so a cashier could click into nothing happening.
 */
import { describe, expect, it } from "vitest";
import {
    getLineStockState,
    stockLabelClass,
    stockLabelText,
    LOW_STOCK_THRESHOLD,
} from "@/lib/cartStockState";
import type { LineStockState } from "@/lib/cartStockState";

describe("getLineStockState precedence", () => {
    it("unknown when availability has not loaded", () => {
        expect(getLineStockState(1, undefined)).toBe("unknown");
        expect(getLineStockState(1, NaN)).toBe("unknown");
        // Crucially NOT "low"/"over": a missing number is not zero.
        expect(getLineStockState(1, undefined)).not.toBe("low");
        expect(getLineStockState(1, undefined)).not.toBe("over");
    });

    it("over wins over everything else", () => {
        expect(getLineStockState(11, 10)).toBe("over");
        // Even when also scarce.
        expect(getLineStockState(11, 2)).toBe("over");
    });

    it("max when the line exactly fills the available stock", () => {
        expect(getLineStockState(113, 113)).toBe("max");
    });

    it("max is NOT applied to a zero-stock line at quantity 0", () => {
        // 0 available at quantity 0 is empty, not full.
        expect(getLineStockState(0, 0)).toBe("low");
    });

    it("max takes precedence over low for a scarce, exactly-full line", () => {
        expect(getLineStockState(2, 2)).toBe("max");
        expect(getLineStockState(2, 2)).not.toBe("low");
    });

    it("low at or below the threshold", () => {
        expect(getLineStockState(1, LOW_STOCK_THRESHOLD)).toBe("low");
        expect(getLineStockState(1, LOW_STOCK_THRESHOLD - 1)).toBe("low");
    });

    it("ok above the threshold with room to spare", () => {
        expect(getLineStockState(1, 6)).toBe("ok");
        expect(getLineStockState(113, 114)).toBe("ok");
    });

    it("honours an explicit threshold", () => {
        expect(getLineStockState(1, 20, 25)).toBe("low");
        expect(getLineStockState(1, 6, 5)).toBe("ok");
    });

    it("the default threshold is 5, not the drug's reorder level", () => {
        expect(LOW_STOCK_THRESHOLD).toBe(5);
        // Gebedol's real reorder_level is 300 against 113 in stock. Using it
        // would mark every stocked line "low".
        expect(getLineStockState(5, 113)).toBe("ok");
    });
});

describe("label text", () => {
    // (quantity, available, expected state, expected text)
    const cases: Array<[number, number | undefined, LineStockState, string]> = [
        [1, undefined, "unknown", "Checking stock…"],
        [11, 10, "over", "10 available"],
        [113, 113, "max", "Max reached (113)"],
        [1, 3, "low", "Only 3 left"],
        [5, 113, "ok", "113 available"],
    ];

    for (const [quantity, available, state, text] of cases) {
        it(`${state}: qty ${quantity}, available ${available} -> "${text}"`, () => {
            expect(getLineStockState(quantity, available)).toBe(state);
            expect(stockLabelText(state, available)).toBe(text);
        });
    }

    it("never says '?' or a bare slash-number", () => {
        for (const s of ["unknown", "over", "max", "low", "ok"] as const) {
            const text = stockLabelText(s, s === "unknown" ? undefined : 7);
            expect(text).not.toContain("?");
            expect(text.startsWith("/")).toBe(false);
        }
    });

    it("is single-line: no wrapping class is applied by the state helper", () => {
        // The class string is purely colour; truncation/wrapping lives in the
        // component, so this pins that colour and layout do not get conflated.
        expect(stockLabelClass("over")).toContain("red");
        expect(stockLabelClass("max")).toContain("amber");
        expect(stockLabelClass("low")).toContain("amber");
        expect(stockLabelClass("ok")).toContain("muted");
        expect(stockLabelClass("unknown")).toContain("muted");
    });
});

describe("the '+' button limit", () => {
    /** Mirrors CartPanel's `atStockLimit`. */
    const atLimit = (quantity: number, available: number | undefined) =>
        available !== undefined && quantity >= available;

    it("is disabled once the line reaches the available stock", () => {
        expect(atLimit(113, 113)).toBe(true);
        expect(atLimit(114, 113)).toBe(true);
    });

    it("stays enabled below the limit", () => {
        expect(atLimit(112, 113)).toBe(false);
        expect(atLimit(1, 113)).toBe(false);
    });

    it("stays enabled when stock is unknown", () => {
        // Nothing to limit against; maxQty falls back to 1000.
        expect(atLimit(1, undefined)).toBe(false);
        expect(atLimit(999, undefined)).toBe(false);
    });
});
