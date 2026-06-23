import { useState, useEffect, useCallback, useRef } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { motion, AnimatePresence } from "framer-motion";
import {
    Users, Plus, Search, Filter, MoreVertical,
    Shield, ShieldOff, Trash2, KeyRound, Edit3,
    CheckCircle, XCircle, Clock, ChevronLeft,
    ChevronRight, RefreshCw, UserCog, Eye, EyeOff,
    Building2, Mail, Phone, BadgeCheck, AlertTriangle,
    X, Loader2,
} from "lucide-react";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/authStore";
import { usersApi } from "@/api/users";
import { branchApi } from "@/api/branches";
import { rolesApi } from "@/api/roles";
import { isBackendReachable, isOfflineError, parseApiError } from "@/api/client";
import { offlineCache } from "@/lib/storage";
import { Input, Button } from "@/components/ui";
import type { UserResponse, BranchListItem, Role } from "@/types";

// ─────────────────────────────────────────────────────────────────────────────
// Zod schemas
// ─────────────────────────────────────────────────────────────────────────────

const createUserSchema = z.object({
    username: z.string().min(3).max(100).regex(/^[a-zA-Z0-9_-]+$/, "Alphanumeric, dash or underscore only"),
    email: z.string().email("Invalid email address"),
    full_name: z.string().min(2).max(255),
    password: z
        .string()
        .min(8)
        .max(100)
        .regex(/[A-Z]/, "Must contain an uppercase letter")
        .regex(/[a-z]/, "Must contain a lowercase letter")
        .regex(/[0-9]/, "Must contain a digit")
        .regex(/[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]/, "Must contain a special character"),
    role_ids: z.array(z.string()).min(0),
    phone: z.string().max(20).optional().or(z.literal("")),
    employee_id: z.string().max(50).optional().or(z.literal("")),
    assigned_branches: z.array(z.string()),
});

const editUserSchema = z.object({
    full_name: z.string().min(2).max(255),
    phone: z.string().max(20).optional().or(z.literal("")),
    role_ids: z.array(z.string()).min(0),
    assigned_branches: z.array(z.string()),
});

type CreateValues = z.infer<typeof createUserSchema>;
type EditValues = z.infer<typeof editUserSchema>;

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

function RoleBadge({ role }: { role: Role }) {
    return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-tight bg-brand-50 text-brand-700 border border-brand-100">
            {role.name}
        </span>
    );
}

function StatusDot({ active, locked }: { active: boolean; locked: boolean }) {
    if (locked) return (
        <span className="inline-flex items-center gap-1 text-xs text-amber-600 font-medium">
            <Clock className="w-3 h-3" /> Locked
        </span>
    );
    return active
        ? <span className="inline-flex items-center gap-1 text-xs text-emerald-600 font-medium"><CheckCircle className="w-3 h-3" /> Active</span>
        : <span className="inline-flex items-center gap-1 text-xs text-slate-400 font-medium"><XCircle className="w-3 h-3" /> Inactive</span>;
}

function BranchPicker({
    branches,
    selected,
    onChange,
}: {
    branches: BranchListItem[];
    selected: string[];
    onChange: (ids: string[]) => void;
}) {
    const toggle = (id: string) => {
        onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
    };
    return (
        <div className="flex flex-wrap gap-2">
            {branches.map((b) => {
                const active = selected.includes(String(b.id));
                return (
                    <button
                        key={b.id}
                        type="button"
                        onClick={() => toggle(String(b.id))}
                        className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all ${active
                            ? "border-brand-500 bg-brand-50 text-brand-700"
                            : "border-slate-200 bg-white text-slate-500 hover:border-slate-300"
                            }`}
                    >
                        <Building2 className="w-3 h-3" />
                        {b.name}
                        {active && <CheckCircle className="w-3 h-3 text-brand-500" />}
                    </button>
                );
            })}
            {branches.length === 0 && (
                <p className="text-xs text-slate-400">No branches available</p>
            )}
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Create User Modal
// ─────────────────────────────────────────────────────────────────────────────

function RolePicker({
    roles,
    selected,
    onChange,
}: {
    roles: Role[];
    selected: string[];
    onChange: (ids: string[]) => void;
}) {
    const toggle = (id: string) => {
        onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
    };
    return (
        <div className="flex flex-wrap gap-2">
            {roles.map((r) => {
                const active = selected.includes(String(r.id));
                return (
                    <button
                        key={r.id}
                        type="button"
                        onClick={() => toggle(String(r.id))}
                        className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all ${active
                            ? "border-brand-500 bg-brand-50 text-brand-700"
                            : "border-slate-200 bg-white text-slate-500 hover:border-slate-300"
                            }`}
                    >
                        <Shield className="w-3 h-3" />
                        {r.name}
                    </button>
                );
            })}
        </div>
    );
}

