import { useEffect, useState, useCallback } from "react";
import {
    Activity, RefreshCw, ChevronLeft,
    ChevronRight, Calendar, Search, X
} from "lucide-react";
import { auditApi, type AuditLogEntry } from "@/api/audit";
import { parseApiError } from "@/api/client";

const PAGE_SIZE = 50;

function fmtDateTime(iso: string) {
    return new Date(iso).toLocaleString("en-GH", {
        dateStyle: "medium",
        timeStyle: "short"
    });
}

function ChangesDisplay({ changes }: { changes: any }) {
    if (!changes) return <span className="text-slate-400">—</span>;

    // Handle standard list of keys
    const entries = Object.entries(changes);
    if (entries.length === 0) return <span className="text-slate-400">—</span>;

    return (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
            {entries.map(([key, value]) => (
                <div key={key} className="flex items-center gap-1.5 min-w-0">
                    <span className="text-[10px] font-bold text-slate-400 uppercase tracking-tighter shrink-0">
                        {key.replace(/_/g, " ")}:
                    </span>
                    <span className="text-xs text-slate-700 truncate max-w-[150px]" title={String(value)}>
                        {typeof value === "object" ? JSON.stringify(value) : String(value)}
                    </span>
                </div>
            ))}
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

    const fetchLogs = useCallback(async (targetPage = 1) => {
        setLoading(true);
        setError(null);
        try {
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
            setError(parseApiError(err));
        } finally {
            setLoading(false);
        }
    }, [search, actionFilter, startDate, endDate]);

    useEffect(() => {
        void fetchLogs(1);
    }, [fetchLogs]);

    const hasFilters = search || actionFilter || startDate || endDate;

    return (
        <div className="flex flex-col h-full bg-surface">
            {/* Header */}
            <div className="px-6 py-4 border-b border-slate-200 bg-white flex items-center justify-between">
                <div>
                    <h1 className="font-display text-2xl font-bold text-ink flex items-center gap-2">
                        <Activity className="w-6 h-6 text-brand-600" />
                        Audit Logs
                    </h1>
                    <p className="text-sm text-ink-muted mt-0.5">
                        Track user activity and system events
                    </p>
                </div>
                <button
                    onClick={() => void fetchLogs(page)}
                    disabled={loading}
                    className="flex items-center gap-2 px-3 py-2 text-sm text-slate-500 border border-slate-200 rounded-xl hover:bg-slate-50 disabled:opacity-50 transition-colors"
                >
                    <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
                    Refresh
                </button>
            </div>

            {/* Filters */}
            <div className="px-6 py-3 border-b border-slate-200 bg-white flex flex-wrap gap-3 items-center">
                <div className="relative flex-1 min-w-[200px] max-w-sm">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                    <input
                        type="text"
                        value={search}
                        onChange={(e) => { setSearch(e.target.value); setPage(1); }}
                        placeholder="Search user, action, entity…"
                        className="w-full pl-9 pr-3 h-9 rounded-xl border border-slate-200 text-sm bg-surface focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500"
                    />
                </div>
                <input
                    type="date"
                    value={startDate}
                    onChange={(e) => { setStartDate(e.target.value); setPage(1); }}
                    className="h-9 px-3 rounded-xl border border-slate-200 text-sm bg-surface focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    title="Start date"
                />
                <input
                    type="date"
                    value={endDate}
                    onChange={(e) => { setEndDate(e.target.value); setPage(1); }}
                    className="h-9 px-3 rounded-xl border border-slate-200 text-sm bg-surface focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                    title="End date"
                />
                <select
                    value={actionFilter}
                    onChange={(e) => { setActionFilter(e.target.value); setPage(1); }}
                    className="h-9 px-3 rounded-xl border border-slate-200 text-sm bg-surface focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                >
                    <option value="">All Actions</option>
                    <option value="process_sale">Sale</option>
                    <option value="refund_sale">Refund</option>
                    <option value="create">Create</option>
                    <option value="update">Update</option>
                    <option value="delete">Delete</option>
                    <option value="login">Login</option>
                </select>
                {hasFilters && (
                    <button
                        onClick={() => { setSearch(""); setActionFilter(""); setStartDate(""); setEndDate(""); setPage(1); }}
                        className="flex items-center gap-1 px-3 h-9 text-sm text-slate-500 hover:text-slate-700 border border-slate-200 rounded-xl hover:bg-slate-50 transition-colors"
                    >
                        <X className="w-3.5 h-3.5" />
                        Clear
                    </button>
                )}
            </div>

            {/* Table Area */}
            <div className="flex-1 overflow-auto p-6">
                {error && (
                    <div className="mb-4 p-4 rounded-xl bg-red-50 border border-red-100 text-sm text-red-600">
                        {error}
                    </div>
                )}

                <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden shadow-sm">
                    <table className="w-full text-left border-collapse">
                        <thead>
                            <tr className="bg-slate-50 border-b border-slate-200">
                                <th className="px-4 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">Time</th>
                                <th className="px-4 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">User</th>
                                <th className="px-4 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">Action</th>
                                <th className="px-4 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">Entity</th>
                                <th className="px-4 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">Details</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100">
                            {loading && logs.length === 0 ? (
                                <tr>
                                    <td colSpan={5} className="px-4 py-12 text-center text-slate-400">
                                        <RefreshCw className="w-6 h-6 animate-spin mx-auto mb-2" />
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
                                logs.map((log) => (
                                    <tr key={log.id} className="hover:bg-slate-50 transition-colors">
                                        <td className="px-4 py-3 whitespace-nowrap">
                                            <div className="flex items-center gap-2 text-xs text-slate-500">
                                                <Calendar className="w-3 h-3" />
                                                {fmtDateTime(log.created_at)}
                                            </div>
                                        </td>
                                        <td className="px-4 py-3">
                                            <div className="flex items-center gap-2">
                                                <div className="w-6 h-6 rounded-full bg-slate-100 flex items-center justify-center text-[10px] font-bold text-slate-500">
                                                    {log.user_full_name?.[0] || "?"}
                                                </div>
                                                <span className="text-sm font-medium text-ink">
                                                    {log.user_full_name || "System"}
                                                </span>
                                            </div>
                                        </td>
                                        <td className="px-4 py-3">
                                            <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-bold bg-slate-100 text-slate-600 border border-slate-200">
                                                {log.action.replace(/_/g, " ")}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3 text-sm text-slate-500">
                                            {log.entity_type || "—"}
                                            {log.entity_id && (
                                                <p className="text-[10px] font-mono text-slate-400 mt-0.5">
                                                    {log.entity_id.slice(0, 8)}...
                                                </p>
                                            )}
                                        </td>
                                        <td className="px-4 py-3">
                                            <ChangesDisplay changes={log.changes} />
                                        </td>
                                    </tr>
                                ))
                            )}
                        </tbody>
                    </table>
                </div>

                {/* Pagination */}
                {totalPages > 1 && (
                    <div className="flex items-center justify-between mt-4 px-2">
                        <p className="text-xs text-slate-400">
                            Showing page {page} of {totalPages} ({total} entries)
                        </p>
                        <div className="flex gap-2">
                            <button
                                onClick={() => void fetchLogs(page - 1)}
                                disabled={page <= 1 || loading}
                                className="p-2 rounded-xl border border-slate-200 text-slate-500 hover:bg-white disabled:opacity-40 transition-colors"
                            >
                                <ChevronLeft className="w-4 h-4" />
                            </button>
                            <button
                                onClick={() => void fetchLogs(page + 1)}
                                disabled={page >= totalPages || loading}
                                className="p-2 rounded-xl border border-slate-200 text-slate-500 hover:bg-white disabled:opacity-40 transition-colors"
                            >
                                <ChevronRight className="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
