# Cybersecurity Audit & Remediation Plan

This document presents the security audit findings for the Laso Pharmacy Management system. It highlights several critical-to-low security gaps across the FastAPI backend (`backend.laso`) and the Tauri React frontend (`ui.laso`), followed by a remediation plan.

---

## 1. Identified Security Gaps

| # | Severity | Category | Target File & Symbol | Description |
|---|---|---|---|---|
| 1 | **Critical** | Tauri command injection | [db.rs](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/db.rs#L295-L367) — [db_execute](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/db.rs#L295-L322) and [db_select](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/db.rs#L327-L367) | **Tauri Command Injection / Arbitrary SQL Execution**: Exposes general SQL query execution interfaces to the frontend. A compromised frontend can run arbitrary SQL commands (e.g. drop tables, extract credentials, wipe inventory). |
| 2 | **Critical** | Tenant isolation bypass | [crr_sync_endpoints.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/crr_sync_endpoints.py#L112-L136) — [crr_pull](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/crr_sync_endpoints.py#L112) | **Cross-Tenant Offline Sync Leakage**: The shadow database is shared across all tenants. The `get_changes_since` query retrieves all changes from `crsql_changes` without filtering by `organization_id` or `branch_id`, leaking sensitive data across organizations. |
| 3 | **High** | Privilege escalation | [export_endpoints.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/export_endpoints.py#L20-L80) — [export_sales_excel](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/export_endpoints.py#L20) and [export_inventory_excel](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/export_endpoints.py#L51) | **Data Export Authorization Bypass**: Excel export routes only require authentication (`get_current_user`) but do not verify permissions. A low-privilege cashier can download full organization sales and inventory data. |
| 4 | **High** | Privilege escalation | [stats.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/stats.py#L70-L238) — [get_sales_summary](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/stats.py#L74) and [get_top_selling_drugs](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/stats.py#L181) | **Sales Reports Authorization Bypass**: Statistics and reporting summaries are exposed to any authenticated user because they lack role/permission restrictions. |
| 5 | **High** | Privilege escalation / Info disclosure | [roles.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/roles.py#L34-L51) — [get_roles](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/roles.py#L34) and [get_role](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/roles.py#L43) | **Role Mapping Leakage**: Querying roles and permissions only checks authentication. Any user can map out the entire permission structure of the tenant. |
| 6 | **Medium** | Cryptographic weakness | [encryption.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/encryption.py#L33-L39) — [decrypt_secret](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/encryption.py#L33) | **Plaintext Secret Read Fallback**: The decryption utility silently returns the raw value when decryption fails, allowing legacy plaintext MFA secrets to persist in the database without migration. |
| 7 | **Medium** | Session hijacking / hijacking risk | [lib.rs](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/lib.rs#L21-L44) — [secure_set](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/lib.rs#L21) | **Shared OS Session Credential Overwrite**: Secure credentials in Tauri are stored under static keys (`auth.access_token`, `auth.refresh_token`). On shared operating system logins, logging in as one cashier overwrites the token of another. |
| 8 | **Medium** | Missing defense in depth | [middleware_config.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/middleware_config.py#L40-L61) — [register_middleware](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/middleware_config.py#L40) | **Missing Security Response Headers**: The FastAPI app lacks standard secure HTTP headers like `X-Frame-Options`, `X-Content-Type-Options`, and strict HSTS. |

---

## 2. Technical Deep-Dive

### 1. Tauri Raw SQL Execution
Exposing generic SQL execute/select endpoints via IPC is a primary vector for client-side compromise. In [db.rs](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/db.rs#L295-L367), the Tauri commands accept any `sql` string parameter:
```rust
#[tauri::command]
pub fn db_execute(
    db: State<DbState>,
    sql: String,
    values: Vec<JsonValue>,
) -> Result<ExecResult, DbError> { ... }
```
If an attacker exploits a dependency or uses XSS in the frontend, they bypass all frontend routing, authorization, and constraints by running raw SQL directly.

### 2. Multi-Tenant CRR Sync Delta Leakage
In [crr_sync_endpoints.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/crr_sync_endpoints.py#L134-L136), the pull request queries the shadow database changes:
```python
changes = await shadow.get_changes_since(
    since_db_version=request.crr_since_db_version,
)
```
Inside [shadow_db.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/services/sync/shadow_db.py#L969-L977), this is executed on `crsql_changes`:
```sql
SELECT "table", pk, cid, val, col_version, db_version, site_id, cl, seq
FROM crsql_changes
WHERE db_version > ?
ORDER BY db_version, seq
LIMIT ?
```
Because `crsql_changes` tracks row-level changes for all organizations inside the same database file, A client pulling changes gets updates belonging to other tenants.

### 3. Missing Role Restrictions on Admin Endpoints
Several admin-only routes in the API routers lack authorization guards:
- **Exports:** In [export_endpoints.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/export_endpoints.py#L24-L55), the sales and inventory export endpoints do not call `require_permission`.
- **Stats:** In [stats.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/stats.py#L74-L181), global reporting statistics endpoints do not require permissions.
- **Roles:** In [roles.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/roles.py#L34-L48), list and get role queries do not restrict read access.

---

## 3. Remediation Plan

### Phase 1: High-Priority & Critical Fixes

#### A. Isolate Shadow Databases per Organization
To fix the CRR sync leak:
1. Reconfigure [shadow_db.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/services/sync/shadow_db.py) to initialize and open isolated SQLite files: `shadow_{organization_id}.db` instead of the single global `shadow.db`.
2. Update the singleton factory `get_shadow_db()` or instantiate `ShadowDB` dynamically in [deps.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/deps.py) using the request's resolved `organization_id`.
3. Separate the periodic background reconciliation task (`_crr_reconciliation_loop` in [main.py](file:///home/ubuntu/projects/com.pharma/backend.laso/main.py#L42-L54)) so it iterates over all active organizations and reconciles their individual shadow databases.

#### B. Replace Raw SQL Tauri IPC Commands with High-Level API
To fix Tauri SQL Injection:
1. Deprecate `db_execute` and `db_select` in [db.rs](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/db.rs).
2. Introduce typed Tauri command functions representing specific actions, such as:
   - `get_local_drugs(search: String) -> Result<Vec<Drug>, String>`
   - `save_local_sale(sale: SaleCreate) -> Result<String, String>`
3. Relocate SQL statement preparation and execution entirely inside Rust's safe boundary.

#### C. Tighten Endpoint Permission Checks
1. Add `require_permission` checks to [export_endpoints.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/export_endpoints.py):
   - Wrap `export_sales_excel` with `Depends(require_permission("view_reports"))`.
   - Wrap `export_inventory_excel` with `Depends(require_permission("view_inventory"))`.
2. Add `require_permission` checks to [stats.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/stats.py):
   - Wrap `get_sales_summary` and `get_top_selling_drugs` with `Depends(require_permission("view_reports"))`.
3. Add `require_permission` checks to [roles.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/api/v1/endpoints/roles.py):
   - Wrap `get_roles` and `get_role` with `Depends(require_permission(Permission.MANAGE_ORGANIZATION))`.

### Phase 2: Medium & Defense-in-Depth Fixes

#### A. Force Cryptographic Migration & Remove Plaintext Fallback
1. Write a database migration script to encrypt all plaintext legacy values in the `two_factor_secret` column of the `users` table.
2. Remove the fallback block in [encryption.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/encryption.py#L37-L39):
   ```python
   # Replace with strict exception raising
   def decrypt_secret(value: str) -> str:
       return get_cipher_suite().decrypt(value.encode()).decode()
   ```

#### B. Qualify Keyring Entries with User Identifiers
Update [lib.rs](file:///home/ubuntu/projects/com.pharma/ui.laso/src-tauri/src/lib.rs#L15-L19) to accept the active user's username or ID as part of the keyring lookup entry:
```rust
fn credential_entry(key: &str, username: &str) -> Result<keyring::Entry, String> {
    validate_secret_key(key)?;
    let qualified_key = format!("{KEYRING_SERVICE}:{username}:{key}");
    keyring::Entry::new(&qualified_key, key)
        .map_err(|error| format!("Keyring error: {error}"))
}
```

#### C. Register Security Headers Middleware
In [middleware_config.py](file:///home/ubuntu/projects/com.pharma/backend.laso/app/core/middleware_config.py#L40-L61), append an HTTP middleware to inject secure HTTP response headers:
```python
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
```
