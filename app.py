import json
import os
import sqlite3
import threading
from datetime import date, datetime, timezone

import altair as alt
import pandas as pd
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


@st.cache_resource
def bootstrap_lock() -> threading.Lock:
    # Streamlit runs every session's script in its own thread but @st.cache_resource
    # hands them the SAME connection object, so two sessions hitting a cold start at
    # once both see an empty benchmark table and both start writing it concurrently.
    # That interleaves two insert loops on one connection and raises IntegrityError
    # from a UNIQUE collision, not a "database is locked" error, so it wasn't caught
    # by the retry path below. This lock, cached the same way as db(), makes the
    # bootstrap section critical: only one thread rebuilds at a time; the rest block,
    # then see "ready" once they get in and do nothing.
    return threading.Lock()


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


def inr_compact(paise) -> str:
    """Lakh/crore scale for the headline cards.

    The exposure figures span four orders of magnitude - roughly INR 1.86L at risk
    against INR 1.38Cr awaiting settlement. Printed in full both numbers are long
    strings of digits that a reader has to count, which is exactly the comparison
    the hero exists to make obvious.
    """
    rupees = paise / 100
    if abs(rupees) >= 10_000_000:
        return f"INR {rupees / 10_000_000:,.2f} Cr"
    if abs(rupees) >= 100_000:
        return f"INR {rupees / 100_000:,.2f} L"
    return f"INR {rupees:,.0f}"


# Amber for money genuinely at risk, blue for settlement lag, slate for structural
# breaks that reconcile to zero. Keyed to EXPOSURE_LABELS so the exposure bar and the
# taxonomy bar cannot drift apart.
EXPOSURE_COLORS = {
    "amount_at_risk": "#F59E0B",
    "awaiting_settlement": "#3B82F6",
    "structural": "#64748B",
}


# Cheap row counts decide the cold-start path. Calling dashboard() here as well would
# run a full evaluation pass on every rerun purely to learn whether the table is empty.
try:
    with bootstrap_lock():
        state = bootstrap_state()
        if state != "ready":
            # Drop anything an interrupted run left uncommitted so the rebuild starts clean.
            conn.rollback()
            # The free-tier host sleeps, so a judge's first click pays a ~30s rebuild.
            # An unlabelled spinner for that long reads as a broken app; naming the
            # steps makes the same wait read as work being done.
            with st.status(
                "Preparing the demo dataset - about 30 seconds on a cold start.",
                expanded=True,
            ) as status:
                if state == "load":
                    st.write(f"Generating {BENCHMARK_RECORDS:,} synthetic payments from a fixed seed.")
                    st.write("Reconciling every record and scoring the held-out split.")
                    first_run = load_benchmark_and_reconcile()
                else:
                    st.write("Finishing an interrupted run: re-reconciling from the stored payments.")
                    first_run = reconcile(conn)
                status.update(label="Demo dataset ready.", state="complete", expanded=False)
            st.session_state["last_action"] = first_run
            st.session_state["last_throughput"] = first_run["throughput_per_second"]
except (sqlite3.OperationalError, sqlite3.IntegrityError):
    st.warning(
        "RazorRecon is preparing the demo dataset and the database is briefly busy "
        "(this happens on a cold start when more than one session connects at once). "
        "This is not a data problem \u2014 wait a few seconds and retry."
    )
    if st.button("Retry now", use_container_width=True):
        st.rerun()
    st.stop()

# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

