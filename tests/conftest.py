"""Test harness: a throwaway PostgreSQL database and a vault in a temp directory.

The database is talos_test on the same server (port 5433), created fresh for each
test session and emptied between tests. The real talos database is never touched;
db.drop_database refuses to drop it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# The tests never read or write the owner's data folder: TALOS_HOME is a temporary folder for the whole run,
# set before talos is imported (talos.personal reads the owner's configuration from TALOS_HOME/config, and
# talos.jev its recipient text at import). A test that needs a home of its own sets TALOS_HOME itself.
os.environ["TALOS_HOME"] = tempfile.mkdtemp(prefix="talos-test-home-")

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))

from talos import db, taxonomy  # noqa: E402
from talos.ingest import Ingestor  # noqa: E402
from talos.vault import Vault  # noqa: E402

TEST_DSN = os.environ.get("TALOS_TEST_DSN", "host=/tmp port=5433 dbname=talos_test")

TABLES = [
    "calendar_entry", "calendar", "work_item_event", "work_item", "note", "fetch_failure", "ingest_failure", "changeset_op", "changeset", "sync_run", "sync_cursor", "event", "model_run", "rule", "rule_removed", "assignment",
    "object", "edge", "attachment", "participant", "message_location", "message_text", "message", "thread",
    "address", "person", "org", "blob", "entity", "my_address", "account", "importance_vip",
    "message_pattern", "subject_pattern", "sender_profile", "enrich_prediction", "enrich_case", "jev_prediction", "jev_case", "gold_check_item", "gold_label",
    "gold_item", "gold_set", "insight_cache", "improvement_job", "discovery_item", "discovery_source", "enrich_tick",
    "watcher", "aggregation", "web_session", "web_event", "web_state", "studio_verdict", "studio_lift", "studio_decision",
]


@pytest.fixture(scope="session")
def database():
    db.drop_database(TEST_DSN)
    db.ensure_database(TEST_DSN)
    with db.connect(TEST_DSN) as conn:
        db.migrate(conn)
        conn.commit()
    yield TEST_DSN


@pytest.fixture
def conn(database):
    with db.connect(database) as c:
        c.execute("truncate " + ", ".join(TABLES) + " restart identity cascade")
        c.execute("insert into account (id, provider, address) values"
                  " ('gmail', 'gmail', 'owner@gmail.com'),"
                  " ('work', 'graph', 'owner@company.example'),"
                  " ('local', 'local', 'local@talos.invalid')")
        c.execute("insert into my_address (address, account_id) values"
                  " ('owner@gmail.com', 'gmail'), ('owner@company.example', 'work'),"
                  " ('o@company.example', 'work')")
        c.commit()
        yield c
        c.rollback()


@pytest.fixture
def taxonomy_loaded(conn):
    """rules/taxonomy.json loaded, as `talos setup` does; afterwards the dimensions are put back
    as the migrations left them (open topic, ask, value and route), for the other tests."""
    saved = conn.execute("select * from dimension").fetchall()
    taxonomy.load(conn)
    conn.commit()
    yield
    conn.rollback()
    for d in saved:
        conn.execute("update dimension set label = %s, cardinality = %s, allowed = %s, value_meta = %s,"
                     " description = %s where id = %s",
                     (d["label"], d["cardinality"], Jsonb(d["allowed"]) if d["allowed"] is not None else None,
                      Jsonb(d["value_meta"]), d["description"], d["id"]))
    # values a test committed in a dimension the migrations do not have (kind, when an earlier test
    # removed it) would hold the dimension back; the next test truncates them anyway
    conn.execute("delete from assignment where not (dimension_id = any(%s))", ([d["id"] for d in saved],))
    conn.execute("delete from dimension where not (id = any(%s))", ([d["id"] for d in saved],))
    conn.commit()


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


@pytest.fixture
def ingestor(conn, vault):
    return Ingestor(conn, vault)
