from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            clean_key = key.strip()
            clean_value = value.strip().strip('"').strip("'")
            if not os.environ.get(clean_key):
                os.environ[clean_key] = clean_value


load_env_file()
DB_PATH = os.getenv("DATABASE_URL", "razorrecon.sqlite").replace("sqlite:///", "")
GEMINI_MODEL = "gemini-3.5-flash-lite"
BENCHMARK_RECORDS = 5_000
BENCHMARK_SEED = 42
HELD_OUT_FRACTION = 0.20
APPROVED_TOOLS = {
    "get_payment",
    "get_settlement",
    "get_reconciliation_record",
    "find_related_transactions",
    "find_refunds",
    "find_adjustments",
    "compare_bank_entry",
    "calculate_expected_settlement",
}


def now_ts() -> int:
    return int(time.time())


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        create table if not exists payments (
            id text primary key,
            order_id text,
            amount integer not null,
            currency text not null,
            status text not null,
            method text not null,
            captured_at integer not null,
            created_at integer not null,
            label text not null
        );
        create table if not exists settlements (
            id text primary key,
            amount integer not null,
            status text not null,
            fees integer not null,
            tax integer not null,
            utr text not null,
            created_at integer not null
        );
        create table if not exists reconciliation_records (
            id text primary key,
            entity_id text not null,
            entity_type text not null,
            settlement_id text,
            payment_id text,
            refund_id text,
            transfer_id text,
            adjustment_id text,
            amount integer not null,
            fee integer not null,
            tax integer not null,
            debit_credit text not null,
            created_at integer not null
        );
        create table if not exists bank_entries (
            id text primary key,
            reference text not null,
            utr text not null,
            amount integer not null,
            date integer not null,
            description text not null
        );
        create table if not exists reconciliation_results (
            id text primary key,
            payment_id text not null,
            settlement_id text,
            status text not null,
            reason_code text not null,
            expected_amount integer not null,
            actual_amount integer not null,
            difference integer not null,
            created_at integer not null
        );
        create table if not exists benchmark_ground_truth (
            payment_id text primary key,
            dataset_split text not null,
            scenario text not null,
            expected_status text not null,
            expected_exception_type text,
            seed integer not null
        );
        create table if not exists reconciliation_runs (
            id text primary key,
            records integer not null,
            matched integer not null,
            exceptions integer not null,
            elapsed_seconds real not null,
            throughput_per_second real not null,
            created_at integer not null
        );
        create table if not exists exceptions (
            id text primary key,
            transaction_id text not null,
            severity text not null,
            type text not null,
            expected_amount integer not null,
            actual_amount integer not null,
            difference integer not null,
            status text not null,
            confidence real not null,
            ai_summary text not null,
            recommendation text not null,
            investigation_source text not null default 'Deterministic fallback',
            likely_root_cause text not null default '',
            evidence_summary text not null default '[]',
            requires_human_review integer not null default 1,
            evidence text not null,
            tool_calls text not null,
            created_at integer not null,
            resolved_at integer,
            label text not null
        );
        create table if not exists audit_events (
            id text primary key,
            actor text not null,
            action text not null,
            entity_id text not null,
            before_state text,
            after_state text,
            timestamp integer not null
        );
        """
    )
    ensure_column(db, "exceptions", "investigation_source", "text not null default 'Deterministic fallback'")
    ensure_column(db, "exceptions", "likely_root_cause", "text not null default ''")
    ensure_column(db, "exceptions", "evidence_summary", "text not null default '[]'")
    ensure_column(db, "exceptions", "requires_human_review", "integer not null default 1")
    db.commit()


def ensure_column(db: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    existing = {row["name"] for row in db.execute(f"pragma table_info({table_name})")}
    if column_name not in existing:
        db.execute(f"alter table {table_name} add column {column_name} {definition}")


def reset_data(db: sqlite3.Connection) -> None:
    for table in [
        "audit_events",
        "exceptions",
        "reconciliation_results",
        "reconciliation_runs",
        "benchmark_ground_truth",
        "bank_entries",
        "reconciliation_records",
        "settlements",
        "payments",
    ]:
        db.execute(f"delete from {table}")
    db.commit()


def audit(db: sqlite3.Connection, actor: str, action: str, entity_id: str, before: Any, after: Any) -> None:
    db.execute(
        "insert into audit_events values (?, ?, ?, ?, ?, ?, ?)",
        (
            f"audit_{now_ts()}_{random.randint(1000, 9999)}",
            actor,
            action,
            entity_id,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            now_ts(),
        ),
    )


def _benchmark_distribution(records: int) -> dict[str, int]:
    if records < 11:
        raise ValueError("Benchmark requires at least 11 records so every scenario is represented")
    weights = {
        "exact_match": 70,
        "fee_tax_discrepancy": 3,
        "refund_mismatch": 3,
        "duplicate": 3,
        "missing_settlement": 3,
        "bank_discrepancy": 3,
        "wrong_mapping": 3,
        "timing_mismatch": 3,
        "unknown_exception": 3,
        "missing_reconciliation_record": 3,
        "amount_mismatch": 3,
    }
    raw = {label: records * weight / 100 for label, weight in weights.items()}
    counts = {label: max(1, int(value)) for label, value in raw.items()}
    difference = records - sum(counts.values())
    order = sorted(weights, key=lambda label: raw[label] - int(raw[label]), reverse=difference > 0)
    cursor = 0
    while difference:
        label = order[cursor % len(order)]
        if difference > 0:
            counts[label] += 1
            difference -= 1
        elif counts[label] > 1:
            counts[label] -= 1
            difference += 1
        cursor += 1
    return counts


def load_benchmark(db: sqlite3.Connection, records: int = BENCHMARK_RECORDS, seed: int = BENCHMARK_SEED) -> dict[str, int]:
    reset_data(db)
    rng = random.Random(seed)
    base = 1_785_542_400
    distribution = _benchmark_distribution(records)
    expected_types = {
        "fee_tax_discrepancy": "SETTLEMENT_AMOUNT_DISCREPANCY",
        "refund_mismatch": "REFUND_AMOUNT_MISMATCH",
        "duplicate": "DUPLICATE_RECONCILIATION_RECORD",
        "missing_settlement": "MISSING_SETTLEMENT",
        "bank_discrepancy": "BANK_UTR_AMOUNT_MISMATCH",
        "wrong_mapping": "WRONG_MAPPING",
        "timing_mismatch": "TIMING_WINDOW_EXCEEDED",
        "unknown_exception": "SETTLEMENT_AMOUNT_DISCREPANCY",
        "missing_reconciliation_record": "MISSING_RECONCILIATION_RECORD",
        "amount_mismatch": "AMOUNT_MISMATCH",
    }

    index = 1
    for label, count in distribution.items():
        held_out_count = max(1, round(count * HELD_OUT_FRACTION))
        split_order = ["held_out"] * held_out_count + ["development"] * (count - held_out_count)
        rng.shuffle(split_order)
        for scenario_index in range(count):
            amount = rng.randint(300, 90_000) * 100 + rng.choice([0, 25, 50, 75])
            fee_rate = rng.choice([0.015, 0.018, 0.02, 0.022, 0.025])
            fee = round(amount * fee_rate)
            tax = round(fee * 0.18)
            expected = amount - fee - tax
            payment_id = f"pay_bench_{index:05d}"
            settlement_id = f"setl_bench_{index:05d}"
            utr = f"UTR{202608000000 + index}"
            created_at = base + index * rng.randint(75, 240)
            settled_at = created_at + rng.randint(1, 4) * 86400 + rng.randint(0, 3600)
            settlement_amount = expected
            recon_payment_id = payment_id
            recon_settlement_id: str | None = settlement_id
            recon_created_at = settled_at
            bank_amount = settlement_amount
            create_settlement = True
            create_reconciliation = True
            recon_amount = amount

            if label == "fee_tax_discrepancy":
                settlement_amount = expected + rng.choice([-1, 1]) * rng.randint(50, 450)
                bank_amount = settlement_amount
            elif label == "refund_mismatch":
                settlement_amount = expected - rng.randint(5000, min(25000, max(5001, expected // 2)))
                bank_amount = settlement_amount
            elif label == "missing_settlement":
                create_settlement = False
                recon_settlement_id = None
                settlement_amount = 0
                bank_amount = 0
            elif label == "bank_discrepancy":
                bank_amount = expected - rng.randint(100, 900)
            elif label == "wrong_mapping":
                recon_payment_id = f"pay_wrong_{index:05d}"
            elif label == "timing_mismatch":
                recon_created_at = created_at + 864000
            elif label == "unknown_exception":
                settlement_amount = expected - rng.randint(900, 1700)
                bank_amount = settlement_amount + rng.randint(200, 600)
            elif label == "missing_reconciliation_record":
                create_reconciliation = False
            elif label == "amount_mismatch":
                recon_amount = amount + rng.choice([-1, 1]) * rng.randint(100, 5000)

            db.execute(
                "insert into payments values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (payment_id, f"order_{index:05d}", amount, "INR", "captured", rng.choice(["card", "upi", "netbanking", "wallet"]), created_at + rng.randint(5, 120), created_at, label),
            )
            if create_settlement:
                db.execute(
                    "insert into settlements values (?, ?, ?, ?, ?, ?, ?)",
                    (settlement_id, settlement_amount, "processed", fee, tax, utr, settled_at),
                )
            if create_reconciliation:
                db.execute(
                    "insert into reconciliation_records values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"recon_{index:05d}", payment_id if label == "wrong_mapping" else recon_payment_id, "payment", recon_settlement_id, recon_payment_id, None, None, None, recon_amount, fee, tax, "credit", recon_created_at),
                )
            db.execute(
                "insert into bank_entries values (?, ?, ?, ?, ?, ?)",
                (f"bank_{index:04d}", settlement_id, utr, bank_amount, settled_at + 3600, f"Razorpay settlement {settlement_id}"),
            )
            if label == "duplicate":
                db.execute(
                    "insert into reconciliation_records values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"recon_{index:05d}_dupe", payment_id, "payment", settlement_id, payment_id, None, None, None, amount, fee, tax, "credit", recon_created_at + 5),
                )
            if label == "refund_mismatch":
                db.execute(
                    "insert into reconciliation_records values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"recon_{index:05d}_refund", f"rfnd_{index:05d}", "refund", settlement_id, payment_id, f"rfnd_{index:05d}", None, None, expected - settlement_amount + 700, 0, 0, "debit", recon_created_at),
                )
            db.execute(
                "insert into benchmark_ground_truth values (?, ?, ?, ?, ?, ?)",
                (
                    payment_id,
                    split_order[scenario_index],
                    label,
                    "matched" if label == "exact_match" else "exception",
                    expected_types.get(label),
                    seed,
                ),
            )
            index += 1

    audit(db, "system", "BENCHMARK_LOADED", "benchmark", None, {"payments": index - 1, "seed": seed, "held_out_fraction": HELD_OUT_FRACTION})
    db.commit()
    return {
        "payments": index - 1,
        "development_records": scalar(db, "select count(*) from benchmark_ground_truth where dataset_split = 'development'"),
        "held_out_records": scalar(db, "select count(*) from benchmark_ground_truth where dataset_split = 'held_out'"),
        "reconciliation_records": scalar(db, "select count(*) from reconciliation_records"),
        "seed": seed,
    }


def reconcile(db: sqlite3.Connection) -> dict[str, Any]:
    start = time.perf_counter()
    db.execute("delete from reconciliation_results")
    db.execute("delete from exceptions")
    # Ground-truth labels are deliberately excluded: the financial engine receives
    # only source fields and is evaluated against labels after reconciliation.
    payments = rows(
        db,
        "select id, order_id, amount, currency, status, method, captured_at, created_at from payments",
    )
    matched = 0
    exception_count = 0
    for payment in payments:
        result = reconcile_payment(db, payment)
        db.execute(
            "insert into reconciliation_results values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"rr_{payment['id']}",
                payment["id"],
                result["settlement_id"],
                result["status"],
                result["reason_code"],
                result["expected_amount"],
                result["actual_amount"],
                result["difference"],
                now_ts(),
            ),
        )
        if result["status"] == "matched":
            matched += 1
        else:
            exception_count += 1
            db.execute(
                """
                insert into exceptions (
                    id, transaction_id, severity, type, expected_amount, actual_amount,
                    difference, status, confidence, ai_summary, recommendation,
                    investigation_source, evidence, tool_calls, created_at, resolved_at, label
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ex_{payment['id']}",
                    payment["id"],
                    result["severity"],
                    result["reason_code"],
                    result["expected_amount"],
                    result["actual_amount"],
                    result["difference"],
                    "open",
                    0.0,
                    "",
                    "",
                    "Deterministic fallback",
                    "[]",
                    "[]",
                    now_ts(),
                    None,
                    "unlabelled",
                ),
            )
    # Compatibility-only annotation happens after every financial result is fixed.
    # It is never read by reconcile_payment and cannot affect classification.
    db.execute(
        """
        update exceptions
        set label = coalesce(
            (select scenario from benchmark_ground_truth where payment_id = exceptions.transaction_id),
            label
        )
        """
    )
    elapsed = time.perf_counter() - start
    throughput = round(len(payments) / elapsed, 2) if elapsed else 0
    run_id = f"run_{time.time_ns()}"
    db.execute(
        "insert into reconciliation_runs values (?, ?, ?, ?, ?, ?, ?)",
        (run_id, len(payments), matched, exception_count, elapsed, throughput, now_ts()),
    )
    audit(db, "system", "RECONCILIATION_RUN", "reconciliation", None, {"matched": matched, "exceptions": exception_count, "throughput_per_second": throughput})
    db.commit()
    return {"total": len(payments), "matched": matched, "exceptions": exception_count, "elapsed_seconds": round(elapsed, 4), "throughput_per_second": throughput}


