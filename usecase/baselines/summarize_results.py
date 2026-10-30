"""Summarize all NALT splits from final metadata and predictions only."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
import math
from pathlib import Path
import statistics

from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_score, recall_score

from common import (HERE, evaluate_scores, fixed_metrics, load_benchmark, load_protocol,
                    load_results, prediction_path, read_json, write_json)

LABELS = {"soil_science": "Soil science", "pest_management": "Pest management",
          "plant_health": "Plant health", "immunology": "Immunology", "cell_biology": "Cell biology"}
METRICS = ("precision", "recall", "f1", "accuracy")


def mean_sd(values):
    values = list(values)
    return {"mean": statistics.mean(values), "sd": statistics.stdev(values) if len(values) > 1 else 0.0}


def display(value):
    return f"{100 * value['mean']:.2f} ± {100 * value['sd']:.2f}"


def metrics_from_predictions(truth, prediction):
    tn, fp, fn, tp = map(int, confusion_matrix(truth, prediction, labels=[0, 1]).ravel())
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": len(truth),
            "precision": float(precision_score(truth, prediction, zero_division=0)),
            "recall": float(recall_score(truth, prediction, zero_division=0)),
            "f1": float(f1_score(truth, prediction, zero_division=0)),
            "accuracy": float(accuracy_score(truth, prediction))}


def prediction_summary(output, model, run, benchmark, protocol, metadata):
    algorithm, fold, config = model["algorithm"], model["fold"], model["configuration"]
    signature = run["signature"]
    expected_config = next(c for c in protocol[algorithm]["configurations"] if c["id"] == config)
    if (signature["algorithm"], signature["fold"], signature["config"], signature["seed"], signature["epoch_cap"]) != (
            algorithm, fold, expected_config, run["seed"], protocol[algorithm]["epoch_cap"]):
        raise ValueError("Selected run differs from its configuration or split")
    for key in ("benchmark_sha256", "embedding_sha256"):
        if signature[key] != metadata["inputs"][key]:
            raise ValueError(f"Run input differs: {key}")
    if signature["protocol_sha256"] not in [metadata["inputs"]["protocol_sha256"], *metadata.get("historical_protocol_sha256", [])]:
        raise ValueError("Unknown run protocol")
    path = prediction_path(output, algorithm, fold, run["seed"])
    with path.open(encoding="utf-8", newline="") as handle:
        predictions = list(csv.DictReader(handle))
    branch = benchmark["branches"][fold["test"]]
    expected = {benchmark["concepts"][r["node"]]: r for r in branch["decisions"]}
    actual = {r["uri"]: r for r in predictions}
    if len(actual) != len(predictions) or actual.keys() != expected.keys():
        raise ValueError("Prediction population differs from the frozen benchmark")
    for uri, row in actual.items():
        if any(int(row[key]) != int(expected[uri][key]) for key in ("label", "scored", "seed")):
            raise ValueError("Prediction labels or scoring exclusions differ")
        if row["score"] and not (math.isfinite(float(row["score"])) and 0 <= float(row["score"]) <= 1):
            raise ValueError("Invalid probability")
    scored = [r for r in predictions if r["scored"] == "1"]
    metrics = metrics_from_predictions([int(r["label"]) for r in scored], [int(r["included"]) for r in scored])
    if any(abs(value - run["test"][key]) > 1e-12 for key, value in metrics.items()):
        raise ValueError("Saved metrics differ from predictions")
    scores = {expected[r["uri"]]["node"]: float(r["score"]) for r in predictions if r["score"]}
    _, included, visited, iterations = evaluate_scores(branch, scores, run["validation"]["threshold"], protocol["maximum_iterations"])
    if included != {expected[r["uri"]]["node"] for r in predictions if r["included"] == "1"} or visited != {
            expected[r["uri"]]["node"] for r in predictions if r["visited"] == "1"}:
        raise ValueError("Saved traversal differs from prediction scores")
    if iterations != run["test"]["iterations"] or run["test"]["threshold"] != run["validation"]["threshold"]:
        raise ValueError("Test cutoff differs from the validation-selected policy")
    order = protocol["branch_order"]
    schedule = (order.index(fold["validation"]) - order.index(fold["test"])) % len(order)
    return {"algorithm": algorithm, "test_branch": fold["test"], "validation_branch": fold["validation"],
            "training_branches": ";".join(fold["training"]), "schedule": schedule,
            "seed": run["seed"], "configuration": config, "threshold": run["validation"]["threshold"],
            "validation_f1": run["validation"]["f1"], "epochs_run": run["epochs_run"],
            "best_epoch": run["best_epoch"], **metrics,
            "unreached_positive": sum(r["scored"] == "1" and r["label"] == "1" and r["visited"] == "0" for r in predictions)}


def aggregate(rows, protocol):
    models, assignments = {}, []
    for algorithm in ("lstm", "pa"):
        values = [r for r in rows if r["algorithm"] == algorithm]
        def seed_metrics(population):
            return {metric: mean_sd(statistics.mean(r[metric] for r in population if r["seed"] == seed)
                                    for seed in protocol["reporting_seeds"]) for metric in METRICS}
        models[algorithm] = {
            "macro": seed_metrics(values),
            "branches": {b: seed_metrics([r for r in values if r["test_branch"] == b]) for b in protocol["branch_order"]},
            "schedules": {str(k): seed_metrics([r for r in values if r["schedule"] == k]) for k in range(1, 5)}}
        for test in protocol["branch_order"]:
            for validation in protocol["branch_order"]:
                if test == validation:
                    continue
                selected = [r for r in values if r["test_branch"] == test and r["validation_branch"] == validation]
                record = {key: selected[0][key] for key in (
                    "algorithm", "test_branch", "validation_branch", "training_branches", "schedule", "configuration")}
                for metric in METRICS:
                    stats = mean_sd(r[metric] for r in selected)
                    record[f"{metric}_mean"], record[f"{metric}_seed_sd"] = stats["mean"], stats["sd"]
                assignments.append(record)
    return models, assignments


def llm_summary(benchmark, protocol):
    grouped = defaultdict(dict)
    for row in read_json(HERE / "results/llm_replay.json"):
        counts = row["metrics"]
        tp, fn, fp, tn = (counts[k] for k in ("tp", "fn", "fp", "tn"))
        scored = [r for r in benchmark["branches"][row["branch"]]["decisions"] if r["scored"]]
        if tp + fn != sum(r["label"] for r in scored) or tp + fn + fp + tn != len(scored):
            raise ValueError("Cached LLM population differs")
        checked = metrics_from_predictions([1] * (tp + fn) + [0] * (fp + tn),
                                           [1] * tp + [0] * fn + [1] * fp + [0] * tn)
        if any(abs(checked[k] - counts[k]) > 1e-12 for k in checked):
            raise ValueError("Cached LLM metrics differ from confusion counts")
        grouped[row["model"] + " / " + row["mode"]][row["branch"]] = counts
    return {name: {"branches": branches, "macro": {m: statistics.mean(branches[b][m] for b in protocol["branch_order"])
                                                  for m in METRICS}} for name, branches in grouped.items()}


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def report(output, results, assignments, metadata, protocol):
    models = results["models"]
    branches = protocol["branch_order"]
    lines = ["# NALT LSTM and Path Analogy results", "",
             "Five held-out test branches, four validation choices per test branch, and three training branches per split: "
             "20 splits, 40 selected configurations and 120 final evaluations across seeds 41, 42 and 43. "
             "All use one frozen PBG embedding and the same 7,902 scored decisions.", "",
             "## Results across all assignments", "",
             "F1 values are percentages. Means weight branches and validation choices equally. "
             "The ± values describe sample standard deviations across three per-seed averages, with embeddings fixed. "
             "They do not measure uncertainty across topics or assignments.", "",
             "| Method | " + " | ".join(LABELS[b] for b in branches) + " | Macro F1 |",
             "|---|" + "---:|" * 6]
    for a in ("lstm", "pa"):
        lines.append("| " + a.upper() + " | " + " | ".join(display(models[a]["branches"][b]["f1"]) for b in branches)
                     + " | " + display(models[a]["macro"]["f1"]) + " |")
    for name, value in sorted(results["cached_llm_replays"].items()):
        lines.append("| " + name.replace("zero_shot_", "") + " (cached replay) | " + " | ".join(
            f"{value['branches'][b]['f1']*100:.2f}" for b in branches) + f" | {value['macro']['f1']*100:.2f} |")
    trivial = results["always_include"]
    lines.append("| Always INCLUDE | " + " | ".join(f"{trivial[b]['f1']*100:.2f}" for b in branches)
                 + f" | {statistics.mean(r['f1'] for r in trivial.values())*100:.2f} |")
    lines += ["", "The LLM rows replay retained judgments under the common candidate population and five-root mask; they are not fresh provider runs.", "",
              "| Model | Macro precision | Macro recall | Macro F1 | Macro accuracy |", "|---|---:|---:|---:|---:|"]
    for a in ("lstm", "pa"):
        lines.append("| " + a.upper() + " | " + " | ".join(display(models[a]["macro"][m]) for m in METRICS) + " |")
    lines += ["", "## Assignment schedules", "",
              "Schedules 1–4 choose the validation branch one, two, three or four positions after the test branch in the cyclic order "
              "Soil science, Pest management, Plant health, Immunology, Cell biology. Each schedule contains five splits. "
              "All four schedules are reported equally; the table gives their observed macro F1.", "",
              "| Schedule | LSTM macro F1 | PA macro F1 |", "|---|---:|---:|"]
    for k in range(1, 5):
        lines.append(f"| {k} | " + " | ".join(display(models[a]["schedules"][str(k)]["f1"]) for a in ("lstm", "pa")) + " |")
    lines += ["", "## Every test/validation pair", "",
              "Each cell is mean F1 ± sample SD over the three seeds. Rows identify the test branch; columns identify validation. "
              "The other three branches are used for training. No split is selected using its test score."]
    for a in ("lstm", "pa"):
        lines += ["", f"### {a.upper()}", "", "| Test / validation | " + " | ".join(LABELS[b] for b in branches) + " |",
                  "|---|" + "---:|" * 5]
        for test in branches:
            cells = [LABELS[test]]
            for val in branches:
                if test == val:
                    cells.append("—")
                else:
                    r = next(r for r in assignments if r["algorithm"] == a and r["test_branch"] == test and r["validation_branch"] == val)
                    cells.append(f"{r['f1_mean']*100:.2f} ± {r['f1_seed_sd']*100:.2f}")
            lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Protocol and retained files", "",
              "Configuration selection uses seed-41 validation F1; each seed selects its own cutoff using validation data. "
              "Early stopping uses validation loss. PA references are training positives only. Every encountered non-seed receives a decision; "
              "only INCLUDE decisions expand, and unreached scored nodes count as EXCLUDE. "
              "The embedding uses unlabeled graph structure, including held-out branch concepts, so this is transductive transfer.", "",
              "The observed-negative pool inherits earlier LLM traversal coverage. There are only five topics, and overlapping splits are not independent samples. "
              "Macro F1 is averaged separately from macro precision and recall.", "",
              f"Recorded neural fits: {metadata['costs']['fits']}. Summed training time: {metadata['costs']['training_seconds']/60:.1f} minutes. "
              f"Peak memory in one model process: {metadata['costs']['peak_process_rss_mb']/1000:.2f} GB. "
              "Execution timing and environment details are retained in `results/experiment.json`.", "",
              "- [Per-seed results](results/summary.csv) and [per-assignment results](results/assignment_summary.csv).",
              "- [Selected models and per-seed metadata](results/selected_models.json); all detailed fit/search logs live under `tmp/`.",
              "- [Aggregate metrics](results/summary.json), [experiment metadata](results/experiment.json), and [embedding selection](results/embedding.json).",
              "- [Reproduction instructions](README.md). Frozen embeddings remain in `artifacts/`; retraining PBG is not bitwise deterministic.", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    models, metadata = load_results(output)
    protocol, benchmark = load_protocol(), load_benchmark()
    rows = [prediction_summary(output, model, run, benchmark, protocol, metadata)
            for model in models for run in model["runs"]]
    aggregated, assignments = aggregate(rows, protocol)
    trivial = {b: fixed_metrics(benchmark["branches"][b], {r["node"] for r in benchmark["branches"][b]["decisions"]})
               for b in protocol["branch_order"]}
    results = {"models": aggregated, "cached_llm_replays": llm_summary(benchmark, protocol),
               "always_include": trivial, "costs": metadata["costs"]}
    write_csv(output / "results/summary.csv", rows)
    write_csv(output / "results/assignment_summary.csv", assignments)
    write_json(output / "results/summary.json", results)
    report(output, results, assignments, metadata, protocol)
    print(json.dumps({"evaluations": len(rows), "macro_f1": {a: aggregated[a]["macro"]["f1"] for a in aggregated}}))


if __name__ == "__main__":
    main()
