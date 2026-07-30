"""Filter decision CSVs to rows whose seed and target QIDs have PBG names."""

from __future__ import annotations

import argparse
import csv
import json
import pickle
import random
import sys
from pathlib import Path
from typing import Iterable

sys.path.append(str(Path(__file__).resolve().parent))
from bridge_traversal import reachable_decision_paths_from_records


def read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def write_csv_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_qid_lines(path: Path, qids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for qid in qids:
            handle.write(f"{qid}\n")


def deterministic_folds(seeds: list[str], n_splits: int, seed: int) -> dict[int, dict[str, list[str]]]:
    shuffled = list(seeds)
    random.Random(seed).shuffle(shuffled)

    fold_sizes = [len(shuffled) // n_splits] * n_splits
    for index in range(len(shuffled) % n_splits):
        fold_sizes[index] += 1

    folds: dict[int, dict[str, list[str]]] = {}
    start = 0
    all_seeds = set(shuffled)
    for fold_index, fold_size in enumerate(fold_sizes):
        test = shuffled[start : start + fold_size]
        folds[fold_index] = {
            "train": [qid for qid in shuffled if qid in all_seeds - set(test)],
            "test": test,
        }
        start += fold_size
    return folds


def available_qids_from_manifest(manifest: dict[str, object]) -> set[str]:
    hits = manifest.get("hits", {})
    if not isinstance(hits, dict):
        raise ValueError("PBG names manifest must contain a 'hits' object")
    return set(hits)


def filter_rows_by_available_qids(
    rows: list[dict[str, str]],
    available_qids: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    kept: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    for row in rows:
        seed_qid = row.get("from", "").strip()
        decision_qid = row.get("QID", "").strip()
        if seed_qid in available_qids and decision_qid in available_qids:
            kept.append(row)
        else:
            removed.append(row)
    return kept, removed


def relation_objects(values: Iterable[object]) -> set[str]:
    objects: set[str] = set()
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            objects.add(value["value"])
        elif isinstance(value, str):
            objects.add(value)
    return objects


def reachable_targets(record: dict[str, object] | None, properties: Iterable[str], allowed_qids: set[str]) -> set[str]:
    if not record:
        return set()
    claims = record.get("claims", {})
    if not isinstance(claims, dict):
        return set()
    targets: set[str] = set()
    for prop in properties:
        values = claims.get(prop, [])
        if isinstance(values, list):
            targets |= relation_objects(values)
    return targets & allowed_qids


def clean_rows_by_available_paths(
    rows: list[dict[str, str]],
    records: dict[str, dict[str, object]],
    available_qids: set[str],
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows_by_pair: dict[tuple[str, str], list[dict[str, str]]] = {}
    rows_by_seed: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        seed_qid = row.get("from", "").strip()
        decision_qid = row.get("QID", "").strip()
        rows_by_seed.setdefault(seed_qid, []).append(row)
        rows_by_pair.setdefault((seed_qid, decision_qid), []).append(row)

    kept_rows: list[dict[str, str]] = []
    kept_pairs: set[tuple[str, str]] = set()

    for seed_qid in sorted(rows_by_seed):
        if seed_qid not in available_qids:
            continue
        decision_qids = {row.get("QID", "").strip() for row in rows_by_seed[seed_qid]}
        decision_targets = {
            row.get("QID", "").strip(): row.get("target", "").strip()
            for row in rows_by_seed[seed_qid]
        }
        decision_paths = reachable_decision_paths_from_records(
            get_record=records.get,
            seed_qid=seed_qid,
            decision_qids=decision_qids,
            initial_properties=initial_properties,
            next_properties=next_properties,
            decision_targets=decision_targets,
            keep_gated=True,
            allowed_qids=available_qids,
        )
        for decision_path in decision_paths:
            pair = (seed_qid, decision_path.qid)
            for source_row in rows_by_pair.get(pair, []):
                kept_rows.append(dict(source_row))
            kept_pairs.add(pair)

    removed_rows = [
        row
        for row in rows
        if (row.get("from", "").strip(), row.get("QID", "").strip()) not in kept_pairs
    ]
    return kept_rows, removed_rows


def load_lmdb_records(path: Path) -> dict[str, dict[str, object]]:
    try:
        import lmdb
    except ImportError as exc:
        raise SystemExit(
            "The 'lmdb' package is required for path-aware filtering. "
            "Install it in the active environment with: python -m pip install lmdb"
        ) from exc

    records: dict[str, dict[str, object]] = {}
    env = lmdb.open(str(path), readonly=True, readahead=False, lock=False)
    try:
        with env.begin() as txn:
            cursor = txn.cursor()
            for key, value in cursor:
                records[key.decode("ascii")] = pickle.loads(value)
    finally:
        env.close()
    return records


def target_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts = {"keep": 0, "prune": 0}
    for row in rows:
        if row.get("target", "").strip() == "1":
            counts["keep"] += 1
        else:
            counts["prune"] += 1
    return counts


def ordered_seed_qids(rows: list[dict[str, str]]) -> list[str]:
    return sorted({row["from"].strip() for row in rows if row.get("from", "").strip()})


def write_filtered_artifacts(
    decisions: Path,
    names_manifest: Path,
    output: Path,
    folds_output: Path,
    qids_output: Path,
    summary_output: Path,
    fold_seed: int,
    n_splits: int = 5,
    wikidata_lmdb: Path | None = None,
    expansion_properties: tuple[str, ...] | None = None,
) -> dict[str, object]:
    rows, fieldnames = read_csv_rows(decisions)
    manifest = json.loads(names_manifest.read_text(encoding="utf-8"))
    available_qids = available_qids_from_manifest(manifest)
    if wikidata_lmdb:
        properties = expansion_properties or ("P31", "P279", "(-)P279")
        next_properties = expansion_properties or ("(-)P279",)
        kept_rows, removed_rows = clean_rows_by_available_paths(
            rows,
            load_lmdb_records(wikidata_lmdb),
            available_qids,
            initial_properties=tuple(properties),
            next_properties=tuple(next_properties),
        )
    else:
        kept_rows, removed_rows = filter_rows_by_available_qids(rows, available_qids)
    seeds = ordered_seed_qids(kept_rows)

    write_csv_rows(output, kept_rows, fieldnames)
    write_qid_lines(qids_output, seeds)
    folds_output.parent.mkdir(parents=True, exist_ok=True)
    with folds_output.open("wb") as handle:
        pickle.dump(deterministic_folds(seeds, n_splits=n_splits, seed=fold_seed), handle)

    removed_qids = sorted(
        {
            qid
            for row in removed_rows
            for qid in (row.get("from", "").strip(), row.get("QID", "").strip())
            if qid and qid not in available_qids
        }
    )
    summary = {
        "input_decisions": str(decisions),
        "names_manifest": str(names_manifest),
        "output_decisions": str(output),
        "folds": str(folds_output),
        "seed_qids": str(qids_output),
        "fold_seed": fold_seed,
        "path_aware_cleaning": wikidata_lmdb is not None,
        "wikidata_lmdb": str(wikidata_lmdb) if wikidata_lmdb else None,
        "input_rows": len(rows),
        "output_rows": len(kept_rows),
        "removed_rows": len(removed_rows),
        "input_seeds": len(ordered_seed_qids(rows)),
        "output_seeds": len(seeds),
        "removed_missing_qids": len(removed_qids),
        "removed_missing_qid_sample": removed_qids[:20],
        "target_counts": target_counts(kept_rows),
    }
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--names-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--folds-output", type=Path, required=True)
    parser.add_argument("--qids-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--fold-seed", type=int, default=20260509)
    parser.add_argument("--n-splits", type=int, default=5)
    parser.add_argument("--wikidata-lmdb", type=Path)
    parser.add_argument("--expansion-properties", nargs="+")
    args = parser.parse_args()

    summary = write_filtered_artifacts(
        args.decisions,
        args.names_manifest,
        args.output,
        args.folds_output,
        args.qids_output,
        args.summary_output,
        fold_seed=args.fold_seed,
        n_splits=args.n_splits,
        wikidata_lmdb=args.wikidata_lmdb,
        expansion_properties=tuple(args.expansion_properties) if args.expansion_properties else None,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["output_rows"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