def reconcile_payment(db: sqlite3.Connection, payment: sqlite3.Row) -> dict[str, Any]:
    related = rows(db, "select * from reconciliation_records where payment_id = ?", (payment["id"],))
    wrong_mapping_rows = rows(db, "select * from reconciliation_records where entity_id = ? and payment_id != ?", (payment["id"], payment["id"]))
    payment_rows = [r for r in related if r["entity_type"] == "payment"]
    expected = calculate_expected_settlement(db, payment["id"])["expected_amount"]
    settlement_id = payment_rows[0]["settlement_id"] if payment_rows else wrong_mapping_rows[0]["settlement_id"] if wrong_mapping_rows else None
    settlement = row(db, "select * from settlements where id = ?", (settlement_id,)) if settlement_id else None
    bank = row(db, "select * from bank_entries where reference = ?", (settlement_id,)) if settlement_id else None
    actual = settlement["amount"] if settlement else 0
    reason = "PAYMENT_SETTLEMENT_FEE_TAX_RECONCILED"
    status = "matched"
    severity = "low"

    if wrong_mapping_rows:
        wrong = wrong_mapping_rows[0]
        expected = payment["amount"] - wrong["fee"] - wrong["tax"]
        status, reason, severity = "exception", "WRONG_MAPPING", "high"
    elif len(payment_rows) > 1:
        status, reason, severity = "exception", "DUPLICATE_RECONCILIATION_RECORD", "high"
    elif not payment_rows:
        status, reason, severity = "exception", "MISSING_RECONCILIATION_RECORD", "high"
    elif payment_rows[0]["entity_id"] != payment["id"]:
        status, reason, severity = "exception", "PAYMENT_ID_MISMATCH", "high"
    elif payment_rows[0]["amount"] != payment["amount"]:
        expected = payment["amount"]
        actual = payment_rows[0]["amount"]
        status, reason, severity = "exception", "AMOUNT_MISMATCH", "high"
    elif not settlement:
        status, reason, severity = "exception", "MISSING_SETTLEMENT", "high"
    elif abs(payment_rows[0]["created_at"] - payment["captured_at"]) > 7 * 86400:
        status, reason, severity = "exception", "TIMING_WINDOW_EXCEEDED", "medium"
    elif actual != expected:
        refunds = [r for r in related if r["entity_type"] == "refund"]
        status = "exception"
        reason = "REFUND_AMOUNT_MISMATCH" if refunds else "SETTLEMENT_AMOUNT_DISCREPANCY"
        severity = "medium" if abs(actual - expected) < 1000 else "high"
    elif bank and bank["utr"] == settlement["utr"] and bank["amount"] != settlement["amount"]:
        status, reason, severity = "exception", "BANK_UTR_AMOUNT_MISMATCH", "high"
        actual = bank["amount"]

    return {
        "status": status,
        "reason_code": reason,
        "settlement_id": settlement_id,
        "expected_amount": expected,
        "actual_amount": actual,
        "difference": actual - expected,
        "severity": severity,
    }


