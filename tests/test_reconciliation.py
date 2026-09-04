import json
import sqlite3
import threading

import requests

from core import (
    APPROVED_TOOLS,
    RazorpayClient,
    audit,
    connect,
    evaluation,
    gemini_investigation,
    get_exception,
    init_db,
    investigate_exception,
    load_benchmark,
    razorpay_sync,
    reconcile,
    rows,
    tool_result,
    validate_llm_result,
)


def fresh_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


def test_benchmark_generates_required_labelled_records():
    db = fresh_db()
    result = load_benchmark(db)
    labels = {r["label"] for r in rows(db, "select distinct label from payments")}

    assert result["payments"] == 5000
    assert result["development_records"] + result["held_out_records"] == 5000
    assert 900 <= result["held_out_records"] <= 1100
    assert {
        "exact_match",
        "fee_tax_discrepancy",
        "refund_mismatch",
        "duplicate",
        "missing_settlement",
        "bank_discrepancy",
        "wrong_mapping",
        "timing_mismatch",
        "unknown_exception",
    }.issubset(labels)


def test_benchmark_is_reproducible_with_fixed_seed():
    first = fresh_db()
    second = fresh_db()
    load_benchmark(first, records=220, seed=73)
    load_benchmark(second, records=220, seed=73)

    query = """
        select p.id, p.amount, p.method, p.created_at, g.dataset_split, g.scenario,
               g.expected_status, g.expected_exception_type
        from payments p join benchmark_ground_truth g on g.payment_id = p.id
        order by p.id
    """
    assert [tuple(item) for item in rows(first, query)] == [tuple(item) for item in rows(second, query)]


def test_held_out_evaluation_is_complete_and_independent():
    db = fresh_db()
    load_benchmark(db, records=550, seed=42)
    financial_columns = []

    def capture(statement):
        if statement.lower().startswith("select id, order_id"):
            financial_columns.append(statement.lower())

    db.set_trace_callback(capture)
    reconcile(db)
    db.set_trace_callback(None)
    held_out = evaluation(db, "held_out")
    types = {item["exception_type"] for item in held_out["exception_breakdown"]}

    assert financial_columns
    assert all("label" not in statement and "ground_truth" not in statement for statement in financial_columns)
    assert held_out["records_processed"] > 0
    assert held_out["false_positives"] == 0
    assert held_out["false_negatives"] == 0
    assert held_out["reconciliation_accuracy"] == 100.0
    assert {"WRONG_MAPPING", "MISSING_RECONCILIATION_RECORD", "AMOUNT_MISMATCH"}.issubset(types)


def test_required_exception_types_match_ground_truth():
    db = fresh_db()
    load_benchmark(db, records=550, seed=42)
    reconcile(db)
    detected = {
        item["scenario"]: item["reason_code"]
        for item in rows(
            db,
            """
            select g.scenario, r.reason_code
            from benchmark_ground_truth g
            join reconciliation_results r on r.payment_id = g.payment_id
            where g.scenario in ('wrong_mapping', 'missing_reconciliation_record', 'amount_mismatch')
            """,
        )
    }

    assert detected == {
        "wrong_mapping": "WRONG_MAPPING",
        "missing_reconciliation_record": "MISSING_RECONCILIATION_RECORD",
        "amount_mismatch": "AMOUNT_MISMATCH",
    }


def test_unlabelled_operational_data_does_not_claim_accuracy():
    db = fresh_db()
    load_benchmark(db, records=110)
    reconcile(db)
    db.execute("delete from benchmark_ground_truth")
    metrics = evaluation(db)

    assert metrics["records_processed"] == 110
    assert metrics["matched"] + metrics["exceptions"] == 110
    assert metrics["reconciliation_accuracy"] is None
    assert metrics["exception_precision"] is None
    assert metrics["exception_recall"] is None


