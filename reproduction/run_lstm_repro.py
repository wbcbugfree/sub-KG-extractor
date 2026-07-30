"""Preflight and run the original LSTM reproduction pipeline."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from run_path_analogy_repro import (
    AP_MODELS,
    AP_UTILS,
    DEFAULT_RANDOM_SEED,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_ROW_DECISIONS_DIR,
    REPO_ROOT,
    DatasetConfig,
    dataset_config,
    format_command,
    preflight,
    reproducible_subprocess_environment,
    resolve_output_dir,
)


@dataclass(frozen=True)
class LstmSetting:
    name: str
    sequence_length: int
    padding: str
    units: int
    learning_rate: float
    voting_threshold: float = 0.5
    epochs: int = 200


def lstm_setting(name: str) -> LstmSetting:
    settings = {
        "dataset1": LstmSetting("dataset1_config", 5, "before", 150, 0.01, 0.4),
        "dataset2": LstmSetting("dataset2_config", 3, "before", 150, 0.001, 0.2),
    }
    try:
        return settings[name]
    except KeyError as exc:
        raise ValueError(f"Unknown LSTM setting: {name}") from exc


def build_lstm_pipeline_commands(
    config: DatasetConfig,
    setting: LstmSetting,
    python_executable: Path,
    random_seed: int = DEFAULT_RANDOM_SEED,
    row_decisions_dir: Path = DEFAULT_ROW_DECISIONS_DIR,
) -> list[list[str | Path]]:
    prefix = config.output_dir / f"{config.name}_lstm_{setting.name}"
    predictions = prefix.with_name(prefix.name + "_predictions.pkl")
    validation_predictions = prefix.with_name(prefix.name + "_validation_predictions.pkl")
    classifier_decisions = prefix.with_name(prefix.name + "_classifier_decisions.pkl")
    metrics = prefix.with_name(prefix.name + "_metrics.csv")
    seen_unseen_metrics = prefix.with_name(prefix.name + "_seen_unseen_metrics.csv")
    row_decisions = row_decisions_dir / f"{config.name}_lstm_{setting.name}.csv"
    expansion_args: list[str] = []
    if config.expansion_properties:
        expansion_args = ["--expansion-properties", *config.expansion_properties]
    bridge_args = ["--allow-bridge-nodes"] if config.allow_bridge_nodes else []

    commands: list[list[str | Path]] = [
        [
            python_executable,
            AP_MODELS / "lstm_pruning.py",
            "--folds",
            config.folds,
            "--decisions",
            config.decisions,
            "--wikidata",
            config.wikidata_lmdb,
            "--embeddings",
            config.embeddings_lmdb,
            "--sequence-len",
            str(setting.sequence_length),
            "--padding",
            setting.padding,
            "--nb-units",
            str(setting.units),
            "--learning-rate",
            str(setting.learning_rate),
            "--random-seed",
            str(random_seed),
            "--epochs",
            str(setting.epochs),
            "--predictions-output",
            predictions,
            "--validation-predictions-output",
            validation_predictions,
            *expansion_args,
            *bridge_args,
        ],
        [
            python_executable,
            AP_MODELS / "lstm_voting.py",
            "--folds",
            config.folds,
            "--decisions",
            config.decisions,
            "--wikidata",
            config.wikidata_lmdb,
            "--embeddings",
            config.embeddings_lmdb,
            "--predictions",
            predictions,
            "--voting-threshold",
            str(setting.voting_threshold),
            "--output",
            classifier_decisions,
            *expansion_args,
            *bridge_args,
        ],
        [
            python_executable,
            AP_UTILS / "decision_statistics.py",
            "--classifier-decisions",
            classifier_decisions,
            "--gold-decisions",
            config.decisions,
            "--output",
            metrics,
            "--row-output",
            row_decisions,
        ],
        [
            python_executable,
            AP_UTILS / "decision_statistics_seen_unseen.py",
            "--classifier-decisions",
            classifier_decisions,
            "--gold-decisions",
            config.decisions,
            "--folds",
            config.folds,
            "--wikidata",
            config.wikidata_lmdb,
            "--output",
            seen_unseen_metrics,
            *expansion_args,
        ],
    ]

    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("1", "2", "3"), required=True)
    parser.add_argument("--setting", choices=("dataset1", "dataset2"), required=True)
    parser.add_argument("--wikidata-lmdb", type=Path, required=True)
    parser.add_argument("--embeddings-lmdb", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--row-decisions-dir", type=Path, default=DEFAULT_ROW_DECISIONS_DIR)
    parser.add_argument("--folds", type=Path, help="Override the default fold pickle, mainly for dataset3 sensitivity runs")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    output_dir = resolve_output_dir(args.output_dir)
    config = dataset_config(args.dataset, output_dir, args.wikidata_lmdb, args.embeddings_lmdb)
    if args.folds:
        config.folds = args.folds
    setting = lstm_setting(args.setting)

    issues = preflight(config, allow_non_hierarchy=True)
    if issues:
        print("Preflight failed:")
        for issue in issues:
            print(f"- {issue}")
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    commands = build_lstm_pipeline_commands(
        config,
        setting,
        args.python,
        args.random_seed,
        args.row_decisions_dir,
    )
    if not args.run:
        print("Dry run. Commands:")
        for command in commands:
            print(format_command(command))
        return 0

    environment = reproducible_subprocess_environment(args.random_seed)
    for command in commands:
        print(format_command(command), flush=True)
        subprocess.run(
            [str(part) for part in command],
            cwd=REPO_ROOT,
            check=True,
            env=environment,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
