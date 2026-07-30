"""Validation-only universal-threshold selection for Dataset 3 baselines."""

from __future__ import annotations

import pickle
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import lmdb
import numpy
import pandas


REPRO_ROOT = Path(__file__).resolve().parent
sys.path.append(str(REPRO_ROOT / "src" / "utils"))

from bridge_traversal import (  # noqa: E402
    embedding_qids_from_transaction,
    reachable_decision_paths_for_mode,
)
from decision_statistics import (  # noqa: E402
    build_row_decisions,
    metric_summary_for_rows,
    metrics_for_rows,
)
import utils  # noqa: E402


METRIC_NAMES = ("precision", "recall", "f1", "accuracy")


@dataclass(frozen=True)
class PredictionCandidate:
    name: str
    test_predictions: Path
    validation_predictions: Path


def fold_predictions(predictions: dict, fold: int) -> dict:
    return predictions.get(fold, predictions.get(str(fold), {}))


def path_analogy_score(prediction: object) -> float:
    if not isinstance(prediction, dict) or "keeping" not in prediction:
        return 0.0
    return float(numpy.asarray(prediction["keeping"]).mean())


def lstm_score(prediction: object) -> float:
    return float(numpy.asarray(prediction).mean())


def metric_percentages(rows: pandas.DataFrame) -> dict[str, float]:
    metrics = metrics_for_rows(rows)
    return {name: round(100.0 * float(metrics[name]), 4) for name in METRIC_NAMES}


def summary_metric_percentages(summary: dict[str, float | int]) -> dict[str, dict[str, float]]:
    return {
        "row_count_weighted_fold": {
            name: round(100.0 * float(summary[f"weighted {name}"]), 4)
            for name in METRIC_NAMES
        },
        "row_count_weighted_fold_std": {
            name: round(100.0 * float(summary[f"weighted std {name}"]), 4)
            for name in METRIC_NAMES
        },
        "pooled_predictions": {
            name: round(100.0 * float(summary[f"pooled {name}"]), 4)
            for name in METRIC_NAMES
        },
    }


def classifier_decisions_for_fold(
    *,
    fold: int,
    seed_qids: set[str],
    predictions: dict,
    threshold: float,
    score_prediction: Callable[[object], float],
    decisions: pandas.DataFrame,
    wikidata_hashmap,
    embedding_hashmap,
    available_embedding_qids: set[str] | None,
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
    allow_bridge_nodes: bool,
) -> dict[int, dict[str, dict[str, dict[str, int]]]]:
    fold_output: dict[str, dict[str, dict[str, int]]] = {}
    predictions_for_fold = fold_predictions(predictions, fold)

    for qid in sorted(seed_qids):
        fold_output[qid] = {}
        qid_embedding = utils.get_hashmap_content(
            utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap
        )
        if qid_embedding is None:
            continue

        seed_predictions = predictions_for_fold.get(qid, {})
        predicted_targets = {
            str(candidate_qid): "1" if score_prediction(prediction) >= threshold else "0"
            for candidate_qid, prediction in seed_predictions.items()
        }
        decision_qids = set(decisions.loc[decisions["from"] == qid, "QID"].astype(str))
        decision_paths = reachable_decision_paths_for_mode(
            get_record=lambda node: utils.get_hashmap_content(node, wikidata_hashmap),
            seed_qid=qid,
            decision_qids=decision_qids,
            initial_properties=initial_properties,
            next_properties=next_properties,
            allow_bridge_nodes=allow_bridge_nodes,
            available_embedding_qids=available_embedding_qids,
            decision_targets=predicted_targets,
            keep_gated=True,
        )
        for decision_path in decision_paths:
            if decision_path.qid not in predicted_targets:
                continue
            fold_output[qid][decision_path.qid] = {
                "depth": decision_path.depth,
                "decision": int(predicted_targets[decision_path.qid]),
            }

    return {fold: fold_output}


