import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, FilePlus2, FileText, Loader2, Plus, RefreshCw, X } from "lucide-react";
import { isOfflineError, parseApiError } from "@/api/client";
import { prescriptionsApi, type PrescriptionSearchItem } from "@/api/prescriptions";
import { localRead } from "@/lib/localRead";
import { writeLocal } from "@/lib/localWrite";
import { useAuthStore } from "@/stores/authStore";
import type { CartItem } from "@/hooks/useCart";
import type { Prescription, PrescriptionMedication } from "@/types";

interface PrescriptionSelectorProps {
    customerId: string | null;
    rxItems: CartItem[];
    prescriptionId: string | null;
    error?: string;
    onSetPrescriptionId: (id: string | null) => void;
    onSetPrescriptionVerified: (drugId: string, verified: boolean) => void;
}

const inputCls =
    "w-full h-10 px-3 rounded-lg border border-slate-200 text-sm text-ink bg-white " +
    "focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-colors";

function today(): string {
    return new Date().toISOString().slice(0, 10);
}

function addDays(days: number): string {
    const date = new Date();
    date.setDate(date.getDate() + days);
    return date.toISOString().slice(0, 10);
}

function makePrescriptionNumber(): string {
    const iso = new Date().toISOString();
    const stamp = iso.replace(/[-]/g, "").replace(/[:]/g, "").replace(/[TZ.]/g, "").slice(0, 14);
    return `RX-${stamp}`;
}

function itemToMedication(item: CartItem): PrescriptionMedication {
    return {
        drug_id: item.drug.id,
        drug_name: item.drug.name,
        dosage: item.drug.strength || "As prescribed",
        frequency: "As directed",
        duration: "As directed",
        quantity: item.quantity,
    };
}

function toSearchItem(rx: Prescription): PrescriptionSearchItem {
    const expiry = new Date(rx.expiry_date);
    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);

    return {
        id: rx.id,
        prescription_number: rx.prescription_number,
        prescriber_name: rx.prescriber_name,
        medications_count: rx.medications.length,
        issue_date: rx.issue_date,
        expiry_date: rx.expiry_date,
        is_expired: !Number.isNaN(expiry.getTime()) && expiry < todayStart,
        status: rx.status,
        refills_remaining: rx.refills_remaining,
        refills_allowed: rx.refills_allowed,
    };
}

