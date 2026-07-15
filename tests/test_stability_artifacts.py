import csv
import json

from scripts.build_stability_artifacts import build_artifacts


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def transaction(sequence, status="success", latency_ms=2000, **extra):
    return {
        "type": "transaction",
        "sequence": sequence,
        "status": status,
        "latency_ms": latency_ms,
        **extra,
    }


def test_interrupted_run_builds_partial_readable_reports(tmp_path):
    source = tmp_path / "fiber-to-lnd-run.jsonl"
    write_jsonl(source, [transaction(1, latency_ms=1000), transaction(2, latency_ms=3000)])

    paths = build_artifacts(source, "fiber-to-lnd", 5, 300)
    summary = json.loads(paths["summary_json"].read_text(encoding="utf-8"))

    assert summary["completion_status"] == "interrupted"
    assert summary["partial"] is True
    assert summary["target_transactions"] == 1500
    assert summary["recorded_transactions"] == 2
    assert summary["succeeded"] == 2
    assert summary["latency_ms"]["p50"] == 2000
    assert "partial report" in paths["summary_md"].read_text(encoding="utf-8")
    assert "Successful flow latency p95 (ms)" in paths["summary_md"].read_text(
        encoding="utf-8"
    )
    assert "| Failure rate | 0.000% |" in paths["summary_md"].read_text(
        encoding="utf-8"
    )
    with paths["transactions_csv"].open(encoding="utf-8") as source_csv:
        assert len(list(csv.DictReader(source_csv))) == 2


def test_completed_summary_is_preserved_and_failures_are_separate(tmp_path):
    source = tmp_path / "lnd-to-fiber-run.jsonl"
    records = [
        transaction(1),
        transaction(2, status="failed", error_type="ExampleError", error="boom"),
        {
            "type": "summary",
            "flow": "lnd-to-fiber",
            "target_tps": 5,
            "scheduled": 2,
            "succeeded": 1,
            "failed": 1,
            "rejected": 0,
            "failure_rate": 0.5,
            "latency_ms": {"p50": 2000, "p95": 2000, "max": 2000},
            "errors": {"ExampleError": 1},
            "passed": False,
        },
    ]
    write_jsonl(source, records)

    paths = build_artifacts(source, "lnd-to-fiber", 5, 300)
    summary = json.loads(paths["summary_json"].read_text(encoding="utf-8"))

    assert summary["completion_status"] == "completed"
    assert summary["partial"] is False
    assert summary["passed"] is False
    with paths["failures_csv"].open(encoding="utf-8") as failures_csv:
        failures = list(csv.DictReader(failures_csv))
    assert len(failures) == 1
    assert failures[0]["sequence"] == "2"
    assert failures[0]["error"] == "boom"


def test_sequential_partial_report_has_no_target_tps_or_transaction_count(tmp_path):
    source = tmp_path / "lnd-to-fiber-run.jsonl"
    write_jsonl(source, [transaction(1)])

    paths = build_artifacts(source, "lnd-to-fiber", 5, 300, "sequential")
    summary = json.loads(paths["summary_json"].read_text(encoding="utf-8"))
    markdown = paths["summary_md"].read_text(encoding="utf-8")

    assert summary["load_mode"] == "sequential"
    assert summary["target_tps"] is None
    assert summary["target_transactions"] is None
    assert "| Load mode | sequential |" in markdown
