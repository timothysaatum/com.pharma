import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
    Activity,
    Calendar,
    ChevronLeft,
    ChevronRight,
    Clock,
    Copy,
    Eye,
    FileText,
    Monitor,
    RefreshCw,
    Search,
    UserRound,
    X,
} from "lucide-react";
import { toast } from "sonner";
import { auditApi, type AuditLogEntry } from "@/api/audit";
import { isOfflineError, isBackendReachable, parseApiError } from "@/api/client";
import { getDb } from "@/lib/localDb";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;

const JSON_FIELDS = new Set(["changes", "context_metadata"] as const);

function parseAuditRow(row: Record<string, unknown>): AuditLogEntry {
    const parsed = { ...row };
    for (const field of JSON_FIELDS) {
        const val = parsed[field];
        if (typeof val === "string") {
            try { parsed[field] = JSON.parse(val); } catch { parsed[field] = null; }
        }
    }
    return parsed as unknown as AuditLogEntry;
}

function fmtDateTime(iso: string) {
    return new Date(iso).toLocaleString("en-GH", {
        dateStyle: "medium",
        timeStyle: "short",
    });
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasValue(value: unknown): boolean {
    if (value === null || value === undefined) return false;
    if (typeof value === "string") return value.trim().length > 0;
    if (Array.isArray(value)) return value.length > 0;
    if (isRecord(value)) return Object.keys(value).length > 0;
    return true;
}

function formatLabel(value: string) {
    return value
        .replace(/_/g, " ")
        .replace(/\b\w/g, (match) => match.toUpperCase());
}

function stringifyAuditValue(value: unknown): string {
    if (value === null || value === undefined || value === "") return "Not recorded";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
        return String(value);
    }

    try {
        return JSON.stringify(value, null, 2) ?? String(value);
    } catch {
        return String(value);
    }
}

function formatPreviewValue(value: unknown): string {
    if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
    if (isRecord(value)) {
        const count = Object.keys(value).length;
        return `${count} field${count === 1 ? "" : "s"}`;
    }
    return stringifyAuditValue(value);
}

async function copyToClipboard(label: string, value: unknown) {
    if (!navigator.clipboard) {
        toast.error("Clipboard is unavailable in this browser.");
        return;
    }

    try {
        await navigator.clipboard.writeText(stringifyAuditValue(value));
        toast.success(`${label} copied.`);
    } catch {
        toast.error(`Could not copy ${label.toLowerCase()}.`);
    }
}

function ChangesDisplay({
    changes,
    onViewDetails,
}: {
    changes: AuditLogEntry["changes"];
    onViewDetails: () => void;
}) {
    if (!hasValue(changes) || !isRecord(changes)) {
        return <span className="text-slate-500">No changes recorded</span>;
    }

    const entries = Object.entries(changes).filter(([, value]) => hasValue(value));
    if (entries.length === 0) return <span className="text-slate-500">No changes recorded</span>;

    return (
        <div className="min-w-[18rem] space-y-2">
            <div className="space-y-1.5">
                {entries.slice(0, 3).map(([key, value]) => (
                    <div key={key} className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2 text-xs">
                        <span className="font-bold uppercase tracking-wide text-slate-600">
                            {formatLabel(key)}
                        </span>
                        <span className="whitespace-normal break-words text-slate-700">
                            {formatPreviewValue(value)}
                        </span>
                    </div>
                ))}
            </div>
            <button
                type="button"
                onClick={(event) => {
                    event.stopPropagation();
                    onViewDetails();
                }}
                className="inline-flex items-center gap-1.5 rounded-lg border border-brand-200 px-2 py-1 text-xs font-semibold text-brand-700 transition-colors hover:bg-brand-50"
            >
                <Eye className="h-3.5 w-3.5" />
                View full details
            </button>
        </div>
    );
}

