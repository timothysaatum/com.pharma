import { useState, useRef } from "react";
import { motion } from "framer-motion";
import { X, Upload, FileText, AlertCircle, Loader2, Download } from "lucide-react";
import { drugApi } from "@/api/drugs";
import { parseApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import { appEvents } from "@/lib/events";
import { toast } from "sonner";

interface DrugImportWizardProps {
  onClose: () => void;
  onSuccess: () => void;
}

export function DrugImportWizard({ onClose, onSuccess }: DrugImportWizardProps) {
  const { user } = useAuthStore();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [file, setFile] = useState<File | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [results, setResults] = useState<{ successful: number; failed: number; errors: any[] } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile && selectedFile.type === "application/json") {
      setFile(selectedFile);
      setStep(2);
    } else {
      toast.error("Please select a valid JSON file.");
    }
  };

  const processImport = async () => {
    if (!file || !user?.organization_id) return;

    setIsProcessing(true);
    try {
      const text = await file.text();
      const drugs = JSON.parse(text);

      if (!Array.isArray(drugs)) {
        throw new Error("Invalid format: Expected an array of drugs.");
      }

      // Ensure organization_id is set for each drug
      const drugsWithOrg = drugs.map(d => ({
        ...d,
        organization_id: user.organization_id,
        // Default required fields if missing
        unit_price: d.unit_price ?? 0,
      }));

      const response = await drugApi.import(drugsWithOrg);
      setResults(response);
      setStep(3);
      if (response.successful > 0) {
        appEvents.emit("drugs:changed");
      }
    } catch (err) {
      toast.error(parseApiError(err));
    } finally {
      setIsProcessing(false);
    }
  };

  const downloadTemplate = () => {
    const template = [
      {
        name: "Amoxicillin 500mg",
        generic_name: "Amoxicillin",
        sku: "AMOX-500",
        drug_type: "prescription",
        unit_price: 15.50,
        cost_price: 10.00,
        requires_prescription: true,
        reorder_level: 100
      }
    ];
    const blob = new Blob([JSON.stringify(template, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "drug_import_template.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        className="bg-white rounded-2xl shadow-xl w-full max-w-lg overflow-hidden flex flex-col"
      >
        <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
          <h2 className="text-xl font-bold text-ink">Import Drugs</h2>
          <button onClick={onClose} className="p-2 hover:bg-slate-100 rounded-full transition-colors">
            <X className="w-5 h-5 text-ink-muted" />
          </button>
        </div>

        <div className="p-8 flex-1">
          {step === 1 && (
            <div className="text-center space-y-6">
              <div
                onClick={() => fileInputRef.current?.click()}
                className="border-2 border-dashed border-slate-200 rounded-2xl p-12 hover:border-brand-500 hover:bg-brand-50/30 cursor-pointer transition-all group"
              >
                <Upload className="w-12 h-12 text-slate-300 group-hover:text-brand-500 mx-auto mb-4" />
                <p className="text-sm font-medium text-ink">Click to upload or drag and drop</p>
                <p className="text-xs text-ink-muted mt-1">JSON files only</p>
                <input
                  type="file"
                  ref={fileInputRef}
                  onChange={handleFileChange}
                  accept=".json"
                  className="hidden"
                />
              </div>
              <div className="flex flex-col gap-2">
                <button
                  onClick={downloadTemplate}
                  className="text-sm text-brand-600 hover:text-brand-700 font-medium flex items-center justify-center gap-1"
                >
                  <Download className="w-4 h-4" /> Download Sample Template
                </button>
                <p className="text-xs text-ink-muted">
                  Use our template to ensure your data is formatted correctly for import.
                </p>
              </div>
            </div>
          )}

          {step === 2 && file && (
            <div className="space-y-6">
              <div className="flex items-center gap-4 p-4 bg-slate-50 rounded-xl border border-slate-100">
                <FileText className="w-8 h-8 text-brand-500" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-bold text-ink truncate">{file.name}</p>
                  <p className="text-xs text-ink-muted">{(file.size / 1024).toFixed(1)} KB</p>
                </div>
                <button onClick={() => { setFile(null); setStep(1); }} className="text-xs font-bold text-red-600 hover:text-red-700">
                  Change
                </button>
              </div>

              <div className="bg-amber-50 border border-amber-100 p-4 rounded-xl flex gap-3">
                <AlertCircle className="w-5 h-5 text-amber-600 flex-shrink-0" />
                <p className="text-xs text-amber-800 leading-relaxed">
                  Before importing, please ensure your data matches our schema. Duplicate SKUs or barcodes will be skipped.
                  All imported drugs will be assigned to your current organization.
                </p>
              </div>

              <button
                onClick={processImport}
                disabled={isProcessing}
                className="w-full py-3 bg-brand-600 hover:bg-brand-700 text-white font-bold rounded-xl shadow-lg shadow-brand-500/20 flex items-center justify-center gap-2 transition-all disabled:opacity-50"
              >
                {isProcessing ? (
                  <>
                    <Loader2 className="w-5 h-5 animate-spin" />
                    Processing Import...
                  </>
                ) : (
                  "Start Import"
                )}
              </button>
            </div>
          )}

          {step === 3 && results && (
            <div className="space-y-6">
              <div className="grid grid-cols-2 gap-4 text-center">
                <div className="p-4 bg-green-50 border border-green-100 rounded-2xl">
                  <p className="text-2xl font-black text-green-700">{results.successful}</p>
                  <p className="text-xs font-bold text-green-600 uppercase tracking-widest mt-1">Successful</p>
                </div>
                <div className="p-4 bg-red-50 border border-red-100 rounded-2xl">
                  <p className="text-2xl font-black text-red-700">{results.failed}</p>
                  <p className="text-xs font-bold text-red-600 uppercase tracking-widest mt-1">Failed</p>
                </div>
              </div>

              {results.errors.length > 0 && (
                <div className="space-y-2">
                  <p className="text-sm font-bold text-ink">Import Errors</p>
                  <div className="max-h-48 overflow-y-auto border border-slate-100 rounded-xl divide-y divide-slate-50 bg-slate-50/50">
                    {results.errors.map((err, idx) => (
                      <div key={idx} className="p-3">
                        <p className="text-xs font-bold text-ink">{err.drug_name || "Unknown Drug"}</p>
                        <p className="text-[10px] text-red-600 mt-0.5">{err.error}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <button
                onClick={onSuccess}
                className="w-full py-3 bg-slate-900 hover:bg-black text-white font-bold rounded-xl transition-colors"
              >
                Close Wizard
              </button>
            </div>
          )}
        </div>
      </motion.div>
    </div>
  );
}
