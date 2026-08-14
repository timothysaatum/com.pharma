import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Check, ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { conflictsApi, type ConflictRecord, type ConflictResolution } from "@/api/conflicts";
import { isOfflineError } from "@/api/client";
import { cn } from "@/lib/utils";

const STATUS_LABELS: Record<string, string> = {
    pending: "Pending",
    resolved_keep_local: "Kept local",
    resolved_keep_incoming: "Applied incoming",
};

const AGGREGATE_LABELS: Record<string, string> = {
    customer: "Customer",
    drug: "Drug",
    drug_category: "Drug Category",
};

function fmtDateTime(iso: string) {
    return new Date(iso).toLocaleString("en-GH", {
        dateStyle: "medium",
        timeStyle: "short",
    });
}

function formatLabel(key: string) {
    return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function renderValue(v: unknown): string {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
}

const DISPLAY_FIELDS = [
    "first_name", "last_name", "phone", "email", "date_of_birth",
    "loyalty_points", "loyalty_tier", "is_active",
    "insurance_provider_id", "insurance_member_id",
    "preferred_contact_method", "allergies", "chronic_conditions",
    "name", "unit_price", "is_active", "category_id",
];

function ConflictDiff({
    local,
    incoming,
}: {
    local: Record<string, unknown>;
    incoming: Record<string, unknown>;
}) {
    const allKeys = Array.from(
        new Set([...DISPLAY_FIELDS].filter((k) => k in local || k in incoming))
    );
    const diffKeys = allKeys.filter(
        (k) => JSON.stringify(local[k]) !== JSON.stringify(incoming[k])
    );

    if (diffKeys.length === 0) {
        return (
            <p className="text-sm text-muted-foreground italic">
                No field differences detected.
            </p>
        );
    }

    return (
        <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
                <thead>
                    <tr className="border-b">
                        <th className="text-left py-1 pr-4 font-medium w-36">Field</th>
                        <th className="text-left py-1 pr-4 font-medium text-blue-700 w-1/2">
                            Current (server)
                        </th>
                        <th className="text-left py-1 font-medium text-amber-700 w-1/2">
                            Incoming (offline edit)
                        </th>
                    </tr>
                </thead>
                <tbody>
                    {diffKeys.map((k) => (
                        <tr key={k} className="border-b border-muted/40">
                            <td className="py-1 pr-4 text-muted-foreground">{formatLabel(k)}</td>
                            <td className="py-1 pr-4 font-mono text-blue-800 break-all">
                                {renderValue(local[k])}
                            </td>
                            <td className="py-1 font-mono text-amber-800 break-all">
                                {renderValue(incoming[k])}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function ConflictRow({
    conflict,
    onResolved,
}: {
    conflict: ConflictRecord;
    onResolved: (c: ConflictRecord) => void;
}) {
    const [expanded, setExpanded] = useState(false);
    const [resolving, setResolving] = useState<ConflictResolution | null>(null);

    const resolve = async (resolution: ConflictResolution) => {
        setResolving(resolution);
        try {
            const updated = await conflictsApi.resolve(conflict.id, resolution);
            toast.success(
                resolution === "keep_local"
                    ? "Current version kept."
                    : "Incoming changes applied."
            );
            onResolved(updated);
        } catch (err) {
            if (isOfflineError(err)) {
                toast.error("You're offline — resolve conflicts when back online.");
            } else {
                toast.error("Failed to resolve conflict. Try again.");
            }
        } finally {
            setResolving(null);
        }
    };

    const isPending = conflict.status === "pending";
    const aggregateLabel =
        AGGREGATE_LABELS[conflict.aggregate_type] ?? conflict.aggregate_type;

    return (
        <div
            className={cn(
                "border rounded-lg overflow-hidden",
                isPending ? "border-amber-300 bg-amber-50/40" : "border-muted bg-muted/20"
            )}
        >
            {/* Header row */}
            <button
                type="button"
                className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-black/5 transition-colors"
                onClick={() => setExpanded((x) => !x)}
            >
                <div className="flex items-center gap-3 min-w-0">
                    {isPending ? (
                        <AlertTriangle className="h-4 w-4 text-amber-500 shrink-0" />
                    ) : (
                        <Check className="h-4 w-4 text-green-600 shrink-0" />
                    )}
                    <div className="min-w-0">
                        <span className="font-medium text-sm">{aggregateLabel}</span>
                        <span className="text-muted-foreground text-xs ml-2">
                            {conflict.aggregate_id.slice(0, 8)}…
                        </span>
                        {Boolean(conflict.local_snapshot["first_name"]) && (
                            <span className="text-sm ml-2">
                                {String(conflict.local_snapshot["first_name"])}{" "}
                                {String(conflict.local_snapshot["last_name"] ?? "")}
                            </span>
                        )}
                        {!conflict.local_snapshot["first_name"] && Boolean(conflict.local_snapshot["name"]) && (
                            <span className="text-sm ml-2">
                                {String(conflict.local_snapshot["name"])}
                            </span>
                        )}
                    </div>
                </div>
                <div className="flex items-center gap-3 shrink-0 ml-4">
                    <span
                        className={cn(
                            "text-xs px-2 py-0.5 rounded-full font-medium",
                            isPending
                                ? "bg-amber-100 text-amber-800"
                                : "bg-green-100 text-green-800"
                        )}
                    >
                        {STATUS_LABELS[conflict.status] ?? conflict.status}
                    </span>
                    <span className="text-xs text-muted-foreground hidden sm:block">
                        {fmtDateTime(conflict.created_at)}
                    </span>
                    {expanded ? (
                        <ChevronUp className="h-4 w-4 text-muted-foreground" />
                    ) : (
                        <ChevronDown className="h-4 w-4 text-muted-foreground" />
                    )}
                </div>
            </button>

            {/* Expanded body */}
            {expanded && (
                <div className="px-4 pb-4 border-t border-muted/40 space-y-4">
                    <div className="pt-3">
                        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground mb-2">
                            Field differences
                        </h4>
                        <ConflictDiff
                            local={conflict.local_snapshot}
                            incoming={conflict.incoming_payload}
                        />
                    </div>

                    {isPending && (
                        <div className="flex gap-2 pt-1">
                            <button
                                type="button"
                                disabled={resolving !== null}
                                onClick={() => resolve("keep_local")}
                                className={cn(
                                    "flex items-center gap-1.5 px-3 py-1.5 rounded text-sm font-medium border",
                                    "border-blue-300 bg-blue-50 text-blue-800",
                                    "hover:bg-blue-100 disabled:opacity-50 transition-colors"
                                )}
                            >
                                {resolving === "keep_local" ? (
                                    <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                    <Check className="h-3.5 w-3.5" />
                                )}
                                Keep current
                            </button>
                            <button
                                type="button"
                                disabled={resolving !== null}
                                onClick={() => resolve("keep_incoming")}
                                className={cn(
                                    "flex items-center gap-1.5 px-3 py-1.5 rounded text-sm font-medium border",
                                    "border-amber-300 bg-amber-50 text-amber-800",
                                    "hover:bg-amber-100 disabled:opacity-50 transition-colors"
                                )}
                            >
                                {resolving === "keep_incoming" ? (
                                    <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                    <Check className="h-3.5 w-3.5" />
                                )}
                                Apply incoming
                            </button>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

const STATUS_FILTERS = [
    { value: "pending", label: "Pending" },
    { value: "all", label: "All" },
    { value: "resolved_keep_local", label: "Kept local" },
    { value: "resolved_keep_incoming", label: "Applied incoming" },
] as const;

type StatusFilter = (typeof STATUS_FILTERS)[number]["value"];

export default function ConflictsPage() {
    const [conflicts, setConflicts] = useState<ConflictRecord[]>([]);
    const [total, setTotal] = useState(0);
    const [pending, setPending] = useState(0);
    const [loading, setLoading] = useState(true);
    const [statusFilter, setStatusFilter] = useState<StatusFilter>("pending");
    const [page, setPage] = useState(1);
    const pageSize = 25;

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const result = await conflictsApi.list({
                status: statusFilter,
                page,
                page_size: pageSize,
            });
            setConflicts(result.conflicts);
            setTotal(result.total);
            setPending(result.pending);
        } catch (err) {
            if (!isOfflineError(err)) {
                toast.error("Could not load conflicts.");
            }
        } finally {
            setLoading(false);
        }
    }, [statusFilter, page]);

    useEffect(() => {
        void load();
    }, [load]);

    const handleResolved = (updated: ConflictRecord) => {
        setConflicts((prev) =>
            prev.map((c) => (c.id === updated.id ? updated : c))
        );
        setPending((n) => Math.max(0, n - 1));
    };

    const totalPages = Math.max(1, Math.ceil(total / pageSize));

    return (
        <div className="p-6 max-w-4xl mx-auto space-y-6">
            {/* Page header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold">Sync Conflicts</h1>
                    <p className="text-sm text-muted-foreground mt-0.5">
                        Concurrent edits from multiple branches that couldn't be
                        merged automatically.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    {pending > 0 && (
                        <span className="bg-amber-500 text-white text-xs font-bold px-2 py-0.5 rounded-full">
                            {pending} pending
                        </span>
                    )}
                    <button
                        type="button"
                        onClick={() => void load()}
                        disabled={loading}
                        className="p-2 rounded hover:bg-muted transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
                    </button>
                </div>
            </div>

            {/* Filter tabs */}
            <div className="flex gap-1 border-b">
                {STATUS_FILTERS.map((f) => (
                    <button
                        key={f.value}
                        type="button"
                        onClick={() => { setStatusFilter(f.value); setPage(1); }}
                        className={cn(
                            "px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors",
                            statusFilter === f.value
                                ? "border-primary text-primary"
                                : "border-transparent text-muted-foreground hover:text-foreground"
                        )}
                    >
                        {f.label}
                    </button>
                ))}
            </div>

            {/* List */}
            {loading ? (
                <div className="flex justify-center py-12 text-muted-foreground text-sm">
                    <RefreshCw className="h-5 w-5 animate-spin mr-2" /> Loading…
                </div>
            ) : conflicts.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                    <Check className="h-10 w-10 text-green-500 mb-3" />
                    <p className="font-medium">No conflicts</p>
                    <p className="text-sm text-muted-foreground mt-1">
                        {statusFilter === "pending"
                            ? "All edits are consistent across branches."
                            : "Nothing to show for this filter."}
                    </p>
                </div>
            ) : (
                <div className="space-y-3">
                    {conflicts.map((c) => (
                        <ConflictRow key={c.id} conflict={c} onResolved={handleResolved} />
                    ))}
                </div>
            )}

            {/* Pagination */}
            {totalPages > 1 && (
                <div className="flex items-center justify-between text-sm pt-2">
                    <span className="text-muted-foreground">
                        {total} total · page {page} of {totalPages}
                    </span>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            disabled={page <= 1}
                            onClick={() => setPage((p) => p - 1)}
                            className="px-3 py-1 border rounded hover:bg-muted disabled:opacity-40"
                        >
                            Previous
                        </button>
                        <button
                            type="button"
                            disabled={page >= totalPages}
                            onClick={() => setPage((p) => p + 1)}
                            className="px-3 py-1 border rounded hover:bg-muted disabled:opacity-40"
                        >
                            Next
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
