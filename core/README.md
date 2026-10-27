# Payroll demo: engine, review flow and screens

Multi-location retail payroll. Messy time clock exports from three stores go in. Every exception is caught and explained, and a clean payroll comes out. It is finalized only after review. All data is synthetic.

## Quick start

```bash
pip install -r requirements.txt
python manage.py makemigrations user_management core   # migrations/ is gitignored in this repo
python manage.py migrate
python manage.py seed_demo                    # or: --period 2026-10-05, --missing-store B
python manage.py run_payroll --period 2026-09-07
pytest
```

`seed_demo --period YYYY-MM-DD` generates the demo for any pay period. The date is the period start and must fall on the rules' `period.week_start` (Monday); a date on any other day is refused, with the nearest valid dates suggested. The period length comes from `period.length_days` in the rules. Every planted scenario is placed relative to the start date, so it lands on the same weekday in every period. The totals and exceptions are the same in every period; only the dates move. Without `--period`, the demo uses Sep 7 to 20, 2026.

**Bigger demos.** The 18 core employees and stores A, B and C are always there, so every planted scenario always appears. You can add more on top:
- `--extra-employees N` adds clean filler staff (E19, E20, ...). They are hourly at $15.00 to $18.00, spread across all stores in turn, and work Monday to Friday with punches inside the grace period, so they raise no exceptions and no overtime.
- `--stores N` (at least 3, at most 26) adds stores D, E, F and so on. Each extra store reuses the store_a, store_b and store_c formats in turn and has its own export file.

Every store must deliver an export, so `--missing-store` works for extra stores too. The options combine with `--period`, and the same options always produce identical output. `seed_demo --extra-employees 200 --stores 10` and `run_payroll` take a few seconds. The Payroll screen has a search box (name or ID) and a home store filter.

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
1. **Overview** shows that 6 items need review, and 3 attendance items to discuss.
2. **Inputs** shows each store's raw export. Flagged rows link to their exception.
3. **Exceptions** is where you resolve each item. Approve it as is, or approve it with a value: hours, where a shift was worked, or a loan amount. An absence without notice can optionally be recorded as paid or unpaid leave.
4. Totals update after every decision.
5. **Finalize** becomes available once nothing is open. **Reopen** puts the run back to draft so you can walk through it again.

## Per-visitor sandboxes

- **The baseline.** The seeded run (`PayrollRun.sandbox = NULL`) is the baseline, and the web never changes it. Inputs are shared and read-only: locations, employees, schedules, raw rows, leaves, add-ons and loans.
- **When a sandbox is created.** A visitor's first action (any POST) creates a `DemoSandbox`. That visitor's run and decisions belong to it. Its id lives in the Django session under `sandbox_id`. Visitors who only browse, and crawlers, never create one; they see the baseline, with the review forms visible.
- **No run ids in URLs.** Pages are `/`, `/inputs/`, `/exceptions/`, `/payroll/` and `/employees/<id>/`. Each one resolves the visitor's own run from the session. Old `/runs/<id>/...` links redirect to the matching page, and the id is ignored.
- **Start over.** This button, on the overview and the queue, deletes the visitor's sandbox and returns them to the clean demo. A session that points to a deleted sandbox is simply treated as a new visitor.
- **Loan balances.** Finalizing in a sandbox does not post loan deductions, because loan balances are shared inputs. Only the baseline (`services.finalize(period)`) posts them.
- **Cleanup.** `python manage.py cleanup_sandboxes --older-than-hours 24` deletes stale sandboxes, along with their runs, lines, exceptions and decisions. `seed_demo` deletes all sandboxes before reseeding.

## Layout

| Path | What it does |
|------|--------------|
| `engine/` | Pure Python, no Django. Dataclasses in, `PayrollResult` out |
| `engine/importers/` | One adapter per export format. All of them normalize to `Punch` |
| `engine/hr_stage.py` | Dedupe and pair punches into shifts, then lates, partial days, absences, store mismatches and weekly overtime |
| `engine/finance_stage.py` | Add-ons (with OT recalculation), gross pay, loans and advances, final pay |
| `engine/custom_rules.py` | Registry of client-configured deductions (`percent_of_gross`, `table_lookup`) |
| `engine/run.py` | `run_payroll(inputs, rules, adjustments)` orchestrator |
| `rules/demo_rules.yaml` | Every threshold, rate and penalty |
| `demo_data/generator.py` | Seeded generator. Writes the inputs and `scenario_manifest.json` |
| `services.py` | ORM-to-engine bridge: `run_payroll`, `decide`, `undo_decision`, `clear_decisions`, `finalize`, `reopen`, each taking `sandbox` (None = baseline) |
| `sandbox.py` | Session helpers: `get_sandbox(request, create=False)`, `current_run(request, period)` |
| `views.py`, `templates/core/`, `static/core/demo.css` | The demo screens: plain Django templates, one stylesheet, a little vanilla JS |

