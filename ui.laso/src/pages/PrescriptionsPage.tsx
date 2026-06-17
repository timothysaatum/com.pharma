import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, CheckCircle2, Clock, Edit2, FileText, Loader2,
  Plus, RefreshCw, Search, Trash2, X, XCircle,
} from "lucide-react";
import { customersApi, type CustomerQuickLookup } from "@/api/customers";
import { drugApi } from "@/api/drugs";
import { prescriptionsApi } from "@/api/prescriptions";
import { localRead } from "@/lib/localRead";
import { writeLocal } from "@/lib/localWrite";
import { isBackendReachable, isOfflineError, parseApiError } from "@/api/client";
import type { Prescription, PrescriptionMedication, PrescriptionStatus, Drug } from "@/types";

const STATUS_OPTIONS: Array<{ value: "" | PrescriptionStatus; label: string }> = [
  { value: "", label: "All statuses" },
  { value: "active", label: "Active" },
  { value: "filled", label: "Filled" },
  { value: "expired", label: "Expired" },
  { value: "cancelled", label: "Cancelled" },
];

const STATUS_STYLE: Record<string, { label: string; cls: string; icon: React.ElementType }> = {
  active: { label: "Active", cls: "bg-emerald-50 text-emerald-700 border-emerald-100", icon: CheckCircle2 },
  filled: { label: "Filled", cls: "bg-blue-50 text-blue-700 border-blue-100", icon: FileText },
  expired: { label: "Expired", cls: "bg-amber-50 text-amber-700 border-amber-100", icon: Clock },
  cancelled: { label: "Cancelled", cls: "bg-red-50 text-red-700 border-red-100", icon: XCircle },
};

type PrescriptionRow = Prescription & {
  customer_name?: string | null;
  is_expired?: boolean;
};

const inputCls =
  "w-full h-10 px-3 rounded-lg border border-slate-200 text-sm text-ink bg-white " +
  "outline-none focus:ring-2 focus:ring-brand-200 focus:border-brand-500";

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

function formatDate(value: string) {
  return new Date(value).toLocaleDateString("en-GH", { dateStyle: "medium" });
}

