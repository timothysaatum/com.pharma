/**
 * Choosing which price contract the POS starts on.
 *
 * Why this is a function and not three lines inside CartPanel
 * ----------------------------------------------------------
 * The picker used to do `contracts.find((c) => c.is_default) ?? contracts[0]`
 * inline. That is not enough on its own, because the question is asked at
 * several moments that must all agree: first load, a contract list that changed,
 * and every cart reset. When the rule lives inline it drifts, and the failure is
 * silent — the cashier just sees "— Select contract —" with no explanation.
 *
 * The precedence
 * --------------
 *   1. the contract flagged default (`is_default`; the DB column is
 *      `is_default_contract`, mapped to `is_default` by both the online
 *      serializer and the local read, so this one flag means the same thing
 *      whether the list came from the server or from the device)
 *   2. otherwise the first `standard` contract — an org that never flagged a
 *      default still has an obvious safe choice, and "standard" is that choice.
 *      Relying on list order instead picks whatever sorts first by name, which
 *      for an insurance-heavy list can be a contract that needs verification.
 *   3. otherwise the first contract, so the sale button is never blocked by a
 *      list we are perfectly able to price against.
 *
 * Pure and total: no throwing, no defaults that hide a bug. An empty or missing
 * list gives null, and the caller decides what an absent contract means.
 */
import type { AvailableContract } from "@/api/contracts";

/** Sort by name so "the first" is deterministic even if the caller passes a
 *  list whose order came from a query without an ORDER BY. */
function byName(a: AvailableContract, b: AvailableContract): number {
    return a.name.localeCompare(b.name);
}

export function pickDefaultContract(
    contracts: AvailableContract[] | null | undefined
): AvailableContract | null {
    if (!contracts || contracts.length === 0) return null;

    // 1. Flagged default. Several may be flagged (the DB does not enforce
    //    uniqueness), so take the first by name rather than array order: the
    //    same org must not get a different answer depending on row order.
    const flagged = contracts.filter((c) => c.is_default).sort(byName);
    if (flagged.length > 0) return flagged[0];

    // 2. No flag: prefer a standard contract, again by name for determinism.
    const standard = contracts.filter((c) => c.type === "standard").sort(byName);
    if (standard.length > 0) return standard[0];

    // 3. Nothing standard either. Still return something: an insurance-only org
    //    must be able to sell, and the cashier can change it.
    return [...contracts].sort(byName)[0];
}
