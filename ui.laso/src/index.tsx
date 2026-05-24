import React from "react";
import { cn } from "@/lib/utils";

// ── Button ────────────────────────────────────────────────
interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
    variant?: "primary" | "secondary" | "ghost" | "danger";
    size?: "sm" | "md" | "lg";
    loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
    (
        { className, variant = "primary", size = "md", loading, children, disabled, ...props },
        ref
    ) => {
        const base =
            "inline-flex items-center justify-center font-sans font-medium rounded-xl transition-all duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed select-none";

        const variants = {
            primary:
                "bg-brand-600 text-white hover:bg-brand-700 active:bg-brand-800 shadow-sm",
            secondary:
                "bg-surface-tertiary text-ink border border-slate-200 hover:bg-slate-100 active:bg-slate-200",
            ghost: "text-ink-secondary hover:bg-surface-tertiary active:bg-slate-100",
            danger:
                "bg-red-500 text-white hover:bg-red-600 active:bg-red-700 shadow-sm",
        };

        const sizes = {
            sm: "h-8 px-3 text-sm gap-1.5",
            md: "h-10 px-4 text-sm gap-2",
            lg: "h-12 px-6 text-base gap-2",
        };

        return (
            <button
                ref={ref}
                disabled={disabled || loading}
                className={cn(base, variants[variant], sizes[size], className)}
                {...props}
            >
                {loading && (
                    <svg
                        className="animate-spin h-4 w-4"
                        fill="none"
                        viewBox="0 0 24 24"
                    >
                        <circle
                            className="opacity-25"
                            cx="12"
                            cy="12"
                            r="10"
                            stroke="currentColor"
                            strokeWidth="4"
                        />
                        <path
                            className="opacity-75"
                            fill="currentColor"
                            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                        />
                    </svg>
                )}
                {children}
            </button>
        );
    }
);
Button.displayName = "Button";

// ── Input ────────────────────────────────────────────────
interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
    error?: string;
    label?: string;
    hint?: string;
    leftIcon?: React.ReactNode;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
    ({ className, error, label, hint, leftIcon, id, ...props }, ref) => {
        const inputId = id ?? label?.toLowerCase().replace(/\s+/g, "-");
        return (
            <div className="flex flex-col gap-1.5">
                {label && (
                    <label
                        htmlFor={inputId}
                        className="text-sm font-medium text-ink"
                    >
                        {label}
                        {props.required && <span className="text-red-500 ml-0.5">*</span>}
                    </label>
                )}
                <div className="relative">
                    {leftIcon && (
                        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted">
                            {leftIcon}
                        </span>
                    )}
                    <input
                        ref={ref}
                        id={inputId}
                        className={cn(
                            "w-full h-10 rounded-xl border bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-muted",
                            "transition-colors duration-150",
                            "focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500",
                            "disabled:opacity-50 disabled:bg-surface-tertiary disabled:cursor-not-allowed",
                            error
                                ? "border-red-400 focus:ring-red-400"
                                : "border-slate-200 hover:border-slate-300",
                            leftIcon && "pl-9",
                            className
                        )}
                        {...props}
                    />
                </div>
                {hint && !error && (
                    <p className="text-xs text-ink-muted">{hint}</p>
                )}
                {error && (
                    <p className="text-xs text-red-500 flex items-center gap-1">
                        <svg className="w-3 h-3 flex-shrink-0" viewBox="0 0 16 16" fill="currentColor">
                            <path d="M8 1a7 7 0 100 14A7 7 0 008 1zM7 4h2v5H7V4zm0 6h2v2H7v-2z" />
                        </svg>
                        {error}
                    </p>
                )}
            </div>
        );
    }
);
Input.displayName = "Input";

// ── Select ────────────────────────────────────────────────
interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
    error?: string;
    label?: string;
    hint?: string;
    options: Array<{ value: string; label: string }>;
    placeholder?: string;
}

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
    ({ className, error, label, hint, options, placeholder, id, ...props }, ref) => {
        const inputId = id ?? label?.toLowerCase().replace(/\s+/g, "-");
        return (
            <div className="flex flex-col gap-1.5">
                {label && (
                    <label htmlFor={inputId} className="text-sm font-medium text-ink">
                        {label}
                        {props.required && <span className="text-red-500 ml-0.5">*</span>}
                    </label>
                )}
                <select
                    ref={ref}
                    id={inputId}
                    className={cn(
                        "w-full h-10 rounded-xl border bg-white px-3 py-2 text-sm text-ink",
                        "transition-colors duration-150 cursor-pointer",
                        "focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500",
                        "disabled:opacity-50 disabled:cursor-not-allowed",
                        error
                            ? "border-red-400 focus:ring-red-400"
                            : "border-slate-200 hover:border-slate-300",
                        className
                    )}
                    {...props}
                >
                    {placeholder && (
                        <option value="" disabled>
                            {placeholder}
                        </option>
                    )}
                    {options.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                            {opt.label}
                        </option>
                    ))}
                </select>
                {hint && !error && <p className="text-xs text-ink-muted">{hint}</p>}
                {error && (
                    <p className="text-xs text-red-500">{error}</p>
                )}
            </div>
        );
    }
);
Select.displayName = "Select";

// ── Card ──────────────────────────────────────────────────
interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
    padding?: "none" | "sm" | "md" | "lg";
}
export const Card = ({ className, padding = "md", children, ...props }: CardProps) => {
    const pads = { none: "", sm: "p-4", md: "p-6", lg: "p-8" };
    return (
        <div
            className={cn(
                "bg-white rounded-2xl border border-slate-100 shadow-card",
                pads[padding],
                className
            )}
            {...props}
        >
            {children}
        </div>
    );
};

// ── Badge ─────────────────────────────────────────────────
interface BadgeProps {
    variant?: "default" | "success" | "warning" | "error" | "info";
    children: React.ReactNode;
    className?: string;
}
export const Badge = ({ variant = "default", children, className }: BadgeProps) => {
    const variants = {
        default: "bg-slate-100 text-ink-secondary",
        success: "bg-brand-50 text-brand-700",
        warning: "bg-amber-50 text-amber-700",
        error: "bg-red-50 text-red-700",
        info: "bg-blue-50 text-blue-700",
    };
    return (
        <span
            className={cn(
                "inline-flex items-center rounded-lg px-2 py-0.5 text-xs font-medium",
                variants[variant],
                className
            )}
        >
            {children}
        </span>
    );
};