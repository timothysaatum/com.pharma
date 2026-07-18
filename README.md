# Laso Pharmacy Management

A complete pharmacy and inventory management system with a Python/FastAPI backend and a Tauri-powered React frontend. This repository includes:

- `backend.laso`: FastAPI service for authentication, inventory, sales, prescriptions, reporting, and sync features
- `ui.laso`: Tauri + React + TypeScript desktop application shell for the pharmacy dashboard
- GitHub Actions build workflow for cross-platform Tauri packaging

## Features

- Inventory management and product tracking
- Sales order creation, billing, and pricing controls
- Prescription management workflows
- Customer and branch management
- Role-based authentication and session controls
- Reporting, export, and sync utilities
- Multi-platform desktop app via Tauri

## Repository Structure

- `backend.laso/`
  - `app/`: application modules, API, models, services, and utilities
  - `main.py`: FastAPI application entrypoint
  - `requirements.txt`: Python package dependencies
  - `migrations/`: Alembic database migrations
  - `tests/`: backend unit and integration tests

- `ui.laso/`
  - `src/`: React application source code
  - `package.json`: frontend package scripts and dependencies
  - `tsconfig.json`, `vite.config.ts`: TypeScript and Vite configuration
  - `src-tauri/`: Tauri app configuration and Rust runtime

## Tech Stack

- Backend: Python, FastAPI, SQLAlchemy, Alembic, Uvicorn
- Frontend: React, TypeScript, Vite, Tailwind CSS, Zustand, React Router
- Desktop: Tauri
- CI / Build: GitHub Actions

## Getting Started

> These instructions assume you are working from the repository root: `com.pharma/`

### 1. Backend Setup

```bash
cd backend.laso
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file at `backend.laso/.env` and set required values. At minimum:

```env
PROJECT_NAME=Laso
VERSION=1.0.0
ENVIRONMENT=development
DATABASE_URL=sqlite+aiosqlite:///./laso.sqlite3
SECRET_KEY=your-secret-key
```

Run the backend locally:

```bash
cd backend.laso
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 2. Database Migrations

If you use Alembic migrations:

```bash
cd backend.laso
alembic upgrade head
```

### Seed a Demo Drug Catalog

The catalog seed uses the configured API database and targets the organization
belonging to a specific UI login. It is repeat-safe and also adds opening stock
to every active branch in that organization.

```bash
cd backend.laso
.venv/bin/python scripts/seed_test_catalog.py status
.venv/bin/python scripts/seed_test_catalog.py seed --username <ui-login>
```

Run artifacts and the rollback snapshot are written to
`/tmp/drug-catalog-seed/`.

### 3. Frontend Setup

```bash
cd ui.laso
pnpm install
pnpm dev
```

The React/Tauri app will launch in development mode. If you need desktop integration via Tauri:

```bash
cd ui.laso
pnpm tauri dev
```

### 4. Build for Production

Build the frontend app:

```bash
cd ui.laso
pnpm build
```

Build the Tauri application:

```bash
cd ui.laso
pnpm tauri build
```

## Environment Variables

Common environment variables used by the backend:

- `DATABASE_URL` — SQLAlchemy database connection string
- `SECRET_KEY` — JWT signing secret
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `REFRESH_TOKEN_EXPIRE_DAYS`
- `RATE_LIMIT_ENABLED`
- `CORS_ORIGINS`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `FROM_EMAIL`
- `ARKESEL_API_KEY`, `ARKESEL_BASE_URL`, `ARKESEL_SENDER_ID`

## Testing

Run backend tests from `backend.laso`:

```bash
cd backend.laso
pytest
```

Run frontend tests from `ui.laso`:

```bash
cd ui.laso
pnpm test
```

## CI / Build Pipeline

The repository includes a GitHub Actions workflow at `.github/workflows/build-tauri.yml` that builds the Tauri app across multiple platforms. The workflow handles:

- Node.js and pnpm setup
- Rust installation
- Linux dependency installation
- Frontend installation and packaging

## Notes

- The backend is designed for production-ready security and logging.
- The frontend is built as a Tauri desktop app but can also run in browser development mode.
- Environment configuration is loaded from `backend.laso/.env`.

## Contribution

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request with a clear description

## License

This repository does not include a license file by default. Add `LICENSE` if you want to open source the project.
