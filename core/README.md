# Payroll demo: engine, rules, data (Spec 1)

Multi-location retail payroll. Messy time clock exports from three stores go in. Every exception is caught and explained, and a clean payroll comes out. It is finalized only after review. All data is synthetic.

## Quick start

```bash
pip install -r requirements.txt
python manage.py makemigrations user_management core   # migrations/ is gitignored in this repo
python manage.py migrate
python manage.py seed_demo                    # or: seed_demo --missing-store B
python manage.py run_payroll --period 2026-09-07
pytest
```

## Layout

| Path | What it does |
|------|--------------|
| `engine/` | Pure Python, no Django. Dataclasses in, `PayrollResult` out |
| `engine/importers/` | One adapter per export format. All of them normalize to `Punch` |
| `engine/hr_stage.py` | Dedupe and pair punches into shifts, then lates, half days, absences, store mismatches and weekly overtime |
| `engine/finance_stage.py` | Add-ons (with OT recalculation), gross pay, loans and advances, final pay |
| `engine/custom_rules.py` | Registry of client-configured deductions (`percent_of_gross`, `table_lookup`) |
| `engine/run.py` | `run_payroll(inputs, rules)` orchestrator |
| `rules/demo_rules.yaml` | Every threshold, rate and penalty |
| `demo_data/generator.py` | Seeded generator. Writes the inputs and `scenario_manifest.json` |
| `services.py` | ORM-to-engine bridge, `run_payroll`, `review_exception`, `finalize` |

## Pay calculation

- **Hourly pay.** Straight time is paid at each store's own rate. Overtime is worked out per workweek, with hours added up across all stores. The premium is `(multiplier - 1) x weighted average regular rate x OT hours`. The weighted rate is rounded to cents, then used in the premium, so the trail line can be checked by hand. Each week's premium is split across stores in proportion to the hours worked there.
- **Salaried pay.** Salaried staff are exempt from overtime. Daily rate = `salary_per_period / standard working days in the period`. New hires and leavers are prorated by the days they were scheduled to work while employed.
- **Gross pay** = regular + overtime + add-ons - absence deductions.
- **Net pay** = gross - penalties - loans - custom deductions.
- **Loan cap.** The cap is a percentage of net pay before loans. For a leaver, the engine deducts the remaining balance up to that cap. Anything still owed is flagged as a final settlement.
- **Shifts at a store with no export.** The engine never turns these into absences. The `STORE_FILE_MISSING` blocking exception covers them.
- **Finalizing.** `finalize` adds each loan deduction to `paid_to_date`, so whatever was capped carries into the next period. A finalized period cannot be re-run.

## Severities

- `info`: a rule settled it.
- `needs_review`: a person must approve or override it before finalizing.
- `blocking`: the run cannot be finalized until the inputs are fixed and payroll is re-run.
