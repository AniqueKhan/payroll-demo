# Payroll demo: engine, review flow and screens

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

## Settings

Secrets and per-environment values live in `payroll/environment.py`, next to `settings.py`. That file is gitignored and imported at the end of `settings.py`. `settings.py` itself ships with an empty `SECRET_KEY`. Everything reads its key from `environment.py`: the screens, sessions and messages, and the test suite. For local development:

```python
# payroll/environment.py
SECRET_KEY = "your-local-secret-key"
DEBUG = True  # serves core/static/core/demo.css with runserver
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
```

Then run `python manage.py runserver` and open http://127.0.0.1:8000/.

The demo story:
1. **Overview** shows that 7 items need review.
2. **Inputs** shows each store's raw export. Flagged rows link to their exception.
3. **Exceptions** is where you resolve each item. Approve it as is, or approve it with a value: hours, a store, a leave type, or a loan amount.
4. Totals update after every decision.
5. **Finalize** becomes available once nothing is open. **Reopen** puts the run back to draft so you can walk through it again.

## Layout

| Path | What it does |
|------|--------------|
| `engine/` | Pure Python, no Django. Dataclasses in, `PayrollResult` out |
| `engine/importers/` | One adapter per export format. All of them normalize to `Punch` |
| `engine/hr_stage.py` | Dedupe and pair punches into shifts, then lates, half days, absences, store mismatches and weekly overtime |
| `engine/finance_stage.py` | Add-ons (with OT recalculation), gross pay, loans and advances, final pay |
| `engine/custom_rules.py` | Registry of client-configured deductions (`percent_of_gross`, `table_lookup`) |
| `engine/run.py` | `run_payroll(inputs, rules, adjustments)` orchestrator |
| `rules/demo_rules.yaml` | Every threshold, rate and penalty |
| `demo_data/generator.py` | Seeded generator. Writes the inputs and `scenario_manifest.json` |
| `services.py` | ORM-to-engine bridge: `run_payroll`, `decide`, `undo_decision`, `clear_decisions`, `finalize`, `reopen` |
| `views.py`, `templates/core/`, `static/core/demo.css` | The demo screens: plain Django templates, one stylesheet, a little vanilla JS |

## Pay calculation

- **Hourly pay.** Straight time is paid at each store's own rate. Overtime is worked out per workweek, with hours added up across all stores. The premium is `(multiplier - 1) x weighted average regular rate x OT hours`. The weighted rate keeps full `Decimal` precision. Only the premium is rounded, half up to cents. The trail shows the rate to cents for display, and spells the premium out as `(earnings / hours)` so it can be checked by hand. Each week's premium is split across stores in proportion to the hours worked there.
- **Salaried pay.** Salaried staff are exempt from overtime. Daily rate = `salary_per_period / standard working days in the period`. New hires and leavers are prorated by the days they were scheduled to work while employed.
- **Gross pay** = regular + overtime + add-ons - absence deductions.
- **Net pay** = gross - penalties - loans - custom deductions.
- **Loan cap.** The cap is a percentage of net pay before loans. For a leaver, the engine deducts the remaining balance up to that cap. Anything still owed is flagged as a final settlement.
- **Unreadable rows.** A bad row for a known employee, dated inside the period, is raised as `needs_review`. If the employee's remaining punches for that day at that store pair cleanly, the day is paid from them and the reviewer confirms the lost punch wasn't a real break or extra shift. If they can't be paired, the day is counted as 0 h, with the remaining punches attached and the scheduled hours suggested. A row whose date can't be read is also `needs_review`. Rows dated outside the period stay `info` and have no effect on pay.
- **Shifts at a store with no export.** The engine never turns these into absences. The `STORE_FILE_MISSING` blocking exception covers them.
- **Finalizing.** `finalize` adds each loan deduction to `paid_to_date`, so whatever was capped carries into the next period. A finalized period cannot be re-run.

## Review decisions

- **Where decisions live.** A decision (`ReviewDecision`) belongs to the pay period, not the run. It is matched to its exception by a stable key: `code|employee_id|date|location_code`.
- **Every re-run applies them.** Each re-run loads the period's decisions. Decisions approved with a value become engine `Adjustments`: hours, pay store, leave type, or loan amount. Exceptions that have a decision are then marked approved.
- **The original exception stays visible.** When an adjustment applies, the engine still raises the exception, marked as resolved with the reviewer's resolution text. The trail gets a "Reviewer adjustment: ..." step.
- **What can't be decided.** Blocking exceptions can't be approved; fix the input and re-run. Info exceptions need no decision.

## Severities

- `info`: a rule settled it.
- `needs_review`: a person must decide it (approve as is, or approve with a value) before finalizing.
- `blocking`: the run cannot be finalized until the inputs are fixed and payroll is re-run.
