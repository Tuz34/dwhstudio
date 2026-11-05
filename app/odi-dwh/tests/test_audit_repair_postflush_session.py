"""A newly active ODI session must still block before committing repair metadata."""
import json

import pytest

from tests import dim_product_audit_role_fake_sdk as fake
from tests.test_dim_product_audit_role_repair import SCENARIOS
from dim_product_audit_role_repair import normalize_audit_role_report


@pytest.fixture(scope="module")
def appeared(tmp_path_factory):
    java = fake.available()
    if java is None:
        pytest.skip("Local Groovy unavailable; nothing downloaded")
    classes = dict(fake._CLASSES)
    classes["oracle.odi.domain.runtime.session.finder.IOdiSessionFinder"] = '''
import fake.World
class IOdiSessionFinder {
    int countByStatus(oracle.odi.domain.runtime.session.Status status) {
        World.log('countByStatus:' + status)
        if (World.trace.contains('flush') && World.spec.active_after_flush == status.name()) return 1
        0
    }
}
'''
    root = tmp_path_factory.mktemp("audit_session_appeared")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fake, "_CLASSES", classes)
        runtime = fake.prepare_runtime(java, root)
        cases = {status: {"params": SCENARIOS["stable_repair_all"]["params"],
                          "spec": {"active_after_flush": status}} for status in ("RUNNING", "WAITING", "QUEUED")}
        return fake.run_scenarios(java, root, runtime, cases)


@pytest.mark.parametrize("status", ["RUNNING", "WAITING", "QUEUED"])
def test_session_appearing_after_flush_blocks_commit_and_rolls_back(appeared, status):
    run = appeared[status]
    report = json.loads(run["output"])
    assert report["status"] == "blocked" and report["code"] == "active_odi_sessions"
    assert report["repository_outcome"] == "rolled_back" and report["side_effects"] is False
    assert "commit" not in run["trace"] and "rollback" in run["trace"]
    assert normalize_audit_role_report(report, "stable_repair")["status"] == "blocked"
