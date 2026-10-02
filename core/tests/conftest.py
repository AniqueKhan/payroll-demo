from datetime import date

import pytest

from core.demo_data.generator import generate
from core.engine.config import DEFAULT_RULES_PATH, load_rules
from core.engine.inputs import load_inputs_from_dir, read_manifest
from core.engine.run import run_payroll


@pytest.fixture(scope="session")
def rules():
    return load_rules(DEFAULT_RULES_PATH)


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    generate(out)
    return out


@pytest.fixture(scope="session")
def manifest(demo_dir):
    return read_manifest(demo_dir)


@pytest.fixture(scope="session")
def inputs(demo_dir, rules):
    return load_inputs_from_dir(demo_dir, rules)


@pytest.fixture(scope="session")
def result(inputs, rules):
    return run_payroll(inputs, rules)


@pytest.fixture
def scenario(manifest):
    """Look up a planted scenario: ``scenario(9, code="ABSENCE_UNINFORMED")``."""
    def find(number, **match):
        entries = manifest["scenarios"][str(number)]
        hits = [e for e in entries if all(e.get(k) == v for k, v in match.items())]
        assert len(hits) == 1, f"scenario {number} {match}: {hits}"
        hit = dict(hits[0])
        if hit.get("date"):
            hit["date"] = date.fromisoformat(hit["date"])
        return hit

    return find

