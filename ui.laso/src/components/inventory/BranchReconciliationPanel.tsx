import React, { useState, useEffect } from "react";
import { reconciliationApi } from "@/api/reconciliation";
import type { ReconciliationReportResponse } from "@/types";
import { Card } from "@/components/ui";

export const BranchReconciliationPanel: React.FC<{ branchId: string }> = ({ branchId }) => {
    const [report, setReport] = useState<ReconciliationReportResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        const fetchReport = async () => {
            setLoading(true);
            setError(null);
            try {
                const data = await reconciliationApi.getReconciliationReport(branchId);
                setReport(data);
            } catch (err: any) {
                setError(err.message || "Failed to load report");
            } finally {
                setLoading(false);
            }
        };
        fetchReport();
    }, [branchId]);

    if (loading) return <div className="p-4 text-sm text-ink-muted">Loading report...</div>;
    if (error) return <div className="p-4 text-sm text-red-500">{error}</div>;
    if (!report) return null;

    const mismatches = report.items.filter(item => item.drift !== 0);

    return (
        <div className="flex flex-col gap-6 p-6">
            <h2 className="text-xl font-semibold text-ink">Daily Reconciliation & Drift Auditor</h2>
            
            <div className="grid grid-cols-4 gap-4">
                <Card padding="md">
                    <div className="text-sm text-ink-muted">Total Checked</div>
                    <div className="text-2xl font-semibold mt-1">{report.total_drugs_checked}</div>
                </Card>
                <Card padding="md">
                    <div className="text-sm text-ink-muted">Balanced</div>
                    <div className="text-2xl font-semibold mt-1 text-green-600">{report.balanced_count}</div>
                </Card>
                <Card padding="md">
                    <div className="text-sm text-ink-muted">Drifted</div>
                    <div className="text-2xl font-semibold mt-1 text-amber-600">{report.drift_count}</div>
                </Card>
                <Card padding="md">
                    <div className="text-sm text-ink-muted">Dead Letters</div>
                    <div className="text-2xl font-semibold mt-1 text-red-600">{report.dead_letter_count}</div>
                </Card>
            </div>

            <Card padding="md" className="overflow-hidden">
                <h3 className="text-lg font-medium text-ink mb-4">Mismatch Details</h3>
                {mismatches.length === 0 ? (
                    <div className="text-sm text-ink-muted">No mismatches found.</div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="border-b border-slate-200">
                                    <th className="pb-2 font-medium text-ink-muted">Drug Name</th>
                                    <th className="pb-2 font-medium text-ink-muted">Inventory Qty</th>
                                    <th className="pb-2 font-medium text-ink-muted">Batch Sum</th>
                                    <th className="pb-2 font-medium text-ink-muted">Sellable Qty</th>
                                    <th className="pb-2 font-medium text-ink-muted">Drift</th>
                                    <th className="pb-2 font-medium text-ink-muted">Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {mismatches.map((item) => (
                                    <tr key={item.drug_id} className="border-b border-slate-100 last:border-0">
                                        <td className="py-3 text-ink font-medium">{item.drug_name}</td>
                                        <td className="py-3 text-ink">{item.inventory_quantity}</td>
                                        <td className="py-3 text-ink">{item.batch_sum_quantity}</td>
                                        <td className="py-3 text-ink">{item.sellable_quantity}</td>
                                        <td className="py-3 text-red-600 font-medium">{item.drift > 0 ? `+${item.drift}` : item.drift}</td>
                                        <td className="py-3">
                                            <span className={`inline-flex items-center rounded-lg px-2 py-0.5 text-xs font-medium ${
                                                item.status === 'batch_mismatch' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'
                                            }`}>
                                                {item.status}
                                            </span>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </Card>
        </div>
    );
};
