# Contributing to Bazaar Skills

Thanks for your interest in improving Bazaar Skills! This is an early-stage open-source project, and contributions — bug reports, fixes, new marketplace recipes, docs — are all welcome.

## Dev setup

```bash
git clone https://github.com/jerryneoneo/bazaar-skills.git ~/bazaar-skills
cd ~/bazaar-skills
./setup            # idempotent; installs launchers, then guided onboarding on first run
```

You'll need Python 3, Node + npx, Google Chrome, and the Claude Code CLI (signed in). See [SETUP.md](SETUP.md) for the full prerequisites and gotchas, and [README.md](README.md) for how the pieces fit together.

> On macOS, keep your checkout outside `~/Documents`, `~/Desktop`, and `~/Downloads` (TCC blocks background processes from reading those).

## Running the tests

The deterministic engines have plain-Python adversarial tests (no framework, no network):

```bash
for t in floor_gate shipping telegram; do python3 tests/test_$t.py; done
# run any single suite directly:
python3 tests/test_floor_gate.py
```

The full suite lives in [tests/](tests/) — run the ones relevant to your change and make sure they print `ALL PASS`. Money/identity logic (`bin/floor_gate.py`, `bin/shipping.py`, `bin/checkout.py`) must keep its invariant: **the floor and the exact address never appear in stdout, a record, or an error.** If you touch that code, add or extend a test that proves it.

## Conventions

- **Standard library only** for `bin/*.py` — no `pip install` dependencies. Keep it portable.
- **Commit messages:** conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- **No secrets, no personal data.** Everything that carries money, identity, or conversation content is gitignored — see [data/README.md](data/README.md). Never hard-code a real token, address, or buyer handle (use clearly-fake fixtures in tests).
- **Keep flows adapter- and marketplace-neutral** where they should be; put site-specific behavior in `skills/listing-flows/<site>.md`.

## Pull requests

1. Branch from `main`, make focused changes, and keep the diff scoped.
2. Run the relevant tests and confirm they pass.
3. Open a PR describing what changed and why, with a short test plan.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