def calculate_expected_settlement(db: sqlite3.Connection, payment_id: str) -> dict[str, Any]:
    payment = row(db, "select * from payments where id = ?", (payment_id,))
    if not payment:
        return {"payment_id": payment_id, "expected_amount": None}
    payment_records = rows(db, "select * from reconciliation_records where payment_id = ? and entity_type = 'payment'", (payment_id,))
    refunds = rows(db, "select * from reconciliation_records where payment_id = ? and entity_type = 'refund'", (payment_id,))
    fee = sum(r["fee"] for r in payment_records)
    tax = sum(r["tax"] for r in payment_records)
    refund_amount = sum(r["amount"] for r in refunds)
    return {"payment_id": payment_id, "expected_amount": payment["amount"] - fee - tax - refund_amount, "fee": fee, "tax": tax, "refund_amount": refund_amount}


def investigate_exception(db: sqlite3.Connection, exception_id: str) -> dict[str, Any]:
    case = row(db, "select * from exceptions where id = ?", (exception_id,))
    if not case:
        raise ValueError("Exception not found")
    before = dict(case)
    payment_id = case["transaction_id"]
    tool_outputs = [
        tool_result("get_payment", get_payment(db, payment_id)),
        tool_result("get_reconciliation_record", get_reconciliation_record(db, payment_id)),
        tool_result("find_related_transactions", find_related_transactions(db, payment_id)),
        tool_result("find_refunds", find_refunds(db, payment_id)),
        tool_result("find_adjustments", find_adjustments(db, payment_id)),
        tool_result("calculate_expected_settlement", calculate_expected_settlement(db, payment_id)),
    ]
    recon_rows = tool_outputs[1]["result"] or tool_outputs[2]["result"] or []
    settlement_id = recon_rows[0]["settlement_id"] if recon_rows else None
    tool_outputs += [
        tool_result("get_settlement", get_settlement(db, settlement_id)),
        tool_result("compare_bank_entry", compare_bank_entry(db, settlement_id)),
    ]
    evidence = [t for t in tool_outputs if t["result"] not in (None, [], {})]
    llm_result = gemini_investigation(dict(case), evidence)
    if llm_result:
        summary = f"{llm_result['likely_root_cause']}: {llm_result['explanation']}"
        likely_root_cause = llm_result["likely_root_cause"]
        evidence_summary = llm_result["evidence_summary"]
        confidence = llm_result["confidence"]
        recommendation = llm_result["recommendation"]
        source = "Gemini AI"
        requires_human_review = llm_result["requires_human_review"]
    else:
        summary, confidence, recommendation = classify_case(dict(case), evidence)
        likely_root_cause = case["type"] if summary != "INSUFFICIENT_EVIDENCE" else "INSUFFICIENT_EVIDENCE"
        evidence_summary = summarize_evidence(evidence)
        source = "Deterministic fallback"
        requires_human_review = True
    status = "human_review" if confidence < 0.7 or summary == "INSUFFICIENT_EVIDENCE" else case["status"]
    if requires_human_review:
        status = "human_review"
    db.execute(
        """
        update exceptions
        set confidence = ?, ai_summary = ?, recommendation = ?, investigation_source = ?,
            likely_root_cause = ?, evidence_summary = ?, requires_human_review = ?,
            evidence = ?, tool_calls = ?, status = ?
        where id = ?
        """,
        (
            confidence,
            summary,
            recommendation,
            source,
            likely_root_cause,
            json.dumps(evidence_summary),
            int(requires_human_review),
            json.dumps(evidence),
            json.dumps([{"name": t["tool"], "allowed": t["tool"] in APPROVED_TOOLS} for t in tool_outputs]),
            status,
            exception_id,
        ),
    )
    after = dict(row(db, "select * from exceptions where id = ?", (exception_id,)))
    audit(db, "ai_investigator", "EXCEPTION_INVESTIGATED", exception_id, before, after)
    db.commit()
    return get_exception(db, exception_id)


