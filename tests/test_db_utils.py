import os
import sys
import uuid

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "agent-app"))

import pytest

pytestmark = pytest.mark.integration

if not os.getenv("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set — skipping Postgres-backed integration tests",
                allow_module_level=True)

import db_utils


@pytest.fixture(autouse=True)
def _init_db():
    db_utils.init_db()


def test_create_and_list_threads():
    thread_id = str(uuid.uuid4())
    title = f"test thread {thread_id}"
    db_utils.create_thread(thread_id, title=title)
    matching = [t for t in db_utils.list_threads() if str(t["thread_id"]) == thread_id]
    assert len(matching) == 1
    assert matching[0]["title"] == title


def test_list_threads_has_no_per_user_scoping():
    thread_id = str(uuid.uuid4())
    db_utils.create_thread(thread_id, title="anyone should see this")
    all_ids = {str(t["thread_id"]) for t in db_utils.list_threads()}
    assert thread_id in all_ids


def test_touch_thread_bumps_updated_at():
    thread_id = str(uuid.uuid4())
    db_utils.create_thread(thread_id, title="will be touched")
    before = next(t for t in db_utils.list_threads() if str(t["thread_id"]) == thread_id)
    db_utils.touch_thread(thread_id)
    after = next(t for t in db_utils.list_threads() if str(t["thread_id"]) == thread_id)
    assert after["updated_at"] >= before["updated_at"]