export function PrescriptionSelector({
    customerId,
    rxItems,
    prescriptionId,
    error,
    onSetPrescriptionId,
    onSetPrescriptionVerified,
}: PrescriptionSelectorProps) {
    const { user } = useAuthStore();
    const [prescriptions, setPrescriptions] = useState<PrescriptionSearchItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [creating, setCreating] = useState(false);
    const [showCreate, setShowCreate] = useState(false);
    const [formError, setFormError] = useState<string | null>(null);
    const [prescriptionNumber, setPrescriptionNumber] = useState(makePrescriptionNumber);
    const [prescriberName, setPrescriberName] = useState("");
    const [prescriberLicense, setPrescriberLicense] = useState("");
    const [prescriberPhone, setPrescriberPhone] = useState("");
    const [issueDate, setIssueDate] = useState(today);
    const [expiryDate, setExpiryDate] = useState(() => addDays(30));
    const [refillsAllowed, setRefillsAllowed] = useState(0);
    const [notes, setNotes] = useState("");
    const [medications, setMedications] = useState<PrescriptionMedication[]>(() => rxItems.map(itemToMedication));

    useEffect(() => {
        setMedications((current) => {
            const byDrugId = new Map(current.map((med) => [med.drug_id, med]));
            return rxItems.map((item) => ({
                ...itemToMedication(item),
                ...byDrugId.get(item.drug.id),
                quantity: byDrugId.get(item.drug.id)?.quantity ?? item.quantity,
            }));
        });
    }, [rxItems]);

    const selected = useMemo(
        () => prescriptions.find((rx) => rx.id === prescriptionId) ?? null,
        [prescriptions, prescriptionId]
    );

    const markVerified = useCallback(() => {
        rxItems.forEach((item) => onSetPrescriptionVerified(item.drug.id, true));
    }, [rxItems, onSetPrescriptionVerified]);

    const load = async (signal?: AbortSignal) => {
        if (!customerId) {
            setPrescriptions([]);
            return;
        }
        setLoading(true);
        setLoadError(null);
        try {
            const result = await prescriptionsApi.listForCustomer(
                customerId,
                { page: 1, size: 10, status_filter: "active", include_expired: false },
                signal
            );
            setPrescriptions(result.items ?? []);
        } catch (err) {
            if (err instanceof Error && err.name === "AbortError") return;
            try {
                const fallback = await localRead.searchPrescriptions(
                    { customer_id: customerId, status_filter: "active", include_expired: false },
                    1,
                    10
                );
                setPrescriptions(fallback.items.map(toSearchItem));
                setLoadError(null);
            } catch {
                setLoadError(parseApiError(err));
                setPrescriptions([]);
            }
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        const ctrl = new AbortController();
        void load(ctrl.signal);
        return () => ctrl.abort();
    }, [customerId]);

    const selectPrescription = useCallback((id: string) => {
        onSetPrescriptionId(id);
        markVerified();
        setShowCreate(false);
    }, [onSetPrescriptionId, markVerified]);

    const updateMedication = (drugId: string, patch: Partial<PrescriptionMedication>) => {
        setMedications((prev) => prev.map((med) => med.drug_id === drugId ? { ...med, ...patch } : med));
    };

    const createPrescription = async () => {
        if (!customerId) return;
        setFormError(null);
        if (!prescriberName.trim() || !prescriberLicense.trim()) {
            setFormError("Prescriber name and license are required.");
            return;
        }
        if (medications.some((med) => !med.dosage.trim() || !med.frequency.trim() || !med.duration.trim() || med.quantity <= 0)) {
            setFormError("Complete dosage, frequency, duration, and quantity for every Rx item.");
            return;
        }

        setCreating(true);
        try {
            const payload = {
                prescription_number: prescriptionNumber.trim(),
                customer_id: customerId,
                prescriber_name: prescriberName.trim(),
                prescriber_license: prescriberLicense.trim(),
                prescriber_phone: prescriberPhone.trim() || null,
                issue_date: issueDate,
                expiry_date: expiryDate,
                medications,
                refills_allowed: refillsAllowed,
                notes: notes.trim() || null,
            };
            let saved: Prescription;
            try {
                saved = await prescriptionsApi.create(payload);
                await writeLocal.cachePrescriptions([saved]);
            } catch (err) {
                if (!isOfflineError(err)) throw err;

                const now = new Date().toISOString();
                saved = {
                    id: crypto.randomUUID(),
                    organization_id: user?.organization_id ?? "",
                    ...payload,
                    prescriber_address: null,
                    diagnosis: null,
                    special_instructions: null,
                    refills_remaining: payload.refills_allowed,
                    last_refill_date: null,
                    status: "active",
                    verified_by: null,
                    verified_at: null,
                    sync_status: "pending",
                    sync_version: 1,
                    synced_at: null,
                    created_at: now,
                    updated_at: now,
                };
                await writeLocal.prescription(saved);
            }
            const savedSearchItem = toSearchItem(saved);
            setPrescriptions((prev) => [savedSearchItem, ...prev.filter((rx) => rx.id !== saved.id)]);
            selectPrescription(saved.id);
            setPrescriptionNumber(makePrescriptionNumber());
            setPrescriberName("");
            setPrescriberLicense("");
            setPrescriberPhone("");
            setNotes("");
            setRefillsAllowed(0);
        } catch (err) {
            setFormError(parseApiError(err));
        } finally {
            setCreating(false);
        }
    };

    if (!customerId) {
        return (
            <div className="flex items-start gap-2 p-2.5 rounded-lg bg-amber-50 border border-amber-100">
                <AlertCircle className="w-3.5 h-3.5 text-amber-600 flex-shrink-0 mt-0.5" />
                <p className="text-xs text-amber-700">
                    Select a <strong>registered customer</strong> before linking a prescription.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-2.5">
            <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-slate-500">
                    {selected ? `Selected ${selected.prescription_number}` : "Choose an active prescription for this customer."}
                </p>
                <button
                    type="button"
                    onClick={() => void load()}
                    className="p-1.5 rounded-lg text-slate-400 hover:text-ink hover:bg-slate-100"
                    title="Refresh prescriptions"
                >
                    <RefreshCw className="w-3.5 h-3.5" />
                </button>
            </div>

            {loading ? (
                <div className="flex items-center gap-2 p-3 rounded-lg border border-slate-200 text-xs text-slate-500">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading prescriptions...
                </div>
            ) : loadError ? (
                <div className="p-3 rounded-lg border border-red-100 bg-red-50 text-xs text-red-600">{loadError}</div>
            ) : prescriptions.length > 0 ? (
                <div className="space-y-1.5">
                    {prescriptions.map((rx) => {
                        const isSelected = rx.id === prescriptionId;
                        return (
                            <button
                                key={rx.id}
                                type="button"
                                onClick={() => selectPrescription(rx.id)}
                                className={`w-full flex items-start gap-2.5 p-3 rounded-lg border text-left transition-colors ${
                                    isSelected
                                        ? "border-brand-200 bg-brand-50"
                                        : "border-slate-200 bg-white hover:bg-slate-50"
                                }`}
                            >
                                {isSelected ? (
                                    <CheckCircle2 className="w-4 h-4 text-brand-600 mt-0.5" />
                                ) : (
                                    <FileText className="w-4 h-4 text-slate-400 mt-0.5" />
                                )}
                                <div className="min-w-0 flex-1">
                                    <p className="text-sm font-semibold text-ink truncate">{rx.prescription_number}</p>
                                    <p className="text-xs text-slate-500 truncate">
                                        {rx.prescriber_name} · {rx.medications_count} med{rx.medications_count === 1 ? "" : "s"} · {rx.refills_remaining}/{rx.refills_allowed} refills
                                    </p>
                                    <p className="text-[10px] text-slate-400">Expires {new Date(rx.expiry_date).toLocaleDateString()}</p>
                                </div>
                            </button>
                        );
                    })}
                </div>
            ) : (
                <div className="p-3 rounded-lg border border-slate-200 bg-slate-50 text-xs text-slate-500">
                    No active prescription found for this customer.
                </div>
            )}

            {prescriptionId && (
                <button
                    type="button"
                    onClick={() => {
                        onSetPrescriptionId(null);
                        rxItems.forEach((item) => onSetPrescriptionVerified(item.drug.id, false));
                    }}
                    className="inline-flex items-center gap-1 text-xs font-semibold text-slate-500 hover:text-red-600"
                >
                    <X className="w-3 h-3" /> Clear selected prescription
                </button>
            )}

            <button
                type="button"
                onClick={() => setShowCreate((v) => !v)}
                className="inline-flex items-center gap-1.5 text-xs font-semibold text-brand-700 hover:text-brand-800"
            >
                {showCreate ? <X className="w-3.5 h-3.5" /> : <FilePlus2 className="w-3.5 h-3.5" />}
                {showCreate ? "Cancel new prescription" : "Record new prescription"}
            </button>

            {showCreate && (
                <div className="space-y-3 p-3 rounded-xl border border-brand-100 bg-brand-50/40">
                    <div className="grid grid-cols-2 gap-2">
                        <input value={prescriptionNumber} onChange={(e) => setPrescriptionNumber(e.target.value)} className={inputCls} placeholder="Prescription #" />
                        <input value={prescriberName} onChange={(e) => setPrescriberName(e.target.value)} className={inputCls} placeholder="Prescriber name *" />
                        <input value={prescriberLicense} onChange={(e) => setPrescriberLicense(e.target.value)} className={inputCls} placeholder="License # *" />
                        <input value={prescriberPhone} onChange={(e) => setPrescriberPhone(e.target.value)} className={inputCls} placeholder="Phone" />
                        <input type="date" value={issueDate} onChange={(e) => setIssueDate(e.target.value)} className={inputCls} />
                        <input type="date" value={expiryDate} onChange={(e) => setExpiryDate(e.target.value)} className={inputCls} />
                    </div>

                    <div className="space-y-2">
                        {medications.map((med) => (
                            <div key={med.drug_id} className="p-2 rounded-lg bg-white border border-slate-200 space-y-2">
                                <div className="flex items-center justify-between gap-2">
                                    <p className="text-xs font-bold text-ink truncate">{med.drug_name}</p>
                                    <input
                                        type="number"
                                        min={1}
                                        value={med.quantity}
                                        onChange={(e) => updateMedication(med.drug_id, { quantity: Math.max(1, Number(e.target.value) || 1) })}
                                        className="w-16 h-8 px-2 rounded border border-slate-200 text-xs"
                                        title="Quantity prescribed"
                                    />
                                </div>
                                <div className="grid grid-cols-3 gap-1.5">
                                    <input value={med.dosage} onChange={(e) => updateMedication(med.drug_id, { dosage: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-xs" placeholder="Dosage" />
                                    <input value={med.frequency} onChange={(e) => updateMedication(med.drug_id, { frequency: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-xs" placeholder="Frequency" />
                                    <input value={med.duration} onChange={(e) => updateMedication(med.drug_id, { duration: e.target.value })} className="h-8 px-2 rounded border border-slate-200 text-xs" placeholder="Duration" />
                                </div>
                            </div>
                        ))}
                    </div>

                    <div className="grid grid-cols-[1fr_96px] gap-2">
                        <input value={notes} onChange={(e) => setNotes(e.target.value)} className={inputCls} placeholder="Notes" />
                        <input type="number" min={0} max={10} value={refillsAllowed} onChange={(e) => setRefillsAllowed(Math.max(0, Math.min(10, Number(e.target.value) || 0)))} className={inputCls} title="Refills allowed" />
                    </div>

                    {formError && (
                        <p className="text-xs text-red-600 flex items-center gap-1">
                            <AlertCircle className="w-3 h-3" />{formError}
                        </p>
                    )}

                    <button
                        type="button"
                        onClick={createPrescription}
                        disabled={creating}
                        className="w-full h-10 rounded-lg bg-brand-600 text-white text-sm font-semibold hover:bg-brand-700 disabled:opacity-60 flex items-center justify-center gap-2"
                    >
                        {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                        Save and Link Prescription
                    </button>
                </div>
            )}

            <p className="text-[10px] text-slate-400">
                Prescription must be active, unexpired, and have refills remaining. The server validates it again at checkout.
            </p>
            {error && (
                <p className="text-xs text-red-500 flex items-center gap-1">
                    <AlertCircle className="w-3 h-3" />{error}
                </p>
            )}
        </div>
    );
}