## Pay calculation

- **Hourly pay.** Straight time is paid at each store's own rate. Overtime is worked out per workweek, with hours added up across all stores. The premium is `(multiplier - 1) x weighted average regular rate x OT hours`. The weighted rate keeps full `Decimal` precision. Only the premium is rounded, half up to cents. The trail shows the rate to cents for display, and spells the premium out as `(earnings / hours)` so it can be checked by hand. Each week's premium is split across stores in proportion to the hours worked there. Workweeks start at `week_start_time` (default `"00:00"`) on `period.week_start`. A shift that crosses that moment, such as an overnight shift from Sunday 22:00 to Monday 06:00, has its hours split between the two workweeks for the overtime count only; the trail says so (`Overnight shift Sun Sep 13 22:00 to Mon Sep 14 06:00: 2.00 h counted in Week 1, 6.00 h in Week 2. ...`), and the shift stays on, and is paid on, the day it started. With `"06:00"` that shift counts entirely in Week 1. Hours after the period ends stay in its last week: carrying them into the next period's first workweek is out of scope. Scenario 21 (E05) shows the split: 42.00 h + 37.90 h gives 2.00 h of overtime, where counting the whole shift in Week 1 would give 8.00 h.
- **Salaried partial days.** Exempt salaried pay can't be reduced for a partial-day absence. When salaried staff work less than `half_day.threshold_fraction` of their scheduled hours, the day becomes a "Partial day" attendance item with no deduction, and the trail says why. Set `salaried_partial_day_deduction: true` in the rules to deduct half a day instead (the old behaviour). A full-day absence without notice is still deducted.
- **Salaried pay.** Salaried staff are exempt from overtime. Daily rate = `salary_per_period / standard working days in the period`. New hires and leavers are prorated by the days they were scheduled to work while employed.
- **Gross pay** = regular + overtime + add-ons - absence deductions.
- **Net before taxes** = gross - penalties - loans - custom deductions. Every screen, the trail and `run_payroll` use this label. The engine calculates no taxes: federal, state and payroll tax withholding is left to the payroll provider (for example ADP, Gusto or Paychex), which takes these checked numbers as its input.
- **Attendance penalties.** `attendance_penalties_enabled` in the rules file is `false` by default, so a late arrival or an absence without notice costs no penalty. The amounts (`late.penalty_per_late`, `absence.uninformed_penalty`) stay in the rules file; set the switch to `true` to apply them again exactly as before.
- **Loan cap.** The cap is a percentage of net pay before loans. For a leaver, the engine deducts the remaining balance up to that cap. Anything still owed is flagged as a final settlement.
- **Odd punch counts.** A day with an odd number of punches (in exports without IN/OUT) gets a suggested pairing: punches are paired in order, each gap reads as a break, and the last punch is a clock-in with a missed clock-out. The last block runs to the scheduled end when the schedule ends after it; with no schedule (or one that ended earlier) only the paired blocks are suggested. For example: `3 punches: 09:00, 13:00, 13:30. Looks like a lunch break and a missed clock-out. Suggested 7.50 h, using the scheduled end of 17:00.` The suggestion pre-fills the review form, and the trail shows the blocks. Until reviewed the day still counts as 0 h.
- **Why a value can't be read.** Each importer checks every date and time against its store's format (`HH:MM` and `YYYY-MM-DD` for store_a and store_c, `MM/DD/YYYY hh:mm AM` for store_b) and explains in plain words what is wrong: a letter that looks like a digit (O for 0, l or I for 1, S for 5, B for 8), the wrong separator, a value that can't exist (minute 60, February 30), a missing part, or AM/PM in a 24-hour file. If nothing specific is found it says which format was expected. `Did you mean 18:30?` is offered only when fixing the slips gives a valid value: `9:6O` gets two reasons (the letter O, and minute 60) and no suggestion. The Inputs and Exceptions screens highlight the characters involved in a monospace font. A one-digit hour such as `9:30` is read as 09:30. The checks live in `engine/importers/diagnose.py`.
- **Unreadable rows.** A bad row for a known employee, dated inside the period, is raised as `needs_review`. If the employee's remaining punches for that day at that store pair cleanly, the day is paid from them and the reviewer confirms the lost punch wasn't a real break or extra shift. If they can't be paired, the day is counted as 0 h, with the remaining punches attached and the scheduled hours suggested. A row whose date can't be read is also `needs_review`.
- **Pay period boundaries.** A shift belongs to the day it started (its clock-in date). The importers read one day either side of the period, so shifts pair across both edges. Then only shifts that clock in inside the period are kept:
  - A shift that starts the evening before the period belongs to the previous payroll. It is not counted, not flagged and not shown.
  - A shift that starts on the last day is counted in full here, even though it ends the morning after.
  - Rows from other dates that aren't part of such a shift are skipped quietly. The Inputs screen shows how many were skipped.
