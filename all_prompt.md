# Laso Pharmacy — Gap Fix Implementation Prompt

This document provides the instructions to guide an AI coding agent to implement the gap fixes for the com.pharma project repository.

---

```markdown
You are tasked with addressing six specific technical gaps in the com.pharma project repository. Ensure all changes adhere to the repository rules defined in AGENTS.md (e.g., vanilla implementation, comprehensive unit/integration test coverage, direct communication without filler words, complete feature closure).

Please implement the following plan sequentially.

---

### GAP 1: Git-untrack `test.db` and Update `.gitignore`
- **Context:** The SQLite database `test.db` (1.4MB) is currently tracked at the repository root `c:/Users/Admin/Desktop/com.pharma/test.db`.
- **Tasks:**
  1. Remove `test.db` from git tracking without deleting the file locally:
     ```bash
     git rm --cached test.db
     ```
  2. Append `*.db` to the root `.gitignore` (`c:/Users/Admin/Desktop/com.pharma/.gitignore`) to prevent it or other database instances from being re-added.
  3. Verify that `git status` shows `test.db` as deleted/untracked and `.gitignore` as modified.

---

### GAP 2: Rename `precriptions` to `prescriptions` (Typo Fix)
- **Context:** The directory name under the backend models is misspelled as `precriptions`.
- **Tasks:**
  1. Rename the directory:
     `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/models/precriptions/` 
     to 
     `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/models/prescriptions/`.
  2. Update references to this path in the following 6 files:
     - `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/api/v1/endpoints/prescription_endpoints.py`
     - `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/models/__init__.py`
     - `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/models/customer/customer_model.py`
     - `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/services/sales/sales_service.py`
     - `c:/Users/Admin/Desktop/com.pharma/backend.laso/app/services/sync/sync_service.py`
     - `c:/Users/Admin/Desktop/com.pharma/backend.laso/tests/unit/test_sales_service.py`
  3. Verify imports are clean by running the existing unit tests.

---

### GAP 3: Split `requirements.txt` into Production and Development
- **Context:** Dev tools (such as `black`, `flake8`, `mypy`, `pytest`, `Faker`, `coverage`) are bundled with runtime requirements in `requirements.txt`, which bloats the production Docker image.
- **Tasks:**
  1. Split `c:/Users/Admin/Desktop/com.pharma/backend.laso/requirements.txt` so it only contains production runtime dependencies.
  2. Create a new `c:/Users/Admin/Desktop/com.pharma/backend.laso/requirements-dev.txt` for development dependencies (linters, testers, formatters) referencing production specs:
     ```text
     -r requirements.txt
     black
     flake8
     mypy
     pytest
     pytest-asyncio
     pytest-cov
     Faker
     coverage
     ```
  3. Ensure `c:/Users/Admin/Desktop/com.pharma/backend.laso/Dockerfile` copies and runs `pip install -r requirements.txt` so production builds remain lightweight.

---

### GAP 4: Modularize `main.py`
- **Context:** The file `c:/Users/Admin/Desktop/com.pharma/backend.laso/main.py` is over 600 lines, packing middleware configurations, database checks, notification settings, and exception handlers into one block.
- **Tasks:**
  1. Extract the `LOGGING_CONFIG` dict and logging initializer to a new module `app/core/logging_config.py`.
  2. Extract all FastAPI exception handlers (for `RequestValidationError`, `ValidationError`, `ValueError`, `IntegrityError`, `DataError`, `Exception`) to `app/core/exception_handlers.py`. Add a function `register_exception_handlers(app: FastAPI)` to map them.
  3. Extract request tracing, logging, and rate limiting middlewares to `app/core/middleware_config.py`. Expose a `register_middleware(app: FastAPI)` function.
  4. Refactor `main.py` to import and call these registration helpers, reducing its size to under 100 lines.

---

### GAP 5: Expand Automated Test Coverage
- **Context:** Critical flows such as authentication, prescription management, custom client/cashier roles, and loyalty systems lack dedicated backend test coverage.
- **Tasks:**
  1. Create `tests/unit/test_auth_service.py` to test JWT creation/verification, password hashing, and active session limits.
  2. Create `tests/unit/test_prescription_service.py` verifying prescription validation, status state machine transitions, and dispense operations.
  3. Create `tests/integration/test_auth_endpoints.py` testing login, token refresh, token revocation/logout, and account lockout constraints.
  4. Create `tests/integration/test_prescription_endpoints.py` validating prescription CRUD and state endpoints.
  5. Enhance `conftest.py` with reusable fixtures: `admin_user`, `pharmacist_user`, `cashier_user`, and an `auth_headers` helper to populate request headers.

---

### GAP 6: Create Dev-Specific PostgreSQL & Redis Docker Compose
- **Context:** The production environment uses PostgreSQL and Redis while dev defaults to SQLite, introducing a risk of schema and behavior drift (especially with CRR sync features).
- **Tasks:**
  1. Create a development-only Compose configuration file at `c:/Users/Admin/Desktop/com.pharma/docker-compose.dev.yml` containing PostgreSQL and Redis services matching production configuration versions:
     ```yaml
     services:
       pharma-db-dev:
         image: postgres:16-alpine
         environment:
           POSTGRES_DB: pharma_dev
           POSTGRES_USER: pharma
           POSTGRES_PASSWORD: pharma_dev_password
         ports:
           - "127.0.0.1:5432:5432"

       pharma-redis-dev:
         image: redis:7-alpine
         ports:
           - "127.0.0.1:6379:6379"
     ```
  2. Keep SQLite configuration in `app/core/config.py` as a fallback, but change `.env` defaults to utilize the Postgres development container.
  3. Document the workflow for running development with PostgreSQL in `README.md`.

---

### Execution and Verification
After making all modifications:
1. Run linting (`black` and `flake8`) to verify formatting.
2. Run pytest to check unit and integration suites:
   ```bash
   pytest --tb=short
   ```
3. Provide a full completion report matching the AGENTS.md requirements.
```
