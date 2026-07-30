"""Preflight and run the original Path analogy reproduction pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPRO_ROOT = Path(__file__).resolve().parent
INPUTS = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "inputs"
FOLDS = REPRO_ROOT / "folds"
WIKIDATA_DATA = REPO_ROOT / "wikidata"
AP_UTILS = REPRO_ROOT / "src" / "utils"
AP_MODELS = REPRO_ROOT / "src" / "models_on_labeled_decisions"
HELPER_DIR = REPRO_ROOT
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "runs"
DEFAULT_ROW_DECISIONS_DIR = REPRO_ROOT / "results" / "decisions"
DEFAULT_RANDOM_SEED = 41


@dataclass
class DatasetConfig:
    name: str
    decisions: Path
    folds: Path
    seed_qids: Path
    wikidata_lmdb: Path
    embeddings_lmdb: Path
    embeddings_manifest: Path
    output_dir: Path
    hierarchy_only: bool = True
    expansion_properties: tuple[str, ...] | None = None
    allow_bridge_nodes: bool = False


@dataclass(frozen=True)
class PathAnalogySetting:
    name: str
    sequence_length: int
    filters1: int
    filters2: int
    dropout: float
    training_analogies_per_decision: int
    test_analogies: int
    voting_threshold: float


def path_analogy_setting(name: str) -> PathAnalogySetting:
    settings = {
        "dataset1": PathAnalogySetting("dataset1_config", 4, 16, 8, 0.0, 5, 5, 0.58),
        "dataset2": PathAnalogySetting("dataset2_config", 3, 4, 2, 0.3, 20, 20, 0.3),
    }
    try:
        return settings[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Path analogy setting: {name}") from exc


def read_property_lines(path: Path) -> tuple[str, ...]:
    with path.open(encoding="utf-8") as handle:
        return tuple(line.strip() for line in handle if line.strip())


def dataset_config(dataset: str, output_dir: Path, wikidata_lmdb: Path, embeddings_lmdb: Path) -> DatasetConfig:
    embeddings_manifest = embeddings_lmdb.with_name(embeddings_lmdb.name.replace("_lmdb", "_manifest.json"))
    if dataset == "1":
        name = "dataset1"
        return DatasetConfig(
            name=name,
            decisions=INPUTS / "dataset1_gold_decisions_filtered.csv",
            folds=FOLDS / "dataset1_5_folds.pkl",
            seed_qids=INPUTS / "dataset1_filtered.csv",
            wikidata_lmdb=wikidata_lmdb,
            embeddings_lmdb=embeddings_lmdb,
            embeddings_manifest=embeddings_manifest,
            output_dir=output_dir,
            hierarchy_only=True,
        )
    if dataset == "2":
        name = "dataset2"
        return DatasetConfig(
            name=name,
            decisions=INPUTS / "dataset2_gold_decisions_filtered.csv",
            folds=FOLDS / "dataset2_5_folds.pkl",
            seed_qids=INPUTS / "dataset2_filtered.csv",
            wikidata_lmdb=wikidata_lmdb,
            embeddings_lmdb=embeddings_lmdb,
            embeddings_manifest=embeddings_manifest,
            output_dir=output_dir,
            hierarchy_only=True,
        )
    if dataset == "3":
        name = "dataset3"
        property_file = WIKIDATA_DATA / "dataset3" / "properties_dataset3.csv"
        decisions = INPUTS / "dataset3_gold_decisions_bridge_filtered.csv"
        folds = FOLDS / "dataset3_overlap_reduced_5_folds.pkl"
        seed_qids = INPUTS / "dataset3_qids.csv"
        return DatasetConfig(
            name=name,
            decisions=decisions,
            folds=folds,
            seed_qids=seed_qids,
            wikidata_lmdb=wikidata_lmdb,
            embeddings_lmdb=embeddings_lmdb,
            embeddings_manifest=embeddings_manifest,
            output_dir=output_dir,
            hierarchy_only=False,
            expansion_properties=read_property_lines(property_file),
            allow_bridge_nodes=True,
        )
    raise ValueError(f"Unknown dataset: {dataset}")


def resolve_output_dir(output_dir: Path | None) -> Path:
    return output_dir or DEFAULT_OUTPUT_ROOT


def read_required_embedding_qids(config: DatasetConfig) -> set[str]:
    qids: set[str] = set()
    with config.decisions.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            for column in ("from", "QID"):
                qid = row.get(column, "").strip()
                if qid:
                    qids.add(qid)

    with config.seed_qids.open(encoding="utf-8") as handle:
        for line in handle:
            qid = line.strip()
            if qid:
                qids.add(qid)
    return qids


def embedding_key(qid: str, key_format: str) -> str:
    if key_format == "qid":
        return qid
    if key_format == "uri":
        return f"<http://www.wikidata.org/entity/{qid}>"
    raise ValueError(f"Unsupported key format: {key_format}")


def embedding_lmdb_missing_qids(
    embeddings_lmdb: Path,
    required_qids: set[str],
    key_format: str = "uri",
) -> list[str] | None:
    try:
        import lmdb
    except ImportError:
        return None

    env = lmdb.open(str(embeddings_lmdb), readonly=True, lock=False, readahead=False)
    try:
        with env.begin() as txn:
            return [
                qid
                for qid in sorted(required_qids)
                if txn.get(embedding_key(qid, key_format).encode("ascii")) is None
            ]
    finally:
        env.close()


def manifest_coverage_fields_valid(manifest: dict[str, object]) -> bool:
    found_qids = manifest.get("found_qids")
    key_format = manifest.get("key_format")
    return (
        isinstance(found_qids, list)
        and all(isinstance(qid, str) for qid in found_qids)
        and key_format in {"qid", "uri"}
        and manifest.get("found") == len(found_qids)
    )


def preflight(config: DatasetConfig, allow_non_hierarchy: bool = False) -> list[str]:
    issues: list[str] = []
    required_files = {
        "decisions CSV": config.decisions,
        "folds pickle": config.folds,
        "seed QID file": config.seed_qids,
    }
    for label, path in required_files.items():
        if not path.exists():
            issues.append(f"Missing {label}: {path}")
    required_inputs_present = all(path.exists() for path in required_files.values())
    if not config.wikidata_lmdb.is_dir():
        issues.append(f"Missing Wikidata LMDB: {config.wikidata_lmdb}")
    elif not (config.wikidata_lmdb / "data.mdb").exists():
        issues.append(f"Invalid Wikidata LMDB, missing data.mdb: {config.wikidata_lmdb}")
    if not config.embeddings_lmdb.is_dir():
        issues.append(f"Missing embeddings LMDB: {config.embeddings_lmdb}")
    elif not (config.embeddings_lmdb / "data.mdb").exists():
        issues.append(f"Invalid embeddings LMDB, missing data.mdb: {config.embeddings_lmdb}")
    elif config.embeddings_manifest.exists():
        manifest = json.loads(config.embeddings_manifest.read_text(encoding="utf-8"))
        if manifest.get("missing", 0):
            issues.append(
                f"Embedding manifest reports {manifest['missing']} missing QIDs: {config.embeddings_manifest}"
            )
        elif not manifest_coverage_fields_valid(manifest):
            issues.append(f"Embedding manifest is missing found_qids/key_format coverage fields: {config.embeddings_manifest}")
        elif required_inputs_present:
            missing_qids = embedding_lmdb_missing_qids(
                config.embeddings_lmdb,
                read_required_embedding_qids(config),
                str(manifest["key_format"]),
            )
            if missing_qids is None:
                issues.append("Cannot verify embeddings LMDB coverage because the lmdb package is not installed.")
            elif missing_qids:
                preview = ", ".join(missing_qids[:10])
                issues.append(
                    f"Embeddings LMDB is missing {len(missing_qids)} required QIDs "
                    f"(first {min(10, len(missing_qids))}: {preview}): {config.embeddings_lmdb}"
                )
    elif required_inputs_present:
        missing_qids = embedding_lmdb_missing_qids(config.embeddings_lmdb, read_required_embedding_qids(config))
        if missing_qids is None:
            issues.append("Cannot verify embeddings LMDB coverage because the lmdb package is not installed.")
        elif missing_qids:
            preview = ", ".join(missing_qids[:10])
            issues.append(
                f"Embeddings LMDB is missing {len(missing_qids)} required QIDs "
                f"(first {min(10, len(missing_qids))}: {preview}): {config.embeddings_lmdb}"
            )
    if not config.hierarchy_only and not config.expansion_properties and not allow_non_hierarchy:
        issues.append(
            f"{config.name} is not compatible with the unmodified original P31/P279 evaluator; "
            "use the KGPrune arbitrary-property route or pass --allow-non-hierarchy only for diagnostics."
        )
    return issues


def build_original_pipeline_commands(
    config: DatasetConfig,
    setting: PathAnalogySetting,
    python_executable: Path,
    random_seed: int = DEFAULT_RANDOM_SEED,
    row_decisions_dir: Path = DEFAULT_ROW_DECISIONS_DIR,
) -> list[list[str | Path]]:
    prefix = config.output_dir / f"{config.name}_{setting.name}"
    sequenced = prefix.with_name(prefix.name + "_sequenced_decisions.pkl")
    distances = prefix.with_name(prefix.name + "_distances_lmdb")
    predictions = prefix.with_name(prefix.name + "_predictions.pkl")
    validation_predictions = prefix.with_name(prefix.name + "_validation_predictions.pkl")
    analogy_stats = prefix.with_name(prefix.name + "_analogy_stats.csv")
    classifier_decisions = prefix.with_name(prefix.name + "_classifier_decisions.pkl")
    metrics = prefix.with_name(prefix.name + "_metrics.csv")
    seen_unseen_metrics = prefix.with_name(prefix.name + "_seen_unseen_metrics.csv")
    row_decisions = row_decisions_dir / f"{config.name}_path_analogy_{setting.name}.csv"
    expansion_args: list[str] = []
    if config.expansion_properties:
        expansion_args = ["--expansion-properties", *config.expansion_properties]
    bridge_args = ["--allow-bridge-nodes"] if config.allow_bridge_nodes else []

    commands = [
        [
            python_executable,
            AP_UTILS / "generate_sequenced_decisions.py",
            "--decisions",
            config.decisions,
            "--wikidata",
            config.wikidata_lmdb,
            "--embeddings",
            config.embeddings_lmdb,
            "--output",
            sequenced,
            *expansion_args,
            *bridge_args,
        ],
        [
            python_executable,
            AP_UTILS / "qids_distance_compute.py",
            "--qids",
            config.seed_qids,
            "--embeddings",
            config.embeddings_lmdb,
            "--lmdb-size",
            "1000000000",
            "--output",
            distances,
        ],
        [
            python_executable,
            AP_MODELS / "sequence_analogy_pruning.py",
            "--folds",
            config.folds,
            "--decisions",
            config.decisions,
            "--wikidata",
            config.wikidata_lmdb,
            "--embeddings",
            config.embeddings_lmdb,
            "--distances-hashmap",
            distances,
            "--knn",
            ".",
            "--nb-training-analogies-per-decision",
            str(setting.training_analogies_per_decision),
            "--nb-test-analogies",
            str(setting.test_analogies),
            "--nb-keeping-in-test",
            "20",
            "--nb-pruning-in-test",
            "20",
            "--sequenced-decisions",
            sequenced,
            "--sequence-length",
            str(setting.sequence_length),
            "--padding",
            "between",
            "--analogical-properties",
            ".",
            *expansion_args,
            "--valid-analogies-pattern",
            "kk",
            "--invalid-analogies-pattern",
            "kp",
            "--nb-filters1",
            str(setting.filters1),
            "--nb-filters2",
            str(setting.filters2),
            "--learning-rate",
            "0.001",
            "--dropout",
            str(setting.dropout),
            "--random-seed",
            str(random_seed),
            "--epochs",
            "50",
            "--predictions-output",
            predictions,
            "--validation-predictions-output",
            validation_predictions,
            "--stats-file",
            analogy_stats,
            *bridge_args,
        ],
        [
            python_executable,
            AP_MODELS / "analogy_voting.py",
            "--folds",
            config.folds,
            "--predictions",
            predictions,
            "--wikidata",
            config.wikidata_lmdb,
            "--embeddings",
            config.embeddings_lmdb,
            *expansion_args,
            "--valid-analogies-pattern",
            "kk",
            "--invalid-analogies-pattern",
            "kp",
            "--decisions",
            config.decisions,
            "--voting",
            "weighted",
            "--voting-threshold",
            str(setting.voting_threshold),
            "--output",
            classifier_decisions,
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


def reproducible_subprocess_environment(
    random_seed: int,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base_environment is None else base_environment)
    environment["PYTHONHASHSEED"] = str(random_seed)
    environment["TF_DETERMINISTIC_OPS"] = "1"
    return environment


def format_command(command: list[str | Path]) -> str:
    return " ".join(f'"{part}"' if " " in str(part) else str(part) for part in command)


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
    parser.add_argument("--allow-non-hierarchy", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    output_dir = resolve_output_dir(args.output_dir)
    config = dataset_config(args.dataset, output_dir, args.wikidata_lmdb, args.embeddings_lmdb)
    if args.folds:
        config.folds = args.folds
    setting = path_analogy_setting(args.setting)

    issues = preflight(config, allow_non_hierarchy=args.allow_non_hierarchy)
    if issues:
        print("Preflight failed:")
        for issue in issues:
            print(f"- {issue}")
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    commands = build_original_pipeline_commands(
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