function DetailField({
    label,
    value,
    icon,
    mono = false,
    copyable = false,
}: {
    label: string;
    value: unknown;
    icon?: ReactNode;
    mono?: boolean;
    copyable?: boolean;
}) {
    const text = stringifyAuditValue(value);
    const canCopy = copyable && hasValue(value);

    return (
        <div className="rounded-xl border border-slate-200 bg-white p-3">
            <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-slate-600">
                    {icon}
                    {label}
                </div>
                {canCopy && (
                    <button
                        type="button"
                        onClick={() => void copyToClipboard(label, value)}
                        title={`Copy ${label}`}
                        className="rounded-md p-1 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-900"
                    >
                        <Copy className="h-3.5 w-3.5" />
                    </button>
                )}
            </div>
            <div
                className={cn(
                    "mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words pr-1 text-sm leading-relaxed text-slate-700",
                    mono && "break-all font-mono text-[11px]"
                )}
            >
                {text}
            </div>
        </div>
    );
}

function JsonBlock({
    title,
    value,
    emptyText,
}: {
    title: string;
    value: unknown;
    emptyText: string;
}) {
    if (!hasValue(value)) {
        return (
            <div className="rounded-xl border border-slate-200 bg-white p-3">
                <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-slate-600">
                    <FileText className="h-3.5 w-3.5" />
                    {title}
                </div>
                <p className="mt-2 text-sm text-slate-500">{emptyText}</p>
            </div>
        );
    }

    return (
        <div className="rounded-xl border border-slate-200 bg-white p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-slate-600">
                    <FileText className="h-3.5 w-3.5" />
                    {title}
                </div>
                <button
                    type="button"
                    onClick={() => void copyToClipboard(title, value)}
                    title={`Copy ${title}`}
                    className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900"
                >
                    <Copy className="h-3.5 w-3.5" />
                    Copy
                </button>
            </div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-900 shadow-inner xl:max-h-[45vh]">
                {stringifyAuditValue(value)}
            </pre>
        </div>
    );
}

function AuditDetailsPanel({
    log,
    onClose,
}: {
    log: AuditLogEntry | null;
    onClose: () => void;
}) {
    if (!log) return null;

    const displayUser = log.user_full_name?.trim() || "System";

    return (
        <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="audit-detail-title"
            className="fixed inset-0 z-50 flex justify-end bg-slate-950/35"
            onClick={onClose}
        >
            <aside
                className="flex h-full w-full max-w-2xl flex-col overflow-hidden border-l border-slate-200 bg-slate-50 shadow-2xl"
                onClick={(event) => event.stopPropagation()}
            >
                <div className="flex shrink-0 items-start justify-between gap-3 border-b border-slate-200 bg-white p-4">
                    <div className="min-w-0">
                        <p className="text-[11px] font-bold uppercase tracking-wide text-slate-600">
                            Audit details
                        </p>
                        <h2 id="audit-detail-title" className="mt-1 break-words text-base font-bold text-ink">
                            {formatLabel(log.action)}
                        </h2>
                        <p className="mt-1 text-xs text-slate-500">{fmtDateTime(log.created_at)}</p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        title="Close details"
                        className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700"
                    >
                        <X className="h-4 w-4" />
                    </button>
                </div>

                <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-4">
                    <div className="grid gap-3 sm:grid-cols-2">
                        <DetailField
                            label="User"
                            value={displayUser}
                            icon={<UserRound className="h-3.5 w-3.5" />}
                            copyable
                        />
                        <DetailField
                            label="Time"
                            value={fmtDateTime(log.created_at)}
                            icon={<Clock className="h-3.5 w-3.5" />}
                        />
                        <DetailField label="Entity" value={log.entity_type} copyable />
                        <DetailField label="Entity ID" value={log.entity_id} mono copyable />
                        <DetailField label="User ID" value={log.user_id} mono copyable />
                        <DetailField
                            label="IP Address"
                            value={log.ip_address}
                            icon={<Monitor className="h-3.5 w-3.5" />}
                            mono
                            copyable
                        />
                    </div>

                    <DetailField label="User Agent" value={log.user_agent} mono copyable />
                    <JsonBlock title="Changes" value={log.changes} emptyText="No change payload was recorded." />
                    <JsonBlock
                        title="Context Metadata"
                        value={log.context_metadata}
                        emptyText="No additional context was recorded."
                    />
                </div>
            </aside>
        </div>
    );
}

