"""
Copyright (C) 2023 Orange
Authors: Lucas Jarnac, Miguel Couceiro, and Pierre Monnin

This software is distributed under the terms and conditions of the 'MIT'
license which can be found in the file 'LICENSE.txt' in this package distribution
or at 'https://opensource.org/license/mit/'.

Compute seen/unseen metrics over the same row-complete BFS decisions used by
``decision_statistics.py``.
"""

from __future__ import annotations

import argparse
import logging
import pickle
import time
from pathlib import Path

import pandas

import utils
from decision_statistics import build_row_decisions, metric_summary_for_rows
from TqdmLoggingHandler import TqdmLoggingHandler


def main() -> None:
    start = time.time()
    parser = argparse.ArgumentParser(
        prog="decision_statistics_seen_unseen",
        description="Compute row-complete seen and unseen decision statistics",
    )
    parser.add_argument(
        "--classifier-decisions",
        dest="classifier_decisions",
        help="Pickle file containing decisions output by a classifier",
        required=True,
    )
    parser.add_argument(
        "--gold-decisions", dest="gold_decisions", help="CSV decision file", required=True
    )
    parser.add_argument("--output", dest="output", help="Output CSV file with statistics", required=True)
    parser.add_argument("--folds", dest="folds_path", help="File containing folds", required=True)
    # Retained for command-line compatibility with the original scorer. Row
    # membership is already fixed by the aligned input, so no graph replay is
    # required here.
    parser.add_argument("--wikidata", dest="wikidata_hashmap")
    parser.add_argument("--expansion-properties", dest="expansion_properties", nargs="+")
    args = parser.parse_args()

    logger = logging.getLogger()
    tqdm_logging_handler = TqdmLoggingHandler()
    tqdm_logging_handler.setFormatter(
        logging.Formatter(fmt="[%(asctime)s][%(levelname)s] %(message)s")
    )
    logger.addHandler(tqdm_logging_handler)
    logger.setLevel(logging.INFO)

    gold_decisions = pandas.read_csv(args.gold_decisions)
    with open(args.classifier_decisions, "rb") as handle:
        classifier_decisions = pickle.load(handle)
    with open(args.folds_path, "rb") as handle:
        folds = pickle.load(handle)

    row_decisions = build_row_decisions(gold_decisions, classifier_decisions)
    seen_parts: list[pandas.DataFrame] = []
    unseen_parts: list[pandas.DataFrame] = []

    for fold in sorted(int(value) for value in classifier_decisions):
        validation_fold = (fold + 1) % len(folds)
        validation_seeds = set(folds[validation_fold]["test"])
        train_seeds = set(folds[fold]["train"]) - validation_seeds
        train_rows = gold_decisions[gold_decisions["from"].isin(train_seeds)]
        seen_qids = set(train_rows["from"].astype(str)) | set(train_rows["QID"].astype(str))

        fold_rows = row_decisions[row_decisions["fold"] == fold].copy()
        fold_rows["seen"] = fold_rows["QID"].astype(str).isin(seen_qids)
        seen_rows = fold_rows[fold_rows["seen"]]
        unseen_rows = fold_rows[~fold_rows["seen"]]
        if seen_rows.empty or unseen_rows.empty:
            raise ValueError(
                f"Fold {fold} has an empty seen/unseen partition: "
                f"seen={len(seen_rows)}, unseen={len(unseen_rows)}"
            )

        seen_parts.append(seen_rows)
        unseen_parts.append(unseen_rows)

    seen_decisions = pandas.concat(seen_parts, ignore_index=True)
    unseen_decisions = pandas.concat(unseen_parts, ignore_index=True)
    seen_count = len(seen_decisions)
    unseen_count = len(unseen_decisions)

    stats: dict[str, float | int] = {}
    stats.update(metric_summary_for_rows(row_decisions))
    stats.update(metric_summary_for_rows(unseen_decisions, "unseen "))
    stats.update(metric_summary_for_rows(seen_decisions, "seen "))
    stats["seen percent"] = 100.0 * seen_count / (seen_count + unseen_count)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pandas.DataFrame([stats]).to_csv(output, header=True, index=False)
    logger.info(
        "Partitioned %d rows into %d seen and %d unseen decisions",
        seen_count + unseen_count,
        seen_count,
        unseen_count,
    )
    logger.info(f"Execution time = {utils.convert(time.time() - start)}")


if __name__ == "__main__":
    main()
