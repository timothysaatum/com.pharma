import { z } from "zod";

// ── Reusable field validators ─────────────────────────────
const phone = z
    .string()
    .regex(/^\+?[\d\s\-\(\)]{10,20}$/, "Invalid phone number")
    .optional()
    .or(z.literal(""));

const optionalEmail = z
    .string()
    .email("Invalid email address")
    .optional()
    .or(z.literal(""));

// ── Address (mirrors BranchAddress Pydantic schema) ───────
// country has a default so it is always string, never undefined
export const addressSchema = z.object({
    street: z.string().max(255).optional(),
    city: z.string().max(100).optional(),
    state: z.string().max(100).optional(),
    zip_code: z.string().max(20).optional(),
    country: z.string().max(100).default("Ghana"),
});

// ── Step 1: Organization ──────────────────────────────────
export const orgStepSchema = z.object({
    name: z
        .string()
        .min(2, "At least 2 characters required")
        .max(255, "Too long")
        .refine((v) => !/[<>"'\\\/]/.test(v), "Name contains invalid characters"),

    // Fix: Zod v4 removed required_error — just pass the array as const
    type: z.enum(["otc", "pharmacy", "hospital_pharmacy", "chain"] as const),

    license_number: z.string().max(100).optional().or(z.literal("")),
    tax_id: z.string().max(50).optional().or(z.literal("")),
    org_phone: phone,
    org_email: optionalEmail,

    subscription_tier: z
        .enum(["basic", "professional", "enterprise"] as const)
        .default("basic"),

    // Fix: as const — same reason
    currency: z.enum(["GHS", "USD", "EUR", "GBP", "NGN", "KES", "ZAR"] as const),
    timezone: z.string().min(1, "Timezone required").default("Africa/Accra"),
    address: addressSchema.optional(),
});

// ── Step 2: Admin account ─────────────────────────────────
export const adminStepSchema = z
    .object({
        username: z
            .string()
            .min(3, "Minimum 3 characters")
            .max(100, "Too long")
            .regex(/^[a-zA-Z0-9_-]+$/, "Only letters, numbers, hyphens and underscores"),
        admin_email: z.string().email("Valid email required"),
        full_name: z.string().min(2, "Enter your full name").max(255, "Too long"),
        password: z
            .string()
            .min(8, "Minimum 8 characters")
            .max(100, "Too long")
            .refine((v) => /[A-Z]/.test(v), "Must contain an uppercase letter")
            .refine((v) => /[a-z]/.test(v), "Must contain a lowercase letter")
            .refine((v) => /[0-9]/.test(v), "Must contain a number")
            .refine(
                (v) => /[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]/.test(v),
                "Must contain a special character"
            )
            .refine(
                (v) => !["password", "12345678", "qwerty", "abc123"].includes(v.toLowerCase()),
                "Password is too common"
            ),
        confirm_password: z.string(),
        admin_phone: phone,
        employee_id: z.string().max(50).optional().or(z.literal("")),
    })
    .refine((d) => d.password === d.confirm_password, {
        message: "Passwords do not match",
        path: ["confirm_password"],
    });

// ── Step 3: Branches (mirrors BranchCreate Pydantic schema) ──
const branchSchema = z.object({
    name: z.string().min(1, "Branch name is required").max(255, "Too long"),
    code: z.string().max(50).optional().or(z.literal("")),
    branch_phone: phone,
    branch_email: optionalEmail,
    // address.country always has a default so it satisfies BranchAddress.country: str
    address: addressSchema.optional(),
});

export const branchesStepSchema = z
    .object({
        branches: z.array(branchSchema).max(10, "Maximum 10 branches allowed"),
    })
    .refine(
        (d) => {
            const names = d.branches.map((b) => b.name.trim().toLowerCase());
            return names.length === new Set(names).size;
        },
        { message: "Branch names must be unique", path: ["branches"] }
    );

// ── Full combined schema ──────────────────────────────────
export const onboardingSchema = orgStepSchema
    .merge(adminStepSchema)
    .merge(branchesStepSchema);

export type OrgStepValues = z.infer<typeof orgStepSchema>;
export type AdminStepValues = z.infer<typeof adminStepSchema>;
export type BranchesStepValues = z.infer<typeof branchesStepSchema>;
export type OnboardingValues = z.infer<typeof onboardingSchema>;