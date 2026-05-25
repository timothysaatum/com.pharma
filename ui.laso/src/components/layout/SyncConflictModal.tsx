import { X, AlertTriangle, ShieldCheck, RotateCcw, ArrowRightCircle } from "lucide-react";
import { useState } from "react";
import { useSyncStatus } from "@/hooks/useSyncStatus";
import type { QueuedConflict } from "@/lib/localDb";

interface SyncConflictModalProps {
  open: boolean;
  onClose: () => void;
}

export function SyncConflictModal({ open, onClose }: SyncConflictModalProps) {
  const { conflicts, resolveConflict } = useSyncStatus();
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  if (!open) return null;

  const handleResolve = async (
    conflict: QueuedConflict,
    resolution: "server_wins" | "local_wins"
  ) => {
    setResolvingId(conflict.record_id);
    try {
      await resolveConflict(conflict, resolution);
    } finally {
      setResolvingId(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="w-full max-w-3xl rounded-3xl border border-white/10 bg-slate-950 shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between gap-3 px-6 py-4 border-b border-white/10">
          <div>
            <p className="text-sm font-semibold text-white">Manual sync conflicts</p>
            <p className="text-xs text-slate-400 mt-1">
              Resolve pending records before the next sync cycle can complete.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-full p-2 text-slate-300 hover:bg-white/10 hover:text-white transition"
            aria-label="Close conflict resolution dialog"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto p-6 space-y-4">
          {conflicts.length === 0 ? (
            <div className="rounded-2xl border border-emerald-500/20 bg-emerald-500/5 p-4 text-sm text-emerald-200">
              No manual conflicts are currently pending.
            </div>
          ) : (
            conflicts.map((conflict) => {
              const serverJson = JSON.stringify(conflict.conflict.server_record, null, 2);
              const localJson = JSON.stringify(conflict.local_data, null, 2);

              return (
                <div key={`${conflict.table_name}-${conflict.record_id}`} className="rounded-3xl border border-white/10 bg-slate-900 p-4">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <p className="text-sm font-semibold text-white">
                        {conflict.table_name.replace(/_/g, " ")} conflict
                      </p>
                      <p className="text-xs text-slate-400">
                        Local id: {conflict.record_id}
                      </p>
                      <p className="text-xs text-slate-400 mt-1">
                        Resolution required: <span className="font-semibold text-amber-300">{conflict.conflict.resolution}</span>
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button
                        disabled={resolvingId === conflict.record_id}
                        onClick={() => handleResolve(conflict, "server_wins")}
                        className="inline-flex items-center gap-2 rounded-full bg-emerald-500 px-3 py-2 text-xs font-semibold text-slate-950 transition hover:bg-emerald-400 disabled:cursor-wait disabled:opacity-60"
                      >
                        <ShieldCheck className="w-3.5 h-3.5" />
                        Use server version
                      </button>
                      <button
                        disabled={resolvingId === conflict.record_id}
                        onClick={() => handleResolve(conflict, "local_wins")}
                        className="inline-flex items-center gap-2 rounded-full bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-200 transition hover:bg-slate-700 disabled:cursor-wait disabled:opacity-60"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                        Keep local version
                      </button>
                    </div>
                  </div>

                  <div className="mt-4 grid gap-4 md:grid-cols-2">
                    <div className="rounded-2xl border border-slate-700 bg-slate-950 p-3">
                      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                        <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
                        Server record
                      </div>
                      <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-words text-[11px] text-slate-300">
                        {serverJson}
                      </pre>
                    </div>
                    <div className="rounded-2xl border border-slate-700 bg-slate-950 p-3">
                      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                        <ArrowRightCircle className="w-3.5 h-3.5 text-sky-400" />
                        Local pending payload
                      </div>
                      <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-words text-[11px] text-slate-300">
                        {localJson}
                      </pre>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