function StatusBadge({ status }: { status: string }) {
  const cfg = STATUS_STYLE[status] ?? STATUS_STYLE.active;
  const Icon = cfg.icon;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-bold rounded-full border ${cfg.cls}`}>
      <Icon className="w-3 h-3" />
      {cfg.label}
    </span>
  );
}

export default function PrescriptionsPage() {
  const [items, setItems] = useState<PrescriptionRow[]>([]);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"" | PrescriptionStatus>("");
  const [includeExpired, setIncludeExpired] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [customerSearch, setCustomerSearch] = useState("");
  const [customerMatches, setCustomerMatches] = useState<CustomerQuickLookup[]>([]);
  const [customerSearching, setCustomerSearching] = useState(false);
  const [selectedCustomer, setSelectedCustomer] = useState<CustomerQuickLookup | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [prescriptionNumber, setPrescriptionNumber] = useState(makePrescriptionNumber);
  const [prescriberName, setPrescriberName] = useState("");
  const [prescriberLicense, setPrescriberLicense] = useState("");
  const [prescriberPhone, setPrescriberPhone] = useState("");
  const [issueDate, setIssueDate] = useState(today);
  const [expiryDate, setExpiryDate] = useState(() => addDays(30));
  const [refillsAllowed, setRefillsAllowed] = useState(0);
  const [notes, setNotes] = useState("");
  const [medications, setMedications] = useState<(PrescriptionMedication & { _key: string })[]>([
    {
      drug_id: "",
      drug_name: "",
      dosage: "",
      frequency: "",
      duration: "",
      quantity: 1,
      _key: `med-${Date.now()}-0`,
    },
  ]);

  // Drug search state per medication key
  const [drugSearches, setDrugSearches] = useState<Record<string, string>>({});
  const [drugMatches, setDrugMatches] = useState<Record<string, Drug[]>>({});
  const [drugSearching, setDrugSearching] = useState<Record<string, boolean>>({});

  const query = useMemo(
    () => ({
      page: 1,
      page_size: 100,
      search: search.trim(),
      status_filter: status,
      include_expired: includeExpired,
    }),
    [includeExpired, search, status]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      if (!navigator.onLine || !isBackendReachable()) {
        const response = await localRead.searchPrescriptions(query);
        setItems(response.items as PrescriptionRow[]);
      } else {
        const response = await prescriptionsApi.list(query);
        await writeLocal.cachePrescriptions(response.items);
        setItems(response.items as PrescriptionRow[]);
      }
    } catch (err) {
      if (isOfflineError(err) || !isBackendReachable()) {
        try {
          const response = await localRead.searchPrescriptions(query);
          setItems(response.items as PrescriptionRow[]);
          setError(null);
        } catch (fallbackErr) {
          setError(parseApiError(fallbackErr));
        }
      } else {
        setError(parseApiError(err));
      }
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    const timer = setTimeout(() => void load(), 250);
    return () => clearTimeout(timer);
  }, [load]);

  const updateStatus = async (rx: PrescriptionRow, nextStatus: PrescriptionStatus) => {
    setUpdatingId(rx.id);
    setError(null);
    try {
      const updated = await prescriptionsApi.update(rx.id, { status: nextStatus });
      setItems((current) =>
        current.map((item) => (item.id === rx.id ? { ...item, ...updated } : item))
      );
    } catch (err) {
      setError(parseApiError(err));
    } finally {
      setUpdatingId(null);
    }
  };

  const deletePrescription = async (id: string) => {
    if (!confirm("Are you sure you want to delete this prescription?")) return;
    setUpdatingId(id);
    try {
      await prescriptionsApi.delete(id);
      setItems((current) => current.filter((item) => item.id !== id));
    } catch (err) {
      setError(parseApiError(err));
    } finally {
      setUpdatingId(null);
    }
  };

  const startEdit = (rx: PrescriptionRow) => {
    setEditingId(rx.id);
    setSelectedCustomer({
      id: rx.customer_id,
      full_name: rx.customer_name ?? "Customer",
      phone: null,
      email: null,
      customer_type: "standard",
      loyalty_points: 0,
      loyalty_tier: "bronze",
      has_insurance: false,
      insurance_provider_name: null,
      preferred_contract_name: null,
      eligible_for_senior_discount: false,
    });
    setPrescriptionNumber(rx.prescription_number);
    setPrescriberName(rx.prescriber_name);
    setPrescriberLicense(rx.prescriber_license);
    setPrescriberPhone(rx.prescriber_phone ?? "");
    setIssueDate(rx.issue_date);
    setExpiryDate(rx.expiry_date);
    setRefillsAllowed(rx.refills_allowed);
    setNotes(rx.notes ?? "");
    setMedications(
      (rx.medications as any[]).map((m, i) => ({
        ...m,
        _key: `med-${Date.now()}-${i}`,
      }))
    );
    setCreateOpen(true);
  };

  useEffect(() => {
    if (!createOpen || customerSearch.trim().length < 2 || selectedCustomer) {
      setCustomerMatches([]);
      return;
    }

    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      setCustomerSearching(true);
      try {
        if (!navigator.onLine || !isBackendReachable()) {
          const result = await localRead.searchCustomers({ search: customerSearch.trim() });
          setCustomerMatches(
            result.customers.map((c) => ({
              id: c.id,
              full_name: `${c.first_name || ""} ${c.last_name || ""}`.trim() || "Customer",
              phone: c.phone,
              email: c.email,
              customer_type: c.customer_type,
              loyalty_points: c.loyalty_points ?? 0,
              loyalty_tier: c.loyalty_tier ?? "bronze",
              has_insurance: !!c.insurance_provider_id,
              insurance_provider_name: null,
              preferred_contract_name: null,
              eligible_for_senior_discount: false,
            }))
          );
        } else {
          const result = await customersApi.search(customerSearch.trim(), ctrl.signal);
          setCustomerMatches(result.matches ?? []);
        }
      } catch {
        setCustomerMatches([]);
      } finally {
        setCustomerSearching(false);
      }
    }, 250);

    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [createOpen, customerSearch, selectedCustomer]);

  // Memoize drugSearches to prevent excessive dependency updates
  const drugSearchesKey = useMemo(() => JSON.stringify(drugSearches), [drugSearches]);

  // Drug search effect — only trigger actual searches with debounce
  useEffect(() => {
    if (!createOpen) {
      setDrugMatches({});
      setDrugSearching({});
      return;
    }

    // Get all active searches (queries >= 2 chars)
    const activeSearches = Object.entries(drugSearches)
      .filter(([_, query]) => query.trim().length >= 2)
      .map(([key, query]) => ({ key, query: query.trim() }));

    if (activeSearches.length === 0) {
      setDrugMatches({});
      setDrugSearching({});
      return;
    }

    const ctrl = new AbortController();
    let mounted = true;

    const performSearches = async () => {
      // Set all as searching
      const searchingMap: Record<string, boolean> = {};
      activeSearches.forEach(({ key }) => {
        searchingMap[key] = true;
      });
      setDrugSearching(searchingMap);

      const resultsMap: Record<string, Drug[]> = {};

      // Execute all searches in parallel
      const searchPromises = activeSearches.map(async ({ key, query }) => {
        try {
          if (!navigator.onLine || !isBackendReachable()) {
            const response = await localRead.searchDrugs({ search: query });
            if (mounted) {
              resultsMap[key] = response.items;
            }
          } else {
            const response = await drugApi.list({ search: query }, ctrl.signal);
            if (mounted) {
              resultsMap[key] = response.items;
            }
          }
        } catch {
          if (mounted) {
            resultsMap[key] = [];
          }
        }
      });

      await Promise.all(searchPromises);

      if (mounted) {
        setDrugMatches(resultsMap);
        setDrugSearching({});
      }
    };

    // Debounce with sufficient delay
    const timer = setTimeout(() => {
      void performSearches();
    }, 400);

    return () => {
      ctrl.abort();
      clearTimeout(timer);
      mounted = false;
    };
  }, [createOpen, drugSearchesKey]);

  const resetCreateForm = () => {
    setEditingId(null);
    setSelectedCustomer(null);
    setCustomerSearch("");
    setCustomerMatches([]);
    setPrescriptionNumber(makePrescriptionNumber());
    setPrescriberName("");
    setPrescriberLicense("");
    setPrescriberPhone("");
    setIssueDate(today());
    setExpiryDate(addDays(30));
    setRefillsAllowed(0);
    setNotes("");
    setMedications([
      {
        drug_id: "",
        drug_name: "",
        dosage: "",
        frequency: "",
        duration: "",
        quantity: 1,
        _key: `med-${Date.now()}-0`,
      },
    ]);
    setDrugSearches({});
    setDrugMatches({});
    setDrugSearching({});
    setCreateError(null);
  };

  const updateMedication = (key: string, patch: Partial<PrescriptionMedication>) => {
    setMedications((current) => current.map((med) => med._key === key ? { ...med, ...patch } : med));
  };

  const addMedication = () => {
    const newKey = `med-${Date.now()}-${Math.random()}`;
    setMedications((current) => [
      ...current,
      { drug_id: "", drug_name: "", dosage: "", frequency: "", duration: "", quantity: 1, _key: newKey },
    ]);
    setDrugSearches((prev) => ({ ...prev, [newKey]: "" }));
  };

  const removeMedication = (key: string) => {
    setMedications((current) => current.length === 1 ? current : current.filter((med) => med._key !== key));
    // Clean up search states for removed medication
    setDrugSearches((prev) => {
      const updated = { ...prev };
      delete updated[key];
      return updated;
    });
    setDrugMatches((prev) => {
      const updated = { ...prev };
      delete updated[key];
      return updated;
    });
  };

  const savePrescription = async () => {
    setCreateError(null);
    if (!selectedCustomer) {
      setCreateError("Select a registered customer.");
      return;
    }
    if (!prescriptionNumber.trim() || !prescriberName.trim() || !prescriberLicense.trim()) {
      setCreateError("Prescription number, prescriber name, and license are required.");
      return;
    }

    const cleanedMedications = medications.map((m) => ({
      drug_id: m.drug_id,
      drug_name: m.drug_name,
      dosage: m.dosage,
      frequency: m.frequency,
      duration: m.duration,
      quantity: m.quantity,
    }));

    if (
      cleanedMedications.some(
        (med) =>
          !med.drug_id.trim() ||
          !med.drug_name.trim() ||
          !med.dosage.trim() ||
          !med.frequency.trim() ||
          !med.duration.trim() ||
          med.quantity <= 0
      )
    ) {
      setCreateError("Complete every medication row.");
      return;
    }

    setCreating(true);
    try {
      const data = {
        prescription_number: prescriptionNumber.trim(),
        customer_id: selectedCustomer.id,
        prescriber_name: prescriberName.trim(),
        prescriber_license: prescriberLicense.trim(),
        prescriber_phone: prescriberPhone.trim() || null,
        issue_date: issueDate,
        expiry_date: expiryDate,
        medications: cleanedMedications,
        refills_allowed: refillsAllowed,
        notes: notes.trim() || null,
      };

      if (editingId) {
        const updated = await prescriptionsApi.update(editingId, data);
        setItems((current) =>
          current.map((item) => (item.id === editingId ? { ...item, ...updated } : item))
        );
      } else {
        const id = crypto.randomUUID();
        const now = new Date().toISOString();
        const localPrescriptionData: Omit<Prescription, "sync_status" | "sync_version"> &
          { id: string } = {
          ...data,
          id,
          organization_id: "",
          prescriber_address: null,
          diagnosis: null,
          special_instructions: null,
          refills_remaining: refillsAllowed,
          last_refill_date: null,
          status: "active",
          verified_by: null,
          verified_at: null,
          synced_at: null,
          created_at: now,
          updated_at: now,
        };

        if (!navigator.onLine || !isBackendReachable()) {
          await writeLocal.prescription(localPrescriptionData);
        } else {
          try {
            const saved = await prescriptionsApi.create(data as any);
            await writeLocal.cachePrescriptions([saved]);
          } catch (err) {
            if (!isOfflineError(err)) throw err;
            await writeLocal.prescription(localPrescriptionData);
          }
        }
      }
      resetCreateForm();
      setCreateOpen(false);
      await load();
    } catch (err) {
      setCreateError(parseApiError(err));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-surface">
      <div className="px-6 py-4 border-b border-slate-200 bg-white flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl font-bold text-ink">Prescriptions</h1>
          <p className="text-sm text-ink-muted mt-0.5">{items.length} prescriptions</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              resetCreateForm();
              setCreateOpen(true);
            }}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-brand-600 text-sm font-semibold text-white hover:bg-brand-700"
          >
            <Plus className="w-4 h-4" />
            New Prescription
          </button>
          <button
            onClick={() => void load()}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-slate-200 text-sm font-semibold text-slate-600 hover:bg-slate-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      <div className="px-6 py-3 bg-white border-b border-slate-100 flex items-center gap-3">
        <div className="relative w-full max-w-md">
          <Search className="absolute left-3 top-2.5 w-4 h-4 text-slate-400" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Prescription #, prescriber, customer..."
            className="w-full h-10 pl-9 pr-3 rounded-lg border border-slate-200 text-sm outline-none focus:ring-2 focus:ring-brand-200"
          />
        </div>
        <select
          value={status}
          onChange={(event) => setStatus(event.target.value as "" | PrescriptionStatus)}
          className="h-10 px-3 rounded-lg border border-slate-200 text-sm bg-white"
        >
          {STATUS_OPTIONS.map((option) => (
            <option key={option.value || "all"} value={option.value}>{option.label}</option>
          ))}
        </select>
        <label className="inline-flex items-center gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={includeExpired}
            onChange={(event) => setIncludeExpired(event.target.checked)}
            className="w-4 h-4 rounded accent-brand-600"
          />
          Include expired
        </label>
      </div>

      {error && (
        <div className="mx-6 mt-4 p-3 rounded-lg bg-red-50 border border-red-100 text-sm text-red-700 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4" />
          {error}
        </div>
      )}

      <div className="flex-1 overflow-auto bg-white">
        <table className="min-w-full text-sm">
          <thead className="sticky top-0 bg-white border-b border-slate-100 text-[11px] uppercase tracking-widest text-slate-400">
            <tr>
              <th className="text-left px-6 py-3">Prescription</th>
              <th className="text-left px-6 py-3">Customer</th>
              <th className="text-left px-6 py-3">Prescriber</th>
              <th className="text-left px-6 py-3">Dates</th>
              <th className="text-left px-6 py-3">Refills</th>
              <th className="text-left px-6 py-3">Status</th>
              <th className="text-right px-6 py-3">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {loading && (
              <tr>
                <td colSpan={7} className="px-6 py-10 text-center text-slate-400">
                  <RefreshCw className="w-5 h-5 animate-spin mx-auto mb-2" />
                  Loading prescriptions...
                </td>
              </tr>
            )}
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-6 py-10 text-center text-slate-400">
                  No prescriptions found.
                </td>
              </tr>
            )}
            {!loading && items.map((rx) => (
              <tr key={rx.id} className="hover:bg-slate-50/70">
                <td className="px-6 py-4">
                  <p className="font-bold text-ink font-mono">{rx.prescription_number}</p>
                  <p className="text-xs text-slate-400">{rx.medications?.length ?? 0} medications</p>
                </td>
                <td className="px-6 py-4 text-slate-600">{rx.customer_name ?? "Customer"}</td>
                <td className="px-6 py-4">
                  <p className="font-semibold text-slate-700">{rx.prescriber_name}</p>
                  <p className="text-xs text-slate-400">{rx.prescriber_license}</p>
                </td>
                <td className="px-6 py-4 text-slate-500">
                  <p>{formatDate(rx.issue_date)}</p>
                  <p className={rx.is_expired ? "text-amber-700 text-xs font-semibold" : "text-xs text-slate-400"}>
                    Expires {formatDate(rx.expiry_date)}
                  </p>
                </td>
                <td className="px-6 py-4 font-semibold text-slate-700">
                  {rx.refills_remaining} / {rx.refills_allowed}
                </td>
                <td className="px-6 py-4"><StatusBadge status={rx.status} /></td>
                <td className="px-6 py-4 text-right">
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() => startEdit(rx)}
                      className="p-1.5 rounded-lg border border-slate-200 text-slate-400 hover:text-brand-600 hover:bg-brand-50"
                      title="Edit prescription"
                    >
                      <Edit2 className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={() => void deletePrescription(rx.id)}
                      className="p-1.5 rounded-lg border border-slate-200 text-slate-400 hover:text-red-600 hover:bg-red-50"
                      title="Delete prescription"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                    <select
                      value={rx.status}
                      disabled={updatingId === rx.id}
                      onChange={(event) => void updateStatus(rx, event.target.value as PrescriptionStatus)}
                      className="h-8 px-2 rounded-lg border border-slate-200 bg-white text-xs font-semibold"
                    >
                      {STATUS_OPTIONS.filter((option) => option.value).map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
          <div className="w-full max-w-3xl max-h-[90vh] overflow-hidden bg-white rounded-lg shadow-xl flex flex-col">
            <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
              <div>
                <p className="text-xs font-bold text-slate-400 uppercase tracking-widest">Prescription</p>
                <h2 className="text-lg font-bold text-ink">{editingId ? "Edit Prescription" : "New Prescription"}</h2>
              </div>
              <button
                onClick={() => {
                  setCreateOpen(false);
                  resetCreateForm();
                }}
                className="p-2 rounded-lg text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-5 space-y-5">
              <div>
                <p className="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-2">Customer</p>
                {selectedCustomer ? (
                  <div className="flex items-center justify-between p-3 rounded-lg bg-brand-50 border border-brand-100">
                    <div>
                      <p className="text-sm font-semibold text-brand-800">{selectedCustomer.full_name ?? "Registered customer"}</p>
                      <p className="text-xs text-brand-600">{selectedCustomer.phone ?? selectedCustomer.email ?? selectedCustomer.id}</p>
                    </div>
                    <button
                      onClick={() => setSelectedCustomer(null)}
                      className="text-xs font-semibold text-brand-700 hover:text-red-600"
                    >
                      Change
                    </button>
                  </div>
                ) : (
                  <div className="relative">
                    <Search className="absolute left-3 top-2.5 w-4 h-4 text-slate-400" />
                    <input
                      value={customerSearch}
                      onChange={(event) => setCustomerSearch(event.target.value)}
                      className={`${inputCls} pl-9`}
                      placeholder="Search registered customer by name, phone, email..."
                    />
                    {(customerSearching || customerMatches.length > 0) && (
                      <div className="absolute z-10 mt-1 w-full rounded-lg border border-slate-200 bg-white shadow-lg overflow-hidden">
                        {customerSearching ? (
                          <div className="p-3 text-xs text-slate-400 flex items-center gap-2">
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            Searching...
                          </div>
                        ) : (
                          customerMatches.map((customer) => (
                            <button
                              key={customer.id}
                              type="button"
                              onClick={() => {
                                setSelectedCustomer(customer);
                                setCustomerSearch(customer.full_name ?? customer.phone ?? "");
                              }}
                              className="w-full text-left px-3 py-2 hover:bg-slate-50"
                            >
                              <p className="text-sm font-semibold text-ink">{customer.full_name ?? "Registered customer"}</p>
                              <p className="text-xs text-slate-400">{customer.phone ?? customer.email ?? customer.id}</p>
                            </button>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <input value={prescriptionNumber} onChange={(e) => setPrescriptionNumber(e.target.value)} className={inputCls} placeholder="Prescription number *" />
                <input value={prescriberName} onChange={(e) => setPrescriberName(e.target.value)} className={inputCls} placeholder="Prescriber name *" />
                <input value={prescriberLicense} onChange={(e) => setPrescriberLicense(e.target.value)} className={inputCls} placeholder="License number *" />
                <input value={prescriberPhone} onChange={(e) => setPrescriberPhone(e.target.value)} className={inputCls} placeholder="Prescriber phone" />
                <input type="date" value={issueDate} onChange={(e) => setIssueDate(e.target.value)} className={inputCls} />
                <input type="date" value={expiryDate} onChange={(e) => setExpiryDate(e.target.value)} className={inputCls} />
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <p className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Medications</p>
                  <button onClick={addMedication} className="inline-flex items-center gap-1 text-xs font-semibold text-brand-700">
                    <Plus className="w-3.5 h-3.5" />
                    Add medication
                  </button>
                </div>
                {medications.map((med) => (
                  <div key={med._key} className="p-3 rounded-lg border border-slate-200 bg-slate-50 space-y-2">
                    <div className="grid grid-cols-[1fr_1fr_84px_32px] gap-2">
                      {/* Drug Search/Select */}
                      <div className="relative col-span-2">
                        <div className="flex items-center gap-2">
                          <Search className="absolute left-3 top-2.5 w-4 h-4 text-slate-400 pointer-events-none" />
                          <input
                            value={drugSearches[med._key] || ""}
                            onChange={(e) => {
                              setDrugSearches((prev) => ({ ...prev, [med._key]: e.target.value }));
                            }}
                            placeholder={med.drug_id ? `Selected: ${med.drug_name}` : "Search drug by name or SKU *"}
                            className={`${inputCls} pl-9 flex-1`}
                          />
                          {med.drug_id && (
                            <button
                              type="button"
                              onClick={() => updateMedication(med._key, { drug_id: "", drug_name: "" })}
                              className="px-2 py-1 text-xs font-semibold text-slate-600 hover:text-red-600"
                            >
                              Clear
                            </button>
                          )}
                        </div>
                        {(drugSearching[med._key] || (drugMatches[med._key]?.length ?? 0) > 0) && !med.drug_id && (
                          <div className="absolute z-20 mt-1 w-full rounded-lg border border-slate-200 bg-white shadow-lg overflow-hidden">
                            {drugSearching[med._key] ? (
                              <div className="p-3 text-xs text-slate-400 flex items-center gap-2">
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                Searching drugs...
                              </div>
                            ) : (
                              (drugMatches[med._key] || []).slice(0, 10).map((drug) => (
                                <button
                                  key={drug.id}
                                  type="button"
                                  onClick={() => {
                                    updateMedication(med._key, { drug_id: drug.id, drug_name: drug.name });
                                    setDrugSearches((prev) => ({ ...prev, [med._key]: "" }));
                                  }}
                                  className="w-full text-left px-3 py-2 hover:bg-slate-50 border-b border-slate-100 last:border-0"
                                >
                                  <p className="text-sm font-semibold text-ink">{drug.name}</p>
                                  <p className="text-xs text-slate-400">
                                    {drug.sku ? `SKU: ${drug.sku}` : ""}{drug.generic_name ? ` • ${drug.generic_name}` : ""}
                                  </p>
                                </button>
                              ))
                            )}
                          </div>
                        )}
                      </div>
                      <input
                        type="number"
                        min={1}
                        value={med.quantity}
                        onChange={(e) => updateMedication(med._key, { quantity: Math.max(1, Number(e.target.value) || 1) })}
                        className={inputCls}
                      />
                      <button
                        onClick={() => removeMedication(med._key)}
                        className="rounded-lg border border-slate-200 bg-white text-slate-400 hover:text-red-600"
                      >
                        <Trash2 className="w-4 h-4 mx-auto" />
                      </button>
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                      <input value={med.dosage} onChange={(e) => updateMedication(med._key, { dosage: e.target.value })} className={inputCls} placeholder="Dosage *" />
                      <input value={med.frequency} onChange={(e) => updateMedication(med._key, { frequency: e.target.value })} className={inputCls} placeholder="Frequency *" />
                      <input value={med.duration} onChange={(e) => updateMedication(med._key, { duration: e.target.value })} className={inputCls} placeholder="Duration *" />
                    </div>
                  </div>
                ))}
              </div>

              <div className="grid grid-cols-[1fr_120px] gap-3">
                <input value={notes} onChange={(e) => setNotes(e.target.value)} className={inputCls} placeholder="Notes" />
                <input type="number" min={0} max={10} value={refillsAllowed} onChange={(e) => setRefillsAllowed(Math.max(0, Math.min(10, Number(e.target.value) || 0)))} className={inputCls} title="Refills allowed" />
              </div>

              {createError && (
                <div className="p-3 rounded-lg bg-red-50 border border-red-100 text-sm text-red-700 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4" />
                  {createError}
                </div>
              )}
            </div>

            <div className="px-5 py-4 border-t border-slate-100 flex justify-end gap-2">
              <button
                onClick={() => {
                  setCreateOpen(false);
                  resetCreateForm();
                }}
                className="px-4 py-2 rounded-lg border border-slate-200 text-sm font-semibold text-slate-600 hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                onClick={() => void savePrescription()}
                disabled={creating}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-brand-600 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
              >
                {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                {editingId ? "Update Prescription" : "Save Prescription"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
