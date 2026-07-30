"""Compute Table 2-style reached-entity seen percentages for each fold."""

from __future__ import annotations

import argparse
import csv
import pickle
import sys
from pathlib import Path

import lmdb
import pandas


REPO_ROOT = Path(__file__).resolve().parents[1]
REPRO_ROOT = Path(__file__).resolve().parent
AP_UTILS = Path(__file__).resolve().parent / "src" / "utils"
sys.path.append(str(REPRO_ROOT))
sys.path.append(str(AP_UTILS))

import expansion_properties
import utils
from bridge_traversal import reachable_decision_paths_from_records


def reached_entities_for_seed(
    qid: str,
    gold_decisions: pandas.DataFrame,
    wikidata_hashmap,
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
) -> set[str]:
    q_gold = set(gold_decisions[gold_decisions["from"] == qid]["QID"])
    if not q_gold:
        return set()
    paths = reachable_decision_paths_from_records(
        get_record=lambda item: utils.get_hashmap_content(item, wikidata_hashmap),
        seed_qid=qid,
        decision_qids=q_gold,
        initial_properties=initial_properties,
        next_properties=next_properties,
    )
    return {path.qid for path in paths}


def compute_percentages(
    gold_decisions_path: Path,
    folds_path: Path,
    wikidata_lmdb: Path,
    expansion_property_values: list[str] | None,
) -> list[dict[str, object]]:
    gold_decisions = pandas.read_csv(gold_decisions_path)
    gold_decisions.drop(gold_decisions[gold_decisions["from"] == gold_decisions["QID"]].index, inplace=True)
    folds = pickle.load(open(folds_path, "rb"))
    initial_properties = expansion_properties.initial_properties(expansion_property_values)
    next_properties = expansion_properties.next_properties(expansion_property_values)

    dump = lmdb.open(str(wikidata_lmdb), readonly=True, readahead=False, lock=False)
    try:
        wikidata_hashmap = dump.begin()
        rows: list[dict[str, object]] = []
        for fold in sorted(folds):
            val_set = set(folds[(fold + 1) % 5]["test"])
            train_set = set(folds[fold]["train"]) - val_set
            train_seen_entities = set(gold_decisions[gold_decisions["from"].isin(train_set)]["QID"])
            train_seen_entities |= set(gold_decisions[gold_decisions["from"].isin(train_set)]["from"])

            seen = 0
            unseen = 0
            for qid in folds[fold]["test"]:
                reached = reached_entities_for_seed(
                    qid,
                    gold_decisions,
                    wikidata_hashmap,
                    initial_properties,
                    next_properties,
                )
                seen += len(reached & train_seen_entities)
                unseen += len(reached - train_seen_entities)

            total = seen + unseen
            rows.append(
                {
                    "fold": fold,
                    "seen": seen,
                    "unseen": unseen,
                    "total": total,
                    "seen_percent": round((seen / total) * 100, 4) if total else 0.0,
                }
            )
        return rows
    finally:
        dump.close()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["fold", "seen", "unseen", "total", "seen_percent"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-decisions", type=Path, required=True)
    parser.add_argument("--folds", type=Path, required=True)
    parser.add_argument("--wikidata", type=Path, required=True)
    parser.add_argument("--expansion-properties", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = compute_percentages(args.gold_decisions, args.folds, args.wikidata, args.expansion_properties)
    write_csv(args.output, rows)
    for row in rows:
        print(
            f"Fold {row['fold']}: {row['seen_percent']:.4f}% "
            f"({row['seen']} seen / {row['total']} reached)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
