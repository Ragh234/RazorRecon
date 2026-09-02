import json
import os
import sqlite3
from datetime import date, datetime, timezone

import streamlit as st


def apply_streamlit_secrets() -> None:
    for key in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "LLM_API_KEY", "DATABASE_URL"):
        try:
            value = st.secrets.get(key)
        except Exception:
            value = None
        if value and not os.environ.get(key):
            os.environ[key] = str(value)


apply_streamlit_secrets()

from core import (
    BENCHMARK_RECORDS,
    DB_PATH,
    EXPOSURE_LABELS,
    QUEUE_COLUMNS,
    connect,
    dashboard,
    deterministic_flag_reason,
    evaluation,
    exposure_class,
    get_exception,
    human_decision,
    init_db,
    investigate_exception,
    load_benchmark,
    razorpay_sync,
    reconcile,
    table,
)


st.set_page_config(page_title="RazorRecon", layout="wide")


@st.cache_resource
def db():
    connection = connect(DB_PATH)
    init_db(connection)
    return connection


conn = db()


def load_benchmark_and_reconcile():
    loaded = load_benchmark(conn)
    reconciled = reconcile(conn)
    return {**loaded, **reconciled}


def count_rows(name: str) -> int:
    return conn.execute(f"select count(*) from {name}").fetchone()[0]


def bootstrap_state() -> str:
    """Decide what the cold start has to do, tolerating a half-finished previous run.

    Reloading the page during the cold-start load kills that script run mid-write. The
    connection is cached across runs, so the next run inherits the abandoned
    transaction and can *see its own partial rows*. Checking only for "is the table
    non-empty" then accepts that partial state as a finished run, which is how a
    reload during startup produced 1,871 results, an empty exception queue and a
    meaningless 40.7% accuracy. Compare counts against each other instead.
    """
    payments = count_rows("payments")
    if payments == 0:
        return "load"
    synthetic = count_rows("payments where label != 'razorpay_test'")
    if synthetic == payments and payments != BENCHMARK_RECORDS:
        return "load"
    if count_rows("reconciliation_results") != payments:
        return "reconcile"
    return "ready"


def format_percent(value):
    return f"{value}%" if value is not None else "N/A"


def inr(paise) -> str:
    return f"INR {paise / 100:,.2f}"


# Cheap row counts decide the cold-start path. Calling dashboard() here as well would
# run a full evaluation pass on every rerun purely to learn whether the table is empty.
try:
    state = bootstrap_state()
    if state != "ready":
        # Drop anything an interrupted run left uncommitted so the rebuild starts clean.
        conn.rollback()
        first_run = load_benchmark_and_reconcile() if state == "load" else reconcile(conn)
        st.session_state["last_action"] = first_run
        st.session_state["last_throughput"] = first_run["throughput_per_second"]
except sqlite3.OperationalError:
    st.warning(
        "RazorRecon is preparing the demo dataset and the database is briefly busy "
        "(this happens on a cold start when more than one session connects at once). "
        "This is not a data problem \u2014 wait a few seconds and retry."
    )
    if st.button("Retry now", use_container_width=True):
        st.rerun()
    st.stop()

st.title("RazorRecon")
st.caption("Deterministic financial truth with constrained AI investigation.")

left, right = st.columns([2, 1])
with right:
    if st.button("Load benchmark", use_container_width=True):
        st.session_state["last_action"] = load_benchmark_and_reconcile()
        st.session_state["last_throughput"] = st.session_state["last_action"]["throughput_per_second"]
    if st.button("Run reconciliation", use_container_width=True):
        st.session_state["last_action"] = reconcile(conn)
        st.session_state["last_throughput"] = st.session_state["last_action"]["throughput_per_second"]

    with st.expander("Razorpay Test Mode sync"):
        configured = bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))
        if not configured:
            st.info(
                "Add Razorpay Test Mode keys to enable sync - `.env` when running locally, "
                "Streamlit Secrets when deployed. The synthetic benchmark works without them."
            )
        # Default to the current period rather than a hardcoded date that goes stale.
        today = date.today()
        year = st.number_input("Year", min_value=2000, max_value=2099, value=today.year)
        month = st.number_input("Month", min_value=1, max_value=12, value=today.month)
        day = st.number_input("Day (optional)", min_value=0, max_value=31, value=0)
        if st.button("Sync Razorpay", use_container_width=True, disabled=not configured):
            try:
                st.session_state["last_action"] = razorpay_sync(conn, int(year), int(month), int(day) or None)
                if not any(st.session_state["last_action"].values()):
                    st.info("Razorpay Test Mode sync completed, but no records were returned for the selected period.")
            except Exception as exc:
                st.error(str(exc))

    if "last_action" in st.session_state:
        st.json(st.session_state["last_action"])

