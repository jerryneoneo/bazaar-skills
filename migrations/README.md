# Migrations

Idempotent upgrade steps run by `setup` (and `/selly-upgrade`) when a migration's version is newer
than `~/.selly/.last-setup-version`. Mirrors gstack's migration runner.

## Convention

- One file per version: `v<MAJOR>.<MINOR>.<PATCH>.sh` (e.g. `v0.2.0.sh`).
- Each script MUST be **idempotent** (safe to run more than once) and **fail-open** where possible —
  a migration error should warn, not abort the whole setup.
- `setup` runs every `v*.sh` whose version is `> last-setup-version`, in ascending version order,
  then stamps `~/.selly/.last-setup-version` with the current `VERSION`.
- Scripts run with the runtime dir as CWD and receive `SELLY_HOME` in the environment.

## When to add one

Add a migration when an upgrade needs to reshape existing per-deployment state — e.g. renaming a
key in `data/config.json`, moving a launcher, or relocating a file. Pure code changes need no
migration.
