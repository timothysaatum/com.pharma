import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
    return twMerge(clsx(inputs));
}

export function formatCurrency(amount: number, currency = "GHS"): string {
    return new Intl.NumberFormat("en-GH", {
        style: "currency",
        currency,
        minimumFractionDigits: 2,
    }).format(amount);
}

export function formatDate(iso: string): string {
    return new Date(iso).toLocaleDateString("en-GH", {
        day: "numeric",
        month: "short",
        year: "numeric",
    });
}