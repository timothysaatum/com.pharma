# POS Checkout - Frontend Implementation Examples

## React/TypeScript Examples

### 1. Prescription Search Component

```typescript
import React, { useState } from 'react';
import axios from 'axios';

interface Prescription {
  id: string;
  prescription_number: string;
  prescriber_name: string;
  medications_count: number;
  issue_date: string;
  expiry_date: string;
  is_expired: boolean;
  status: string;
  refills_remaining: number;
  refills_allowed: number;
}

interface PrescriptionSearchProps {
  customerId: string;
  onSelect: (prescription: Prescription) => void;
}

export const PrescriptionSearchModal: React.FC<PrescriptionSearchProps> = ({
  customerId,
  onSelect,
}) => {
  const [prescriptions, setPrescriptions] = useState<Prescription[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [includeExpired, setIncludeExpired] = useState(false);

  const searchPrescriptions = async () => {
    setLoading(true);
    setError(null);
    
    try {
      const response = await axios.get(
        `/api/v1/prescriptions/customer/${customerId}`,
        {
          params: {
            include_expired: includeExpired,
            page: 1,
            size: 20,
          },
        }
      );
      
      setPrescriptions(response.data.items);
      
      if (response.data.total === 0) {
        setError('No prescriptions found for this customer');
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to search prescriptions');
    } finally {
      setLoading(false);
    }
  };

  React.useEffect(() => {
    if (customerId) {
      searchPrescriptions();
    }
  }, [customerId, includeExpired]);

  return (
    <div className="modal">
      <div className="modal-header">
        <h2>Select Prescription</h2>
      </div>

      <div className="modal-body">
        <label>
          <input
            type="checkbox"
            checked={includeExpired}
            onChange={(e) => setIncludeExpired(e.target.checked)}
          />
          Include Expired Prescriptions
        </label>

        {loading && <div className="spinner">Loading...</div>}

        {error && (
          <div className="alert alert-warning">
            ⚠️ {error}
          </div>
        )}

        {prescriptions.length > 0 && (
          <div className="prescriptions-list">
            {prescriptions.map((rx) => (
              <div
                key={rx.id}
                className={`prescription-card ${
                  rx.is_expired ? 'expired' : ''
                } ${rx.refills_remaining === 0 ? 'no-refills' : ''}`}
              >
                <div className="prescription-header">
                  <span className="rx-number">
                    <strong>RX:</strong> {rx.prescription_number}
                  </span>
                  <span
                    className={`status-badge ${rx.status}`}
                  >
                    {rx.status.toUpperCase()}
                  </span>
                </div>

                <div className="prescription-details">
                  <p>
                    <strong>Prescriber:</strong> {rx.prescriber_name}
                  </p>
                  <p>
                    <strong>Medications:</strong> {rx.medications_count}{' '}
                    drug(s)
                  </p>
                  <p>
                    <strong>Issued:</strong> {rx.issue_date} |{' '}
                    <strong>Expires:</strong> {rx.expiry_date}
                  </p>
                  <p>
                    <strong>Refills:</strong>{' '}
                    <span
                      className={
                        rx.refills_remaining === 0
                          ? 'error'
                          : 'success'
                      }
                    >
                      {rx.refills_remaining} of {rx.refills_allowed}
                    </span>
                  </p>
                </div>

                <button
                  onClick={() => onSelect(rx)}
                  disabled={
                    rx.is_expired ||
                    rx.refills_remaining === 0 ||
                    rx.status !== 'active'
                  }
                  className="btn btn-primary"
                >
                  SELECT
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
```

### 2. Checkout Component (with Prescription Integration)