def test_unresolved_value_separates_real_exposure_from_pipeline_lag():
    db = fresh_db()
    load_benchmark(db, records=550, seed=42)
    reconcile(db)
    metrics = evaluation(db)
    by_class = metrics["unresolved_value_by_class"]

    # The classes must partition the headline total, not overlap or leak.
    assert sum(by_class.values()) == metrics["unresolved_value"]
    assert sum(metrics["unresolved_count_by_class"].values()) == metrics["unresolved_exceptions"]
    assert metrics["unresolved_amount_at_risk"] == by_class["amount_at_risk"]

    # Missing settlements dominate the raw total but are lag, not loss, so the
    # at-risk figure must stay well below the undifferentiated sum.
    assert by_class["awaiting_settlement"] > by_class["amount_at_risk"]

    # Structural exceptions reconcile to the rupee; a non-zero exposure there
    # would mean the difference calculation had drifted.
    assert by_class["structural"] == 0
    assert metrics["unresolved_count_by_class"]["structural"] > 0

    for item in metrics["exception_breakdown"]:
        assert item["exposure_class"] in {"amount_at_risk", "awaiting_settlement", "structural"}


def test_concurrent_cold_starts_on_a_shared_connection_need_a_lock():
    # app.py caches one sqlite3.Connection per process (@st.cache_resource) and every
    # Streamlit session runs its script in its own thread, so two sessions hitting a
    # cold start together both see an empty benchmark table and both call
    # load_benchmark on the SAME connection. Reproduced live on the deployed app as
    # sqlite3.IntegrityError: UNIQUE constraint failed: benchmark_ground_truth.payment_id,
    # crashing the page instead of triggering the "database is busy, retry" path,
    # because IntegrityError is not OperationalError.
    #
    # Unguarded, this is flaky by nature (it depends on thread interleaving), so it
    # isn't asserted here. What's asserted is the fix app.py actually uses: serializing
    # the bootstrap section with one process-wide lock must produce zero errors and a
    # fully consistent result, however many sessions race to start it.
    # fresh_db()'s plain sqlite3.connect() forbids cross-thread use; core.connect()
    # sets check_same_thread=False, matching what app.py actually shares across
    # session threads via @st.cache_resource.
    db = connect(":memory:")
    init_db(db)
    lock = threading.Lock()
    errors = []

    def cold_start():
        try:
            with lock:
                if db.execute("select count(*) from payments").fetchone()[0] == 0:
                    db.rollback()
                    load_benchmark(db, records=300, seed=42)
                    reconcile(db)
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=cold_start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert db.execute("select count(*) from payments").fetchone()[0] == 300
    assert db.execute("select count(*) from reconciliation_results").fetchone()[0] == 300


def test_reconcile_rebuilds_a_partially_written_result_set():
    # Reloading the page mid-run kills that script run, and the cached connection
    # inherits the abandoned transaction. Because the benchmark writes every
    # exact-match payment first, a partial result set looks like a healthy run with
    # a suspiciously high match rate and an empty exception queue. Reconciling again
    # has to reproduce the full result set rather than append to the fragment.
    db = fresh_db()
    load_benchmark(db, records=550, seed=42)
    complete = reconcile(db)

    db.execute("delete from reconciliation_results")
    db.execute("delete from exceptions")
    for index in range(120):
        db.execute(
            "insert into reconciliation_results values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"rr_partial_{index}", f"pay_bench_{index:05d}", None, "matched", "R", 0, 0, 0, 0),
        )

    payments = db.execute("select count(*) from payments").fetchone()[0]
    partial = db.execute("select count(*) from reconciliation_results").fetchone()[0]
    assert partial != payments  # the signal the app boots on
    assert db.execute("select count(*) from exceptions").fetchone()[0] == 0

    repaired = reconcile(db)

    assert repaired["total"] == complete["total"]
    assert repaired["matched"] == complete["matched"]
    assert repaired["exceptions"] == complete["exceptions"]
    assert db.execute("select count(*) from reconciliation_results").fetchone()[0] == payments


def test_no_bank_entry_exists_without_a_settlement():
    db = fresh_db()
    load_benchmark(db, records=550, seed=42)

    orphans = rows(
        db,
        "select b.id from bank_entries b "
        "left join settlements s on s.id = b.reference where s.id is null",
    )

    assert orphans == []