- **Extra columns.** An export with a column the format doesn't use gets a plain-language file note at the top of that store's tab on the Inputs screen. The note is kept on the run (`PayrollRun.file_notes`), but it is not an exception and is never counted. A missing required column still makes the whole file unreadable (blocking).
- **Labor cost by department.** Each export may carry a department column: `Dept` in store_a and store_c, `department` in store_b. A shift's department comes from its clock-in row; a missing or blank one is `Unassigned`. Each store's labor cost (regular + overtime + add-ons - absence deductions) is split across its departments by the hours worked in each, with rounding remainders kept, so department totals add up exactly to store totals and to gross. Each payroll line stores this split (`by_department`). The Overview, Payroll and employee screens show it overall and per store.
- **Clocked in at a store other than the scheduled one.** By default the shift is paid at the rate of the store where the employee clocked in, and its cost goes to that store (and to the department on the clock-in row). The review item states both rates and the difference for the shift. The reviewer chooses "Worked at Store A (where they clocked in)" or "Worked at Store B (scheduled), clocked in on the wrong device"; the choice is stored as a decision (approving as is stores the default) and the trail shows it. If there is no pay rate at the punch store, the scheduled store is the default.
- **Shifts at a store with no export.** The engine never turns these into absences. The `STORE_FILE_MISSING` blocking exception covers them.
- **Finalizing.** `finalize` adds each loan deduction to `paid_to_date`, so whatever was capped carries into the next period. A finalized period cannot be re-run.

## Review decisions

- **Where decisions live.** A decision (`ReviewDecision`) belongs to the pay period, not the run. It is matched to its exception by a stable key: `code|employee_id|date|location_code`.
- **Every re-run applies them.** Each re-run loads the period's decisions. Decisions approved with a value become engine `Adjustments`: hours, pay store, leave type, or loan amount. Exceptions that have a decision are then marked approved.
- **The original exception stays visible.** When an adjustment applies, the engine still raises the exception, marked as resolved with the reviewer's resolution text. The trail gets a "Reviewer adjustment: ..." step.
- **What can't be decided.** Blocking exceptions can't be approved; fix the input and re-run. Info exceptions and attendance items need no decision.
- **Record as leave (optional).** An absence without notice offers one optional action: record it as paid or unpaid leave. Paid leave removes the day's deduction for salaried staff (hourly staff are paid the scheduled hours); unpaid leave keeps it. No absence penalty applies either way. The trail gets a "Reviewer adjustment: recorded as leave" step. It is stored like any other decision (per period and per visitor sandbox, applied on every re-run, undoable), and finalizing never requires it.

## Severities

- `info`: a rule settled it. (File notes, such as an extra column, are not exceptions and don't appear in these counts.)
- `needs_review`: a person must decide it (approve as is, or approve with a value) before finalizing.
- `blocking`: the run cannot be finalized until the inputs are fixed and payroll is re-run.
- `attendance` (shown as "Attendance to discuss"): late arrivals, absences without notice and salaried partial days. They never block finalizing and aren't counted with needs review or info; the Overview gives their own count and the Exceptions screen lists them in their own section below the pay items. Each one carries a running count for the employee, such as "2nd late arrival this period". An absence without notice still deducts the day's pay for salaried staff; the item is there so someone follows up with the employee.
