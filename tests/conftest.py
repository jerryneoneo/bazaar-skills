"""Make pytest gate the project's check()/_fail accumulator pattern.

Every test file in this suite reports assertions through a module-level `check(name, cond)`
that appends failures to a module-level `_fail` list; the file's `__main__` runner then exits
non-zero when `_fail` is non-empty (see CONTRIBUTING.md — "make sure they print ALL PASS").

pytest collects those `test_*` functions too, but because `check()` never raises, a failed
check would otherwise be reported as "passed" under pytest — so `python3 -m pytest` was not a
real gate. This autouse fixture fails the individual test whose checks failed, making pytest as
strict as the `__main__` runner without touching any test file.
"""
import pytest


# The suite uses two accumulator-name conventions for the same check()/_fail idiom.
_ACCUMULATOR_NAMES = ("_fail", "_failures")


def _accumulators(module):
    """The module-level failure lists this test file uses (handles both naming conventions).
    De-duplicated by identity so an aliased _fail/_failures can't be counted twice."""
    by_id = {}
    for name in _ACCUMULATOR_NAMES:
        lst = getattr(module, name, None)
        if isinstance(lst, list):
            by_id.setdefault(id(lst), lst)
    return list(by_id.values())


@pytest.fixture(autouse=True)
def _gate_check_failures(request):
    """Fail any test that appended to its module's check()/_fail(ures) list during this test.

    Note: because this runs in fixture teardown, a violation surfaces as a pytest ERROR (not FAILURE)
    on that test — the exit code is still non-zero, so the run correctly fails."""
    accs = _accumulators(request.module)
    if not accs:               # module doesn't use the check()/_fail idiom — nothing to gate
        yield
        return
    before = {id(a): len(a) for a in accs}
    yield
    new_failures = [f for a in accs for f in a[before[id(a)]:]]
    if new_failures:
        pytest.fail("check() failures: " + "; ".join(str(f) for f in new_failures), pytrace=False)