export default function AuditLogPage() {
    const [logs, setLogs] = useState<AuditLogEntry[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(1);
    const [total, setTotal] = useState(0);
    const [search, setSearch] = useState("");
    const [actionFilter, setActionFilter] = useState("");
    const [startDate, setStartDate] = useState("");
    const [endDate, setEndDate] = useState("");
    const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
    const [isOffline, setIsOffline] = useState(false);

    const fetchLogs = useCallback(async (targetPage = 1) => {
        setLoading(true);
        setError(null);
        try {
            if (!navigator.onLine || !isBackendReachable()) {
                setIsOffline(true);
                const db = await getDb();
                const rawRows = await db.select<Record<string, unknown>[]>(
                    "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                    [PAGE_SIZE, (targetPage - 1) * PAGE_SIZE]
                );
                const rows = rawRows.map(parseAuditRow);
                const countRows = await db.select<{ c: number }[]>(
                    "SELECT COUNT(*) AS c FROM audit_logs"
                );
                setLogs(rows);
                setTotal(countRows[0]?.c ?? 0);
                setTotalPages(Math.max(1, Math.ceil((countRows[0]?.c ?? 0) / PAGE_SIZE)));
                setPage(targetPage);
                return;
            }
            setIsOffline(false);
            const result = await auditApi.list({
                page: targetPage,
                page_size: PAGE_SIZE,
                search: search || undefined,
                action: actionFilter || undefined,
                start_date: startDate || undefined,
                end_date: endDate || undefined,
            });
            setLogs(result.items);
            setTotal(result.total);
            setTotalPages(result.total_pages);
            setPage(targetPage);
        } catch (err) {
            if (isOfflineError(err) || !isBackendReachable()) {
                setIsOffline(true);
                try {
                    const db = await getDb();
                    const rawRows = await db.select<Record<string, unknown>[]>(
                        "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                        [PAGE_SIZE, (targetPage - 1) * PAGE_SIZE]
                    );
                    const rows = rawRows.map(parseAuditRow);
                    const countRows = await db.select<{ c: number }[]>(
                        "SELECT COUNT(*) AS c FROM audit_logs"
                    );
                    setLogs(rows);
                    setTotal(countRows[0]?.c ?? 0);
                    setTotalPages(Math.max(1, Math.ceil((countRows[0]?.c ?? 0) / PAGE_SIZE)));
                    setPage(targetPage);
                } catch {
                    setError(parseApiError(err));
                }
            } else {
                setError(parseApiError(err));
            }
        } finally {
            setLoading(false);
        }
    }, [search, actionFilter, startDate, endDate]);

    useEffect(() => {
        void fetchLogs(1);
    }, [fetchLogs]);

    useEffect(() => {
        if (selectedLogId && !logs.some((log) => log.id === selectedLogId)) {
            setSelectedLogId(null);
        }
    }, [logs, selectedLogId]);

    useEffect(() => {
        if (!selectedLogId) return;

        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") {
                setSelectedLogId(null);
            }
        };

        window.addEventListener("keydown", handleKeyDown);
        return () => window.removeEventListener("keydown", handleKeyDown);
    }, [selectedLogId]);

    const selectedLog = logs.find((log) => log.id === selectedLogId) ?? null;
    const hasFilters = search || actionFilter || startDate || endDate;

    return (
        <div className="flex h-full flex-col bg-surface">
            <div className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
                <div>
                    <h1 className="font-display flex items-center gap-2 text-2xl font-bold text-ink">
                        <Activity className="h-6 w-6 text-brand-600" />
                        Audit Logs
                    </h1>
                    <p className="mt-0.5 text-sm text-ink-muted">
                        Track user activity and system events
                    </p>
                </div>
                <button
                    type="button"
                    onClick={() => void fetchLogs(page)}
                    disabled={loading}
                    className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-500 transition-colors hover:bg-slate-50 disabled:opacity-50"
                >
                    <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
                    Refresh
                </button>
            </div>

            <div className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white px-6 py-3">
                <div className="relative max-w-sm flex-1 min-w-[200px]">
                    <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                    <input
                        type="text"
                        value={search}
                        onChange={(event) => {
                            setSearch(event.target.value);
                            setPage(1);
                        }}
                        placeholder="Search user, action, entity, payload..."
                        className="h-9 w-full rounded-xl border border-slate-200 bg-surface pl-9 pr-3 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    />
                </div>
                <input
                    type="date"
                    value={startDate}
                    onChange={(event) => {
                        setStartDate(event.target.value);
                        setPage(1);
                    }}
                    className="h-9 rounded-xl border border-slate-200 bg-surface px-3 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    title="Start date"
                />
                <input
                    type="date"
                    value={endDate}
                    onChange={(event) => {
                        setEndDate(event.target.value);
                        setPage(1);
                    }}
                    className="h-9 rounded-xl border border-slate-200 bg-surface px-3 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    title="End date"
                />
                <select
                    value={actionFilter}
                    onChange={(event) => {
                        setActionFilter(event.target.value);
                        setPage(1);
                    }}
                    className="h-9 rounded-xl border border-slate-200 bg-surface px-3 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                >
                    <option value="">All Actions</option>
                    <option value="process_sale">Sale</option>
                    <option value="refund_sale">Refund</option>
                    <option value="create">Create</option>
                    <option value="update">Update</option>
                    <option value="delete">Delete</option>
                    <option value="login">Login</option>
                    <option value="login_failed">Login Failed</option>
                    <option value="mfa_failed">MFA Failed</option>
                </select>
                {hasFilters && (
                    <button
                        type="button"
                        onClick={() => {
                            setSearch("");
                            setActionFilter("");
                            setStartDate("");
                            setEndDate("");
                            setPage(1);
                        }}
                        className="flex h-9 items-center gap-1 rounded-xl border border-slate-200 px-3 text-sm text-slate-500 transition-colors hover:bg-slate-50 hover:text-slate-700"
                    >
                        <X className="h-3.5 w-3.5" />
                        Clear
                    </button>
                )}
            </div>

            <div className="min-h-0 flex-1 overflow-auto p-6">
                {isOffline && (
                    <div className="mb-4 rounded-xl border border-amber-100 bg-amber-50 p-4 text-sm text-amber-700">
                        Showing cached audit logs — you are offline.
                    </div>
                )}
                {error && (
                    <div className="mb-4 rounded-xl border border-red-100 bg-red-50 p-4 text-sm text-red-600">
                        {error}
                    </div>
                )}

                <div className="flex min-h-0 flex-col">
                    <div className="min-h-[22rem] overflow-auto rounded-2xl border border-slate-200 bg-white shadow-sm">
                        <table className="w-full min-w-[980px] border-collapse text-left">
                            <thead>
                                <tr className="border-b border-slate-200 bg-slate-50">
                                    <th className="px-4 py-3 text-[10px] font-bold uppercase tracking-widest text-slate-400">Time</th>
                                    <th className="px-4 py-3 text-[10px] font-bold uppercase tracking-widest text-slate-400">User</th>
                                    <th className="px-4 py-3 text-[10px] font-bold uppercase tracking-widest text-slate-400">Action</th>
                                    <th className="px-4 py-3 text-[10px] font-bold uppercase tracking-widest text-slate-400">Entity</th>
                                    <th className="px-4 py-3 text-[10px] font-bold uppercase tracking-widest text-slate-400">Details</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100">
                                {loading && logs.length === 0 ? (
                                    <tr>
                                        <td colSpan={5} className="px-4 py-12 text-center text-slate-400">
                                            <RefreshCw className="mx-auto mb-2 h-6 w-6 animate-spin" />
                                            Loading activity logs...
                                        </td>
                                    </tr>
                                ) : logs.length === 0 ? (
                                    <tr>
                                        <td colSpan={5} className="px-4 py-12 text-center text-slate-400">
                                            No activity logs found.
                                        </td>
                                    </tr>
                                ) : (
                                    logs.map((log) => {
                                        const displayUser = log.user_full_name?.trim() || "System";
                                        const isSelected = selectedLogId === log.id;

                                        return (
                                            <tr
                                                key={log.id}
                                                aria-selected={isSelected}
                                                onClick={() => setSelectedLogId(log.id)}
                                                className={cn(
                                                    "cursor-pointer align-top transition-colors hover:bg-slate-50",
                                                    isSelected && "bg-brand-50/70 hover:bg-brand-50"
                                                )}
                                            >
                                                <td className="whitespace-nowrap px-4 py-3">
                                                    <div className="flex items-center gap-2 text-xs text-slate-500">
                                                        <Calendar className="h-3 w-3" />
                                                        {fmtDateTime(log.created_at)}
                                                    </div>
                                                </td>
                                                <td className="px-4 py-3">
                                                    <div className="flex items-start gap-2">
                                                        <div className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-slate-100 text-[10px] font-bold text-slate-500">
                                                            {displayUser[0]?.toUpperCase() || "S"}
                                                        </div>
                                                        <span className="break-words text-sm font-medium text-ink">
                                                            {displayUser}
                                                        </span>
                                                    </div>
                                                </td>
                                                <td className="px-4 py-3">
                                                    <span className="inline-flex items-center rounded border border-slate-200 bg-slate-100 px-2 py-0.5 text-[11px] font-bold text-slate-600">
                                                        {formatLabel(log.action)}
                                                    </span>
                                                </td>
                                                <td className="px-4 py-3 text-sm text-slate-500">
                                                    <div className="max-w-[12rem] break-words">
                                                        {log.entity_type || "No entity"}
                                                    </div>
                                                    {log.entity_id && (
                                                        <button
                                                            type="button"
                                                            onClick={(event) => {
                                                                event.stopPropagation();
                                                                void copyToClipboard("Entity ID", log.entity_id);
                                                            }}
                                                            title="Copy full entity ID"
                                                            className="mt-1 block max-w-[12rem] break-all text-left font-mono text-[10px] text-slate-400 transition-colors hover:text-slate-700"
                                                        >
                                                            {log.entity_id}
                                                        </button>
                                                    )}
                                                </td>
                                                <td className="px-4 py-3">
                                                    <ChangesDisplay
                                                        changes={log.changes}
                                                        onViewDetails={() => setSelectedLogId(log.id)}
                                                    />
                                                </td>
                                            </tr>
                                        );
                                    })
                                )}
                            </tbody>
                        </table>
                    </div>

                    {totalPages > 1 && (
                        <div className="mt-4 flex items-center justify-between px-2">
                            <p className="text-xs text-slate-400">
                                Showing page {page} of {totalPages} ({total} entries)
                            </p>
                            <div className="flex gap-2">
                                <button
                                    type="button"
                                    onClick={() => void fetchLogs(page - 1)}
                                    disabled={page <= 1 || loading}
                                    className="rounded-xl border border-slate-200 p-2 text-slate-500 transition-colors hover:bg-white disabled:opacity-40"
                                >
                                    <ChevronLeft className="h-4 w-4" />
                                </button>
                                <button
                                    type="button"
                                    onClick={() => void fetchLogs(page + 1)}
                                    disabled={page >= totalPages || loading}
                                    className="rounded-xl border border-slate-200 p-2 text-slate-500 transition-colors hover:bg-white disabled:opacity-40"
                                >
                                    <ChevronRight className="h-4 w-4" />
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            </div>

            <AuditDetailsPanel log={selectedLog} onClose={() => setSelectedLogId(null)} />
        </div>
    );
}
