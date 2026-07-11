import { describe, expect, it, vi } from "vitest";
import { ensureAuditLogSchema } from "@/lib/localDb";

describe("audit log schema repair", () => {
  it("adds user_full_name after CREATE TABLE for existing desktop databases", async () => {
    const execute = vi.fn().mockResolvedValue({ rowsAffected: 0 });
    await ensureAuditLogSchema({ execute } as any);

    expect(execute).toHaveBeenCalledWith(
      "ALTER TABLE audit_logs ADD COLUMN user_full_name TEXT"
    );
  });

  it("is idempotent when user_full_name already exists", async () => {
    const execute = vi.fn()
      .mockResolvedValueOnce({ rowsAffected: 0 })
      .mockRejectedValueOnce(new Error("duplicate column name: user_full_name"));

    await expect(ensureAuditLogSchema({ execute } as any)).resolves.toBeUndefined();
  });
});