def test_reconciliation_detects_exceptions_without_false_positive_exact_matches():
    db = fresh_db()
    load_benchmark(db)
    result = reconcile(db)
    detected_labels = {r["label"] for r in rows(db, "select distinct label from exceptions")}
    detected_types = {r["type"] for r in rows(db, "select distinct type from exceptions")}
    exact_exceptions = rows(
        db,
        """
        select e.* from exceptions e
        join payments p on p.id = e.transaction_id
        where p.label = 'exact_match'
        """,
    )

    assert result["total"] >= 500
    assert result["matched"] > result["exceptions"]
    assert not exact_exceptions
    assert "WRONG_MAPPING" in detected_types
    assert {
        "fee_tax_discrepancy",
        "refund_mismatch",
        "duplicate",
        "missing_settlement",
        "bank_discrepancy",
        "wrong_mapping",
        "timing_mismatch",
        "unknown_exception",
    }.issubset(detected_labels)


def test_investigator_uses_only_approved_tools_and_records_evidence():
    db = fresh_db()
    load_benchmark(db)
    reconcile(db)
    case = rows(db, "select * from exceptions where type = 'MISSING_SETTLEMENT' limit 1")[0]
    investigated = investigate_exception(db, case["id"])

    assert investigated["evidence"]
    assert {call["name"] for call in investigated["tool_calls"]}.issubset(APPROVED_TOOLS)
    assert investigated["ai_summary"]
    assert investigated["confidence"] > 0


