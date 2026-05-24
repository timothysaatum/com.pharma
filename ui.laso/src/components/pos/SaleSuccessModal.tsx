/**
 * ====================
 * Post-sale confirmation modal. Shows:
 *   - Sale number and total
 *   - Change due
 *   - Loyalty points awarded + tier upgrade
 *   - Non-critical warnings (low stock, near expiry)
 *   - Contract discount saved
 *   - Quick receipt display
 *   - "New Sale" and "Print Receipt" actions
 */

import { motion } from "framer-motion";
import {
    CheckCircle2, Star, AlertTriangle, ArrowRight,
    Printer, X, TrendingUp,
} from "lucide-react";
import type { ProcessSaleResponse } from "@/api/sales";

interface SaleSuccessModalProps {
    result: ProcessSaleResponse;
    onNewSale: () => void;
    onClose: () => void;
}

const PAYMENT_METHOD_LABELS: Record<string, string> = {
    cash: "Cash",
    card: "Card",
    mobile_money: "Mobile Money",
    insurance: "Insurance",
    credit: "Credit",
    split: "Split Payment",
};

export function SaleSuccessModal({ result, onNewSale, onClose }: SaleSuccessModalProps) {
    const { sale } = result;
    const change = sale.change_amount ?? 0;
    const hasLoyalty = result.loyalty_points_awarded > 0;
    const hasWarnings = result.warnings.length > 0;
    const hasSavings = result.estimated_savings > 0;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
            <motion.div
                initial={{ opacity: 0, scale: 0.95, y: 16 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                transition={{ duration: 0.2 }}
                className="bg-white rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto"
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 pt-5 pb-4">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-full bg-green-50 flex items-center justify-center">
                            <CheckCircle2 className="w-6 h-6 text-green-600" />
                        </div>
                        <div>
                            <h2 className="font-display text-lg font-bold text-ink">Sale Complete</h2>
                            <p className="text-xs text-ink-muted font-mono">{sale.sale_number}</p>
                        </div>
                    </div>
                    <button
                        onClick={onClose}
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="px-6 pb-6 space-y-4">
                    {/* Primary financials */}
                    <div className="rounded-2xl bg-green-50 border border-green-100 p-4 space-y-2">
                        <div className="flex justify-between items-center">
                            <span className="text-sm text-green-700">Total charged</span>
                            <span className="text-2xl font-bold text-green-700">
                                ₵{Number(sale.total_amount).toFixed(2)}
                            </span>
                        </div>
                        {sale.amount_paid > 0 && (
                            <div className="flex justify-between text-sm text-green-600">
                                <span>Amount tendered</span>
                                <span>₵{Number(sale.amount_paid).toFixed(2)}</span>
                            </div>
                        )}
                        {change > 0 && (
                            <div className="flex justify-between text-sm font-bold text-green-800 bg-green-100 rounded-lg px-3 py-1.5">
                                <span>Change due</span>
                                <span>₵{Number(change).toFixed(2)}</span>
                            </div>
                        )}
                        <div className="flex justify-between text-xs text-green-600 pt-1 border-t border-green-200">
                            <span>Payment</span>
                            <span>{PAYMENT_METHOD_LABELS[sale.payment_method] ?? sale.payment_method}</span>
                        </div>
                    </div>

                    {/* Contract discount / savings */}
                    {hasSavings && (
                        <div className="flex items-center gap-3 rounded-xl bg-brand-50 border border-brand-100 p-3">
                            <TrendingUp className="w-4 h-4 text-brand-600 flex-shrink-0" />
                            <div>
                                <p className="text-xs font-semibold text-brand-700">
                                    {result.contract_applied}
                                </p>
                                <p className="text-xs text-brand-600">
                                    Customer saved ₵{Number(result.estimated_savings).toFixed(2)}
                                </p>
                            </div>
                        </div>
                    )}

                    {/* Loyalty points */}
                    {hasLoyalty && (
                        <div className="flex items-center gap-3 rounded-xl bg-amber-50 border border-amber-100 p-3">
                            <Star className="w-4 h-4 text-amber-500 flex-shrink-0" />
                            <div>
                                <p className="text-xs font-semibold text-amber-700">
                                    +{result.loyalty_points_awarded} loyalty points awarded
                                </p>
                                {result.loyalty_tier_upgraded && result.new_loyalty_tier && (
                                    <p className="text-xs text-amber-600 font-medium">
                                        🎉 Upgraded to {result.new_loyalty_tier} tier!
                                    </p>
                                )}
                            </div>
                        </div>
                    )}

                    {/* Non-critical warnings */}
                    {hasWarnings && (
                        <div className="rounded-xl bg-amber-50 border border-amber-100 p-3 space-y-1">
                            <p className="text-xs font-semibold text-amber-700 flex items-center gap-1.5">
                                <AlertTriangle className="w-3.5 h-3.5" />
                                Alerts
                            </p>
                            {result.warnings.map((w, i) => (
                                <p key={i} className="text-xs text-amber-700 pl-5">{w}</p>
                            ))}
                        </div>
                    )}

                    {/* Line items summary */}
                    <div className="rounded-xl border border-slate-100 overflow-hidden">
                        <div className="bg-slate-50 px-4 py-2 text-xs font-semibold text-ink-muted uppercase tracking-wide">
                            Items ({sale.items?.length ?? 0})
                        </div>
                        <div className="divide-y divide-slate-100">
                            {sale.items?.map((item) => (
                                <div key={item.id} className="px-4 py-2.5 flex items-center justify-between">
                                    <div>
                                        <p className="text-sm font-medium text-ink">{item.drug_name}</p>
                                        <p className="text-xs text-ink-muted">
                                            {item.quantity} × ₵{Number(item.unit_price).toFixed(2)}
                                            {item.batch_number && (
                                                <span className="ml-2 font-mono bg-slate-100 px-1 rounded text-[10px]">
                                                    {item.batch_number}
                                                </span>
                                            )}
                                        </p>
                                    </div>
                                    <div className="text-right">
                                        <p className="text-sm font-semibold text-ink">
                                            ₵{Number(item.total_price).toFixed(2)}
                                        </p>
                                        {Number(item.total_discount_amount) > 0 && (
                                            <p className="text-xs text-green-600">
                                                −₵{Number(item.total_discount_amount).toFixed(2)}
                                            </p>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                        {/* Discount/tax summary */}
                        {(Number(sale.total_discount_amount) > 0 || Number(sale.tax_amount) > 0) && (
                            <div className="bg-slate-50 px-4 py-2 space-y-1 border-t border-slate-100">
                                {Number(sale.total_discount_amount) > 0 && (
                                    <div className="flex justify-between text-xs text-green-600">
                                        <span>Total discount</span>
                                        <span>−₵{Number(sale.total_discount_amount).toFixed(2)}</span>
                                    </div>
                                )}
                                {Number(sale.tax_amount) > 0 && (
                                    <div className="flex justify-between text-xs text-ink-muted">
                                        <span>Tax</span>
                                        <span>₵{Number(sale.tax_amount).toFixed(2)}</span>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>

                    {/* Actions */}
                    <div className="flex gap-2 pt-1">
                        <button
                            onClick={onClose}
                            type="button"
                            className="flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium text-ink-secondary border border-slate-200 bg-white hover:bg-slate-50 rounded-xl transition-colors"
                        >
                            <Printer className="w-4 h-4" />
                            Print Receipt
                        </button>
                        <button
                            onClick={onNewSale}
                            type="button"
                            className="flex-1 flex items-center justify-center gap-2 py-2.5 text-sm font-bold text-white bg-brand-600 hover:bg-brand-700 rounded-xl transition-colors"
                        >
                            New Sale
                            <ArrowRight className="w-4 h-4" />
                        </button>
                    </div>
                </div>
            </motion.div>
        </div>
    );
}