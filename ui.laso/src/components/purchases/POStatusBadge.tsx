/**
 * POStatusBadge.tsx
 * Consistent status pill used across PO list and detail views.
 */
import type { PurchaseOrderStatus } from "@/types";

const CONFIG: Record<
    PurchaseOrderStatus,
    { label: string; cls: string }
> = {
    draft: { label: "Draft", cls: "bg-slate-100 text-slate-600" },
    pending: { label: "Pending", cls: "bg-amber-50  text-amber-700 border border-amber-200" },
    approved: { label: "Approved", cls: "bg-blue-50   text-blue-700  border border-blue-200" },
    ordered: { label: "Partial", cls: "bg-violet-50 text-violet-700 border border-violet-200" },
    received: { label: "Received", cls: "bg-green-50  text-green-700 border border-green-200" },
    cancelled: { label: "Cancelled", cls: "bg-red-50    text-red-600   border border-red-200" },
};

interface Props {
    status: PurchaseOrderStatus;
    size?: "sm" | "md";
}

export function POStatusBadge({ status, size = "sm" }: Props) {
    const { label, cls } = CONFIG[status] ?? CONFIG.draft;
    const px = size === "md" ? "px-3 py-1 text-xs" : "px-2 py-0.5 text-[11px]";
    return (
        <span className={`inline-flex items-center font-semibold rounded-full ${px} ${cls}`}>
            {label}
        </span>
    );
}