function CreateUserModal({
    branches,
    roles,
    onClose,
    onCreated,
}: {
    branches: BranchListItem[];
    roles: Role[];
    onClose: () => void;
    onCreated: (user: UserResponse) => void;
}) {
    const [showPassword, setShowPassword] = useState(false);
    const [isSubmitting, setIsSubmitting] = useState(false);

    const { register, handleSubmit, formState: { errors }, setValue, watch } = useForm<CreateValues>({
        resolver: zodResolver(createUserSchema),
        defaultValues: {
            role_ids: [],
            assigned_branches: [],
        },
    });

    const selectedBranches = watch("assigned_branches");
    const selectedRoles = watch("role_ids");

    const onSubmit = async (values: CreateValues) => {
        setIsSubmitting(true);
        try {
            const user = await usersApi.create({
                ...values,
                phone: values.phone || undefined,
                employee_id: values.employee_id || undefined,
            });
            onCreated(user);
            toast.success(`${user.full_name} has been created`);
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <ModalShell title="Add New User" icon={<Plus className="w-4 h-4" />} onClose={onClose}>
            <form onSubmit={handleSubmit(onSubmit)} className="space-y-5">
                <div className="grid grid-cols-2 gap-4">
                    <Input label="Full Name" required error={errors.full_name?.message} {...register("full_name")} />
                    <Input label="Username" required placeholder="john_doe" error={errors.username?.message} {...register("username")} />
                </div>
                <div className="grid grid-cols-2 gap-4">
                    <Input label="Email" required type="email" error={errors.email?.message} {...register("email")} />
                    <Input label="Phone" placeholder="+233 24 000 0000" error={errors.phone?.message} {...register("phone")} />
                </div>
                <div className="grid grid-cols-2 gap-4">
                    <Input label="Employee ID" placeholder="EMP001" error={errors.employee_id?.message} {...register("employee_id")} />
                </div>

                <div>
                    <label className="block text-xs font-semibold text-ink mb-2">Roles</label>
                    <RolePicker
                        roles={roles}
                        selected={selectedRoles}
                        onChange={(ids) => setValue("role_ids", ids)}
                    />
                </div>
                <div className="relative">
                    <Input
                        label="Password"
                        required
                        type={showPassword ? "text" : "password"}
                        placeholder="Min 8 chars, upper, lower, digit, symbol"
                        error={errors.password?.message}
                        {...register("password")}
                    />
                    <button
                        type="button"
                        onClick={() => setShowPassword((v) => !v)}
                        className="absolute right-3 top-8 text-ink-muted hover:text-ink"
                        tabIndex={-1}
                    >
                        {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                    </button>
                </div>

                <div>
                    <label className="block text-xs font-semibold text-ink mb-2">Assigned Branches</label>
                    <BranchPicker
                        branches={branches}
                        selected={selectedBranches}
                        onChange={(ids) => setValue("assigned_branches", ids)}
                    />
                </div>

                <div className="flex justify-end gap-3 pt-2 border-t border-slate-100">
                    <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
                    <Button type="submit" loading={isSubmitting} disabled={isSubmitting}>
                        Create User
                    </Button>
                </div>
            </form>
        </ModalShell>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Edit User Modal
// ─────────────────────────────────────────────────────────────────────────────

function EditUserModal({
    user,
    branches,
    roles,
    onClose,
    onUpdated,
}: {
    user: UserResponse;
    branches: BranchListItem[];
    roles: Role[];
    onClose: () => void;
    onUpdated: (user: UserResponse) => void;
}) {
    const [isSubmitting, setIsSubmitting] = useState(false);

    const { register, handleSubmit, formState: { errors }, setValue, watch } = useForm<EditValues>({
        resolver: zodResolver(editUserSchema),
        defaultValues: {
            full_name: user.full_name,
            phone: user.phone ?? "",
            role_ids: user.roles.map(r => r.id),
            assigned_branches: user.assigned_branches ?? [],
        },
    });

    const selectedBranches = watch("assigned_branches");
    const selectedRoles = watch("role_ids");

    const onSubmit = async (values: EditValues) => {
        setIsSubmitting(true);
        try {
            const updated = await usersApi.update(user.id, {
                full_name: values.full_name,
                phone: values.phone || undefined,
                role_ids: values.role_ids,
                assigned_branches: values.assigned_branches,
            });
            onUpdated(updated);
            toast.success("User updated successfully");
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <ModalShell title="Edit User" icon={<Edit3 className="w-4 h-4" />} onClose={onClose}>
            <div className="flex items-center gap-3 p-3 rounded-xl bg-slate-50 border border-slate-100 mb-5">
                <UserAvatar name={user.full_name} size="md" />
                <div>
                    <p className="text-sm font-semibold text-ink">{user.full_name}</p>
                    <p className="text-xs text-ink-muted">@{user.username}</p>
                </div>
                <div className="ml-auto flex gap-1">
                    {user.is_super_admin ? (
                        <span className="bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase">Super Admin</span>
                    ) : (
                        user.roles.map(r => <RoleBadge key={r.id} role={r} />)
                    )}
                </div>
            </div>

            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                    <Input label="Full Name" required error={errors.full_name?.message} {...register("full_name")} />
                    <Input label="Phone" placeholder="+233 24 000 0000" error={errors.phone?.message} {...register("phone")} />
                </div>

                {!user.is_super_admin && (
                    <div>
                        <label className="block text-xs font-semibold text-ink mb-2">Roles</label>
                        <RolePicker
                            roles={roles}
                            selected={selectedRoles}
                            onChange={(ids) => setValue("role_ids", ids)}
                        />
                    </div>
                )}

                <div>
                    <label className="block text-xs font-semibold text-ink mb-2">Assigned Branches</label>
                    <BranchPicker
                        branches={branches}
                        selected={selectedBranches}
                        onChange={(ids) => setValue("assigned_branches", ids)}
                    />
                </div>

                <div className="flex justify-end gap-3 pt-2 border-t border-slate-100">
                    <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
                    <Button type="submit" loading={isSubmitting} disabled={isSubmitting}>
                        Save Changes
                    </Button>
                </div>
            </form>
        </ModalShell>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Confirm Dialog
// ─────────────────────────────────────────────────────────────────────────────

function ConfirmDialog({
    title,
    description,
    confirmLabel,
    danger,
    onConfirm,
    onCancel,
}: {
    title: string;
    description: string;
    confirmLabel: string;
    danger?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}) {
    return (
        <ModalShell title={title} icon={<AlertTriangle className="w-4 h-4 text-red-500" />} onClose={onCancel} small>
            <p className="text-sm text-ink-secondary mb-6">{description}</p>
            <div className="flex justify-end gap-3">
                <Button variant="ghost" onClick={onCancel}>Cancel</Button>
                <Button
                    onClick={onConfirm}
                    className={danger ? "bg-red-600 hover:bg-red-700 text-white" : ""}
                >
                    {confirmLabel}
                </Button>
            </div>
        </ModalShell>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Modal Shell
// ─────────────────────────────────────────────────────────────────────────────

function ModalShell({
    title,
    icon,
    onClose,
    children,
    small,
}: {
    title: string;
    icon: React.ReactNode;
    onClose: () => void;
    children: React.ReactNode;
    small?: boolean;
}) {
    // Close on backdrop click
    const backdropRef = useRef<HTMLDivElement>(null);

    return (
        <div
            ref={backdropRef}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-sm"
            onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}
        >
            <motion.div
                initial={{ opacity: 0, scale: 0.97, y: 8 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.97 }}
                transition={{ duration: 0.18 }}
                className={`w-full bg-white rounded-2xl shadow-2xl overflow-hidden ${small ? "max-w-sm" : "max-w-xl"}`}
            >
                <div className="flex items-center gap-2 px-6 py-4 border-b border-slate-100">
                    <div className="w-7 h-7 rounded-lg bg-brand-50 flex items-center justify-center text-brand-600">
                        {icon}
                    </div>
                    <h2 className="font-display text-sm font-bold text-ink flex-1">{title}</h2>
                    <button onClick={onClose} className="text-ink-muted hover:text-ink transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="px-6 py-5">{children}</div>
            </motion.div>
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// User Avatar
// ─────────────────────────────────────────────────────────────────────────────

function UserAvatar({ name, size = "sm" }: { name: string; size?: "sm" | "md" }) {
    const initials = name.split(" ").map((n) => n[0]).join("").slice(0, 2).toUpperCase();
    const colors = [
        "bg-rose-100 text-rose-600", "bg-orange-100 text-orange-600",
        "bg-amber-100 text-amber-600", "bg-teal-100 text-teal-600",
        "bg-sky-100 text-sky-600", "bg-violet-100 text-violet-600",
        "bg-pink-100 text-pink-600", "bg-indigo-100 text-indigo-600",
    ];
    const color = colors[name.charCodeAt(0) % colors.length];
    const sz = size === "md" ? "w-9 h-9 text-sm" : "w-8 h-8 text-xs";
    return (
        <div className={`${sz} ${color} rounded-full flex items-center justify-center font-bold flex-shrink-0`}>
            {initials}
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Row action menu
// ─────────────────────────────────────────────────────────────────────────────

function ActionMenu({
    user,
    currentUser,
    onEdit,
    onToggleActive,
    onUnlock,
    onDelete,
}: {
    user: UserResponse;
    currentUser: UserResponse;
    onEdit: () => void;
    onToggleActive: () => void;
    onUnlock: () => void;
    onDelete: () => void;
}) {
    const [open, setOpen] = useState(false);
    const menuRef = useRef<HTMLDivElement>(null);
    const isSelf = user.id === currentUser.id;
    const canManageUser = !user.is_super_admin && (
        currentUser.is_super_admin ||
        (currentUser.assigned_branches?.length ?? 0) === 0 || // Org admin
        user.assigned_branches.some((branchId) =>
            currentUser.assigned_branches.map(String).includes(String(branchId))
        )
    );
    const isLocked = !!user.account_locked_until;

    useEffect(() => {
        if (!open) return;
        const handler = (e: MouseEvent) => {
            if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, [open]);

    return (
        <div className="relative" ref={menuRef}>
            <button
                onClick={() => setOpen((v) => !v)}
                className="w-7 h-7 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors"
            >
                <MoreVertical className="w-4 h-4" />
            </button>
            <AnimatePresence>
                {open && (
                    <motion.div
                        initial={{ opacity: 0, scale: 0.95, y: -4 }}
                        animate={{ opacity: 1, scale: 1, y: 0 }}
                        exit={{ opacity: 0, scale: 0.95 }}
                        transition={{ duration: 0.12 }}
                        className="absolute right-0 top-8 z-20 w-48 bg-white rounded-xl shadow-xl border border-slate-100 py-1 overflow-hidden"
                    >
                        <MenuItem icon={<Edit3 className="w-3.5 h-3.5" />} onClick={() => { onEdit(); setOpen(false); }}>
                            Edit details
                        </MenuItem>
                        {canManageUser && !isSelf && (
                            <>
                                <MenuItem
                                    icon={user.is_active
                                        ? <ShieldOff className="w-3.5 h-3.5" />
                                        : <Shield className="w-3.5 h-3.5" />}
                                    onClick={() => { onToggleActive(); setOpen(false); }}
                                >
                                    {user.is_active ? "Deactivate" : "Activate"}
                                </MenuItem>
                                {isLocked && (
                                    <MenuItem icon={<KeyRound className="w-3.5 h-3.5" />} onClick={() => { onUnlock(); setOpen(false); }}>
                                        Unlock account
                                    </MenuItem>
                                )}
                                <div className="h-px bg-slate-100 my-1" />
                                <MenuItem
                                    icon={<Trash2 className="w-3.5 h-3.5" />}
                                    onClick={() => { onDelete(); setOpen(false); }}
                                    danger
                                >
                                    Delete user
                                </MenuItem>
                            </>
                        )}
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
}

function MenuItem({
    icon,
    children,
    onClick,
    danger,
}: {
    icon: React.ReactNode;
    children: React.ReactNode;
    onClick: () => void;
    danger?: boolean;
}) {
    return (
        <button
            onClick={onClick}
            className={`w-full flex items-center gap-2.5 px-3 py-2 text-xs font-medium transition-colors ${danger
                ? "text-red-500 hover:bg-red-50"
                : "text-ink-secondary hover:bg-slate-50 hover:text-ink"
                }`}
        >
            {icon}
            {children}
        </button>
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Page
// ─────────────────────────────────────────────────────────────────────────────

type Modal =
    | { type: "create" }
    | { type: "edit"; user: UserResponse }
    | { type: "confirm_deactivate"; user: UserResponse }
    | { type: "confirm_activate"; user: UserResponse }
    | { type: "confirm_delete"; user: UserResponse }
    | null;

export default function UsersPage() {
    const { user: currentUser } = useAuthStore();

    // ── Data state ──────────────────────────────────────────
    const [users, setUsers] = useState<UserResponse[]>([]);
    const [total, setTotal] = useState(0);
    const [totalPages, setTotalPages] = useState(1);
    const [isLoading, setIsLoading] = useState(true);
    const [branches, setBranches] = useState<BranchListItem[]>([]);
    const [roles, setRoles] = useState<Role[]>([]);

    // ── Filters ─────────────────────────────────────────────
    const [search, setSearch] = useState("");
    const [statusFilter, setStatusFilter] = useState<"" | "active" | "inactive">("");
    const [page, setPage] = useState(1);
    const PAGE_SIZE = 15;

    // ── Modals ──────────────────────────────────────────────
    const [modal, setModal] = useState<Modal>(null);
    const [, setActionLoading] = useState(false);

    const abortRef = useRef<AbortController | null>(null);

    // ── Load branches and roles once ────────────────────────
    useEffect(() => {
        if (!isBackendReachable()) {
            offlineCache.getBranches().then((cached) => setBranches(cached ?? []));
            return;
        }

        Promise.all([
            branchApi.listMine(),
            rolesApi.getRoles()
        ]).then(([branchItems, rolesData]) => {
            setBranches(branchItems);
            setRoles(rolesData);
            offlineCache.setBranches(branchItems);
        }).catch(async (err) => {
            if (isOfflineError(err)) {
                setBranches((await offlineCache.getBranches()) ?? []);
            }
        });
    }, []);

    // ── Fetch users ─────────────────────────────────────────
    const fetchUsers = useCallback(async () => {
        abortRef.current?.abort();
        const ctrl = new AbortController();
        abortRef.current = ctrl;

        setIsLoading(true);
        try {
            if (!isBackendReachable()) {
                const cached = await offlineCache.getUsers();
                const source = cached?.items ?? (currentUser ? [currentUser] : []);
                const filtered = source.filter((item) => {
                    const text = `${item.full_name} ${item.username} ${item.email}`.toLowerCase();
                    const matchesSearch = !search || text.includes(search.toLowerCase());
                    const matchesStatus =
                        !statusFilter ||
                        (statusFilter === "active" ? item.is_active : !item.is_active);
                    return matchesSearch && matchesStatus;
                });
                const start = (page - 1) * PAGE_SIZE;
                const pageItems = filtered.slice(start, start + PAGE_SIZE);
                setUsers(pageItems);
                setTotal(filtered.length);
                setTotalPages(Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)));
                return;
            }
            const result = await usersApi.list(
                {
                    page,
                    page_size: PAGE_SIZE,
                    search: search || undefined,
                    is_active: statusFilter === "active" ? true : statusFilter === "inactive" ? false : null,
                },
                ctrl.signal,
            );
            setUsers(result.items);
            setTotal(result.total);
            setTotalPages(result.total_pages);
            await offlineCache.setUsers(result);
        } catch (err: unknown) {
            // Axios aborts throw code "ERR_CANCELED"; the fetch API throws name "AbortError".
            // Both must be silently swallowed — they are not user-facing errors.
            const isAbort =
                (err as { name?: string })?.name === "AbortError" ||
                (err as { code?: string })?.code === "ERR_CANCELED";
            if (!isAbort && isOfflineError(err)) {
                const cached = await offlineCache.getUsers();
                const source = cached?.items ?? (currentUser ? [currentUser] : []);
                const filtered = source.filter((item) => {
                    const text = `${item.full_name} ${item.username} ${item.email}`.toLowerCase();
                    const matchesSearch = !search || text.includes(search.toLowerCase());
                    const matchesStatus =
                        !statusFilter ||
                        (statusFilter === "active" ? item.is_active : !item.is_active);
                    return matchesSearch && matchesStatus;
                });
                const start = (page - 1) * PAGE_SIZE;
                const pageItems = filtered.slice(start, start + PAGE_SIZE);
                setUsers(pageItems);
                setTotal(filtered.length);
                setTotalPages(Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)));
            } else if (!isAbort) {
                toast.error(parseApiError(err));
            }
        } finally {
            setIsLoading(false);
        }
    }, [page, search, statusFilter, currentUser]);

    useEffect(() => {
        fetchUsers();
    }, [fetchUsers]);

    // Debounce search
    useEffect(() => {
        setPage(1);
    }, [search, statusFilter]);

    // ── Action handlers ─────────────────────────────────────
    const handleToggleActive = async (user: UserResponse) => {
        if (user.is_active) {
            setModal({ type: "confirm_deactivate", user });
        } else {
            setModal({ type: "confirm_activate", user });
        }
    };

    const confirmToggleActive = async (user: UserResponse, active: boolean) => {
        setActionLoading(true);
        try {
            const updated = active
                ? await usersApi.activate(user.id)
                : await usersApi.deactivate(user.id);
            setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
            toast.success(`${user.full_name} has been ${active ? "activated" : "deactivated"}`);
            setModal(null);
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setActionLoading(false);
        }
    };

    const handleUnlock = async (user: UserResponse) => {
        try {
            const updated = await usersApi.unlock(user.id);
            setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
            toast.success(`${user.full_name}'s account has been unlocked`);
        } catch (err) {
            toast.error(parseApiError(err));
        }
    };

    const confirmDelete = async (user: UserResponse) => {
        setActionLoading(true);
        try {
            await usersApi.remove(user.id);
            setUsers((prev) => prev.filter((u) => u.id !== user.id));
            setTotal((t) => t - 1);
            toast.success(`${user.full_name} has been deleted`);
            setModal(null);
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setActionLoading(false);
        }
    };

    if (!currentUser) return null;

    const canCreate = currentUser.is_super_admin || (currentUser.assigned_branches?.length ?? 0) === 0;

    return (
        <div className="flex-1 flex flex-col min-h-0 bg-slate-50">
            {/* ── Header ── */}
            <div className="flex items-center justify-between px-6 py-4 bg-white border-b border-slate-100">
                <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-xl bg-brand-50 flex items-center justify-center">
                        <UserCog className="w-4 h-4 text-brand-600" />
                    </div>
                    <div>
                        <h1 className="font-display text-base font-bold text-ink leading-tight">Users</h1>
                        <p className="text-xs text-ink-muted">
                            {isLoading ? "Loading…" : `${total} member${total !== 1 ? "s" : ""} in your organisation`}
                        </p>
                    </div>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        onClick={fetchUsers}
                        disabled={isLoading}
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 transition-colors disabled:opacity-40"
                        title="Refresh"
                    >
                        <RefreshCw className={`w-4 h-4 ${isLoading ? "animate-spin" : ""}`} />
                    </button>
                    {canCreate && (
                        <Button size="sm" onClick={() => setModal({ type: "create" })}>
                            <Plus className="w-3.5 h-3.5" />
                            Add User
                        </Button>
                    )}
                </div>
            </div>

            {/* ── Filters ── */}
            <div className="flex items-center gap-3 px-6 py-3 bg-white border-b border-slate-100">
                <div className="relative flex-1 max-w-xs">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-muted pointer-events-none" />
                    <input
                        type="search"
                        placeholder="Search by name, username, email…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        className="w-full h-9 pl-9 pr-3 rounded-lg border border-slate-200 bg-white text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-brand-500"
                    />
                </div>

                <div className="flex items-center gap-2">
                    <Filter className="w-3.5 h-3.5 text-ink-muted" />
                    <select
                        value={statusFilter}
                        onChange={(e) => setStatusFilter(e.target.value as "" | "active" | "inactive")}
                        className="h-9 rounded-lg border border-slate-200 bg-white px-3 text-xs text-ink focus:outline-none focus:ring-2 focus:ring-brand-500"
                    >
                        <option value="">All statuses</option>
                        <option value="active">Active</option>
                        <option value="inactive">Inactive</option>
                    </select>
                </div>
            </div>

            {/* ── Table ── */}
            <div className="flex-1 overflow-auto px-6 py-4">
                {isLoading && users.length === 0 ? (
                    <div className="flex items-center justify-center h-64">
                        <div className="flex flex-col items-center gap-3 text-ink-muted">
                            <Loader2 className="w-8 h-8 animate-spin text-brand-400" />
                            <p className="text-sm">Loading users…</p>
                        </div>
                    </div>
                ) : users.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-64 text-ink-muted">
                        <Users className="w-10 h-10 mb-3 opacity-30" />
                        <p className="text-sm font-medium">No users found</p>
                        <p className="text-xs mt-1">Try adjusting your filters</p>
                    </div>
                ) : (
                    <div className="bg-white rounded-xl border border-slate-100 overflow-hidden shadow-sm">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-slate-100 bg-slate-50">
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted">User</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted">Contact</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted">Role</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted">Branches</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted">Status</th>
                                    <th className="text-left px-4 py-3 text-xs font-semibold text-ink-muted">Last Login</th>
                                    <th className="px-4 py-3 w-12" />
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-50">
                                {users.map((user) => {
                                    const isLocked = !!user.account_locked_until;
                                    return (
                                        <motion.tr
                                            key={user.id}
                                            initial={{ opacity: 0 }}
                                            animate={{ opacity: 1 }}
                                            className="hover:bg-slate-50/60 transition-colors"
                                        >
                                            {/* User */}
                                            <td className="px-4 py-3">
                                                <div className="flex items-center gap-3">
                                                    <UserAvatar name={user.full_name} />
                                                    <div className="min-w-0">
                                                        <div className="flex items-center gap-1.5">
                                                            <p className="text-xs font-semibold text-ink truncate">{user.full_name}</p>
                                                            {user.two_factor_enabled && (
                                                                <span title="2FA enabled">
                                                                    <BadgeCheck className="w-3 h-3 text-brand-500 flex-shrink-0" />
                                                                </span>
                                                            )}
                                                        </div>
                                                        <p className="text-xs text-ink-muted truncate">@{user.username}</p>
                                                        {user.employee_id && (
                                                            <p className="text-xs text-ink-muted truncate">{user.employee_id}</p>
                                                        )}
                                                    </div>
                                                </div>
                                            </td>

                                            {/* Contact */}
                                            <td className="px-4 py-3">
                                                <div className="space-y-0.5">
                                                    <div className="flex items-center gap-1 text-xs text-ink-secondary">
                                                        <Mail className="w-3 h-3 text-ink-muted flex-shrink-0" />
                                                        <span className="truncate max-w-[160px]">{user.email}</span>
                                                    </div>
                                                    {user.phone && (
                                                        <div className="flex items-center gap-1 text-xs text-ink-muted">
                                                            <Phone className="w-3 h-3 flex-shrink-0" />
                                                            {user.phone}
                                                        </div>
                                                    )}
                                                </div>
                                            </td>

                                            {/* Role */}
                                            <td className="px-4 py-3">
                                                <div className="flex flex-wrap gap-1">
                                                    {user.is_super_admin ? (
                                                        <span className="bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-tight">Super Admin</span>
                                                    ) : (
                                                        user.roles.map(r => <RoleBadge key={r.id} role={r} />)
                                                    )}
                                                    {!user.is_super_admin && user.roles.length === 0 && (
                                                        <span className="text-xs text-slate-400">No roles</span>
                                                    )}
                                                </div>
                                            </td>

                                            {/* Branches */}
                                            <td className="px-4 py-3">
                                                {user.assigned_branches?.length > 0 ? (
                                                    <div className="flex items-center gap-1 text-xs text-ink-secondary">
                                                        <Building2 className="w-3 h-3 text-ink-muted flex-shrink-0" />
                                                        {user.assigned_branches?.length === 1
                                                            ? branches.find((b) => String(b.id) === user.assigned_branches[0])?.name ?? "1 branch"
                                                            : `${user.assigned_branches?.length ?? 0} branches`}
                                                    </div>
                                                ) : (
                                                    <span className="text-xs text-ink-muted">—</span>
                                                )}
                                            </td>

                                            {/* Status */}
                                            <td className="px-4 py-3">
                                                <StatusDot active={user.is_active} locked={isLocked} />
                                            </td>

                                            {/* Last login */}
                                            <td className="px-4 py-3">
                                                <span className="text-xs text-ink-muted">
                                                    {user.last_login
                                                        ? new Intl.DateTimeFormat("en-GB", {
                                                            day: "2-digit", month: "short", year: "numeric",
                                                            hour: "2-digit", minute: "2-digit",
                                                        }).format(new Date(user.last_login))
                                                        : "Never"}
                                                </span>
                                            </td>

                                            {/* Actions */}
                                            <td className="px-4 py-3">
                                                <ActionMenu
                                                    user={user}
                                                    currentUser={currentUser}
                                                    onEdit={() => setModal({ type: "edit", user })}
                                                    onToggleActive={() => handleToggleActive(user)}
                                                    onUnlock={() => handleUnlock(user)}
                                                    onDelete={() => setModal({ type: "confirm_delete", user })}
                                                />
                                            </td>
                                        </motion.tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* ── Pagination ── */}
            {totalPages > 1 && (
                <div className="flex items-center justify-between px-6 py-3 bg-white border-t border-slate-100">
                    <p className="text-xs text-ink-muted">
                        Page {page} of {totalPages} · {total} total
                    </p>
                    <div className="flex items-center gap-1">
                        <button
                            onClick={() => setPage((p) => p - 1)}
                            disabled={page === 1}
                            className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 disabled:opacity-30 transition-colors"
                        >
                            <ChevronLeft className="w-4 h-4" />
                        </button>
                        {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                            const p = Math.max(1, Math.min(totalPages - 4, page - 2)) + i;
                            return (
                                <button
                                    key={p}
                                    onClick={() => setPage(p)}
                                    className={`w-8 h-8 rounded-lg text-xs font-medium transition-colors ${p === page
                                        ? "bg-brand-500 text-white"
                                        : "text-ink-muted hover:bg-slate-100"
                                        }`}
                                >
                                    {p}
                                </button>
                            );
                        })}
                        <button
                            onClick={() => setPage((p) => p + 1)}
                            disabled={page === totalPages}
                            className="w-8 h-8 flex items-center justify-center rounded-lg text-ink-muted hover:text-ink hover:bg-slate-100 disabled:opacity-30 transition-colors"
                        >
                            <ChevronRight className="w-4 h-4" />
                        </button>
                    </div>
                </div>
            )}

            {/* ── Modals ── */}
            <AnimatePresence>
                {modal?.type === "create" && (
                    <CreateUserModal
                        branches={branches}
                        roles={roles}
                        onClose={() => setModal(null)}
                        onCreated={(user) => {
                            setUsers((prev) => [user, ...prev]);
                            setTotal((t) => t + 1);
                            setModal(null);
                        }}
                    />
                )}
                {modal?.type === "edit" && (
                    <EditUserModal
                        user={modal.user}
                        branches={branches}
                        roles={roles}
                        onClose={() => setModal(null)}
                        onUpdated={(updated) => {
                            setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
                            setModal(null);
                        }}
                    />
                )}
                {modal?.type === "confirm_deactivate" && (
                    <ConfirmDialog
                        title="Deactivate User"
                        description={`${modal.user.full_name} will be deactivated and all active sessions will be revoked. They will not be able to log in.`}
                        confirmLabel="Deactivate"
                        danger
                        onConfirm={() => confirmToggleActive(modal.user, false)}
                        onCancel={() => setModal(null)}
                    />
                )}
                {modal?.type === "confirm_activate" && (
                    <ConfirmDialog
                        title="Activate User"
                        description={`${modal.user.full_name} will be re-activated and can log in again.`}
                        confirmLabel="Activate"
                        onConfirm={() => confirmToggleActive(modal.user, true)}
                        onCancel={() => setModal(null)}
                    />
                )}
                {modal?.type === "confirm_delete" && (
                    <ConfirmDialog
                        title="Delete User"
                        description={`This will permanently delete ${modal.user.full_name}. Their data is retained for audit purposes but they will no longer be able to log in or appear in the system. This cannot be undone.`}
                        confirmLabel="Delete"
                        danger
                        onConfirm={() => confirmDelete(modal.user)}
                        onCancel={() => setModal(null)}
                    />
                )}
            </AnimatePresence>
        </div>
    );
}