```typescript
import React, { useState } from 'react';
import axios from 'axios';

interface CartItem {
  drugId: string;
  drugName: string;
  quantity: number;
  requiresPrescription: boolean;
  unitPrice: number;
}

interface CheckoutProps {
  branchId: string;
  customerId: string;
  items: CartItem[];
}

export const CheckoutComponent: React.FC<CheckoutProps> = ({
  branchId,
  customerId,
  items,
}) => {
  const [selectedPrescriptionId, setSelectedPrescriptionId] = useState<
    string | null
  >(null);
  const [paymentMethod, setPaymentMethod] = useState<string>('cash');
  const [amountPaid, setAmountPaid] = useState<number>(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPrescriptionModal, setShowPrescriptionModal] = useState(false);

  // Check if any items require prescription
  const hasRxItems = items.some((item) => item.requiresPrescription);
  const rxItemsMissing =
    hasRxItems && !selectedPrescriptionId;

  // Calculate totals
  const subtotal = items.reduce(
    (sum, item) => sum + item.unitPrice * item.quantity,
    0
  );
  const tax = subtotal * 0.16; // 16% tax
  const total = subtotal + tax;

  const handleSelectPrescription = (prescription: any) => {
    setSelectedPrescriptionId(prescription.id);
    setShowPrescriptionModal(false);
  };

  const handleCheckout = async () => {
    if (rxItemsMissing) {
      setError('Prescription required for Rx items');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      // Build sale request
      const saleRequest = {
        branch_id: branchId,
        customer_id: customerId,
        items: items.map((item) => ({
          drug_id: item.drugId,
          quantity: item.quantity,
        })),
        payment_method: paymentMethod,
        amount_paid: amountPaid,
        ...(selectedPrescriptionId && {
          prescription_id: selectedPrescriptionId,
        }),
      };

      // Submit sale
      const response = await axios.post('/api/v1/sales', saleRequest);

      // Success
      alert(`Sale completed! Receipt #: ${response.data.sale_number}`);
      
      // Could also print receipt or redirect
      printReceipt(response.data);
    } catch (err: any) {
      const errorMsg = err.response?.data?.detail || 'Checkout failed';
      setError(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  const printReceipt = (saleData: any) => {
    // Implement receipt printing logic
    console.log('Printing receipt:', saleData);
  };

  return (
    <div className="checkout-container">
      <h2>Checkout</h2>

      {/* Prescription Alert */}
      {hasRxItems && (
        <div className={`alert ${selectedPrescriptionId ? 'alert-success' : 'alert-danger'}`}>
          {selectedPrescriptionId ? (
            <>
              ✓ Prescription Selected:{' '}
              <strong>{selectedPrescriptionId}</strong>
              <button
                onClick={() => setShowPrescriptionModal(true)}
                className="btn btn-link"
              >
                Change
              </button>
            </>
          ) : (
            <>
              ⚠️ This sale contains prescription-required items.
              <button
                onClick={() => setShowPrescriptionModal(true)}
                className="btn btn-primary"
              >
                Select Prescription
              </button>
            </>
          )}
        </div>
      )}

      {/* Prescription Modal */}
      {showPrescriptionModal && (
        <PrescriptionSearchModal
          customerId={customerId}
          onSelect={handleSelectPrescription}
        />
      )}

      {/* Items */}
      <div className="items-section">
        <h3>Items ({items.length})</h3>
        <table className="items-table">
          <thead>
            <tr>
              <th>Drug</th>
              <th>Qty</th>
              <th>Unit Price</th>
              <th>Total</th>
              <th>Type</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, idx) => (
              <tr key={idx}>
                <td>{item.drugName}</td>
                <td>{item.quantity}</td>
                <td>KES {item.unitPrice.toFixed(2)}</td>
                <td>
                  KES {(item.unitPrice * item.quantity).toFixed(2)}
                </td>
                <td>
                  {item.requiresPrescription ? (
                    <span className="badge badge-danger">Rx</span>
                  ) : (
                    <span className="badge badge-success">OTC</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Totals */}
      <div className="totals-section">
        <div className="total-row">
          <span>Subtotal:</span>
          <span>KES {subtotal.toFixed(2)}</span>
        </div>
        <div className="total-row">
          <span>Tax (16%):</span>
          <span>KES {tax.toFixed(2)}</span>
        </div>
        <div className="total-row total">
          <span>Total:</span>
          <span>KES {total.toFixed(2)}</span>
        </div>
      </div>

      {/* Payment */}
      <div className="payment-section">
        <label>
          Payment Method:
          <select
            value={paymentMethod}
            onChange={(e) => setPaymentMethod(e.target.value)}
          >
            <option value="cash">Cash</option>
            <option value="card">Card</option>
            <option value="mobile_money">Mobile Money</option>
            <option value="insurance">Insurance</option>
          </select>
        </label>

        <label>
          Amount Paid:
          <input
            type="number"
            value={amountPaid}
            onChange={(e) => setAmountPaid(parseFloat(e.target.value))}
            placeholder={total.toFixed(2)}
          />
        </label>

        {amountPaid > 0 && (
          <div className="change">
            Change: KES {(amountPaid - total).toFixed(2)}
          </div>
        )}
      </div>

      {/* Error */}
      {error && <div className="alert alert-danger">{error}</div>}

      {/* Action Buttons */}
      <div className="actions">
        <button
          onClick={handleCheckout}
          disabled={loading || rxItemsMissing || amountPaid < total}
          className="btn btn-success btn-lg"
        >
          {loading ? 'Processing...' : `Complete Sale (KES ${total.toFixed(2)})`}
        </button>
        <button className="btn btn-secondary">Cancel</button>
      </div>
    </div>
  );
};
```

### 3. Prescription Details Modal

```typescript
import React, { useState, useEffect } from 'react';
import axios from 'axios';

interface PrescriptionDetailsProps {
  prescriptionId: string;
  onClose: () => void;
}

export const PrescriptionDetailsModal: React.FC<
  PrescriptionDetailsProps
> = ({ prescriptionId, onClose }) => {
  const [prescription, setPrescription] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchPrescription = async () => {
      try {
        const response = await axios.get(
          `/api/v1/prescriptions/${prescriptionId}`
        );
        setPrescription(response.data);
      } catch (err) {
        console.error('Failed to load prescription:', err);
      } finally {
        setLoading(false);
      }
    };

    fetchPrescription();
  }, [prescriptionId]);

  if (loading) return <div>Loading prescription details...</div>;
  if (!prescription) return <div>Prescription not found</div>;

  return (
    <div className="modal">
      <div className="modal-header">
        <h2>Prescription Details</h2>
        <button onClick={onClose} className="close-btn">
          ×
        </button>
      </div>

      <div className="modal-body">
        {/* Header */}
        <div className="rx-header">
          <div>
            <strong>Prescription #:</strong> {prescription.prescription_number}
          </div>
          <div>
            <strong>Status:</strong>{' '}
            <span className={`badge badge-${prescription.status}`}>
              {prescription.status.toUpperCase()}
            </span>
          </div>
        </div>

        {/* Prescriber */}
        <div className="section">
          <h4>Prescriber Information</h4>
          <p>
            <strong>Name:</strong> {prescription.prescriber_name}
          </p>
          <p>
            <strong>License:</strong> {prescription.prescriber_license}
          </p>
          {prescription.prescriber_phone && (
            <p>
              <strong>Phone:</strong> {prescription.prescriber_phone}
            </p>
          )}
          {prescription.prescriber_address && (
            <p>
              <strong>Address:</strong> {prescription.prescriber_address}
            </p>
          )}
        </div>

        {/* Dates */}
        <div className="section">
          <h4>Prescription Dates</h4>
          <p>
            <strong>Issued:</strong> {prescription.issue_date}
          </p>
          <p>
            <strong>Expires:</strong>{' '}
            <span
              className={prescription.is_expired ? 'text-danger' : ''}
            >
              {prescription.expiry_date}
              {prescription.is_expired && ' (EXPIRED)'}
            </span>
          </p>
        </div>

        {/* Refills */}
        <div className="section">
          <h4>Refill Information</h4>
          <p>
            <strong>Refills Allowed:</strong> {prescription.refills_allowed}
          </p>
          <p>
            <strong>Refills Remaining:</strong>{' '}
            <span
              className={
                prescription.refills_remaining === 0
                  ? 'text-danger'
                  : 'text-success'
              }
            >
              {prescription.refills_remaining}
            </span>
          </p>
          {prescription.last_refill_date && (
            <p>
              <strong>Last Refill:</strong> {prescription.last_refill_date}
            </p>
          )}
        </div>

        {/* Medications */}
        <div className="section">
          <h4>Medications</h4>
          <table className="medications-table">
            <thead>
              <tr>
                <th>Drug</th>
                <th>Dosage</th>
                <th>Frequency</th>
                <th>Duration</th>
                <th>Qty</th>
              </tr>
            </thead>
            <tbody>
              {prescription.medications.map(
                (med: any, idx: number) => (
                  <tr key={idx}>
                    <td>{med.drug_name}</td>
                    <td>{med.dosage}</td>
                    <td>{med.frequency}</td>
                    <td>{med.duration}</td>
                    <td>{med.quantity}</td>
                  </tr>
                )
              )}
            </tbody>
          </table>
        </div>

        {/* Clinical Notes */}
        {prescription.diagnosis && (
          <div className="section">
            <h4>Diagnosis</h4>
            <p>{prescription.diagnosis}</p>
          </div>
        )}

        {prescription.special_instructions && (
          <div className="section">
            <h4>Special Instructions</h4>
            <p>{prescription.special_instructions}</p>
          </div>
        )}

        {prescription.notes && (
          <div className="section">
            <h4>Notes</h4>
            <p>{prescription.notes}</p>
          </div>
        )}

        {/* Verification */}
        {prescription.verified_at && (
          <div className="section">
            <h4>Verification</h4>
            <p>
              <strong>Verified By:</strong> {prescription.verified_by}
            </p>
            <p>
              <strong>Verified At:</strong> {prescription.verified_at}
            </p>
          </div>
        )}
      </div>

      <div className="modal-footer">
        <button onClick={onClose} className="btn btn-secondary">
          Close
        </button>
      </div>
    </div>
  );
};
```

### 4. Create Prescription Admin Panel

```typescript
import React, { useState } from 'react';
import axios from 'axios';

