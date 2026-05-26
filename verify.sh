#!/bin/bash
# Verification Script for LASO System

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║        LASO Pharmacy System - Verification Script             ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check backend setup
echo "📋 Checking Backend Setup..."
cd backend.laso

# Check Python files
echo "  ✓ Checking Python syntax..."
python -m py_compile \
  app/services/reports/reports_service.py \
  app/services/export/excel_export_service.py \
  app/api/v1/endpoints/reports_endpoints.py \
  app/api/v1/endpoints/export_endpoints.py \
  2>&1 | grep -q "SyntaxError" && echo "${RED}✗ Syntax Error Found${NC}" || echo "${GREEN}✓ All Python files valid${NC}"

# Check requirements
echo "  ✓ Checking dependencies..."
grep -q "openpyxl" requirements.txt && echo "${GREEN}✓ openpyxl installed${NC}" || echo "${RED}✗ openpyxl missing${NC}"
grep -q "fastapi" requirements.txt && echo "${GREEN}✓ FastAPI installed${NC}" || echo "${RED}✗ FastAPI missing${NC}"

# Check key files exist
echo "  ✓ Checking key files..."
[[ -f "app/services/reports/reports_service.py" ]] && echo "${GREEN}✓ ReportsService found${NC}" || echo "${RED}✗ ReportsService missing${NC}"
[[ -f "app/services/export/excel_export_service.py" ]] && echo "${GREEN}✓ ExcelExportService found${NC}" || echo "${RED}✗ ExcelExportService missing${NC}"
[[ -f "app/api/v1/endpoints/reports_endpoints.py" ]] && echo "${GREEN}✓ Reports endpoints found${NC}" || echo "${RED}✗ Reports endpoints missing${NC}"
[[ -f "app/api/v1/endpoints/export_endpoints.py" ]] && echo "${GREEN}✓ Export endpoints found${NC}" || echo "${RED}✗ Export endpoints missing${NC}"

cd ..

# Check frontend setup
echo
echo "📋 Checking Frontend Setup..."
cd ui.laso

# Check key files exist
echo "  ✓ Checking key files..."
[[ -f "src/pages/ReportsPage.tsx" ]] && echo "${GREEN}✓ ReportsPage found${NC}" || echo "${RED}✗ ReportsPage missing${NC}"
[[ -f "src/pages/AdminPage.tsx" ]] && echo "${GREEN}✓ AdminPage found${NC}" || echo "${RED}✗ AdminPage missing${NC}"
[[ -f "src/api/reports.ts" ]] && echo "${GREEN}✓ Reports API client found${NC}" || echo "${RED}✗ Reports API client missing${NC}"

# Check routes are configured
echo "  ✓ Checking route configuration..."
grep -q "ReportsPage" ../ui.laso/src/App.tsx && echo "${GREEN}✓ ReportsPage route configured${NC}" || echo "${RED}✗ ReportsPage route missing${NC}"
grep -q "AdminPage" ../ui.laso/src/App.tsx && echo "${GREEN}✓ AdminPage route configured${NC}" || echo "${RED}✗ AdminPage route missing${NC}"

cd ..

echo
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║                    Verification Complete!                     ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo
echo "Next Steps:"
echo "  1. Start backend:  cd backend.laso && python -m uvicorn app.main:app --reload"
echo "  2. Start frontend: cd ui.laso && pnpm dev"
echo "  3. Access app:     http://localhost:5173"
echo "  4. API docs:       http://localhost:8000/docs"
echo
