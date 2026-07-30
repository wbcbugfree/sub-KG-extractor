"""
Copyright (C) 2023 Orange
Authors: Lucas Jarnac, Miguel Couceiro, and Pierre Monnin

This software is distributed under the terms and conditions of the 'MIT'
license which can be found in the file 'LICENSE.txt' in this package distribution
or at 'https://opensource.org/license/mit/'.

Compute row-count-weighted five-fold and pooled metrics, and persist one
prediction for every evaluation row.

Classifier output contains only decisions reached by prediction-gated BFS. Rows
below a predicted PRUNE node are therefore absent from that output. For the
benchmark's BFS semantics those rows have an effective PRUNE prediction, so this
scorer materializes them as ``bfs_auto_prune`` rows instead of silently omitting
their true negatives from the metric numerator.
"""

from __future__ import annotations

import argparse
import logging
import pickle
import time
from pathlib import Path

import numpy
import pandas

import utils
from TqdmLoggingHandler import TqdmLoggingHandler


METRICS = ("precision", "recall", "f1", "accuracy")
ROW_OUTPUT_COLUMNS = (
    "row_id",
    "legacy_row_id",
    "fold",
    "from",
    "starting label",
    "QID",
    "label",
    "depth",
    "target",
    "prediction",
    "correct",
    "reached",
    "decision_source",
    "model_depth",
)


def safe_ratio(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def text_value(value: object) -> str:
    return "" if pandas.isna(value) else str(value)


def confusion_counts_for_rows(rows: pandas.DataFrame) -> dict[str, int]:
    targets = rows["target"].astype(int)
    predictions = rows["prediction"].astype(int)
    return {
        "true positives": int(((targets == 1) & (predictions == 1)).sum()),
        "false positives": int(((targets == 0) & (predictions == 1)).sum()),
        "false negatives": int(((targets == 1) & (predictions == 0)).sum()),
        "true negatives": int(((targets == 0) & (predictions == 0)).sum()),
    }


def metrics_for_rows(rows: pandas.DataFrame) -> dict[str, float]:
    counts = confusion_counts_for_rows(rows)
    true_positive = counts["true positives"]
    false_positive = counts["false positives"]
    false_negative = counts["false negatives"]
    true_negative = counts["true negatives"]

    precision = safe_ratio(true_positive, true_positive + false_positive)
    recall = safe_ratio(true_positive, true_positive + false_negative)
    f1 = safe_ratio(2 * precision * recall, precision + recall)
    accuracy = safe_ratio(true_positive + true_negative, len(rows))
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
    }


def metric_summary_for_rows(
    row_decisions: pandas.DataFrame,
    prefix: str = "",
) -> dict[str, float | int]:
    """Return weighted-fold and pooled views over row-complete decisions.

    Each fold metric is weighted by the number of evaluation rows assigned to
    that fold. The reported standard deviation is the corresponding weighted
    population standard deviation. Pooled metrics are computed once from the
    confusion counts summed across all rows.
    """

    fold_groups = {
        int(fold): fold_rows
        for fold, fold_rows in row_decisions.groupby("fold", sort=True)
    }
    if not fold_groups:
        raise ValueError("No evaluation rows were assigned to a fold")

    fold_metrics = {
        fold: metrics_for_rows(fold_rows)
        for fold, fold_rows in fold_groups.items()
    }
    fold_weights = {fold: len(fold_rows) for fold, fold_rows in fold_groups.items()}
    total_weight = sum(fold_weights.values())

    summary: dict[str, float | int] = {}
    for metric in METRICS:
        weighted_mean = sum(
            fold_weights[fold] * fold_metrics[fold][metric]
            for fold in fold_groups
        ) / total_weight
        weighted_variance = sum(
            fold_weights[fold]
            * (fold_metrics[fold][metric] - weighted_mean) ** 2
            for fold in fold_groups
        ) / total_weight
        summary[f"{prefix}weighted {metric}"] = float(weighted_mean)
        summary[f"{prefix}weighted std {metric}"] = float(numpy.sqrt(weighted_variance))

    pooled_metrics = metrics_for_rows(row_decisions)
    if not numpy.isclose(
        summary[f"{prefix}weighted accuracy"],
        pooled_metrics["accuracy"],
        rtol=0.0,
        atol=1e-12,
    ):
        raise AssertionError(
            "Row-count-weighted fold accuracy must equal pooled accuracy"
        )
    for metric in METRICS:
        summary[f"{prefix}pooled {metric}"] = pooled_metrics[metric]
    for name, count in confusion_counts_for_rows(row_decisions).items():
        summary[f"{prefix}pooled {name}"] = count

    summary[f"{prefix}folds"] = len(fold_groups)
    summary[f"{prefix}rows"] = len(row_decisions)
    summary[f"{prefix}reached rows"] = int(row_decisions["reached"].sum())
    summary[f"{prefix}auto-pruned rows"] = int((~row_decisions["reached"]).sum())
    return summary