metrics = dashboard(conn)
with left:
    # Say what the numbers actually describe. Once Razorpay data is synced the
    # accuracy columns lose their ground truth, and the banner has to admit that.
    if metrics["dataset"] == "synthetic benchmark":
        st.info(
            f"Synthetic benchmark, reproducible from fixed seed {metrics['seed']}. "
            "These are not production-accuracy claims."
        )
    else:
        st.warning(
            "Showing unlabelled operational data. Reconciliation still runs in full, but "
            "accuracy, precision and recall need ground truth and are reported as N/A."
        )
    cards = st.columns(6)
    cards[0].metric("Records", metrics["records_processed"])
    cards[1].metric("Matched", metrics["matched"])
    cards[2].metric("Exceptions", metrics["exceptions"])
    cards[3].metric("Match rate", f"{metrics['match_rate']}%")
    cards[4].metric("Unresolved", metrics["unresolved_exceptions"])
    at_risk_rupees = metrics["unresolved_amount_at_risk"] / 100
    cards[5].metric("Amount at risk", f"INR {at_risk_rupees / 100000:.2f}L")
    cards[5].caption(f"INR {at_risk_rupees:,.2f}")

    by_class = metrics["unresolved_value_by_class"]
    count_by_class = metrics["unresolved_count_by_class"]
    st.caption(
        "Unresolved exposure by class — "
        + "  |  ".join(
            f"**{label}**: {inr(by_class[name])} ({count_by_class[name]:,})"
            for name, label in EXPOSURE_LABELS.items()
        )
    )
    st.caption(
        "Only *amount at risk* is money the ledger actually disagrees about. "
        "*Awaiting settlement* rows have no settlement yet, so their difference is the whole "
        "payment — pipeline lag, not loss. *Structural* exceptions reconcile to the rupee but "
        "are linked wrongly, so their difference is legitimately zero."
    )

    result_cards = st.columns(4)
    result_cards[0].metric("Accuracy", format_percent(metrics["reconciliation_accuracy"]))
    result_cards[1].metric("Precision", format_percent(metrics["exception_precision"]))
    result_cards[2].metric("Recall", format_percent(metrics["exception_recall"]))
    throughput = metrics["throughput_per_second"] or st.session_state.get("last_throughput")
    result_cards[3].metric("Throughput", f"{throughput:,.2f}/s" if throughput else "Run reconciliation")
    if metrics["reconciliation_accuracy"] is not None:
        st.caption(
            "These scores measure the deterministic engine against the labelled synthetic benchmark, "
            "where every scenario is drawn from the failure modes the rules cover. They demonstrate "
            "rule coverage and the absence of regressions — not accuracy on production Razorpay data, "
            "which contains failure modes this benchmark does not generate."
        )
    else:
        st.caption(
            "Accuracy, precision and recall require labelled ground truth, which unlabelled "
            "operational data does not carry. Load the benchmark to score the engine."
        )

# Read once per rerun, without the evidence/tool_calls blobs. This drives both the
# queue tab and the investigation picker.
exceptions_df = table(conn, "exceptions", columns=QUEUE_COLUMNS, order_by="id")

# The benchmark holds 5,000 reconciliation rows and 1,500 exceptions. Serialising all
# of them to the browser on every rerun is slow on a free-tier host, so the wide tables
# are paged and the full set stays available through the CLI benchmark.
TABLE_PREVIEW_ROWS = 500

tabs = st.tabs(["Reconciliation", "Exceptions", "Investigation", "Evaluation", "Audit"])

