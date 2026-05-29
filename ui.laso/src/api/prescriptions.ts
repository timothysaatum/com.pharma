import { get, post } from "./client";
import type { PaginatedResponse, Prescription, PrescriptionMedication } from "@/types";

export interface PrescriptionSearchItem {
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

export interface PrescriptionCreate {
    prescription_number: string;
    customer_id: string;
    prescriber_name: string;
    prescriber_license: string;
    prescriber_phone?: string | null;
    prescriber_address?: string | null;
    issue_date: string;
    expiry_date: string;
    medications: PrescriptionMedication[];
    refills_allowed: number;
    diagnosis?: string | null;
    notes?: string | null;
    special_instructions?: string | null;
}

export const prescriptionsApi = {
    listForCustomer(
        customerId: string,
        params: {
            page?: number;
            size?: number;
            status_filter?: string;
            include_expired?: boolean;
        } = {},
        signal?: AbortSignal
    ): Promise<PaginatedResponse<PrescriptionSearchItem> & { size?: number }> {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([key, value]) => {
            if (value !== undefined && value !== null && value !== "") qs.set(key, String(value));
        });
        return get<PaginatedResponse<PrescriptionSearchItem> & { size?: number }>(
            `/prescriptions/customer/${customerId}?${qs}`,
            { signal }
        );
    },

    create(data: PrescriptionCreate): Promise<Prescription> {
        return post<Prescription>("/prescriptions/", data);
    },
};
