#!/usr/bin/env python3
import asyncio
import os
import sys
import uuid
import json
from datetime import date
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Setup path so we can import from app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.config import settings
from app.services.inventory.reconciliation_service import generate_reconciliation_report

async def run_reconciliation(branch_id: str):
    engine = create_async_engine(settings.DATABASE_URL)
    SessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    try:
        b_id = uuid.UUID(branch_id)
    except ValueError:
        print("Invalid branch ID format.")
        sys.exit(1)
        
    async with SessionLocal() as db:
        report = await generate_reconciliation_report(db, b_id, date.today())
        
    out_dir = "/tmp/reconciliation"
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"report_{branch_id}_{report.report_date}.json")
    
    with open(out_file, "w") as f:
        f.write(report.model_dump_json(indent=2))
        
    print(f"Reconciliation complete. Report saved to {out_file}")
    if report.has_drift:
        print(f"WARNING: Drift detected! {report.drift_count} mismatched items.")
    else:
        print("No drift detected. Inventory is balanced.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python reconcile_branch.py <branch_id>")
        sys.exit(1)
        
    asyncio.run(run_reconciliation(sys.argv[1]))