with tabs[0]:
    st.subheader("Reconciliation records")
    total_results = count_rows("reconciliation_results")
    recon_view = table(conn, "reconciliation_results", limit=TABLE_PREVIEW_ROWS, order_by="payment_id")
    for column in ("expected_amount", "actual_amount", "difference"):
        recon_view[column] = recon_view[column].map(inr)
    st.caption(f"Showing the first {len(recon_view):,} of {total_results:,} reconciliation results.")
    st.dataframe(recon_view, use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Exception queue")
    st.markdown("#### Exception taxonomy")
    taxonomy = metrics["exception_breakdown"]
    taxonomy_display = [
        {
            "Exception Type": item["exception_type"],
            "Exposure": EXPOSURE_LABELS[item["exposure_class"]],
            "Count": item["count"],
            "Percentage": f"{item['percentage']:.2f}%",
            "Unresolved Value": inr(item["unresolved_value"]),
        }
        for item in taxonomy
    ]
    st.dataframe(taxonomy_display, use_container_width=True, hide_index=True)
    st.markdown("#### Inspect exceptions")
    if exceptions_df.empty:
        st.info("No exceptions in the queue yet. Run reconciliation to populate it.")
    else:
        queue_view = exceptions_df.head(TABLE_PREVIEW_ROWS).copy()
        queue_view["exposure"] = queue_view["type"].map(exposure_class).map(EXPOSURE_LABELS)
        for column in ("expected_amount", "actual_amount", "difference"):
            queue_view[column] = queue_view[column].map(inr)
        st.caption(f"Showing the first {len(queue_view):,} of {len(exceptions_df):,} exceptions.")
        st.dataframe(
            queue_view[["id", "transaction_id", "type", "exposure", "expected_amount", "actual_amount", "difference", "severity", "confidence", "status"]],
            use_container_width=True,
            hide_index=True,
        )

with tabs[2]:
    # The queue holds ~1,500 rows, so filter before picking rather than scrolling a flat list.
    selected = None
    if exceptions_df.empty:
        st.info("No exceptions in the queue yet. Run reconciliation to populate it.")
    else:
        picker = st.columns([2, 2, 3])
        types = sorted(exceptions_df["type"].unique().tolist())
        chosen_type = picker[0].selectbox("Exception type", ["All"] + types)
        statuses = sorted(exceptions_df["status"].unique().tolist())
        chosen_status = picker[1].selectbox("Status", ["All"] + statuses)
        filtered = exceptions_df
        if chosen_type != "All":
            filtered = filtered[filtered["type"] == chosen_type]
        if chosen_status != "All":
            filtered = filtered[filtered["status"] == chosen_status]
        exception_ids = filtered["id"].tolist()
        if exception_ids:
            selected = picker[2].selectbox(f"Exception ID ({len(exception_ids):,} matching)", exception_ids)
        else:
            picker[2].info("No exceptions match this filter.")
    if selected:
        case = get_exception(conn, selected)
        st.markdown("### FINANCIAL TRUTH — deterministic engine")
        st.caption("Verified reconciliation output. Gemini cannot alter these records or amounts.")
        c1, c2, c3 = st.columns(3)
        c1.metric("Expected", inr(case["expected_amount"]))
        c2.metric("Actual", inr(case["actual_amount"]))
        c3.metric("Difference", inr(case["difference"]))

        case_exposure = exposure_class(case["type"])
        exposure_notes = {
            "amount_at_risk": "The ledger disagrees about money that has already moved. The difference is real exposure.",
            "awaiting_settlement": "No settlement record exists yet, so the difference is the full payment value. This is pipeline lag, not a loss.",
            "structural": "The amounts reconcile exactly, so the difference is zero by design. The linkage between records is what is broken.",
        }
        st.caption(f"**{EXPOSURE_LABELS[case_exposure]}** — {exposure_notes[case_exposure]}")

        st.markdown(f"**Status:** `{case['status']}`  **Type:** `{case['type']}`  **Severity:** `{case['severity']}`")
        st.markdown(f"**Why flagged:** {deterministic_flag_reason(case['type'])}")
        st.graphviz_chart(f'digraph {{ rankdir=LR; payment [label="{case["transaction_id"]}"]; exception [label="{case["type"]}"]; review [label="{case["status"]}"]; payment -> exception -> review; }}')

        st.markdown("#### VERIFIED EVIDENCE")
        if case["evidence"]:
            st.caption(f"{len(case['evidence'])} read-only tool results captured at investigation time.")
            st.code(json.dumps(case["evidence"], indent=2), language="json")
        else:
            st.caption(
                "No evidence captured yet. Evidence is gathered by the approved read-only tools "
                "when the investigation runs, and is stored with the case so it can be re-read later."
            )

        st.markdown("### AI INVESTIGATION — explanation only")
        st.caption("Gemini is downstream of deterministic reconciliation and has read-only evidence access.")
        # Rerun rather than reassigning `case`: the evidence panel is rendered above
        # this button, so without a rerun it would still show the pre-investigation
        # state while the AI section below showed the new result.
        if st.button("Investigate exception", use_container_width=True):
            with st.spinner("Running read-only investigation tools and asking Gemini..."):
                investigate_exception(conn, selected)
            st.rerun()
        # The stored default is "Deterministic fallback"; showing it before anything has
        # run would claim a fallback that never happened.
        has_investigated = bool(case["ai_summary"])
        source_label = case.get("investigation_source", "Deterministic fallback") if has_investigated else "Not yet investigated"
        st.markdown(f"**Investigation source:** `{source_label}`")
        st.markdown(f"**Likely root cause:** {case['likely_root_cause'] or 'Awaiting investigation.'}")
        st.write(case["ai_summary"] or "No investigation has run yet.")
        st.markdown("**Evidence summary**")
        if case["evidence_summary"]:
            for summary in case["evidence_summary"]:
                st.write(f"- {summary}")
        else:
            st.write("Awaiting investigation.")
        st.progress(min(max(float(case["confidence"]), 0.0), 1.0), text=f"Confidence: {case['confidence']:.2f}")
        st.markdown(f"**Recommendation:** {case['recommendation'] or 'Awaiting investigation.'}")

        st.markdown("### HUMAN REVIEW — financial decision")
        review_text = "Required before any unresolved financial issue is closed." if case["requires_human_review"] else "Gemini did not request review; RazorRecon still leaves resolution to a human."
        st.warning(review_text)
        a, b = st.columns(2)
        # Rerun so the status, headline metrics and audit trail all reflect the
        # decision immediately instead of lagging one interaction behind.
        if a.button("Resolve as human reviewer", use_container_width=True):
            human_decision(conn, selected, "resolve")
            st.rerun()
        if b.button("Escalate as human reviewer", use_container_width=True):
            human_decision(conn, selected, "escalate")
            st.rerun()

        with st.expander("Investigation tool calls and audit history"):
            st.dataframe(case["tool_calls"], use_container_width=True)
            st.dataframe(case["audit_history"], use_container_width=True)

with tabs[3]:
    st.subheader("Synthetic held-out benchmark")
    st.caption("The held-out split is generated reproducibly but its labels are never supplied to the reconciliation engine.")
    held_out = evaluation(conn, "held_out")
    # A plain st.stop() here would abort the whole script and blank the Audit tab,
    # because Streamlit renders every tab in a single pass.
    if held_out["records_processed"] == 0:
        st.info("No held-out split is loaded. Click **Load benchmark** to generate one.")
    else:
        held_cards = st.columns(5)
        held_cards[0].metric("Held-out records", held_out["records_processed"])
        held_cards[1].metric("Accuracy", format_percent(held_out["reconciliation_accuracy"]))
        held_cards[2].metric("Precision", format_percent(held_out["exception_precision"]))
        held_cards[3].metric("Recall", format_percent(held_out["exception_recall"]))
        error_counts = f"{held_out['false_positives']} / {held_out['false_negatives']}" if held_out["false_positives"] is not None else "N/A"
        held_cards[4].metric("False + / False -", error_counts)
        held_breakdown = [
            {
                "Exception Type": item["exception_type"],
                "Exposure": EXPOSURE_LABELS[item["exposure_class"]],
                "Count": item["count"],
                "Percentage": f"{item['percentage']:.2f}%",
                "Unresolved Value": inr(item["unresolved_value"]),
            }
            for item in held_out["exception_breakdown"]
        ]
        st.dataframe(held_breakdown, use_container_width=True, hide_index=True)
        st.caption(
            f"Throughput is measured over the {held_out['throughput_scope']} "
            f"({metrics['throughput_per_second']:,.2f} records/second); the held-out split is scored "
            "afterwards from stored results, so it has no separate timing of its own."
        )
        with st.expander("Full held-out metrics"):
            st.json(held_out)

with tabs[4]:
    st.subheader("Audit events")
    st.caption(
        "Every benchmark load, reconciliation run, investigation, Gemini fallback and human "
        "decision is appended here. Rows are never edited or deleted."
    )
    total_events = count_rows("audit_events")
    audit_view = table(conn, "audit_events", limit=TABLE_PREVIEW_ROWS, order_by="timestamp desc")
    if audit_view.empty:
        st.info("No audit events recorded yet.")
    else:
        audit_view["timestamp"] = audit_view["timestamp"].map(
            lambda value: datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        )
        # The before/after payloads are full row snapshots. Summarise them here and keep
        # the complete JSON in each case's own audit history expander.
        audit_view["change"] = audit_view["after_state"].map(
            lambda value: (value[:120] + "...") if isinstance(value, str) and len(value) > 120 else (value or "")
        )
        st.caption(f"Showing the {len(audit_view):,} most recent of {total_events:,} audit events.")
        st.dataframe(
            audit_view[["timestamp", "actor", "action", "entity_id", "change"]],
            use_container_width=True,
            hide_index=True,
        )