# Streamlit renders every st.metric with identical weight. On this page that buried
# the only two figures a finance reviewer needs to compare - money genuinely at risk
# versus money merely awaiting settlement - among four operational counters. These
# rules add card chrome so the hero can promote three numbers and demote the rest.
st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; max-width: 1550px; }
      [data-testid="stMetric"] {
          background: #141E33;
          border: 1px solid #243352;
          border-radius: 10px;
          padding: 14px 16px;
      }
      [data-testid="stMetricLabel"] p {
          font-size: 0.72rem;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          color: #90A4C6;
      }
      .rr-hero { border-left: 3px solid #3B82F6; padding-left: 18px; margin-bottom: 24px; }
      .rr-hero h1 { font-size: 2.15rem; margin: 0 0 8px 0; letter-spacing: -0.01em; }
      .rr-hero p { color: #A3B6D4; margin: 0; font-size: 1.03rem; max-width: 96ch; }
      .rr-boundary {
          background: rgba(59,130,246,0.08);
          border: 1px solid rgba(59,130,246,0.32);
          border-radius: 10px;
          padding: 13px 17px;
          margin: 18px 0 6px 0;
          font-size: 0.94rem;
          color: #CFDDF2;
      }
      .rr-step {
          font-size: 0.70rem;
          letter-spacing: 0.11em;
          text-transform: uppercase;
          color: #90A4C6;
          margin: 2px 0 8px 0;
      }
      .rr-badge {
          display: inline-block;
          padding: 3px 11px;
          border-radius: 999px;
          font-size: 0.78rem;
          font-weight: 600;
      }
      .rr-ai { background: rgba(59,130,246,0.16); color: #7EB3FF; border: 1px solid rgba(59,130,246,0.42); }
      .rr-fallback { background: rgba(245,158,11,0.14); color: #F2B44C; border: 1px solid rgba(245,158,11,0.42); }
      .rr-idle { background: rgba(148,163,184,0.14); color: #A8B6CC; border: 1px solid rgba(148,163,184,0.35); }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="rr-hero">
      <h1>RazorRecon</h1>
      <p>Deterministic Python decides what is true about the money. Gemini is allowed to explain
      exceptions from read-only evidence, and nothing else. Every unresolved rupee is closed by a
      human, and every step is appended to the audit trail.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# Controls live in the sidebar because the app bootstraps itself: a reviewer should
# not have to press anything to see a result. Buttons in the hero read as setup work
# the reviewer is expected to perform before the demo means anything.
with st.sidebar:
    st.markdown("### Demo controls")
    st.caption("The benchmark loads and reconciles automatically on first visit. These replay it.")
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
                    # An empty result here is the normal case, not a failure, and saying so
                    # matters: a reviewer who reads "no records" as "the connector is
                    # broken" draws the wrong conclusion. Test Mode accounts hold no
                    # payments until someone completes a Checkout flow, and Razorpay does
                    # not run settlement cycles in Test Mode at all, so /settlements and
                    # /settlements/recon/combined stay empty even after payments exist.
                    st.info(
                        "Sync succeeded. The API returned no records for this period, which is "
                        "the expected result for a Test Mode account: Test Mode holds no payments "
                        "until a Checkout flow is completed, and it never runs settlement cycles. "
                        "The synthetic benchmark exists because the full reconciliation pipeline "
                        "cannot be demonstrated from Test Mode data."
                    )
            except Exception as exc:
                st.error(str(exc))

    if "last_action" in st.session_state:
        with st.expander("Last run output"):
            st.json(st.session_state["last_action"])

metrics = dashboard(conn)
# Scored once here and reused by the Evaluation tab. The held-out number is the
# honest headline, so it belongs above the fold, and a second call would repeat the
# whole evaluation pass on every rerun.
held_out = evaluation(conn, "held_out")

# Say what the numbers actually describe. Once Razorpay data is synced the accuracy
# columns lose their ground truth, and the banner has to admit that.
if metrics["dataset"] == "synthetic benchmark":
    st.caption(
        f"Synthetic benchmark, reproducible from fixed seed {metrics['seed']}. "
        "These are not production-accuracy claims."
    )
else:
    st.warning(
        "Showing unlabelled operational data. Reconciliation still runs in full, but "
        "accuracy, precision and recall need ground truth and are reported as N/A."
    )

by_class = metrics["unresolved_value_by_class"]
count_by_class = metrics["unresolved_count_by_class"]

if held_out["records_processed"] and held_out["reconciliation_accuracy"] is not None:
    accuracy_value = format_percent(held_out["reconciliation_accuracy"])
    accuracy_note = f"On {held_out['records_processed']:,} held-out records the engine never saw."
else:
    accuracy_value = "N/A"
    accuracy_note = "Needs labelled ground truth. Load the benchmark to score the engine."

# Three numbers, not six. The exposure split is the point of the project, so it wins
# the top of the page; the operational counters move to the caption row below.
headline = st.columns(3)
headline[0].metric("Amount at risk", inr_compact(by_class["amount_at_risk"]))
headline[0].caption(
    f"{count_by_class['amount_at_risk']:,} exceptions where the ledger disagrees about "
    "money that has already moved."
)
headline[1].metric("Awaiting settlement", inr_compact(by_class["awaiting_settlement"]))
headline[1].caption(
    f"{count_by_class['awaiting_settlement']:,} payments with no settlement row yet. "
    "Pipeline lag, not loss."
)
headline[2].metric("Held-out accuracy", accuracy_value)
headline[2].caption(accuracy_note)

throughput = metrics["throughput_per_second"] or st.session_state.get("last_throughput")
throughput_text = f"{throughput:,.0f}/s" if throughput else "not measured yet"
st.caption(
    f"**{metrics['records_processed']:,}** records reconciled &nbsp;|&nbsp; "
    f"**{metrics['matched']:,}** matched &nbsp;|&nbsp; "
    f"**{metrics['exceptions']:,}** exceptions &nbsp;|&nbsp; "
    f"**{metrics['match_rate']}%** match rate &nbsp;|&nbsp; "
    f"**{metrics['unresolved_exceptions']:,}** unresolved &nbsp;|&nbsp; "
    f"precision **{format_percent(metrics['exception_precision'])}** &nbsp;|&nbsp; "
    f"recall **{format_percent(metrics['exception_recall'])}** &nbsp;|&nbsp; "
    f"throughput **{throughput_text}**"
)

st.markdown(
    '<div class="rr-boundary"><strong>AI boundary.</strong> Gemini runs only after the '
    "deterministic engine has already flagged an exception. It reads evidence through approved "
    "read-only tools and cannot write to payments, settlements, reconciliation records or bank "
    "entries. If the call fails or the structured output does not validate, investigation falls "
    "back to the deterministic classifier and the fallback is recorded in the audit trail.</div>",
    unsafe_allow_html=True,
)

# A stacked proportional bar makes the exposure argument in one glance: the crore-scale
# figure is almost entirely awaiting settlement, and the sliver is what is actually at
# risk. Two paragraphs of caption used to ask the reader to work that out themselves.
exposure_total = sum(by_class.values())
if exposure_total > 0:
    exposure_frame = pd.DataFrame(
        [
            {
                "Exposure": EXPOSURE_LABELS[name],
                "Rupees": by_class[name] / 100,
                "Exceptions": count_by_class[name],
                # A single constant band. Without a y encoding Vega-Lite has no band
                # height to draw the bar into and renders the axis alone, which is
                # exactly what the first version of this chart did.
                "Band": "Unresolved difference",
            }
            for name in EXPOSURE_LABELS
        ]
    )
    exposure_chart = (
        alt.Chart(exposure_frame)
        # An explicit bar size rather than letting the single band decide it. Streamlit
        # renders with autosize "fit", so the height below is the whole canvas and the
        # percentage axis is subtracted from it - at 54px the axis took everything and
        # the bar was drawn 0px tall, which looked exactly like a chart with no data.
        .mark_bar(cornerRadius=2, size=34)
        .encode(
            y=alt.Y("Band:N", title=None, axis=None),
            x=alt.X(
                "Rupees:Q",
                stack="normalize",
                title=None,
                axis=alt.Axis(format=".0%", grid=False, tickCount=5),
            ),
            color=alt.Color(
                "Exposure:N",
                scale=alt.Scale(
                    domain=[EXPOSURE_LABELS[name] for name in EXPOSURE_LABELS],
                    range=[EXPOSURE_COLORS[name] for name in EXPOSURE_LABELS],
                ),
                legend=alt.Legend(orient="bottom", title=None, direction="horizontal", labelLimit=240),
            ),
            tooltip=[
                alt.Tooltip("Exposure:N", title="Class"),
                alt.Tooltip("Rupees:Q", title="INR", format=",.2f"),
                alt.Tooltip("Exceptions:Q", title="Exceptions", format=","),
            ],
        )
        .properties(height=150)
    )
    st.altair_chart(exposure_chart, use_container_width=True)
st.caption(
    "Share of unresolved reconciliation difference by exposure class. Only *amount at risk* is "
    "money the ledger actually disagrees about. *Awaiting settlement* rows have no settlement "
    "yet, so their difference is the whole payment. *Structural* exceptions reconcile to the "
    "rupee but are linked wrongly, so their difference is legitimately zero."
)

# Read once per rerun, without the evidence/tool_calls blobs. This drives both the
# queue tab and the investigation picker.
exceptions_df = table(conn, "exceptions", columns=QUEUE_COLUMNS, order_by="id")

# The benchmark holds 5,000 reconciliation rows and 1,500 exceptions. Serialising all
# of them to the browser on every rerun is slow on a free-tier host, so the wide tables
# are paged and the full set stays available through the CLI benchmark.
TABLE_PREVIEW_ROWS = 500

SECTIONS = ["Reconciliation", "Exceptions", "Investigation", "Evaluation", "Audit"]

# st.tabs keeps its selection only in the browser, and it resets to the first tab on
# every rerun - and in Streamlit every button press is a rerun. So clicking "Investigate
# exception" dropped the reviewer back on Reconciliation, away from the result they had
# just asked for, and the same happened on resolve and escalate. A segmented control
# bound to a session_state key survives reruns, so the section you are working in stays
# put while the numbers underneath update.
section = st.segmented_control(
    "Section",
    SECTIONS,
    default=SECTIONS[0],
    key="section",
    label_visibility="collapsed",
)
# The control is deselectable, and a deselected control returns None. Falling back to
# the first section keeps the page from rendering nothing at all.
section = section or SECTIONS[0]

if section == "Reconciliation":
    st.subheader("Reconciliation records")
    total_results = count_rows("reconciliation_results")
    recon_view = table(conn, "reconciliation_results", limit=TABLE_PREVIEW_ROWS, order_by="payment_id")
    for column in ("expected_amount", "actual_amount", "difference"):
        recon_view[column] = recon_view[column].map(inr)
    st.caption(f"Showing the first {len(recon_view):,} of {total_results:,} reconciliation results.")
    st.dataframe(recon_view, use_container_width=True, hide_index=True)

if section == "Exceptions":
    st.subheader("Exception queue")
    taxonomy = metrics["exception_breakdown"]
    if taxonomy:
        # Shape first, detail second: a sorted bar shows which failure modes dominate
        # far faster than reading a nine-row table.
        taxonomy_frame = pd.DataFrame(
            [
                {
                    "Exception": item["exception_type"],
                    "Count": item["count"],
                    "Exposure": EXPOSURE_LABELS[item["exposure_class"]],
                    "Unresolved": item["unresolved_value"] / 100,
                }
                for item in taxonomy
            ]
        )
        taxonomy_chart = (
            alt.Chart(taxonomy_frame)
            .mark_bar(cornerRadius=2)
            .encode(
                # Exception codes are long (SETTLEMENT_AMOUNT_DISCREPANCY). Vega's default
                # 180px label limit truncates them to an unreadable prefix.
                y=alt.Y("Exception:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
                x=alt.X("Count:Q", title="Exceptions", axis=alt.Axis(grid=False)),
                color=alt.Color(
                    "Exposure:N",
                    scale=alt.Scale(
                        domain=[EXPOSURE_LABELS[name] for name in EXPOSURE_LABELS],
                        range=[EXPOSURE_COLORS[name] for name in EXPOSURE_LABELS],
                    ),
                    legend=alt.Legend(orient="bottom", title=None, labelLimit=240),
                ),
                tooltip=[
                    alt.Tooltip("Exception:N", title="Type"),
                    alt.Tooltip("Count:Q", title="Count", format=","),
                    alt.Tooltip("Exposure:N", title="Exposure"),
                    alt.Tooltip("Unresolved:Q", title="Unresolved INR", format=",.2f"),
                ],
            )
            # Same autosize caveat as the exposure bar: this is the whole canvas, so it
            # has to cover the bands plus the value axis and the bottom legend.
            .properties(height=34 * len(taxonomy_frame) + 95)
        )
        st.altair_chart(taxonomy_chart, use_container_width=True)

    with st.expander("Exception taxonomy table"):
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

if section == "Investigation":
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

        # Three numbered lanes rather than a flat stack of headings. The numbering is
        # what communicates the guarantee: AI sits in the middle and can reach neither
        # the deterministic record above it nor the human decision below it.
        with st.container(border=True):
            st.markdown('<div class="rr-step">Step 1 &middot; Deterministic truth</div>', unsafe_allow_html=True)
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
            st.caption(f"**{EXPOSURE_LABELS[case_exposure]}** - {exposure_notes[case_exposure]}")
            st.markdown(f"**Status:** `{case['status']}` &nbsp; **Type:** `{case['type']}` &nbsp; **Severity:** `{case['severity']}`")
            st.markdown(f"**Why flagged:** {deterministic_flag_reason(case['type'])}")

            with st.expander("Verified evidence - the complete read-only input Gemini receives"):
                if case["evidence"]:
                    st.caption(f"{len(case['evidence'])} read-only tool results captured at investigation time.")
                    st.code(json.dumps(case["evidence"], indent=2), language="json")
                else:
                    st.caption(
                        "No evidence captured yet. Evidence is gathered by the approved read-only tools "
                        "when the investigation runs, and is stored with the case so it can be re-read later."
                    )

        with st.container(border=True):
            st.markdown('<div class="rr-step">Step 2 &middot; AI investigation (read-only, explanation only)</div>', unsafe_allow_html=True)
            # on_click rather than handling the return value: a callback runs before the
            # script re-executes, so Step 1 above re-reads the case and shows the fresh
            # evidence on the same pass. The earlier version called st.rerun() instead,
            # which worked but reset st.tabs to the first tab - click Investigate and the
            # app dropped you back on Reconciliation, away from the result you asked for.
            st.button(
                "Investigate exception",
                use_container_width=True,
                on_click=investigate_exception,
                args=(conn, selected),
                help="Runs the approved read-only tools, then asks Gemini to explain the exception.",
            )
            # The stored default is "Deterministic fallback"; showing it before anything has
            # run would claim a fallback that never happened.
            has_investigated = bool(case["ai_summary"])
            source_label = case.get("investigation_source", "Deterministic fallback") if has_investigated else "Not yet investigated"
            if not has_investigated:
                badge = "rr-idle"
            elif source_label == "Gemini AI":
                badge = "rr-ai"
            else:
                badge = "rr-fallback"
            st.markdown(
                f'Investigation source &nbsp; <span class="rr-badge {badge}">{source_label}</span>',
                unsafe_allow_html=True,
            )
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

        with st.container(border=True):
            st.markdown('<div class="rr-step">Step 3 &middot; Human decision</div>', unsafe_allow_html=True)
            review_text = "Required before any unresolved financial issue is closed." if case["requires_human_review"] else "Gemini did not request review; RazorRecon still leaves resolution to a human."
            st.caption(review_text)
            a, b = st.columns(2)
            # Callbacks for the same reason as Investigate above: the decision is written
            # before the script re-executes, so the status, headline metrics and audit
            # trail all reflect it on this pass without an st.rerun() that would throw the
            # reviewer back to the first tab.
            a.button(
                "Resolve as human reviewer",
                use_container_width=True,
                on_click=human_decision,
                args=(conn, selected, "resolve"),
            )
            b.button(
                "Escalate as human reviewer",
                use_container_width=True,
                on_click=human_decision,
                args=(conn, selected, "escalate"),
            )

            with st.expander("Investigation tool calls and audit history"):
                st.dataframe(case["tool_calls"], use_container_width=True)
                st.dataframe(case["audit_history"], use_container_width=True)

if section == "Evaluation":
    st.subheader("Synthetic held-out benchmark")
    st.caption("The held-out split is generated reproducibly but its labels are never supplied to the reconciliation engine.")
    # A plain st.stop() here would abort the whole script and blank the Audit tab,
    # because Streamlit renders every tab in a single pass.
    if held_out["records_processed"] == 0:
        st.info("No held-out split is loaded. Click **Load benchmark** in the sidebar to generate one.")
    else:
        held_cards = st.columns(5)
        held_cards[0].metric("Held-out records", held_out["records_processed"])
        held_cards[1].metric("Accuracy", format_percent(held_out["reconciliation_accuracy"]))
        held_cards[2].metric("Precision", format_percent(held_out["exception_precision"]))
        held_cards[3].metric("Recall", format_percent(held_out["exception_recall"]))
        error_counts = f"{held_out['false_positives']} / {held_out['false_negatives']}" if held_out["false_positives"] is not None else "N/A"
        held_cards[4].metric("False + / False -", error_counts)
        st.caption(
            "These scores measure the deterministic engine against the labelled synthetic benchmark, "
            "where every scenario is drawn from the failure modes the rules cover. They demonstrate "
            "rule coverage and the absence of regressions - not accuracy on production Razorpay data, "
            "which contains failure modes this benchmark does not generate."
        )
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

if section == "Audit":
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
