"""Run Jev with human-authored zero/few-shot prompts in the existing Wikidata BFS."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "meta-prompting"))

import subkg_extractor_wikidata as wikidata
from decision_models import jev_nalt as jev


def relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def build_request(model: str, seed_label: str, batch: list[dict], system_prompt: str) -> dict:
    """Keep the legacy prompt and numbered path evidence, without gold labels."""
    return {
        "model": model,
        "state": wikidata.build_eval_user_message(batch, "path_simple"),
        "questions": {
            f"candidate_{number}": {
                "type": "choice",
                "instructions": {
                    "system_prompt": system_prompt,
                    "task": (
                        f"Apply the system prompt to candidate number {number} in the supplied state. "
                        "Return this candidate's classification."
                    ),
                },
                "criteria": {
                    "INCLUDE": "Include this candidate according to the system prompt.",
                    "EXCLUDE": "Exclude this candidate according to the system prompt.",
                },
            }
            for number in range(1, len(batch) + 1)
        },
    }


def metric_views(rows: list[dict], aligned_ids: set[str], dataset: int) -> dict:
    aligned = [row for row in rows if row["row_id"] in aligned_ids]
    if {row["row_id"] for row in aligned} != aligned_ids:
        raise ValueError("Predictions do not cover the complete aligned evaluation set")
    return {
        "full_common": wikidata.pooled_metric_aggregation(rows),
        "aligned": wikidata.compute_fold_metric_aggregation(aligned, dataset),
    }


def prepare(dataset: int, prompt_setting: str = "zero_shot_manual") -> dict:
    """Reuse frozen evaluation identities; gold labels never select live frontiers."""
    if prompt_setting not in {"zero_shot_manual", "few_shot_manual"}:
        raise ValueError("Only human-authored zero/few-shot settings are supported")
    include_examples = prompt_setting == "few_shot_manual"
    prompt_path = ROOT / f"meta-prompting/prompts/manual_dataset{dataset}.md"
    examples_path = ROOT / f"meta-prompting/examples/dataset{dataset}_path_simple_manual_examples.json"
    examples = wikidata.load_examples(examples_path) if include_examples else None
    if examples and (examples["dataset"] != dataset or examples["context_level"] != "path_simple"):
        raise ValueError("Demonstration dataset or evidence format differs")
    example_count = len(examples["examples"]) if examples else 0
    prompt = wikidata.build_effective_prompt(prompt_path.read_text(encoding="utf-8"), examples, include_examples)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    reference_dir = ROOT / f"meta-prompting/results/gpt-5.4-mini/dataset{dataset}/{prompt_setting}"
    reference_path = reference_dir / "rows.csv"
    reference = wikidata._index_unique_retained_rows(jev.read_csv(reference_path), reference_path)
    graph = wikidata.load_graph_context(dataset)
    all_rows = wikidata.augment_rows_with_graph_context(dataset, wikidata.load_decision_rows(dataset))
    traversal, skipped_absent = wikidata.skip_rows_absent_from_local_graph(all_rows, graph)
    source_by_id = {row["row_id"]: row for row in traversal}
    selected = []
    for row_id, retained in reference.items():
        source = source_by_id[row_id]
        for key in ("seed_qid", "qid", "target", "path", "seed_label", "label"):
            if str(source[key]) != str(retained[key]):
                raise ValueError(f"Frozen Dataset {dataset} input changed: {row_id}, {key}")
        selected.append(source)

    aligned_path = (wikidata.DEFAULT_BASELINE_DECISIONS_DIR
                    / wikidata.RETAINED_BASELINE_FILES[dataset]["lstm"])
    aligned_rows = jev.read_csv(aligned_path)
    aligned_ids = {row["row_id"] for row in aligned_rows}
    if not aligned_ids <= reference.keys() or len(aligned_ids) != len(aligned_rows):
        raise ValueError("Invalid aligned evaluation identities")
    for row in aligned_rows:
        source = source_by_id[row["row_id"]]
        if (source["seed_qid"], source["qid"], source["target"]) != (row["from"], row["QID"], int(row["target"])):
            raise ValueError("Aligned evaluation identity or target changed")

    sources = [prompt_path, reference_path, aligned_path,
               wikidata.DEFAULT_FOLDS_DIR / wikidata.FOLD_FILES[dataset]]
    sources.extend((ROOT / f"wikidata/dataset{dataset}").glob("*"))
    held_out_ids = set()
    for folder, kind in (("", "manual"), ("meta/", "meta")):
        path = ROOT / f"meta-prompting/examples/{folder}dataset{dataset}_path_simple_{kind}_examples.json"
        sources.append(path)
        held_out_ids.update(row["row_id"] for row in json.loads(path.read_text(encoding="utf-8"))["examples"])
    if held_out_ids & reference.keys():
        raise ValueError("Held-out demonstrations leaked into the evaluation population")

    baselines = []
    for model in wikidata.RETAINED_LLM_MODELS:
        path = wikidata._retained_llm_path(dataset, model, prompt_setting, wikidata.DEFAULT_RESULTS_DIR)
        rows, indexed = wikidata._normalize_retained_rows(jev.read_csv(path), path)
        if indexed.keys() != reference.keys():
            raise ValueError(f"Different comparison population for {model}")
        for row in rows:
            source = source_by_id[row["row_id"]]
            if (row["seed_qid"], row["target"]) != (source["seed_qid"], source["target"]):
                raise ValueError(f"Different comparison identity or target for {model}")
        metadata_path = path.with_suffix(".json") if model == "deepseek-v4-pro" else path.parent / "manifest.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (metadata["effective_prompt_sha256"] != prompt_hash
                or metadata["examples_in_prompt"] != include_examples
                or (include_examples and metadata["prompt_examples_count"] != example_count)):
            raise ValueError(f"Different effective prompt or demonstration count for {model}")
        expected = (metadata["metrics"] if model == "deepseek-v4-pro" else
                    json.loads((path.parent / "summary.json").read_text(encoding="utf-8"))["metrics"]["bfs_gated"])
        views = metric_views(rows, aligned_ids, dataset)
        if views["full_common"]["pooled"] != expected:
            raise ValueError(f"Retained metrics do not reproduce for {model}")
        baselines.append({"model": model, "inference": "retained", "source": relative(path), "metrics": views})
        sources.extend([path, metadata_path])
    by_seed, traversal_by_seed = defaultdict(list), defaultdict(list)
    for row in selected:
        by_seed[row["seed_qid"]].append(row)
    for row in traversal:
        if row["seed_qid"] in by_seed:
            traversal_by_seed[row["seed_qid"]].append(row)
    prepared = {
        "prompt": prompt, "prompt_path": prompt_path, "prompt_sha256": prompt_hash,
        "graph": graph, "by_seed": by_seed, "traversal_by_seed": traversal_by_seed,
        "aligned_ids": aligned_ids, "baselines": baselines,
        "metadata": {
            "scored_rows": len(selected), "aligned_rows": len(aligned_ids), "seeds": len(by_seed),
            "traversal_decision_rows": sum(map(len, traversal_by_seed.values())),
            "skipped_rows_absent_from_local_graph": skipped_absent,
            "held_out_example_rows": len(held_out_ids), "examples_in_prompt": include_examples,
            "prompt_examples_count": example_count, "evaluation_source": relative(reference_path),
            "source_hashes": {relative(path): jev.file_hash(path) for path in sources if path.is_file()},
            "baselines": baselines,
        },
    }
    if include_examples:
        example_ids = [row["row_id"] for row in examples["examples"]]
        if len(set(example_ids)) != example_count or not set(example_ids) <= held_out_ids:
            raise ValueError("Demonstration identities must be unique and held out")
        prepared["examples_path"] = examples_path
        prepared["metadata"].update(
            examples_file=relative(examples_path), examples_sha256=jev.file_hash(examples_path),
            prompt_example_row_ids=example_ids,
            prompt_example_decision_counts={value: sum(row["decision"] == value for row in examples["examples"])
                                            for value in ("INCLUDE", "EXCLUDE")},
        )
    return prepared


def save_prompt_artifacts(prepared: dict, output_dir: Path, manifest: dict) -> None:
    """Retain the base prompt and, for few-shot runs, the exact appended examples."""
    (output_dir / "system_prompt.md").write_bytes(prepared["prompt_path"].read_bytes())
    manifest.update(examples_in_prompt=prepared["metadata"]["examples_in_prompt"],
                    prompt_examples_count=prepared["metadata"]["prompt_examples_count"])
    if prepared["metadata"]["examples_in_prompt"]:
        (output_dir / "effective_prompt.md").write_bytes(prepared["prompt"].encode("utf-8"))
        (output_dir / "examples.json").write_bytes(prepared["examples_path"].read_bytes())
        manifest.update(examples_file=relative(prepared["examples_path"]),
                        examples_sha256=jev.file_hash(prepared["examples_path"]))


def run_seed(seed_qid, seed_label, gold_rows, graph_context, prompt, args,
             decision_rows, *, api_key, output_dir, classifier_factory=None):
    directory = output_dir / "seeds" / seed_qid
    client = (classifier_factory(api_key, directory) if classifier_factory else
              jev.JevClassifier(api_key, directory, args.transport, request_builder=build_request))

    def classify(prompt, rows, context_level, provider, model, batch_size, workers):
        if context_level != "path_simple":
            raise ValueError("Only the retained path_simple protocol is supported")
        nodes = [{**row, "uri": f"http://www.wikidata.org/entity/{row['qid']}"} for row in rows]
        decisions = client(model=model, seed_label=seed_label, nodes=nodes, batch_size=batch_size,
                           max_workers=workers, system_prompt=prompt)
        return [{**row, "prediction": int(row["classification"] == "INCLUDE"),
                 "depth": row["bfs_depth"]} for row in decisions]

    started_at, started = jev.utc_now(), time.perf_counter()
    result = wikidata.run_bfs_for_seed(seed_qid, seed_label, gold_rows, graph_context, prompt, args,
                                     decision_rows=decision_rows, classifier=classify)
    if result["iterations"] >= args.max_iterations and result["iteration_details"][-1]["included"]:
        raise RuntimeError(f"Iteration limit may have truncated {seed_qid}; results are incomplete")
    jev.write_json(directory / "traversal.json", {
        key: value for key, value in result.items() if key not in {"gold_rows", "raw_decisions"}
    } | {"started_at_utc": started_at, "finished_at_utc": jev.utc_now(),
         "wall_seconds": time.perf_counter() - started, "frontier_exhausted": True})
    return result


def export_results(seed_results, scored, prepared, args, output_dir, bfs_seconds, usage_summarizer=None):
    raw = [row for seed in seed_results for row in seed["raw_decisions"]]
    by_pair = {(row["seed_qid"], row["qid"]): row for row in raw}
    if len(by_pair) != len(raw):
        raise ValueError("A candidate was classified more than once for a seed")
    scored_ids = {row["row_id"] for row in scored}
    raw = [{**row, "scored": row["row_id"] in scored_ids,
            "aligned": row["row_id"] in prepared["aligned_ids"]} for row in raw]
    for row in scored:
        decision = by_pair.get((row["seed_qid"], row["qid"]))
        row.update({key: decision.get(key) if decision else None for key in
                    ("p_include", "p_exclude", "selected_probability", "response_model", "path", "iteration", "batch", "candidate_number")})
        row.update(effective_decision="INCLUDE" if row["prediction"] else "EXCLUDE",
                   decision_source=decision["decision_source"] if decision else "not_visited",
                   aligned=row["row_id"] in prepared["aligned_ids"])
    jev.write_csv(output_dir / "decisions.csv", raw)
    jev.write_csv(output_dir / "bfs_gated_predictions.csv", scored)
    views = metric_views(scored, prepared["aligned_ids"], args.dataset)
    visited = [row for row in scored if row["p_include"] is not None]
    views["visited_scored_probability_metrics"] = {
        "rows": len(visited), "excluded_unvisited_rows": len(scored) - len(visited),
        "brier_score": sum((row["p_include"] - row["target"]) ** 2 for row in visited) / len(visited) if visited else None,
        "log_loss": -sum(math.log(max(1e-15, row["p_include"] if row["target"] else row["p_exclude"])) for row in visited) / len(visited) if visited else None,
    }
    jev.write_json(output_dir / "metrics.json", views)
    comparisons = [{"model": args.model, "inference": "live_bfs", "metrics": views}] + prepared["baselines"]
    table = []
    for item in comparisons:
        for name in ("full_common", "aligned"):
            aggregation = item["metrics"][name]
            row = {"model": item["model"], "inference": item["inference"], "view": name,
                   **aggregation["pooled"]}
            weighted = aggregation["weighted_fold"] or {}
            row.update({f"weighted_{key}": weighted.get(key) for key in
                        ("precision", "recall", "f1", "accuracy", "std_precision", "std_recall", "std_f1", "std_accuracy")})
            table.append(row)
    jev.write_csv(output_dir / "comparison.csv", table)
    usage = (usage_summarizer or jev.summarize_usage)(output_dir, bfs_seconds)
    usage.update(candidate_decisions=len(raw), scored_decisions=len(visited),
                 scored_not_visited=len(scored) - len(visited), seed_count=len(seed_results))
    jev.write_json(output_dir / "usage.json", usage)
    return views, usage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--model", default="jev-1.14.0")
    parser.add_argument("--prompt-setting", choices=("zero_shot_manual", "few_shot_manual"), default="zero_shot_manual")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--transport", choices=("curl", "urllib"), default="curl")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if min(args.batch_size, args.workers, args.max_iterations) < 1:
        parser.error("Batch size, workers, and maximum iterations must be positive")
    args.context_level, args.provider = "path_simple", "typesafe"
    started, started_at = time.perf_counter(), jev.utc_now()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (args.output_dir or ROOT / "decision_models/results/wikidata"
                  / f"dataset{args.dataset}" / wikidata.model_slug(args.model) / args.prompt_setting / timestamp).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "dataset": args.dataset, "requested_model": args.model, "provider": "typesafe", "endpoint": jev.ENDPOINT,
        "status": "preparing", "started_at_utc": started_at, "prompt_setting": args.prompt_setting,
        "evaluation_mode": "live_bfs", "context_level": "path_simple",
        "examples_in_prompt": args.prompt_setting == "few_shot_manual",
        "prompt_examples_count": None, "python": platform.python_version(), "platform": platform.platform(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("rdflib", "pydantic")},
        "code_hashes": {relative(path): jev.file_hash(path) for path in
                        (Path(__file__), Path(jev.__file__), Path(wikidata.__file__))},
        "settings": {"batch_size": args.batch_size, "seed_workers": args.workers, "max_iterations": args.max_iterations,
                     "transport": args.transport, "request_timeout_seconds": 60, "max_attempts": 3,
                     "primitive": "choice", "decision_rule": "provider Choice (argmax)",
                     "confidence_gating": False, "fallback_provider": None},
        "traversal_policy": {"seeds": "included_without_model_decision", "expand": "INCLUDE_only",
                             "unlabeled_bridges": "existing_Wikidata_traversal", "unreached_scoring": "EXCLUDE",
                             "unreached_probabilities": None},
        "pricing": {"source": jev.PRICE_SOURCE, "checked_on": jev.PRICE_CHECKED_ON,
                    "input_usd_per_million": jev.INPUT_USD_PER_MILLION, "output_usd_per_million": 0},
    }
    jev.write_json(output_dir / "manifest.json", manifest)
    inference_started = None
    try:
        prepared = prepare(args.dataset, args.prompt_setting)
        jev.write_json(output_dir / "preflight.json", prepared["metadata"])
        save_prompt_artifacts(prepared, output_dir, manifest)
        manifest.update(prompt_file=relative(prepared["prompt_path"]),
                        prompt_file_sha256=jev.file_hash(prepared["prompt_path"]),
                        effective_prompt_sha256=prepared["prompt_sha256"],
                        scored_rows=prepared["metadata"]["scored_rows"], aligned_rows=len(prepared["aligned_ids"]),
                        seed_count=len(prepared["by_seed"]))
        print(json.dumps({key: value for key, value in prepared["metadata"].items()
                          if key not in {"source_hashes", "baselines"}}, indent=2), flush=True)
        if args.prepare_only:
            manifest.update(status="prepared", finished_at_utc=jev.utc_now(), elapsed_seconds=time.perf_counter() - started)
            jev.write_json(output_dir / "manifest.json", manifest)
            return 0
        api_key = jev.load_api_key()
        manifest["transport_version"] = subprocess.run(
            ["curl.exe" if sys.platform == "win32" else "curl", "--version"],
            capture_output=True, text=True, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ).stdout.splitlines()[0] if args.transport == "curl" else platform.python_version()
        manifest.update(status="running", live_inference_started_at_utc=jev.utc_now())
        jev.write_json(output_dir / "manifest.json", manifest)
        inference_started = time.perf_counter()
        manifest["preparation_wall_seconds"] = inference_started - started

        def seed_runner(*positional, **keyword):
            return run_seed(*positional, **keyword, api_key=api_key, output_dir=output_dir)

        seed_results, scored = wikidata.run_bfs_for_seeds(
            prepared["by_seed"], prepared["traversal_by_seed"], prepared["graph"],
            prepared["prompt"], args, seed_runner=seed_runner)
        bfs_seconds = time.perf_counter() - inference_started
        views, usage = export_results(seed_results, scored, prepared, args, output_dir, bfs_seconds)
        manifest.update(status="complete", finished_at_utc=jev.utc_now(), elapsed_seconds=time.perf_counter() - started,
                        response_models=usage["response_models"])
        usage.update(total_run_wall_seconds=manifest["elapsed_seconds"],
                     preparation_wall_seconds=manifest["preparation_wall_seconds"],
                     postprocessing_wall_seconds=manifest["elapsed_seconds"] - manifest["preparation_wall_seconds"] - bfs_seconds)
        jev.write_json(output_dir / "usage.json", usage)
        jev.write_json(output_dir / "manifest.json", manifest)
        print(json.dumps({"output_dir": str(output_dir), "metrics": views, "usage": usage}, indent=2), flush=True)
        return 0
    except Exception as error:
        manifest.update(status="failed", finished_at_utc=jev.utc_now(), error_type=type(error).__name__,
                        elapsed_seconds=time.perf_counter() - started)
        jev.write_json(output_dir / "manifest.json", manifest)
        if inference_started is not None:
            jev.write_json(output_dir / "partial_usage.json", jev.summarize_usage(output_dir, time.perf_counter() - inference_started))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
