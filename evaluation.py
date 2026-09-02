from core import (
    BENCHMARK_RECORDS,
    BENCHMARK_SEED,
    EXPOSURE_LABELS,
    connect,
    evaluation,
    init_db,
    load_benchmark,
    reconcile,
)


def print_report(title: str, metrics: dict) -> None:
    print(title)
    print("-" * len(title))
    print(f"Dataset: Synthetic ({metrics['dataset_split']})")
    print(f"Seed: {metrics['seed']}")
    print(f"Records: {metrics['records_processed']:,}")
    print(f"Matched: {metrics['matched']:,}")
    print(f"Exceptions: {metrics['exceptions']:,}")
    print(f"Match rate: {metrics['match_rate']:.2f}%")
    print(f"Accuracy: {metrics['reconciliation_accuracy']:.2f}%")
    print(f"Precision: {metrics['exception_precision']:.2f}%")
    print(f"Recall: {metrics['exception_recall']:.2f}%")
    print(f"False positives: {metrics['false_positives']:,}")
    print(f"False negatives: {metrics['false_negatives']:,}")
    print(f"Throughput: {metrics['throughput_per_second']:,.2f} records/second ({metrics['throughput_scope']})")
    print(f"Unresolved exceptions: {metrics['unresolved_exceptions']:,}")
    print(f"Unresolved value: INR {metrics['unresolved_value'] / 100:,.2f}")
    for name, label in EXPOSURE_LABELS.items():
        print(
            f"  {label}: INR {metrics['unresolved_value_by_class'][name] / 100:,.2f} "
            f"across {metrics['unresolved_count_by_class'][name]:,} exceptions"
        )
    print("Exception breakdown:")
    for item in metrics["exception_breakdown"]:
        print(
            f"  {item['exception_type']} [{item['exposure_class']}]: {item['count']:,} "
            f"({item['percentage']:.2f}%), unresolved INR {item['unresolved_value'] / 100:,.2f}"
        )


def main() -> None:
    # The CLI is isolated from the app database and cannot overwrite local finance data.
    db = connect(":memory:")
    init_db(db)
    load_benchmark(db, records=BENCHMARK_RECORDS, seed=BENCHMARK_SEED)
    reconcile(db)
    print_report("RazorRecon Benchmark", evaluation(db))
    print()
    print_report("Held-Out Evaluation", evaluation(db, "held_out"))
    print()
    print("Synthetic benchmark only; these results are not production accuracy claims.")


if __name__ == "__main__":
    main()