interface CreatePrescriptionProps {
  customerId: string;
  onSuccess: (prescription: any) => void;
}

export const CreatePrescriptionForm: React.FC<
  CreatePrescriptionProps
> = ({ customerId, onSuccess }) => {
  const [formData, setFormData] = useState({
    prescription_number: '',
    prescriber_name: '',
    prescriber_license: '',
    prescriber_phone: '',
    prescriber_address: '',
    issue_date: new Date().toISOString().split('T')[0],
    expiry_date: new Date(
      Date.now() + 30 * 24 * 60 * 60 * 1000
    )
      .toISOString()
      .split('T')[0],
    refills_allowed: 1,
    diagnosis: '',
    notes: '',
    special_instructions: '',
    medications: [
      {
        drug_id: '',
        drug_name: '',
        dosage: '',
        frequency: '',
        duration: '',
        quantity: 1,
      },
    ],
  });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleInputChange = (
    field: string,
    value: any
  ) => {
    setFormData({
      ...formData,
      [field]: value,
    });
  };

  const handleMedicationChange = (
    index: number,
    field: string,
    value: any
  ) => {
    const medications = [...formData.medications];
    medications[index] = {
      ...medications[index],
      [field]: value,
    };
    setFormData({ ...formData, medications });
  };

  const addMedication = () => {
    setFormData({
      ...formData,
      medications: [
        ...formData.medications,
        {
          drug_id: '',
          drug_name: '',
          dosage: '',
          frequency: '',
          duration: '',
          quantity: 1,
        },
      ],
    });
  };

  const removeMedication = (index: number) => {
    const medications = formData.medications.filter(
      (_, i) => i !== index
    );
    setFormData({ ...formData, medications });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const payload = {
        ...formData,
        customer_id: customerId,
      };

      const response = await axios.post(
        '/api/v1/prescriptions',
        payload
      );

      onSuccess(response.data);
      alert(
        'Prescription created successfully!\\nID: ' +
          response.data.id
      );
    } catch (err: any) {
      const errorMsg =
        err.response?.data?.detail || 'Failed to create prescription';
      setError(errorMsg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="prescription-form">
      <h3>Create Prescription</h3>

      {error && <div className="alert alert-danger">{error}</div>}

      {/* Basic Info */}
      <fieldset>
        <legend>Prescription Information</legend>

        <label>
          Prescription Number:
          <input
            type="text"
            value={formData.prescription_number}
            onChange={(e) =>
              handleInputChange(
                'prescription_number',
                e.target.value
              )
            }
            placeholder="RX-2026-001234"
            required
          />
        </label>

        <label>
          Issue Date:
          <input
            type="date"
            value={formData.issue_date}
            onChange={(e) =>
              handleInputChange('issue_date', e.target.value)
            }
            required
          />
        </label>

        <label>
          Expiry Date:
          <input
            type="date"
            value={formData.expiry_date}
            onChange={(e) =>
              handleInputChange('expiry_date', e.target.value)
            }
            required
          />
        </label>

        <label>
          Refills Allowed:
          <input
            type="number"
            min="0"
            max="10"
            value={formData.refills_allowed}
            onChange={(e) =>
              handleInputChange('refills_allowed', parseInt(e.target.value))
            }
            required
          />
        </label>
      </fieldset>

      {/* Prescriber Info */}
      <fieldset>
        <legend>Prescriber Information</legend>

        <label>
          Prescriber Name:
          <input
            type="text"
            value={formData.prescriber_name}
            onChange={(e) =>
              handleInputChange('prescriber_name', e.target.value)
            }
            placeholder="Dr. Jane Smith"
            required
          />
        </label>

        <label>
          Medical License:
          <input
            type="text"
            value={formData.prescriber_license}
            onChange={(e) =>
              handleInputChange('prescriber_license', e.target.value)
            }
            placeholder="MD123456"
            required
          />
        </label>

        <label>
          Phone:
          <input
            type="tel"
            value={formData.prescriber_phone}
            onChange={(e) =>
              handleInputChange('prescriber_phone', e.target.value)
            }
          />
        </label>

        <label>
          Address:
          <textarea
            value={formData.prescriber_address}
            onChange={(e) =>
              handleInputChange('prescriber_address', e.target.value)
            }
          />
        </label>
      </fieldset>

      {/* Medications */}
      <fieldset>
        <legend>Medications</legend>

        {formData.medications.map((med, idx) => (
          <div key={idx} className="medication-entry">
            <h5>Medication {idx + 1}</h5>

            <label>
              Drug Name:
              <input
                type="text"
                value={med.drug_name}
                onChange={(e) =>
                  handleMedicationChange(idx, 'drug_name', e.target.value)
                }
                placeholder="Amoxicillin"
                required
              />
            </label>

            <label>
              Dosage:
              <input
                type="text"
                value={med.dosage}
                onChange={(e) =>
                  handleMedicationChange(idx, 'dosage', e.target.value)
                }
                placeholder="500mg"
                required
              />
            </label>

            <label>
              Frequency:
              <input
                type="text"
                value={med.frequency}
                onChange={(e) =>
                  handleMedicationChange(idx, 'frequency', e.target.value)
                }
                placeholder="Twice daily"
                required
              />
            </label>

            <label>
              Duration:
              <input
                type="text"
                value={med.duration}
                onChange={(e) =>
                  handleMedicationChange(idx, 'duration', e.target.value)
                }
                placeholder="7 days"
                required
              />
            </label>

            <label>
              Quantity:
              <input
                type="number"
                min="1"
                value={med.quantity}
                onChange={(e) =>
                  handleMedicationChange(
                    idx,
                    'quantity',
                    parseInt(e.target.value)
                  )
                }
                required
              />
            </label>

            {formData.medications.length > 1 && (
              <button
                type="button"
                onClick={() => removeMedication(idx)}
                className="btn btn-danger btn-sm"
              >
                Remove
              </button>
            )}
          </div>
        ))}

        <button
          type="button"
          onClick={addMedication}
          className="btn btn-secondary"
        >
          + Add Medication
        </button>
      </fieldset>

      {/* Additional Info */}
      <fieldset>
        <legend>Additional Information</legend>

        <label>
          Diagnosis:
          <textarea
            value={formData.diagnosis}
            onChange={(e) =>
              handleInputChange('diagnosis', e.target.value)
            }
            placeholder="Bacterial respiratory infection"
          />
        </label>

        <label>
          Special Instructions:
          <textarea
            value={formData.special_instructions}
            onChange={(e) =>
              handleInputChange('special_instructions', e.target.value)
            }
            placeholder="Do not crush tablets. Take with food."
          />
        </label>

        <label>
          Notes:
          <textarea
            value={formData.notes}
            onChange={(e) =>
              handleInputChange('notes', e.target.value)
            }
          />
        </label>
      </fieldset>

      <button
        type="submit"
        disabled={loading}
        className="btn btn-primary btn-lg"
      >
        {loading ? 'Creating...' : 'Create Prescription'}
      </button>
    </form>
  );
};
```

---

## Summary

These components provide:

1. **PrescriptionSearchModal** - Find prescriptions during checkout
2. **CheckoutComponent** - Complete POS checkout with Rx validation
3. **PrescriptionDetailsModal** - View full prescription details
4. **CreatePrescriptionForm** - Admin panel to enter new prescriptions

All integrate with your API endpoints for professional pharmacy management!

