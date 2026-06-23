import { useState, useEffect } from "react";
import {
    Shield, Plus, Trash2, Edit3, Check,
    Loader2
} from "lucide-react";
import { toast } from "sonner";
import { rolesApi } from "@/api/roles";
import { Button, Input } from "@/components/ui";
import { Role, PermissionInfo } from "@/types";
import { parseApiError } from "@/api/client";
import { motion, AnimatePresence } from "framer-motion";

export function RolesTab() {
    const [roles, setRoles] = useState<Role[]>([]);
    const [permissions, setPermissions] = useState<PermissionInfo[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [isEditing, setIsEditing] = useState<Role | null>(null);
    const [isCreating, setIsCreating] = useState(false);

    useEffect(() => {
        const loadData = async () => {
            try {
                const [rolesData, permsData] = await Promise.all([
                    rolesApi.getRoles(),
                    rolesApi.getPermissions()
                ]);
                setRoles(rolesData);
                setPermissions(permsData);
            } catch (err) {
                toast.error(parseApiError(err));
            } finally {
                setIsLoading(false);
            }
        };
        loadData();
    }, []);

    const handleDelete = async (role: Role) => {
        if (!confirm(`Are you sure you want to delete the role "${role.name}"?`)) return;
        try {
            await rolesApi.deleteRole(role.id);
            setRoles(prev => prev.filter(r => r.id !== role.id));
            toast.success("Role deleted");
        } catch (err) {
            toast.error(parseApiError(err));
        }
    };

    if (isLoading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="w-8 h-8 animate-spin text-brand-500" />
            </div>
        );
    }

    return (
        <div className="p-6 max-w-4xl">
            <div className="flex items-center justify-between mb-6">
                <div>
                    <h2 className="text-lg font-bold text-ink">Roles & Permissions</h2>
                    <p className="text-sm text-ink-muted">Define custom roles and assign permissions for your staff.</p>
                </div>
                {!isCreating && !isEditing && (
                    <Button onClick={() => setIsCreating(true)} size="sm">
                        <Plus className="w-4 h-4" />
                        Create Role
                    </Button>
                )}
            </div>

            <AnimatePresence mode="wait">
                {(isCreating || isEditing) ? (
                    <motion.div
                        key="form"
                        initial={{ opacity: 0, y: 10 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -10 }}
                    >
                        <RoleForm
                            role={isEditing}
                            permissions={permissions}
                            onCancel={() => { setIsCreating(false); setIsEditing(null); }}
                            onSave={(role) => {
                                if (isEditing) {
                                    setRoles(prev => prev.map(r => r.id === role.id ? role : r));
                                } else {
                                    setRoles(prev => [...prev, role]);
                                }
                                setIsCreating(false);
                                setIsEditing(null);
                            }}
                        />
                    </motion.div>
                ) : (
                    <motion.div
                        key="list"
                        className="grid grid-cols-1 gap-4"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                    >
                        {roles.map(role => (
                            <div key={role.id} className="bg-white border border-slate-200 rounded-xl p-4 flex items-start justify-between hover:border-brand-200 transition-colors group">
                                <div className="flex items-start gap-4">
                                    <div className="w-10 h-10 rounded-lg bg-brand-50 flex items-center justify-center text-brand-600 flex-shrink-0">
                                        <Shield className="w-5 h-5" />
                                    </div>
                                    <div>
                                        <h3 className="font-bold text-ink">{role.name}</h3>
                                        <p className="text-sm text-ink-muted mb-3">{role.description || "No description provided."}</p>
                                        <div className="flex flex-wrap gap-1.5">
                                            {role.permissions.map(p => (
                                                <span key={p} className="px-2 py-0.5 bg-slate-100 text-slate-600 rounded-md text-[10px] font-medium uppercase tracking-wider border border-slate-200">
                                                    {p.replace(/_/g, ' ')}
                                                </span>
                                            ))}
                                            {role.permissions.length === 0 && (
                                                <span className="text-xs text-slate-400 italic">No permissions assigned</span>
                                            )}
                                        </div>
                                    </div>
                                </div>
                                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                    <Button variant="ghost" size="sm" onClick={() => setIsEditing(role)}>
                                        <Edit3 className="w-4 h-4" />
                                    </Button>
                                    <Button variant="ghost" size="sm" className="text-red-500 hover:text-red-600 hover:bg-red-50" onClick={() => handleDelete(role)}>
                                        <Trash2 className="w-4 h-4" />
                                    </Button>
                                </div>
                            </div>
                        ))}
                        {roles.length === 0 && (
                            <div className="text-center py-12 bg-slate-50 rounded-2xl border-2 border-dashed border-slate-200">
                                <Shield className="w-12 h-12 text-slate-300 mx-auto mb-3" />
                                <p className="text-slate-500 font-medium">No roles defined yet.</p>
                                <p className="text-xs text-slate-400 mt-1">Start by creating your first organization-specific role.</p>
                            </div>
                        )}
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
}

function RoleForm({ role, permissions, onCancel, onSave }: {
    role: Role | null;
    permissions: PermissionInfo[];
    onCancel: () => void;
    onSave: (role: Role) => void;
}) {
    const [name, setName] = useState(role?.name || "");
    const [description, setDescription] = useState(role?.description || "");
    const [selectedPerms, setSelectedPerms] = useState<string[]>(role?.permissions || []);
    const [isSaving, setIsSaving] = useState(false);

    const togglePerm = (perm: string) => {
        setSelectedPerms(prev =>
            prev.includes(perm) ? prev.filter(p => p !== perm) : [...prev, perm]
        );
    };

    const handleSave = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!name.trim()) return toast.error("Role name is required");

        setIsSaving(true);
        try {
            if (role) {
                const updated = await rolesApi.updateRole(role.id, {
                    name,
                    description,
                    permissions: selectedPerms
                });
                onSave(updated);
                toast.success("Role updated");
            } else {
                const created = await rolesApi.createRole({
                    name,
                    description,
                    permissions: selectedPerms
                });
                onSave(created);
                toast.success("Role created");
            }
        } catch (err) {
            toast.error(parseApiError(err));
        } finally {
            setIsSaving(false);
        }
    };

    return (
        <form onSubmit={handleSave} className="bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-sm">
            <div className="p-6 border-b border-slate-100">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <Input
                        label="Role Name"
                        required
                        placeholder="e.g. Senior Pharmacist"
                        value={name}
                        onChange={e => setName(e.target.value)}
                    />
                    <Input
                        label="Description"
                        placeholder="What is this role for?"
                        value={description}
                        onChange={e => setDescription(e.target.value)}
                    />
                </div>
            </div>

            <div className="p-6 bg-slate-50/50">
                <label className="block text-xs font-bold text-ink-muted uppercase tracking-wider mb-4">
                    Assigned Permissions ({selectedPerms.length})
                </label>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {permissions.map(perm => (
                        <div
                            key={perm.name}
                            onClick={() => togglePerm(perm.name)}
                            className={`flex items-start gap-3 p-3 rounded-xl border transition-all cursor-pointer ${
                                selectedPerms.includes(perm.name)
                                ? "bg-white border-brand-500 ring-2 ring-brand-500/10"
                                : "bg-white border-slate-200 hover:border-slate-300"
                            }`}
                        >
                            <div className={`mt-0.5 w-4 h-4 rounded border flex items-center justify-center transition-colors ${
                                selectedPerms.includes(perm.name)
                                ? "bg-brand-500 border-brand-500"
                                : "bg-slate-50 border-slate-300"
                            }`}>
                                {selectedPerms.includes(perm.name) && <Check className="w-3 h-3 text-white" />}
                            </div>
                            <div>
                                <p className="text-xs font-bold text-ink leading-tight mb-0.5">{perm.description || perm.name}</p>
                                <p className="text-[10px] text-ink-muted leading-tight">{perm.name}</p>
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            <div className="p-4 bg-white border-t border-slate-100 flex items-center justify-end gap-3">
                <Button type="button" variant="ghost" onClick={onCancel} disabled={isSaving}>
                    Cancel
                </Button>
                <Button type="submit" loading={isSaving} disabled={isSaving}>
                    {role ? "Save Changes" : "Create Role"}
                </Button>
            </div>
        </form>
    );
}
