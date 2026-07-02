# AGENTS.md

Guidance for automated agents working in this repository.

## What this project is

A Python 3.10+ **stdlib-only** CLI that tweaks Android battery-related settings
over ADB (no root). The core safety invariant: **every device mutation must be
snapshotted before it is applied, verified after it is applied, and revertible
via `revert`.** Nothing may mutate the device outside `StateRecorder`.

## Build / verify commands

```bash
# Full test suite (must pass, zero failures):
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

# Lint (package code must be free of F-class errors; do not introduce NEW
# violations of any class; pre-existing E501 line-length debt may remain):
python3 -m ruff check android_battery_optimizer
```

There is no network access needed and **no real device is ever used in tests**
— all ADB interaction in tests goes through fake runners (see `FakeRunner` in
`tests/test_smart_restrict.py` and `MagicMock` patterns in
`tests/test_optimizer.py`).

## Code conventions

- Standard library only. Do not add dependencies.
- Match the typing style of the file you touch (`Dict`/`List`/`Optional` from
  `typing` in most modules).
- New kinds of device mutation require ALL of: a `TypedDict` in `ledger.py`,
  snapshot logic in `recorder.py` (`snapshot_*` before mutate), verification in
  `verification.py`, rollback in `rollback.py` (`perform_rollback`), restore in
  `rollback.py` (`restore_state`), and state persistence/validation in
  `state.py` (`_normalize_state` must accept the new section/fields).
- Inside `StateRecorder.transaction()` mutations are queued and dispatched as
  one `adb shell` script with `SUCCESS_<i>` markers; outside a transaction they
  run immediately with verify-then-revert-on-failure. Preserve both paths.
- Dry-run (`client.dry_run`) must never write device state OR local state files.
- Error reporting: raise `CommandError` / `SnapshotError` / `VerificationError`
  / `ValueError`; the CLI catches these and prints `Error: ...`.
- Keep diffs surgical: do not reformat, rename, or "improve" code unrelated to
  the task brief.

## Hard rules

- **NEVER run `git commit`, `git push`, or any git state mutation.** Leave all
  changes in the working tree.
- **NEVER invoke `adb`** or otherwise touch real devices; tests must be
  hermetic.
- Do not edit files under `.git/`.
- Do not change `pyproject.toml` lint/type configuration to make checks pass.
