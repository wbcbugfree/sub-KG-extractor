"""Run the full Path analogy reproduction sequence once artifacts exist."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "runs"
DEFAULT_WIKIDATA_ROOT = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy"


def build_full_reproduction_commands(
    python_executable: Path,
    wikidata_root: Path,
    embeddings_lmdb: Path,
    output_root: Path,
) -> list[list[str | Path]]:
    path_runner = HELPER_DIR / "run_path_analogy_repro.py"
    lstm_runner = HELPER_DIR / "run_lstm_repro.py"
    reached_seen = HELPER_DIR / "compute_reached_seen_percentages.py"
    path_tuner = HELPER_DIR / "tune_path_analogy_dataset3.py"
    lstm_tuner = HELPER_DIR / "tune_lstm_dataset3.py"
    input_dir = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "inputs"
    fold_dir = HELPER_DIR / "folds"
    dataset3_properties = [
        line.strip()
        for line in (REPO_ROOT / "wikidata" / "dataset3" / "properties_dataset3.csv").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    path_specs = [
        ("1", "dataset1", wikidata_root / "dataset1" / "wikidata_lmdb"),
        ("2", "dataset2", wikidata_root / "dataset2" / "wikidata_lmdb"),
        ("3", "dataset1", wikidata_root / "dataset3" / "wikidata_lmdb_ap"),
        ("3", "dataset2", wikidata_root / "dataset3" / "wikidata_lmdb_ap"),
    ]
    lstm_specs = [
        ("1", "dataset1", wikidata_root / "dataset1" / "wikidata_lmdb"),
        ("2", "dataset2", wikidata_root / "dataset2" / "wikidata_lmdb"),
        ("3", "dataset1", wikidata_root / "dataset3" / "wikidata_lmdb_ap"),
        ("3", "dataset2", wikidata_root / "dataset3" / "wikidata_lmdb_ap"),
    ]

    commands: list[list[str | Path]] = []
    for dataset, setting, wikidata_lmdb in path_specs:
        dataset3_preflight_args = ["--allow-non-hierarchy"] if dataset == "3" else []
        candidate_row_args = (
            ["--row-decisions-dir", output_root / "candidate_rows"]
            if dataset == "3"
            else []
        )
        commands.append(
            [
                python_executable,
                path_runner,
                "--dataset",
                dataset,
                "--setting",
                setting,
                "--wikidata-lmdb",
                wikidata_lmdb,
                "--embeddings-lmdb",
                embeddings_lmdb,
                "--output-dir",
                output_root,
                "--python",
                python_executable,
                *dataset3_preflight_args,
                *candidate_row_args,
                "--run",
            ]
        )

    for dataset, setting, wikidata_lmdb in lstm_specs:
        candidate_row_args = (
            ["--row-decisions-dir", output_root / "candidate_rows"]
            if dataset == "3"
            else []
        )
        commands.append(
            [
                python_executable,
                lstm_runner,
                "--dataset",
                dataset,
                "--setting",
                setting,
                "--wikidata-lmdb",
                wikidata_lmdb,
                "--embeddings-lmdb",
                embeddings_lmdb,
                "--output-dir",
                output_root,
                "--python",
                python_executable,
                *candidate_row_args,
                "--run",
            ]
        )

    commands.extend(
        [
            [
                python_executable,
                reached_seen,
                "--gold-decisions",
                input_dir / "dataset1_gold_decisions_filtered.csv",
                "--folds",
                fold_dir / "dataset1_5_folds.pkl",
                "--wikidata",
                wikidata_root / "dataset1" / "wikidata_lmdb",
                "--output",
                output_root / "dataset1_reached_seen_percentages.csv",
            ],
            [
                python_executable,
                reached_seen,
                "--gold-decisions",
                input_dir / "dataset2_gold_decisions_filtered.csv",
                "--folds",
                fold_dir / "dataset2_5_folds.pkl",
                "--wikidata",
                wikidata_root / "dataset2" / "wikidata_lmdb",
                "--output",
                output_root / "dataset2_reached_seen_percentages.csv",
            ],
            [
                python_executable,
                reached_seen,
                "--gold-decisions",
                input_dir / "dataset3_gold_decisions_bridge_filtered.csv",
                "--folds",
                fold_dir / "dataset3_overlap_reduced_5_folds.pkl",
                "--wikidata",
                wikidata_root / "dataset3" / "wikidata_lmdb_ap",
                "--expansion-properties",
                *dataset3_properties,
                "--output",
                output_root / "dataset3_reached_seen_percentages.csv",
            ],
            [
                python_executable,
                path_tuner,
                "--wikidata-lmdb",
                wikidata_root / "dataset3" / "wikidata_lmdb_ap",
                "--embeddings-lmdb",
                embeddings_lmdb,
                "--output-dir",
                output_root,
                "--python",
                python_executable,
                "--run",
            ],
            [
                python_executable,
                lstm_tuner,
                "--wikidata-lmdb",
                wikidata_root / "dataset3" / "wikidata_lmdb_ap",
                "--embeddings-lmdb",
                embeddings_lmdb,
                "--output-dir",
                output_root,
                "--python",
                python_executable,
                "--run",
            ],
        ]
    )

    return commands


def publish_compact_results(output_root: Path, publish_root: Path) -> list[Path]:
    metrics_dir = publish_root / "metrics"
    tuning_dir = publish_root / "tuning"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    tuning_dir.mkdir(parents=True, exist_ok=True)

    metric_names = (
        "dataset1_dataset1_config_metrics.csv",
        "dataset1_dataset1_config_seen_unseen_metrics.csv",
        "dataset1_lstm_dataset1_config_metrics.csv",
        "dataset1_lstm_dataset1_config_seen_unseen_metrics.csv",
        "dataset1_reached_seen_percentages.csv",
        "dataset2_dataset2_config_metrics.csv",
        "dataset2_dataset2_config_seen_unseen_metrics.csv",
        "dataset2_lstm_dataset2_config_metrics.csv",
        "dataset2_lstm_dataset2_config_seen_unseen_metrics.csv",
        "dataset2_reached_seen_percentages.csv",
        "dataset3_path_analogy_tuned_metrics.csv",
        "dataset3_path_analogy_tuned_seen_unseen_metrics.csv",
        "dataset3_lstm_tuned_metrics.csv",
        "dataset3_lstm_tuned_seen_unseen_metrics.csv",
        "dataset3_reached_seen_percentages.csv",
    )

    published: list[Path] = []
    for name in metric_names:
        source = output_root / name
        if not source.is_file():
            raise FileNotFoundError(f"Missing final metric artifact: {source}")
        destination = metrics_dir / name
        shutil.copy2(source, destination)
        published.append(destination)

    for name in (
        "dataset3_path_analogy_tuning_summary.json",
        "dataset3_lstm_tuning_summary.json",
    ):
        source = output_root / name
        if not source.is_file():
            raise FileNotFoundError(f"Missing final tuning artifact: {source}")
        destination = tuning_dir / name
        shutil.copy2(source, destination)
        published.append(destination)

    return published


def format_command(command: list[str | Path]) -> str:
    return " ".join(f'"{part}"' if " " in str(part) else str(part) for part in command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--wikidata-root", type=Path, default=DEFAULT_WIKIDATA_ROOT)
    parser.add_argument("--embeddings-lmdb", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    commands = build_full_reproduction_commands(
        python_executable=args.python,
        wikidata_root=args.wikidata_root,
        embeddings_lmdb=args.embeddings_lmdb,
        output_root=args.output_root,
    )

    if not args.run:
        print("Dry run. Commands:")
        for command in commands:
            print(format_command(command))
        return 0

    for command in commands:
        print(format_command(command), flush=True)
        subprocess.run([str(part) for part in command], cwd=REPO_ROOT, check=True)

    published = publish_compact_results(output_root=args.output_root, publish_root=HELPER_DIR / "results")
    print(f"Published {len(published)} compact result artifacts to {HELPER_DIR / 'results'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
