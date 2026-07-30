"""Create exact aligned-runtime decision inputs under ``tmp``.

The aligned row sets are already the canonical denominators used by the LLM
experiments. This script selects those exact ``row_id`` values from the checked-in
gold CSVs and validates every seed, candidate, and target against a canonical
row-complete BFS artifact. It deliberately reuses the checked-in five-fold seed
splits rather than generating or modifying folds during runtime preparation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pickle
from dataclasses import dataclass
from pathlib import Path

from bridge_traversal import reachable_decision_paths_from_records
from filter_decisions_by_pbg_names import (
    available_qids_from_manifest,
    load_lmdb_records,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REPRO_ROOT = Path(__file__).resolve().parent
WIKIDATA = REPO_ROOT / "wikidata"
DEFAULT_OUTPUT = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "inputs"
DEFAULT_NAMES_MANIFEST = (
    REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "e1_combined_names_manifest.json"
)
DEFAULT_WIKIDATA_ROOT = REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy"


@dataclass(frozen=True)
class DatasetInput:
    dataset: int
    source: Path
    canonical_rows: Path
    decisions_output: str
    qids_output: str
    summary_output: str
    folds: Path
    wikidata_lmdb: Path
    initial_properties: tuple[str, ...]
    next_properties: tuple[str, ...]
    expected_rows: int


def default_canonical_rows(dataset: int) -> Path:
    return (
        REPO_ROOT
        / "meta-prompting"
        / "results"
        / "gemini-3.5-flash"
        / f"dataset{dataset}"
        / "few_shot_manual"
        / "bfs_gated_predictions.csv"
    )


def dataset_inputs(
    canonical_paths: dict[int, Path],
    wikidata_lmdb_paths: dict[int, Path],
) -> tuple[DatasetInput, ...]:
    dataset3_properties = tuple(
        line.strip()
        for line in (WIKIDATA / "dataset3" / "properties_dataset3.csv")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    )
    return (
        DatasetInput(
            1,
            WIKIDATA / "dataset1" / "dataset1_gold_decisions.csv",
            canonical_paths[1],
            "dataset1_gold_decisions_filtered.csv",
            "dataset1_filtered.csv",
            "dataset1_filter_summary.json",
            REPRO_ROOT / "folds" / "dataset1_5_folds.pkl",
            wikidata_lmdb_paths[1],
            ("P31", "P279", "(-)P279"),
            ("(-)P279",),
            4_400,
        ),
        DatasetInput(
            2,
            WIKIDATA / "dataset2" / "dataset2_gold_decisions.csv",
            canonical_paths[2],
            "dataset2_gold_decisions_filtered.csv",
            "dataset2_filtered.csv",
            "dataset2_filter_summary.json",
            REPRO_ROOT / "folds" / "dataset2_5_folds.pkl",
            wikidata_lmdb_paths[2],
            ("P31", "P279", "(-)P279"),
            ("(-)P279",),
            868,
        ),
        DatasetInput(
            3,
            WIKIDATA / "dataset3" / "dataset3_gold_decisions.csv",
            canonical_paths[3],
            "dataset3_gold_decisions_bridge_filtered.csv",
            "dataset3_qids.csv",
            "dataset3_filter_summary.json",
            REPRO_ROOT / "folds" / "dataset3_overlap_reduced_5_folds.pkl",
            wikidata_lmdb_paths[3],
            dataset3_properties,
            dataset3_properties,
            1_384,
        ),
    )


def read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_rows_with_ids(path: Path) -> list[dict[str, str]]:
    rows, _ = read_rows(path)
    annotated: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        seed = row.get("from", "").strip()
        qid = row.get("QID", "").strip()
        if not seed or not qid or not row.get("target", "").strip():
            continue
        annotated.append(
            {
                "row_id": f"row{index}",
                "legacy_row_id": f"{seed}|{qid}",
                "QID": qid,
                "label": row.get("label", "").strip(),
                "from": seed,
                "starting label": row.get("starting label", "").strip(),
                "target": row["target"].strip(),
                "depth": row.get("depth", "").strip(),
            }
        )
    return annotated


def canonical_row_index(path: Path) -> dict[str, dict[str, str]]:
    rows, _ = read_rows(path)
    required = {"row_id", "seed_qid", "qid", "target"}
    if rows and not required.issubset(rows[0]):
        raise ValueError(f"Canonical aligned-row file lacks {sorted(required)}: {path}")
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        row_id = row.get("row_id", "").strip()
        if not row_id:
            raise ValueError(f"Blank row_id in canonical aligned-row file: {path}")
        if row_id in by_id:
            raise ValueError(f"Duplicate canonical row_id {row_id}: {path}")
        by_id[row_id] = row
    return by_id


def validate_fold_coverage(folds_path: Path, seeds: set[str]) -> dict[str, int]:
    with folds_path.open("rb") as handle:
        folds = pickle.load(handle)
    test_occurrences: dict[str, int] = {}
    for fold in folds.values():
        for seed in fold["test"]:
            test_occurrences[seed] = test_occurrences.get(seed, 0) + 1
    missing = sorted(seeds - set(test_occurrences))
    repeated = sorted(seed for seed in seeds if test_occurrences.get(seed) != 1)
    if missing or repeated:
        raise ValueError(
            f"Aligned seeds do not map exactly once to {folds_path}: "
            f"missing={missing[:10]}, repeated={repeated[:10]}"
        )
    return {
        "fold_seed_count": len(test_occurrences),
        "aligned_seed_count": len(seeds),
        "fold_only_seed_count": len(set(test_occurrences) - seeds),
    }


def prepare_dataset(
    config: DatasetInput,
    output_dir: Path,
    available_qids: set[str],
) -> dict[str, object]:
    source_rows = source_rows_with_ids(config.source)
    source_by_id = {row["row_id"]: row for row in source_rows}
    canonical_by_id = canonical_row_index(config.canonical_rows)

    missing = sorted(set(canonical_by_id) - set(source_by_id))
    if missing:
        raise ValueError(
            f"Dataset {config.dataset} has {len(missing)} canonical row IDs absent from its gold CSV: "
            f"{missing[:10]}"
        )

    candidates: list[dict[str, str]] = []
    for source_row in source_rows:
        reference = canonical_by_id.get(source_row["row_id"])
        if reference is None:
            continue
        expected = (
            reference.get("seed_qid", "").strip(),
            reference.get("qid", "").strip(),
            reference.get("target", "").strip(),
        )
        observed = (source_row["from"], source_row["QID"], source_row["target"])
        if observed != expected:
            raise ValueError(
                f"Canonical row mismatch for Dataset {config.dataset} {source_row['row_id']}: "
                f"gold={observed}, canonical={expected}"
            )
        # The aligned LLM input resolves English labels from the checked-in
        # local graph when the original decision CSV leaves a label blank.
        # Reuse that resolved text so the two methods receive the same usable
        # row rather than treating a CSV formatting gap as a new exclusion.
        if not source_row["starting label"]:
            source_row["starting label"] = (
                reference.get("seed_label", "").strip()
                or reference.get("path", "").split(" --", 1)[0].strip()
            )
        if not source_row["label"]:
            source_row["label"] = (
                reference.get("label", "").strip()
                or reference.get("path", "").rsplit("-->", 1)[-1].strip()
            )
        if config.dataset in (1, 2) and (
            not source_row["starting label"] or not source_row["label"]
        ):
            raise ValueError(
                f"Aligned row lacks an English seed/candidate label: {source_row['row_id']}"
            )
        candidates.append(source_row)

    records = load_lmdb_records(config.wikidata_lmdb)
    candidates_by_seed: dict[str, list[dict[str, str]]] = {}
    for row in candidates:
        candidates_by_seed.setdefault(row["from"], []).append(row)

    kept_pairs: set[tuple[str, str]] = set()
    ungated_pair_count = 0
    strict_keep_conflicts = 0
    for seed, seed_rows in candidates_by_seed.items():
        # A seed without a PBG embedding cannot initialize either reproduced
        # classifier, even if its outgoing neighbors have embeddings.
        if seed not in available_qids:
            continue
        decision_qids = {row["QID"] for row in seed_rows}
        decision_targets = {row["QID"]: row["target"] for row in seed_rows}
        ungated = {
            path.qid
            for path in reachable_decision_paths_from_records(
                get_record=records.get,
                seed_qid=seed,
                decision_qids=decision_qids,
                initial_properties=config.initial_properties,
                next_properties=config.next_properties,
                allowed_qids=available_qids,
            )
        }
        gold_gated = {
            path.qid
            for path in reachable_decision_paths_from_records(
                get_record=records.get,
                seed_qid=seed,
                decision_qids=decision_qids,
                initial_properties=config.initial_properties,
                next_properties=config.next_properties,
                decision_targets=decision_targets,
                keep_gated=True,
                allowed_qids=available_qids,
            )
        }
        ungated_pair_count += sum(row["QID"] in ungated for row in seed_rows)
        strict_keep_conflicts += sum(
            row["target"] == "1" and row["QID"] in ungated and row["QID"] not in gold_gated
            for row in seed_rows
        )
        for row in seed_rows:
            if row["QID"] in ungated and (row["target"] == "0" or row["QID"] in gold_gated):
                kept_pairs.add((seed, row["QID"]))

    selected = [row for row in candidates if (row["from"], row["QID"]) in kept_pairs]

    if len(selected) != config.expected_rows:
        raise ValueError(
            f"Dataset {config.dataset} expected {config.expected_rows} aligned rows, "
            f"found {len(selected)} in {config.canonical_rows}"
        )

    decisions_path = output_dir / config.decisions_output
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "row_id",
        "legacy_row_id",
        "QID",
        "label",
        "from",
        "starting label",
        "target",
        "depth",
    ]
    with decisions_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)

    seeds = sorted({row["from"] for row in selected})
    qids_path = output_dir / config.qids_output
    qids_path.write_text("".join(f"{seed}\n" for seed in seeds), encoding="utf-8")
    fold_summary = validate_fold_coverage(config.folds, set(seeds))

    target_counts = {
        "keep": sum(row["target"] == "1" for row in selected),
        "prune": sum(row["target"] == "0" for row in selected),
    }
    summary: dict[str, object] = {
        "dataset": config.dataset,
        "source_decisions": str(config.source),
        "canonical_aligned_rows": str(config.canonical_rows),
        "canonical_sha256": file_sha256(config.canonical_rows),
        "output_decisions": str(decisions_path),
        "folds": str(config.folds),
        "seed_qids": str(qids_path),
        "source_rows": len(source_rows),
        "canonical_rows": len(candidates),
        "pbg_ungated_reachable_rows": ungated_pair_count,
        "strict_prune_ancestor_keep_descendant_rows": strict_keep_conflicts,
        "output_rows": len(selected),
        "output_seeds": len(seeds),
        "target_counts": target_counts,
        **fold_summary,
    }
    (output_dir / config.summary_output).write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def prepare_inputs(
    output_dir: Path,
    canonical_paths: dict[int, Path],
    names_manifest: Path,
    wikidata_lmdb_paths: dict[int, Path],
) -> list[dict[str, object]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(names_manifest.read_text(encoding="utf-8"))
    available_qids = available_qids_from_manifest(manifest)
    return [
        prepare_dataset(config, output_dir, available_qids)
        for config in dataset_inputs(canonical_paths, wikidata_lmdb_paths)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--names-manifest", type=Path, default=DEFAULT_NAMES_MANIFEST)
    parser.add_argument(
        "--dataset1-wikidata-lmdb",
        type=Path,
        default=DEFAULT_WIKIDATA_ROOT / "dataset1" / "wikidata_lmdb",
    )
    parser.add_argument(
        "--dataset2-wikidata-lmdb",
        type=Path,
        default=DEFAULT_WIKIDATA_ROOT / "dataset2" / "wikidata_lmdb",
    )
    parser.add_argument(
        "--dataset3-wikidata-lmdb",
        type=Path,
        default=DEFAULT_WIKIDATA_ROOT / "dataset3" / "wikidata_lmdb_ap",
    )
    for dataset in (1, 2, 3):
        parser.add_argument(
            f"--dataset{dataset}-canonical-rows",
            type=Path,
            default=default_canonical_rows(dataset),
        )
    args = parser.parse_args()

    summaries = prepare_inputs(
        args.output_dir,
        {
            1: args.dataset1_canonical_rows,
            2: args.dataset2_canonical_rows,
            3: args.dataset3_canonical_rows,
        },
        args.names_manifest,
        {
            1: args.dataset1_wikidata_lmdb,
            2: args.dataset2_wikidata_lmdb,
            3: args.dataset3_wikidata_lmdb,
        },
    )
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
