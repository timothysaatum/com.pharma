# Demo catalog seed

This service adds a synthetic drug catalog and opening branch inventory to one
existing organization. It targets the same PostgreSQL database as the API and
never creates users, organizations, or branches.

The seed is safe to repeat:

- rows are scoped by `organization_id`;
- seed SKUs use the reserved `DEMO-` prefix;
- UUIDs are deterministic per organization;
- canonical demo metadata is refreshed on repeat runs;
- existing branch stock quantities are preserved;
- every run snapshots affected catalog tables and writes a before/after report.

From `backend.laso`:

```bash
.venv/bin/python scripts/seed_test_catalog.py status
.venv/bin/python scripts/seed_test_catalog.py seed --username <ui-login>
```

Artifacts are written to `/tmp/drug-catalog-seed/`:

- `snapshot-before.json`: rollback baseline;
- `catalog-after.json`: verified final state;
- `before-after.csv`: full row-level comparison;
- `report.md`: outcome and examples;
- `progress.log`: timestamped progress trace.

Gate test:

```bash
.venv/bin/python -m pytest -q tests/unit/test_catalog_seed_service.py
```

Periodic catalog quality eval:

```bash
.venv/bin/python -m pytest -q evals/test_catalog_seed_quality.py
```
