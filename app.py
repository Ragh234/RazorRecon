import json
import os
import sqlite3

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
    DB_PATH,
    connect,
    dashboard,
    deterministic_flag_reason,
    evaluation,
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


def format_percent(value):
    return f"{value}%" if value is not None else "N/A (no ground truth)"


try:
    initial_metrics = dashboard(conn)
    if initial_metrics["records_processed"] == 0 and count_rows("payments") == 0:
        first_run = load_benchmark_and_reconcile()
        st.session_state.setdefault("last_action", first_run)
        st.session_state.setdefault("last_throughput", first_run["throughput_per_second"])
    elif count_rows("payments") > 0 and count_rows("reconciliation_results") == 0:
        first_run = reconcile(conn)
        st.session_state.setdefault("last_action", first_run)
        st.session_state.setdefault("last_throughput", first_run["throughput_per_second"])
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
st.info("All metrics shown for the included benchmark are synthetic and reproducible (fixed seed 42); they are not production-accuracy claims.")

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
            st.info("Add Razorpay Test Mode keys to .env to enable sync. The synthetic benchmark works without them.")
        year = st.number_input("Year", min_value=2000, max_value=2099, value=2026)
        month = st.number_input("Month", min_value=1, max_value=12, value=8)
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
    cards = st.columns(6)
    cards[0].metric("Records", metrics["records_processed"])
    cards[1].metric("Matched", metrics["matched"])
    cards[2].metric("Exceptions", metrics["exceptions"])
    cards[3].metric("Match rate", f"{metrics['match_rate']}%")
    cards[4].metric("Unresolved", metrics["unresolved_exceptions"])
    unresolved_rupees = metrics["unresolved_value"] / 100
    cards[5].metric("Unresolved value", f"INR {unresolved_rupees / 100000:.2f}L")
    cards[5].caption(f"INR {unresolved_rupees:,.2f}")
    result_cards = st.columns(4)
    result_cards[0].metric("Accuracy", format_percent(metrics["reconciliation_accuracy"]))
    result_cards[1].metric("Precision", format_percent(metrics["exception_precision"]))
    result_cards[2].metric("Recall", format_percent(metrics["exception_recall"]))
    throughput = metrics["throughput_per_second"] or st.session_state.get("last_throughput")
    result_cards[3].metric("Throughput", f"{throughput:,.2f}/s" if throughput else "Run reconciliation")

tabs = st.tabs(["Reconciliation", "Exceptions", "Investigation", "Evaluation", "Audit"])

with tabs[0]:
    st.subheader("Reconciliation records")
    st.dataframe(table(conn, "reconciliation_results"), use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Exception queue")
    exceptions = table(conn, "exceptions")
    st.markdown("#### Exception taxonomy")
    taxonomy = metrics["exception_breakdown"]
    taxonomy_display = [
        {
            "Exception Type": item["exception_type"],
            "Count": item["count"],
            "Percentage": f"{item['percentage']:.2f}%",
            "Unresolved Value": f"INR {item['unresolved_value'] / 100:,.2f}",
        }
        for item in taxonomy
    ]
    st.dataframe(taxonomy_display, use_container_width=True, hide_index=True)
    st.markdown("#### Inspect exceptions")
    st.dataframe(
        exceptions[["id", "transaction_id", "actual_amount", "difference", "type", "severity", "confidence", "status"]] if not exceptions.empty else exceptions,
        use_container_width=True,
        hide_index=True,
    )

with tabs[2]:
    exception_ids = table(conn, "exceptions")["id"].tolist() if not table(conn, "exceptions").empty else []
    selected = st.selectbox("Exception ID", exception_ids)
    if selected:
        case = get_exception(conn, selected)
        st.markdown("### FINANCIAL TRUTH — deterministic engine")
        st.caption("Verified reconciliation output. Gemini cannot alter these records or amounts.")
        c1, c2, c3 = st.columns(3)
        c1.metric("Expected", f"INR {case['expected_amount'] / 100:,.2f}")
        c2.metric("Actual", f"INR {case['actual_amount'] / 100:,.2f}")
        c3.metric("Difference", f"INR {case['difference'] / 100:,.2f}")

        st.markdown(f"**Status:** `{case['status']}`  **Type:** `{case['type']}`  **Severity:** `{case['severity']}`")
        st.markdown(f"**Why flagged:** {deterministic_flag_reason(case['type'])}")
        st.graphviz_chart(f'digraph {{ rankdir=LR; payment [label="{case["transaction_id"]}"]; exception [label="{case["type"]}"]; review [label="{case["status"]}"]; payment -> exception -> review; }}')

        st.markdown("#### VERIFIED EVIDENCE")
        st.code(json.dumps(case["evidence"], indent=2), language="json")

        st.markdown("### AI INVESTIGATION — explanation only")
        st.caption("Gemini is downstream of deterministic reconciliation and has read-only evidence access.")
        if st.button("Investigate exception", use_container_width=True):
            case = investigate_exception(conn, selected)
        st.markdown(f"**Investigation source:** `{case.get('investigation_source', 'Deterministic fallback')}`")
        st.markdown(f"**Likely root cause:** {case['likely_root_cause'] or 'Awaiting investigation.'}")
        st.write(case["ai_summary"] or "No investigation has run yet.")
        st.markdown("**Evidence summary**")
        if case["evidence_summary"]:
            for summary in case["evidence_summary"]:
                st.write(f"- {summary}")
        else:
            st.write("Awaiting investigation.")
        st.progress(min(float(case["confidence"]), 1.0), text=f"Confidence: {case['confidence']:.2f}")
        st.markdown(f"**Recommendation:** {case['recommendation'] or 'Awaiting investigation.'}")

        st.markdown("### HUMAN REVIEW — financial decision")
        review_text = "Required before any unresolved financial issue is closed." if case["requires_human_review"] else "Gemini did not request review; RazorRecon still leaves resolution to a human."
        st.warning(review_text)
        a, b = st.columns(2)
        if a.button("Resolve as human reviewer", use_container_width=True):
            human_decision(conn, selected, "resolve")
            case = get_exception(conn, selected)
        if b.button("Escalate as human reviewer", use_container_width=True):
            human_decision(conn, selected, "escalate")
            case = get_exception(conn, selected)

        with st.expander("Investigation tool calls and audit history"):
            st.dataframe(case["tool_calls"], use_container_width=True)
            st.dataframe(case["audit_history"], use_container_width=True)

with tabs[3]:
    st.subheader("Synthetic held-out benchmark")
    st.caption("The held-out split is generated reproducibly but its labels are never supplied to the reconciliation engine.")
    held_out = evaluation(conn, "held_out")
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
            "Count": item["count"],
            "Percentage": f"{item['percentage']:.2f}%",
            "Unresolved Value": f"INR {item['unresolved_value'] / 100:,.2f}",
        }
        for item in held_out["exception_breakdown"]
    ]
    st.dataframe(held_breakdown, use_container_width=True, hide_index=True)
    with st.expander("Full held-out metrics"):
        st.json(held_out)

with tabs[4]:
    st.subheader("Audit events")
    st.dataframe(table(conn, "audit_events"), use_container_width=True, hide_index=True)