def fold_metric_summary(row_decisions: pandas.DataFrame) -> pandas.DataFrame:
    return pandas.DataFrame([metric_summary_for_rows(row_decisions)])


def seed_fold_mapping(classifier_decisions: dict) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for raw_fold, seed_decisions in classifier_decisions.items():
        fold = int(raw_fold)
        for raw_seed in seed_decisions:
            seed = str(raw_seed)
            previous = mapping.setdefault(seed, fold)
            if previous != fold:
                raise ValueError(f"Seed {seed} appears in test folds {previous} and {fold}")
    return mapping


def build_row_decisions(
    gold_decisions: pandas.DataFrame,
    classifier_decisions: dict,
) -> pandas.DataFrame:
    gold = gold_decisions.copy()
    gold.drop(gold[gold["from"] == gold["QID"]].index, inplace=True)
    gold.reset_index(drop=False, inplace=True)
    fold_by_seed = seed_fold_mapping(classifier_decisions)

    missing_seeds = sorted(set(gold["from"].astype(str)) - set(fold_by_seed))
    if missing_seeds:
        preview = ", ".join(missing_seeds[:10])
        raise ValueError(
            f"{len(missing_seeds)} gold-decision seeds have no test-fold output "
            f"(first {min(10, len(missing_seeds))}: {preview})"
        )

    rows: list[dict[str, object]] = []
    for _, gold_row in gold.iterrows():
        seed = str(gold_row["from"])
        qid = str(gold_row["QID"])
        fold = fold_by_seed[seed]
        classifier_row = classifier_decisions.get(fold, {}).get(seed, {}).get(qid)
        reached = classifier_row is not None
        prediction = int(classifier_row["decision"]) if reached else 0
        target = int(gold_row["target"])
        source_index = int(gold_row["index"])
        row_id = text_value(gold_row.get("row_id", "")) or f"row{source_index}"
        rows.append(
            {
                "row_id": row_id,
                "legacy_row_id": text_value(gold_row.get("legacy_row_id", ""))
                or f"{seed}|{qid}",
                "fold": fold,
                "from": seed,
                "starting label": text_value(gold_row.get("starting label", "")),
                "QID": qid,
                "label": text_value(gold_row.get("label", "")),
                "depth": text_value(gold_row.get("depth", "")),
                "target": target,
                "prediction": prediction,
                "correct": int(prediction == target),
                "reached": reached,
                "decision_source": "model" if reached else "bfs_auto_prune",
                "model_depth": classifier_row.get("depth", "") if reached else "",
            }
        )

    row_decisions = pandas.DataFrame(rows, columns=ROW_OUTPUT_COLUMNS)
    if row_decisions["row_id"].duplicated().any():
        duplicates = row_decisions.loc[
            row_decisions["row_id"].duplicated(keep=False), "row_id"
        ].tolist()
        raise ValueError(f"Duplicate row_id values in gold decisions: {duplicates[:10]}")
    return row_decisions


def main() -> None:
    start = time.time()
    parser = argparse.ArgumentParser(
        prog="decision_statistics",
        description="Compute statistics and effective BFS decisions by evaluation row",
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
    parser.add_argument(
        "--row-output",
        dest="row_output",
        help="Optional CSV output containing the effective prediction for every gold row",
    )
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
    row_decisions = build_row_decisions(gold_decisions, classifier_decisions)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fold_metric_summary(row_decisions).to_csv(output, header=True, index=False)

    if args.row_output:
        row_output = Path(args.row_output)
        row_output.parent.mkdir(parents=True, exist_ok=True)
        row_decisions.to_csv(row_output, header=True, index=False)

    logger.info(
        "Scored %d rows (%d reached, %d BFS-auto-pruned)",
        len(row_decisions),
        int(row_decisions["reached"].sum()),
        int((~row_decisions["reached"]).sum()),
    )
    logger.info(f"Execution time = {utils.convert(time.time() - start)}")


if __name__ == "__main__":
    main()