def test_insufficient_evidence_routes_to_human_review(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    db = fresh_db()
    db.execute(
        """
        insert into exceptions (
            id, transaction_id, severity, type, expected_amount, actual_amount,
            difference, status, confidence, ai_summary, recommendation,
            investigation_source, evidence, tool_calls, created_at, resolved_at, label
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ex_missing", "pay_missing", "high", "UNKNOWN_EXCEPTION", 1000, 0, -1000, "open", 0.0, "", "", "Deterministic fallback", "[]", "[]", 1, None, "unknown_exception"),
    )
    db.commit()
    investigated = investigate_exception(db, "ex_missing")

    assert investigated["status"] == "human_review"
    assert investigated["ai_summary"] == "INSUFFICIENT_EVIDENCE"


def test_unapproved_tool_is_blocked():
    try:
        tool_result("resolve_exception", {})
    except ValueError as exc:
        assert "not approved" in str(exc)
    else:
        raise AssertionError("unapproved mutation tool was allowed")


def test_wrong_mapping_evidence_shows_bad_reference():
    db = fresh_db()
    load_benchmark(db)
    reconcile(db)
    case = rows(db, "select * from exceptions where type = 'WRONG_MAPPING' limit 1")[0]
    investigated = investigate_exception(db, case["id"])
    related = next(item for item in investigated["evidence"] if item["tool"] == "find_related_transactions")

    assert investigated["label"] == "wrong_mapping"
    assert investigated["type"] == "WRONG_MAPPING"
    assert related["result"][0]["entity_id"] == investigated["transaction_id"]
    assert related["result"][0]["payment_id"] != investigated["transaction_id"]


def test_gemini_structured_output_is_used_when_available(monkeypatch):
    db = fresh_db()
    load_benchmark(db)
    reconcile(db)
    case = rows(db, "select * from exceptions where type = 'WRONG_MAPPING' limit 1")[0]

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    '{"likely_root_cause":"WRONG_MAPPING",'
                                    '"explanation":"The recon row references a different payment_id.",'
                                    '"evidence_summary":["Tool evidence shows mismatched payment_id."],'
                                    '"confidence":0.93,'
                                    '"recommendation":"Escalate mapping correction for human approval.",'
                                    '"requires_human_review":true}'
                                ),
                            }
                        ],
                    }
                ]
            }

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    investigated = investigate_exception(db, case["id"])

    assert investigated["investigation_source"] == "Gemini AI"
    assert investigated["confidence"] == 0.93
    assert investigated["status"] == "human_review"


def test_malformed_gemini_response_falls_back(monkeypatch):
    db = fresh_db()
    load_benchmark(db)
    reconcile(db)
    case = rows(db, "select * from exceptions limit 1")[0]

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]}

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    investigated = investigate_exception(db, case["id"])

    assert investigated["investigation_source"] == "Deterministic fallback"
    assert investigated["ai_summary"]


def test_gemini_structured_output_validation_rejects_invalid_fields():
    invalid = {
        "likely_root_cause": "WRONG_MAPPING",
        "explanation": "Evidence-based explanation.",
        "evidence_summary": ["Verified mismatch."],
        "confidence": "high",
        "recommendation": "Review mapping.",
        "requires_human_review": True,
    }

    assert validate_llm_result(invalid) is None


def test_transient_gemini_timeout_is_retried_once_before_falling_back(monkeypatch):
    """A read timeout is the network, not an answer, so it earns one more attempt.

    The audit trail showed real 20s read timeouts and 500s from the Interactions
    endpoint. Each one spent the whole investigation and pushed the case to the
    deterministic fallback, so a slow-but-healthy response looked identical to an
    outage.
    """
    calls = []

    def timeout_once_then_succeed(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        if len(calls) == 1:
            raise requests.Timeout("read timed out")

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output_text": json.dumps(
                        {
                            "likely_root_cause": "FEE_TAX_DISCREPANCY",
                            "explanation": "Settlement is short by the recorded fee.",
                            "evidence_summary": ["fee 1298", "tax 234"],
                            "confidence": 0.91,
                            "recommendation": "Review the fee breakdown.",
                            "requires_human_review": True,
                        }
                    )
                }

        return Response()

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", timeout_once_then_succeed)
    result, reason = gemini_investigation({"id": "e1", "transaction_id": "p1", "type": "AMOUNT_MISMATCH",
                                           "expected_amount": 100, "actual_amount": 90, "difference": -10,
                                           "severity": "medium", "status": "open"}, [])

    assert len(calls) == 2, "a transient timeout should be retried exactly once"
    assert reason is None
    assert result["likely_root_cause"] == "FEE_TAX_DISCREPANCY"


def test_non_transient_gemini_failure_is_not_retried(monkeypatch):
    """A 4xx is a settled answer. Retrying it only delays the fallback."""
    calls = []

    def unauthorized(*args, **kwargs):
        calls.append(1)
        response = requests.Response()
        response.status_code = 401
        raise requests.HTTPError("401 Client Error: Unauthorized", response=response)

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", unauthorized)
    result, reason = gemini_investigation({"id": "e1", "transaction_id": "p1", "type": "AMOUNT_MISMATCH",
                                           "expected_amount": 100, "actual_amount": 90, "difference": -10,
                                           "severity": "medium", "status": "open"}, [])

    assert len(calls) == 1, "a 401 must not be retried"
    assert result is None
    assert "401" in reason


def test_audit_ids_do_not_collide_within_a_single_second():
    """Audit ids used a second-precision timestamp plus randint(1000, 9999).

    That is 9,000 ids per second, and audit rows are written in bursts inside one
    second: investigate_exception writes EXCEPTION_INVESTIGATED and GEMINI_FALLBACK
    back to back. Two rows in the same second collided about once in 9,000 and raised
    "UNIQUE constraint failed: audit_events.id", which showed up as an intermittent
    test failure and would crash a real investigation or human decision in the app.

    2,000 writes inside one second would have collided with ~99.9% probability under
    the old scheme, so this fails loudly if the id ever narrows again.
    """
    db = fresh_db()
    for index in range(2000):
        audit(db, "actor", "ACTION", f"entity_{index}", None, {"n": index})
    db.commit()

    total = db.execute("select count(*) from audit_events").fetchone()[0]
    distinct = db.execute("select count(distinct id) from audit_events").fetchone()[0]
    assert total == 2000
    assert distinct == 2000


def test_gemini_api_failure_uses_deterministic_fallback(monkeypatch):
    db = fresh_db()
    load_benchmark(db, records=110)
    reconcile(db)
    case = rows(db, "select * from exceptions where type = 'AMOUNT_MISMATCH' limit 1")[0]

    def fail_request(*args, **kwargs):
        raise requests.RequestException("simulated outage")

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", fail_request)
    investigated = investigate_exception(db, case["id"])

    assert investigated["investigation_source"] == "Deterministic fallback"
    assert investigated["requires_human_review"] is True
    assert investigated["ai_summary"]


def test_gemini_failure_reason_is_recorded_in_audit_trail(monkeypatch):
    db = fresh_db()
    load_benchmark(db, records=110)
    reconcile(db)
    case = rows(db, "select * from exceptions where type = 'AMOUNT_MISMATCH' limit 1")[0]

    def fail_request(*args, **kwargs):
        raise requests.RequestException("simulated outage")

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", fail_request)
    investigate_exception(db, case["id"])

    fallback_events = rows(
        db,
        "select * from audit_events where action = 'GEMINI_FALLBACK' and entity_id = ?",
        (case["id"],),
    )
    assert len(fallback_events) == 1
    import json as _json

    reason = _json.loads(fallback_events[0]["after_state"])["reason"]
    assert "RequestException" in reason
    assert "simulated outage" in reason


def test_missing_api_key_does_not_log_a_gemini_failure(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    db = fresh_db()
    load_benchmark(db, records=110)
    reconcile(db)
    case = rows(db, "select * from exceptions where type = 'AMOUNT_MISMATCH' limit 1")[0]
    investigate_exception(db, case["id"])

    fallback_events = rows(db, "select * from audit_events where action = 'GEMINI_FALLBACK'")
    assert fallback_events == []


def test_razorpay_pagination_fetches_until_short_page(monkeypatch):
    client = object.__new__(RazorpayClient)
    client.key_id = "rzp_test_key"
    client.key_secret = "secret"
    calls = []

    def fake_get(path, params=None):
        calls.append((path, dict(params)))
        if params["skip"] == 0:
            return {"items": [{"id": "a"}, {"id": "b"}]}
        if params["skip"] == 2:
            return {"items": [{"id": "c"}]}
        return {"items": []}

    monkeypatch.setattr(client, "get", fake_get)
    result = client.fetch_all("/payments", {"count": 2, "skip": 0, "from": 100, "to": 200}, count=2)

    assert [item["id"] for item in result["items"]] == ["a", "b", "c"]
    assert [call[1]["skip"] for call in calls] == [0, 2]
    assert all(call[1]["from"] == 100 and call[1]["to"] == 200 for call in calls)


def test_razorpay_payments_pagination_respects_page_limit(monkeypatch):
    client = object.__new__(RazorpayClient)
    client.key_id = "rzp_test_key"
    client.key_secret = "secret"
    calls = []

    def fake_get(path, params=None):
        calls.append((path, dict(params)))
        return {"items": []}

    monkeypatch.setattr(client, "get", fake_get)
    client.fetch_payments(count=1000)

    assert calls[0][1]["count"] == 100


def test_razorpay_sync_scopes_payments_and_settlements_to_period(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def fetch_settlements(self, **kwargs):
            self.calls.append(("settlements", kwargs))
            return {"items": []}

        def fetch_recon(self, year, month, day):
            self.calls.append(("recon", {"year": year, "month": month, "day": day}))
            return {"items": []}

        def fetch_payments(self, **kwargs):
            self.calls.append(("payments", kwargs))
            return {"items": []}

    client = Client()
    monkeypatch.setattr("core.RazorpayClient", lambda: client)
    result = razorpay_sync(fresh_db(), 2026, 8, 5)

    assert result == {"payments": 0, "settlements": 0, "reconciliation_records": 0}
    assert client.calls[0][1] == {"from_ts": 1785888000, "to_ts": 1785974399}
    assert client.calls[1][1] == {"year": 2026, "month": 8, "day": 5}
    assert client.calls[2][1] == {"from_ts": 1785888000, "to_ts": 1785974399}
