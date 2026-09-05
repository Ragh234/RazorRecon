# RazorRecon Architecture

How RazorRecon is put together, and why the boundaries sit where they do. Everything described here is implemented in `core.py` and `app.py`; nothing is aspirational.

## The one design rule

```
Deterministic Python establishes financial truth.
The model only explains exceptions that Python has already verified.
A human makes every financial decision.
```

Reconciliation status, expected amounts, differences, and exception classification are computed entirely in Python before any model is contacted. The model cannot create, alter, or close a financial record. If it is unavailable, misconfigured, or returns something that fails validation, the system continues deterministically rather than degrading into guesswork.

## Data model

Nine SQLite tables, created in `init_db()`:

| Table | Role |
|---|---|
| `payments` | Captured payments — source of truth for amount, method, capture time |
| `settlements` | Settlement batches with fees, tax, UTR |
| `reconciliation_records` | Per-entity recon rows (payment / refund / transfer / adjustment) |
| `bank_entries` | Bank-side credits keyed by UTR |
| `reconciliation_results` | One row per payment: status, reason code, expected, actual, difference |
| `exceptions` | Open exception queue with evidence, tool calls, confidence, review state |
| `benchmark_ground_truth` | Labels — split, scenario, expected status/type — held separately |
| `reconciliation_runs` | Timing and throughput per run |
| `audit_events` | Append-only actor / action / before / after log |

All money is stored as integer paise. No floating-point arithmetic touches a financial value anywhere in the reconciliation path.

## Pipeline

```
Razorpay Test Mode API   Synthetic benchmark (seed 42)
            \                     /
             v                   v
                Data ingestion (SQLite)
                        |
                        v
            Deterministic reconciliation
                        |
              +---------+---------+
              v                   v
          Matched              Exception
                                  |
                                  v
                    Gemini investigation (read-only)
                                  |
                                  v
                    Human review (resolve / escalate)
                                  |
                                  v
                          Audit trail
```

### 1. Ingestion

Two independent sources, deliberately kept separate:

- **Razorpay Test Mode** (`RazorpayClient`) calls `/v1/payments`, `/v1/settlements/`, and `/v1/settlements/recon/combined` with HTTP basic auth. `fetch_all()` pages with `count`/`skip` until a short page comes back, capping page size at 1000 for the recon endpoint and 100 elsewhere. Responses are normalised into the same schema the benchmark uses. See the README's Known Limitations for why Test Mode cannot exercise the full pipeline — that is a property of Razorpay, not of this connector.
- **Synthetic benchmark** (`load_benchmark()`) generates 5,000 records from a fixed seed (42): 70% clean matches and ten defect scenarios at 3% each. Amounts, fee rates, methods, timestamps, settlement timing, and identifiers all vary from the seeded generator; records are not copies of a smaller fixture. The split is 80/20 development/held-out, stratified per scenario.

The benchmark path exists so the demo is reproducible without credentials. The Razorpay path exists so the system is not purely a simulation.

### 2. Deterministic reconciliation

`reconcile()` clears prior results and walks every payment through `reconcile_payment()`, which checks the presence and uniqueness of a reconciliation record, the mapping between `entity_id` and `payment_id`, the linked settlement, the timing window, refunds and adjustments, the expected settlement derived by `calculate_expected_settlement()` (payment less verified fees, tax, and refunds), and the bank entry matched on UTR.

Every payment ends as exactly one of `matched` or an exception carrying a machine-readable reason code:

`AMOUNT_MISMATCH`, `BANK_UTR_AMOUNT_MISMATCH`, `DUPLICATE_RECONCILIATION_RECORD`, `MISSING_RECONCILIATION_RECORD`, `MISSING_SETTLEMENT`, `REFUND_AMOUNT_MISMATCH`, `SETTLEMENT_AMOUNT_DISCREPANCY`, `TIMING_WINDOW_EXCEEDED`, `WRONG_MAPPING`

**Ground-truth independence.** `reconcile()` selects only source financial columns from `payments`. It never reads `benchmark_ground_truth`. Labels are joined onto exceptions *after* every financial result has been written, and are used only for scoring. This is what keeps the reported accuracy from being circular.

### 3. Exposure classification

A single summed "unresolved value" across all exceptions would be misleading, because a difference does not mean the same thing for every reason code. `EXPOSURE_CLASS` sorts each type into one of three buckets:

