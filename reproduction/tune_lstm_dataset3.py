"""Select a Dataset 3 LSTM config and universal threshold on validation."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from run_path_analogy_repro import (
    AP_UTILS,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_ROW_DECISIONS_DIR,
    REPO_ROOT,
    dataset_config,
    format_command,
)
from validation_selection import (
    PredictionCandidate,
    lstm_score,
    run_fixed_configuration_universal_threshold_selection,
)


DEFAULT_WIKIDATA_LMDB = (
    REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "dataset3" / "wikidata_lmdb_ap"
)
DEFAULT_EMBEDDINGS_LMDB = (
    REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "e1_embeddings_lmdb"
)
CONFIGURATIONS = ("dataset1_config", "dataset2_config")


def prediction_candidate(output_dir: Path, configuration: str) -> PredictionCandidate:
    prefix = output_dir / f"dataset3_lstm_{configuration}"
    return PredictionCandidate(
        name=configuration,
        test_predictions=prefix.with_name(prefix.name + "_predictions.pkl"),
        validation_predictions=prefix.with_name(
            prefix.name + "_validation_predictions.pkl"
        ),
    )


def candidate_artifacts(output_dir: Path, configuration: str) -> dict[str, Path]:
    prefix = (
        output_dir
        / f"dataset3_lstm_{configuration}_universal_validation_threshold"
    )
    return {
        "classifier": prefix.with_name(prefix.name + "_classifier_decisions.pkl"),
        "metrics": prefix.with_name(prefix.name + "_metrics.csv"),
        "rows": output_dir / "candidate_rows" / f"{prefix.name}.csv",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wikidata-lmdb", type=Path, default=DEFAULT_WIKIDATA_LMDB)
    parser.add_argument("--embeddings-lmdb", type=Path, default=DEFAULT_EMBEDDINGS_LMDB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--row-decisions-dir", type=Path, default=DEFAULT_ROW_DECISIONS_DIR)
    parser.add_argument("--folds", type=Path, help="Override the Dataset 3 folds")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--thresholds",
        nargs="+",
        type=float,
        default=[value / 100 for value in range(101)],
        help="Threshold grid; defaults to 0.00--1.00 in increments of 0.01",
    )
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    config = dataset_config(
        "3", args.output_dir, args.wikidata_lmdb, args.embeddings_lmdb
    )
    if args.folds:
        config.folds = args.folds
    candidates = [
        prediction_candidate(args.output_dir, configuration)
        for configuration in CONFIGURATIONS
    ]
    thresholds = sorted(set(args.thresholds))
    if not thresholds or thresholds[0] < 0.0 or thresholds[-1] > 1.0:
        parser.error("Thresholds must be within [0, 1]")

    required = [
        config.decisions,
        config.folds,
        config.wikidata_lmdb,
        config.embeddings_lmdb,
        *(candidate.test_predictions for candidate in candidates),
        *(candidate.validation_predictions for candidate in candidates),
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        print("Missing required artifacts:")
        for path in missing:
            print(f"- {path}")
        return 2

    if not args.run:
        print("Dry run. Each configuration remains fixed across all five folds:")
        for candidate in candidates:
            print(
                f"- {candidate.name}: validation={candidate.validation_predictions}, "
                f"outer-test={candidate.test_predictions}"
            )
        print(
            f"- universal threshold candidates: {len(thresholds)} "
            f"({thresholds[0]:.2f}--{thresholds[-1]:.2f})"
        )
        return 0

    configuration_results: dict[str, dict[str, object]] = {}
    artifacts_by_configuration: dict[str, dict[str, Path]] = {}
    for candidate in candidates:
        artifacts = candidate_artifacts(args.output_dir, candidate.name)
        artifacts_by_configuration[candidate.name] = artifacts
        configuration_results[candidate.name] = (
            run_fixed_configuration_universal_threshold_selection(
                candidate=candidate,
                thresholds=thresholds,
                score_prediction=lstm_score,
                folds_path=config.folds,
                decisions_path=config.decisions,
                wikidata_lmdb=config.wikidata_lmdb,
                embeddings_lmdb=config.embeddings_lmdb,
                initial_properties=config.expansion_properties
                or ("P31", "P279", "(-)P279"),
                next_properties=config.expansion_properties or ("(-)P279",),
                allow_bridge_nodes=config.allow_bridge_nodes,
                classifier_output=artifacts["classifier"],
                metrics_output=artifacts["metrics"],
                row_output=artifacts["rows"],
            )
        )

    selected_configuration = max(
        CONFIGURATIONS,
        key=lambda name: float(
            configuration_results[name]["selected_validation_metric_views"][
                "row_count_weighted_fold"
            ]["f1"]
        ),
    )
    selected_result = configuration_results[selected_configuration]
    selected_artifacts = artifacts_by_configuration[selected_configuration]

    classifier_output = args.output_dir / "dataset3_lstm_tuned_classifier_decisions.pkl"
    metrics_output = args.output_dir / "dataset3_lstm_tuned_metrics.csv"
    seen_unseen_output = args.output_dir / "dataset3_lstm_tuned_seen_unseen_metrics.csv"
    row_output = args.row_decisions_dir / "dataset3_lstm_tuned.csv"
    classifier_output.parent.mkdir(parents=True, exist_ok=True)
    row_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(selected_artifacts["classifier"], classifier_output)
    shutil.copyfile(selected_artifacts["metrics"], metrics_output)
    shutil.copyfile(selected_artifacts["rows"], row_output)

    seen_unseen_command: list[str | Path] = [
        args.python,
        AP_UTILS / "decision_statistics_seen_unseen.py",
        "--classifier-decisions",
        classifier_output,
        "--gold-decisions",
        config.decisions,
        "--folds",
        config.folds,
        "--wikidata",
        config.wikidata_lmdb,
        "--output",
        seen_unseen_output,
    ]
    if config.expansion_properties:
        seen_unseen_command.extend(
            ["--expansion-properties", *config.expansion_properties]
        )
    print(format_command(seen_unseen_command), flush=True)
    subprocess.run(
        [str(part) for part in seen_unseen_command], cwd=REPO_ROOT, check=True
    )

    summary = {
        "strategy": "configuration_and_universal_threshold_selection_on_validation_folds",
        "configuration_policy": "each candidate configuration is fixed across all five outer folds",
        "threshold_policy": "one selected threshold is applied to all five outer test folds",
        "selection_metric": "row_count_weighted_five_validation_fold_f1",
        "selection_data": "validation_predictions_only",
        "configuration_results": configuration_results,
        "selected_configuration": selected_configuration,
        "selected_threshold": selected_result["selected_threshold"],
        "selected_validation_metric_views": selected_result[
            "selected_validation_metric_views"
        ],
        "final_metric_views": selected_result["final_metric_views"],
        "selected_metrics": "metrics/dataset3_lstm_tuned_metrics.csv",
        "selected_seen_unseen_metrics": "metrics/dataset3_lstm_tuned_seen_unseen_metrics.csv",
        "selected_row_decisions": "decisions/dataset3_lstm_tuned.csv",
    }
    summary_path = args.output_dir / "dataset3_lstm_tuning_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "selected_configuration": selected_configuration,
                "selected_threshold": summary["selected_threshold"],
                "selected_validation_metric_views": summary[
                    "selected_validation_metric_views"
                ],
                "configuration_results": {
                    name: {
                        "selected_threshold": result["selected_threshold"],
                        "selected_validation_metric_views": result[
                            "selected_validation_metric_views"
                        ],
                        "final_metric_views": result["final_metric_views"],
                    }
                    for name, result in configuration_results.items()
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