def run_fixed_configuration_universal_threshold_selection(
    *,
    candidate: PredictionCandidate,
    thresholds: list[float],
    score_prediction: Callable[[object], float],
    folds_path: Path,
    decisions_path: Path,
    wikidata_lmdb: Path,
    embeddings_lmdb: Path,
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
    allow_bridge_nodes: bool,
    classifier_output: Path,
    metrics_output: Path,
    row_output: Path,
) -> dict[str, object]:
    if not thresholds:
        raise ValueError("At least one voting threshold is required")

    with folds_path.open("rb") as handle:
        folds = pickle.load(handle)
    decisions = pandas.read_csv(decisions_path)
    decisions["from"] = decisions["from"].astype(str)
    decisions["QID"] = decisions["QID"].astype(str)

    with candidate.test_predictions.open("rb") as handle:
        test_predictions = pickle.load(handle)
    with candidate.validation_predictions.open("rb") as handle:
        validation_predictions = pickle.load(handle)

    wikidata_env = lmdb.open(str(wikidata_lmdb), readonly=True, lock=False, readahead=False)
    embeddings_env = lmdb.open(str(embeddings_lmdb), readonly=True, lock=False, readahead=False)
    try:
        wikidata_hashmap = wikidata_env.begin()
        embedding_hashmap = embeddings_env.begin()
        available_embedding_qids = (
            embedding_qids_from_transaction(embedding_hashmap) if allow_bridge_nodes else None
        )

        trial_records: list[
            tuple[float, dict[str, float | int], list[dict[str, object]]]
        ] = []
        for threshold in thresholds:
            validation_row_parts: list[pandas.DataFrame] = []
            validation_fold_metrics: list[dict[str, object]] = []
            for raw_fold in sorted(folds, key=int):
                outer_fold = int(raw_fold)
                validation_fold = (outer_fold + 1) % len(folds)
                validation_seeds = set(
                    str(seed) for seed in folds[validation_fold]["test"]
                )
                validation_gold = decisions[
                    decisions["from"].isin(validation_seeds)
                ].copy()
                classifier_decisions = classifier_decisions_for_fold(
                    fold=outer_fold,
                    seed_qids=validation_seeds,
                    predictions=validation_predictions,
                    threshold=threshold,
                    score_prediction=score_prediction,
                    decisions=decisions,
                    wikidata_hashmap=wikidata_hashmap,
                    embedding_hashmap=embedding_hashmap,
                    available_embedding_qids=available_embedding_qids,
                    initial_properties=initial_properties,
                    next_properties=next_properties,
                    allow_bridge_nodes=allow_bridge_nodes,
                )
                validation_rows = build_row_decisions(
                    validation_gold,
                    classifier_decisions,
                )
                # The prediction artifact is indexed by the outer model fold, but
                # these rows belong to the held-out validation fold for aggregation.
                validation_rows["fold"] = validation_fold
                validation_row_parts.append(validation_rows)
                validation_fold_metrics.append(
                    {
                        "outer_test_fold": outer_fold,
                        "validation_fold": validation_fold,
                        "configuration": candidate.name,
                        "threshold": float(threshold),
                        "validation_rows": len(validation_rows),
                        **metric_percentages(validation_rows),
                    }
                )

            combined_validation_rows = pandas.concat(
                validation_row_parts,
                ignore_index=True,
            )
            validation_summary = metric_summary_for_rows(combined_validation_rows)
            trial_records.append(
                (float(threshold), validation_summary, validation_fold_metrics)
            )

        selected_threshold, selected_validation_summary, selected_fold_metrics = max(
            trial_records,
            key=lambda trial: (
                float(trial[1]["weighted f1"]),
                -float(trial[0]),
            ),
        )

        final_classifier_decisions: dict[int, dict] = {}
        for raw_fold in sorted(folds, key=int):
            outer_fold = int(raw_fold)
            test_seeds = set(str(seed) for seed in folds[outer_fold]["test"])
            selected_classifier = classifier_decisions_for_fold(
                fold=outer_fold,
                seed_qids=test_seeds,
                predictions=test_predictions,
                threshold=selected_threshold,
                score_prediction=score_prediction,
                decisions=decisions,
                wikidata_hashmap=wikidata_hashmap,
                embedding_hashmap=embedding_hashmap,
                available_embedding_qids=available_embedding_qids,
                initial_properties=initial_properties,
                next_properties=next_properties,
                allow_bridge_nodes=allow_bridge_nodes,
            )
            final_classifier_decisions[outer_fold] = selected_classifier[outer_fold]

        final_rows = build_row_decisions(decisions, final_classifier_decisions)
        final_summary = metric_summary_for_rows(final_rows)
    finally:
        embeddings_env.close()
        wikidata_env.close()

    classifier_output.parent.mkdir(parents=True, exist_ok=True)
    with classifier_output.open("wb") as handle:
        pickle.dump(final_classifier_decisions, handle)
    metrics_output.parent.mkdir(parents=True, exist_ok=True)
    pandas.DataFrame([final_summary]).to_csv(metrics_output, index=False)
    row_output.parent.mkdir(parents=True, exist_ok=True)
    final_rows.to_csv(row_output, index=False)

    return {
        "strategy": "fixed_configuration_with_universal_validation_threshold_selection",
        "selection_metric": "row_count_weighted_five_validation_fold_f1",
        "selection_scope": "one_threshold_applied_to_all_five_outer_test_folds",
        "tie_breaking": "lowest_threshold",
        "fixed_configuration": candidate.name,
        "thresholds": [float(threshold) for threshold in thresholds],
        "prediction_artifacts": {
            "validation": candidate.validation_predictions.name,
            "outer_test": candidate.test_predictions.name,
        },
        "selected_threshold": selected_threshold,
        "selected_validation_metric_views": summary_metric_percentages(
            selected_validation_summary
        ),
        "selected_validation_fold_metrics": selected_fold_metrics,
        "validation_trials": [
            {
                "threshold": threshold,
                "row_count_weighted_validation_f1": round(
                    100.0 * float(summary["weighted f1"]),
                    4,
                ),
            }
            for threshold, summary, _ in trial_records
        ],
        "final_metric_views": summary_metric_percentages(final_summary),
        "final_rows": len(final_rows),
        "threshold_trial_count": len(trial_records),
        "validation_fold_evaluations": len(trial_records) * len(folds),
    }
