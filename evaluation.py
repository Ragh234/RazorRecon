from core import connect, dashboard, evaluation, init_db, load_benchmark, reconcile


def main() -> None:
    db = connect()
    init_db(db)
    print("Loading benchmark...")
    print(load_benchmark(db))
    print("Running reconciliation...")
    print(reconcile(db))
    print("Dashboard:")
    print(dashboard(db))
    print("Evaluation:")
    print(evaluation(db))


if __name__ == "__main__":
    main()