def gemini_investigation(case: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        return None
    schema = {
        "type": "object",
        "properties": {
            "likely_root_cause": {"type": "string"},
            "explanation": {"type": "string"},
            "evidence_summary": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "recommendation": {"type": "string"},
            "requires_human_review": {"type": "boolean"},
        },
        "required": [
            "likely_root_cause",
            "explanation",
            "evidence_summary",
            "confidence",
            "recommendation",
            "requires_human_review",
        ],
    }
    prompt = {
        "instruction": (
            "Investigate this Razorpay reconciliation exception using only the supplied evidence. "
            "Do not invent transactions, API responses, bank entries, settlements, refunds, or amounts. "
            "Do not resolve, refund, pay out, or modify financial records. "
            "If evidence is insufficient, set likely_root_cause to INSUFFICIENT_EVIDENCE, confidence below 0.7, "
            "and requires_human_review to true."
        ),
        "exception": {
            "id": case["id"],
            "transaction_id": case["transaction_id"],
            "type": case["type"],
            "expected_amount": case["expected_amount"],
            "actual_amount": case["actual_amount"],
            "difference": case["difference"],
            "severity": case["severity"],
            "status": case["status"],
        },
        "verified_tool_evidence": evidence,
        "allowed_output_fields": list(schema["properties"].keys()),
    }
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "model": GEMINI_MODEL,
                "input": json.dumps(prompt),
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
                "generation_config": {"max_output_tokens": 1200},
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        text = extract_gemini_text(payload)
        parsed = json.loads(text)
        return validate_llm_result(parsed)
    except Exception:
        return None


def extract_gemini_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return payload["output_text"]
    if payload.get("interaction", {}).get("output_text"):
        return payload["interaction"]["output_text"]
    for step in payload.get("steps", []):
        if step.get("type") == "model_output":
            for part in step.get("content", []):
                if part.get("type") == "text" and part.get("text"):
                    return part["text"]
    return payload["candidates"][0]["content"]["parts"][0]["text"]


def validate_llm_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = {
        "likely_root_cause": str,
        "explanation": str,
        "evidence_summary": list,
        "confidence": (int, float),
        "recommendation": str,
        "requires_human_review": bool,
    }
    for key, expected_type in required.items():
        if key not in value or not isinstance(value[key], expected_type):
            return None
    if any(not value[key].strip() for key in ("likely_root_cause", "explanation", "recommendation")):
        return None
    if not all(isinstance(item, str) for item in value["evidence_summary"]):
        return None
    if isinstance(value["confidence"], bool) or not 0 <= float(value["confidence"]) <= 1:
        return None
    confidence = float(value["confidence"])
    return {
        "likely_root_cause": value["likely_root_cause"],
        "explanation": value["explanation"],
        "evidence_summary": value["evidence_summary"],
        "confidence": confidence,
        "recommendation": value["recommendation"],
        "requires_human_review": value["requires_human_review"],
    }


def classify_case(case: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[str, float, str]:
    if not any(e["tool"] == "get_payment" for e in evidence):
        return "INSUFFICIENT_EVIDENCE", 0.2, "Route to human review; source payment record is missing."
    explanations = {
        "WRONG_MAPPING": ("A reconciliation row references this original payment as its entity, but points to a different payment_id. This is an explicit wrong mapping exception.", 0.91, "Escalate with the original payment ID and incorrectly referenced payment ID before closing."),
        "DUPLICATE_RECONCILIATION_RECORD": ("Duplicate reconciliation records exist for this payment. The case needs a human decision before one row is accepted as canonical.", 0.92, "Review source exports and close only after approving the canonical record."),
        "MISSING_RECONCILIATION_RECORD": ("The payment exists but no settlement reconciliation row is available for it.", 0.84, "Retry Razorpay recon sync for the expected settlement window."),
        "PAYMENT_ID_MISMATCH": ("The reconciliation row maps to a different payment identifier than the captured payment.", 0.88, "Escalate as wrong mapping with payment and recon evidence."),
        "MISSING_SETTLEMENT": ("A captured payment has no linked settlement record in the available data.", 0.86, "Check settlement availability in Test Mode and retry sync."),
        "TIMING_WINDOW_EXCEEDED": ("The settlement relationship exists, but the record falls outside the configured timing window.", 0.78, "Verify settlement cycle before resolution."),
        "REFUND_AMOUNT_MISMATCH": ("Refund rows are linked, but net settlement does not reconcile to the deterministic expected amount.", 0.84, "Review refund timing and settlement rows."),
        "SETTLEMENT_AMOUNT_DISCREPANCY": ("Actual settled amount differs from payment less recorded fees, tax, and refunds.", 0.82, "Review fee/tax/adjustment records before resolving."),
        "BANK_UTR_AMOUNT_MISMATCH": ("The bank UTR matches the settlement, but the bank amount differs.", 0.9, "Route to finance review with UTR evidence."),
    }
    return explanations.get(case["type"], ("INSUFFICIENT_EVIDENCE", 0.55, "Escalate because the available evidence does not support a confident cause."))


def summarize_evidence(evidence: list[dict[str, Any]]) -> list[str]:
    summaries = []
    for item in evidence:
        result = item["result"]
        count = len(result) if isinstance(result, list) else 1
        summaries.append(f"{item['tool']} returned {count} verified record{'s' if count != 1 else ''}.")
    return summaries


def deterministic_flag_reason(exception_type: str) -> str:
    reasons = {
        "WRONG_MAPPING": "The reconciliation entity_id is the original payment ID, while payment_id points to a different payment.",
        "MISSING_RECONCILIATION_RECORD": "A captured payment has no payment reconciliation record.",
        "AMOUNT_MISMATCH": "The reconciliation record amount differs from the captured payment amount.",
        "DUPLICATE_RECONCILIATION_RECORD": "More than one payment reconciliation record exists for the payment.",
        "MISSING_SETTLEMENT": "The reconciliation record has no available linked settlement.",
        "TIMING_WINDOW_EXCEEDED": "The reconciliation timestamp falls outside the deterministic seven-day window.",
        "REFUND_AMOUNT_MISMATCH": "The net settlement differs after applying verified refund records.",
        "SETTLEMENT_AMOUNT_DISCREPANCY": "The settlement amount differs from payment less verified fees, tax, and refunds.",
        "BANK_UTR_AMOUNT_MISMATCH": "The bank UTR matches, but the bank and settlement amounts differ.",
        "PAYMENT_ID_MISMATCH": "The payment reconciliation entity does not match the captured payment ID.",
    }
    return reasons.get(exception_type, "The deterministic reconciliation checks did not satisfy a known match condition.")


def human_decision(db: sqlite3.Connection, exception_id: str, decision: str) -> None:
    if decision not in {"resolve", "escalate"}:
        raise ValueError("Human decision must be resolve or escalate")
    case = row(db, "select * from exceptions where id = ?", (exception_id,))
    if not case:
        raise ValueError("Exception not found")
    before = dict(case)
    status = "resolved" if decision == "resolve" else "escalated"
    resolved_at = now_ts() if decision == "resolve" else None
    db.execute("update exceptions set status = ?, resolved_at = ? where id = ?", (status, resolved_at, exception_id))
    after = dict(row(db, "select * from exceptions where id = ?", (exception_id,)))
    audit(db, "human_reviewer", f"EXCEPTION_{decision.upper()}", exception_id, before, after)
    db.commit()


def evaluation(db: sqlite3.Connection, dataset_split: str | None = None) -> dict[str, Any]:
    if dataset_split not in {None, "development", "held_out"}:
        raise ValueError("dataset_split must be development, held_out, or None")
    truth_query = "select * from benchmark_ground_truth"
    params: tuple[Any, ...] = ()
    if dataset_split:
        truth_query += " where dataset_split = ?"
        params = (dataset_split,)
    truth = rows(db, truth_query, params)
    truth_by_id = {item["payment_id"]: item for item in truth}
    has_ground_truth = bool(truth)
    payment_ids = set(truth_by_id)
    if not has_ground_truth and dataset_split is None:
        payment_ids = {item["id"] for item in rows(db, "select id from payments")}
    results = [r for r in rows(db, "select * from reconciliation_results") if r["payment_id"] in payment_ids]
    exceptions = [e for e in rows(db, "select * from exceptions") if e["transaction_id"] in payment_ids]
    result_by_id = {r["payment_id"]: r for r in results}
    exception_by_id = {e["transaction_id"]: e for e in exceptions}
    positives = {payment_id for payment_id, item in truth_by_id.items() if item["expected_status"] == "exception"}
    predicted = set(exception_by_id)
    tp = len(positives & predicted)
    fp = len(predicted - positives)
    fn = len(positives - predicted)
    tn = len(payment_ids - positives - predicted)
    correctly_classified = sum(
        1
        for payment_id, expected in truth_by_id.items()
        if payment_id in result_by_id
        and result_by_id[payment_id]["status"] == expected["expected_status"]
        and (
            expected["expected_status"] == "matched"
            or result_by_id[payment_id]["reason_code"] == expected["expected_exception_type"]
        )
    )
    unresolved_statuses = {"open", "human_review", "escalated"}
    unresolved = [e for e in exceptions if e["status"] in unresolved_statuses]
    breakdown = []
    for exception_type in sorted({e["type"] for e in exceptions}):
        typed = [e for e in exceptions if e["type"] == exception_type]
        typed_unresolved = [e for e in typed if e["status"] in unresolved_statuses]
        breakdown.append(
            {
                "exception_type": exception_type,
                "count": len(typed),
                "percentage": pct(len(typed), len(exceptions)),
                "unresolved_value": sum(abs(e["difference"]) for e in typed_unresolved),
            }
        )
    latest_run = row(db, "select * from reconciliation_runs order by rowid desc limit 1")
    matched = len([r for r in results if r["status"] == "matched"])
    seed = truth[0]["seed"] if truth else None
    return {
        "dataset": "synthetic benchmark" if truth else "unlabelled data",
        "dataset_split": dataset_split or "all",
        "seed": seed,
        "records_processed": len(payment_ids),
        "matched": matched,
        "exceptions": len(exceptions),
        "match_rate": pct(matched, len(results)),
        "exception_precision": pct(tp, tp + fp) if has_ground_truth else None,
        "exception_recall": pct(tp, tp + fn) if has_ground_truth else None,
        "reconciliation_accuracy": pct(correctly_classified, len(truth)) if has_ground_truth else None,
        "false_positives": fp if has_ground_truth else None,
        "false_negatives": fn if has_ground_truth else None,
        "throughput_per_second": latest_run["throughput_per_second"] if latest_run else 0,
        "unresolved_exceptions": len(unresolved),
        "unresolved_value": sum(abs(e["difference"]) for e in unresolved),
        "exception_breakdown": breakdown,
    }


def dashboard(db: sqlite3.Connection) -> dict[str, Any]:
    metrics = evaluation(db)
    metrics["recent_investigations"] = [dict(r) for r in rows(db, "select id, transaction_id, type, status, confidence from exceptions order by created_at desc limit 5")]
    return metrics


def razorpay_time_window(year: int, month: int, day: int | None = None) -> tuple[int, int]:
    if day:
        start = datetime(year, month, day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    else:
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp()) - 1


def razorpay_sync(db: sqlite3.Connection, year: int, month: int, day: int | None = None) -> dict[str, Any]:
    client = RazorpayClient()
    from_ts, to_ts = razorpay_time_window(year, month, day)
    settlements = client.fetch_settlements(from_ts=from_ts, to_ts=to_ts)
    recon_payload = client.fetch_recon(year, month, day)
    payments = client.fetch_payments(from_ts=from_ts, to_ts=to_ts)
    return normalize_razorpay_payload(db, payments, settlements, recon_payload)


def normalize_razorpay_payload(db: sqlite3.Connection, payments: dict[str, Any], settlements: dict[str, Any], recon_payload: dict[str, Any]) -> dict[str, int]:
    for item in payments.get("items", []):
        db.execute(
            "insert or replace into payments values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item.get("order_id") or "", int(item.get("amount") or 0), item.get("currency") or "INR", item.get("status") or "unknown", item.get("method") or "unknown", int(item.get("captured_at") or item.get("created_at") or now_ts()), int(item.get("created_at") or now_ts()), "razorpay_test"),
        )
    for item in settlements.get("items", []):
        db.execute(
            "insert or replace into settlements values (?, ?, ?, ?, ?, ?, ?)",
            (item["id"], int(item.get("amount") or 0), item.get("status") or "unknown", int(item.get("fees") or 0), int(item.get("tax") or 0), item.get("utr") or "", int(item.get("created_at") or now_ts())),
        )
    for item in recon_payload.get("items", []):
        entity_type = item.get("type") or "payment"
        entity_id = item.get("entity_id") or f"unknown_{random.randint(1000,9999)}"
        payment_id = item.get("payment_id") or (entity_id if entity_type == "payment" else None)
        db.execute(
            "insert or replace into reconciliation_records values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"rzp_{entity_id}_{item.get('settlement_id') or 'unsettled'}",
                entity_id,
                entity_type,
                item.get("settlement_id"),
                payment_id,
                entity_id if entity_type == "refund" else None,
                entity_id if entity_type == "transfer" else None,
                entity_id if entity_type == "adjustment" else None,
                int(item.get("amount") or item.get("credit") or item.get("debit") or 0),
                int(item.get("fee") or 0),
                int(item.get("tax") or 0),
                "debit" if int(item.get("debit") or 0) else "credit",
                int(item.get("created_at") or now_ts()),
            ),
        )
    audit(db, "razorpay_connector", "RAZORPAY_SYNC", "razorpay", None, {"payments": len(payments.get("items", [])), "settlements": len(settlements.get("items", [])), "recon": len(recon_payload.get("items", []))})
    db.commit()
    return {"payments": len(payments.get("items", [])), "settlements": len(settlements.get("items", [])), "reconciliation_records": len(recon_payload.get("items", []))}


class RazorpayClient:
    base_url = "https://api.razorpay.com/v1"

    def __init__(self) -> None:
        self.key_id = os.getenv("RAZORPAY_KEY_ID")
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET")
        if not self.key_id or not self.key_secret:
            raise RuntimeError("Razorpay Test Mode credentials are not configured")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(f"{self.base_url}{path}", params=params, auth=(self.key_id, self.key_secret), timeout=20)
        response.raise_for_status()
        return response.json()

    def fetch_all(self, path: str, params: dict[str, Any] | None = None, count: int = 100) -> dict[str, Any]:
        base_params = dict(params or {})
        collected: list[dict[str, Any]] = []
        skip = int(base_params.pop("skip", 0) or 0)
        max_page_size = 1000 if path == "/settlements/recon/combined" else 100
        page_size = min(int(base_params.pop("count", count) or count), max_page_size)
        while True:
            page_params = {**base_params, "count": page_size, "skip": skip}
            page = self.get(path, page_params)
            items = page.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError(f"Unexpected Razorpay response for {path}: missing items list")
            collected.extend(items)
            if len(items) < page_size:
                break
            skip += page_size
        return {"entity": "collection", "count": len(collected), "items": collected}

    def fetch_payments(self, count: int = 100, skip: int = 0, from_ts: int | None = None, to_ts: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"count": count, "skip": skip}
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        return self.fetch_all("/payments", params, count=count)

    def fetch_settlements(self, count: int = 100, skip: int = 0, from_ts: int | None = None, to_ts: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"count": count, "skip": skip}
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        return self.fetch_all("/settlements/", params, count=count)

    def fetch_recon(self, year: int, month: int, day: int | None = None, count: int = 1000, skip: int = 0) -> dict[str, Any]:
        params = {"year": year, "month": month, "count": count, "skip": skip}
        if day:
            params["day"] = day
        return self.fetch_all("/settlements/recon/combined", params, count=count)


def get_payment(db: sqlite3.Connection, payment_id: str) -> dict[str, Any] | None:
    return maybe_dict(row(db, "select * from payments where id = ?", (payment_id,)))


def get_settlement(db: sqlite3.Connection, settlement_id: str | None) -> dict[str, Any] | None:
    return maybe_dict(row(db, "select * from settlements where id = ?", (settlement_id,))) if settlement_id else None


def get_reconciliation_record(db: sqlite3.Connection, payment_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in rows(db, "select * from reconciliation_records where payment_id = ?", (payment_id,))]


def find_related_transactions(db: sqlite3.Connection, payment_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in rows(db, "select * from reconciliation_records where payment_id = ? or entity_id = ?", (payment_id, payment_id))]


def find_refunds(db: sqlite3.Connection, payment_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in rows(db, "select * from reconciliation_records where payment_id = ? and entity_type = 'refund'", (payment_id,))]


def find_adjustments(db: sqlite3.Connection, payment_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in rows(db, "select * from reconciliation_records where payment_id = ? and entity_type = 'adjustment'", (payment_id,))]


def compare_bank_entry(db: sqlite3.Connection, settlement_id: str | None) -> dict[str, Any] | None:
    return maybe_dict(row(db, "select * from bank_entries where reference = ?", (settlement_id,))) if settlement_id else None


def tool_result(name: str, result: Any) -> dict[str, Any]:
    if name not in APPROVED_TOOLS:
        raise ValueError(f"Tool is not approved: {name}")
    return {"tool": name, "result": result}


def table(db: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"select * from {name}", db)


def get_exception(db: sqlite3.Connection, exception_id: str) -> dict[str, Any]:
    case = row(db, "select * from exceptions where id = ?", (exception_id,))
    if not case:
        raise ValueError("Exception not found")
    data = dict(case)
    data["evidence"] = json.loads(data["evidence"])
    data["evidence_summary"] = json.loads(data["evidence_summary"])
    data["requires_human_review"] = bool(data["requires_human_review"])
    data["tool_calls"] = json.loads(data["tool_calls"])
    data["audit_history"] = [dict(r) for r in rows(db, "select * from audit_events where entity_id = ? order by timestamp desc", (exception_id,))]
    return data


def rows(db: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(db.execute(query, params).fetchall())


def row(db: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return db.execute(query, params).fetchone()


def scalar(db: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    return db.execute(query, params).fetchone()[0]


def maybe_dict(value: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(value) if value else None


def pct(numerator: int, denominator: int) -> float:
    return round((numerator / denominator * 100), 2) if denominator else 0.0