| Class | Meaning | Types |
|---|---|---|
| `amount_at_risk` | The ledger disagrees about money that has already moved | amount mismatch, bank UTR mismatch, refund mismatch, settlement discrepancy, duplicate record |
| `awaiting_settlement` | No settlement or recon row exists yet, so the difference is the whole payment — pipeline lag, not loss | missing settlement, missing reconciliation record |
| `structural` | Amounts reconcile but the linkage does not; the difference is legitimately zero | wrong mapping, timing window exceeded, payment ID mismatch |

This is why the dashboard reports exposure by class rather than one headline number: adding a pending settlement to a genuine shortfall would overstate risk.

### 4. Investigation

`investigate_exception()` gathers evidence by calling eight read-only tools, all whitelisted in `APPROVED_TOOLS`:

`get_payment`, `get_settlement`, `get_reconciliation_record`, `find_related_transactions`, `find_refunds`, `find_adjustments`, `compare_bank_entry`, `calculate_expected_settlement`

`tool_result()` raises on any tool name outside that set, so a mutation tool cannot enter the evidence path even by mistake. **Python decides which tools run; the model does not choose what evidence it sees.** That is deliberate — letting a model select which financial records to look at is itself a control weakness in a reconciliation system.

The assembled evidence goes to Gemini (Interactions API, structured output) with an instruction to explain only from supplied evidence and to declare `INSUFFICIENT_EVIDENCE` when the evidence does not support a conclusion. `validate_llm_result()` then enforces field presence, exact types, non-empty strings, and a confidence inside [0, 1]. Anything failing that check is discarded.

**Model ladder.** `GEMINI_MODELS` tries `gemini-3.5-flash-lite` on a 15-second leash first, then `gemini-3.5-flash` with 40 seconds. The escalation happens only when `gemini_failure_is_transient()` says the first failure was capacity-related, so a genuine bad request is not retried pointlessly.

**Failure handling.** If the API key is absent, every model fails, the payload cannot be parsed, or validation rejects the output, the system falls back to `classify_case()` — a deterministic, evidence-grounded classifier with its own per-type explanation, confidence, and recommendation. When an attempt was made and failed, the reason is written as a `GEMINI_FALLBACK` audit event, so the failure mode is visible in the audit trail rather than silently absorbed. The UI always names the source as either `Gemini AI` or `Deterministic fallback`; the two are never shown interchangeably.

### 5. Human review and audit

An exception routes to `human_review` whenever confidence falls below 0.7, evidence is insufficient, or the investigation requests review. `human_decision()` records `resolve` or `escalate`. Nothing else can close an exception — there is no autonomous resolution path, and no refund, payout, or settlement mutation exists anywhere in the codebase.

`audit()` writes an append-only row for every state change with actor attribution (`system`, `razorpay_connector`, `ai_investigator`, `human_reviewer`), the action, and before/after state.

### 6. Evaluation

`evaluation()` scores the held-out split — 20% of records, stratified by scenario — whose labels the engine never received. It reports total records, matches, match rate, exact classification accuracy, exception precision and recall, false positives and negatives, throughput, and exposure by class, plus a per-type breakdown. Nothing is hardcoded; every figure is computed from the run. See the README on why 100% here demonstrates rule coverage rather than real-world accuracy.

## Reliability

`connect()` opens SQLite in WAL mode with an explicit busy timeout, because Streamlit serves concurrent sessions against one database file and a cold start seeds thousands of rows in a single write transaction. The app's cold-start seeding is wrapped so transient database contention surfaces as a retry prompt rather than taking the whole app down for every visitor.

## What the matcher does not do

Stated plainly, because a reconciliation system that hides its limits is not trustworthy. These are scope limits of the matching logic itself; the README covers data and accuracy limits separately.

- **Fixed timing window.** The timing rule is a fixed seven-day deterministic window. Real settlement cycles vary by merchant, method, and payout schedule, and would need to be configurable.
- **No fuzzy matching.** Matching is by exact key and derived arithmetic. A reconciliation row whose identifier has been corrupted or reformatted surfaces as `MISSING_RECONCILIATION_RECORD` rather than being recovered.
- **No batch decomposition.** A payment is assumed to map to a settlement. Consolidated payouts, where many payments net into a single bank credit, are not decomposed into their components and would present as amount discrepancies.
- **Single currency.** Amounts are integer paise, single-currency. Multi-currency settlement and FX differences are not modelled.
