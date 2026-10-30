"""Run OpenAI Decisions with the retained Jev Wikidata prompts and BFS protocol."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
from pathlib import Path
import platform
import subprocess
import sys
import time
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decision_models import jev_wikidata as shared
from decision_models import openai_decisions_nalt as api

wikidata, jev = shared.wikidata, shared.jev


def build_request(model: str, seed_label: str, batch: list[dict], system_prompt: str) -> dict:
    """Translate the Jev containers while preserving all policy and evidence text."""
    request = shared.build_request(model, seed_label, batch, system_prompt)
    return {"model": model, "input": request["state"], "questions": [
        {"type": "choice", "name": name,
         "instructions": question["instructions"]["system_prompt"] + "\n\n" + question["instructions"]["task"],
         "choices": [{"value": value, "description": description} for value, description in question["criteria"].items()]}
        for name, question in request["questions"].items()]}


def prepare(dataset: int, prompt_setting: str = "zero_shot_manual") -> dict:
    prepared = shared.prepare(dataset, prompt_setting)
    parent = ROOT / f"decision_models/results/wikidata/dataset{dataset}/jev-1.14.0/{prompt_setting}"
    manifests = list(parent.glob("*/manifest.json"))
    if len(manifests) != 1:
        raise ValueError(f"Expected exactly one retained Jev run for Dataset {dataset}")
    directory = manifests[0].parent
    manifest = api.read_json(manifests[0])
    if (manifest["status"] != "complete" or manifest["effective_prompt_sha256"] != prepared["prompt_sha256"]
            or manifest["examples_in_prompt"] != prepared["metadata"]["examples_in_prompt"]
            or manifest["prompt_examples_count"] != prepared["metadata"]["prompt_examples_count"]):
        raise ValueError("Jev is incomplete or uses different prompting")
    if any(manifest["settings"][key] != value for key, value in
           (("batch_size", 20), ("seed_workers", 5), ("max_iterations", 10))):
        raise ValueError("Retained Jev protocol differs from the expected defaults")
    path = directory / "bfs_gated_predictions.csv"
    rows, indexed = wikidata._normalize_retained_rows(jev.read_csv(path), path)
    expected = {row["row_id"]: row for seed_rows in prepared["by_seed"].values() for row in seed_rows}
    if indexed.keys() != expected.keys():
        raise ValueError("Jev evaluation population differs")
    for row in rows:
        source = expected[row["row_id"]]
        if (row["seed_qid"], row["target"]) != (source["seed_qid"], source["target"]):
            raise ValueError("Jev evaluation identity or target differs")
    metrics = shared.metric_views(rows, prepared["aligned_ids"], dataset)
    saved = api.read_json(directory / "metrics.json")
    if metrics != {name: saved[name] for name in metrics}:
        raise ValueError("Jev metrics do not reproduce")
    prepared["baselines"].insert(0, {"model": "jev-1.14.0", "inference": "retained_live_bfs",
                                    "source": shared.relative(path), "metrics": metrics})
    prepared["metadata"]["jev_run"] = shared.relative(directory)
    prepared["metadata"]["source_hashes"].update({shared.relative(file): jev.file_hash(file)
        for file in directory.iterdir() if file.is_file() and file.suffix in {".json", ".csv", ".md"}})
    return prepared


def classifier_factory(api_key: str, directory: Path):
    return api.DecisionsClassifier(api_key, directory, request_builder=build_request)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--prompt-setting", choices=("zero_shot_manual", "few_shot_manual"), default="zero_shot_manual")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if min(args.batch_size, args.workers, args.max_iterations) < 1:
        parser.error("Batch size, workers, and maximum iterations must be positive")
    args.context_level, args.provider, args.model, args.transport = "path_simple", "openai_decisions", api.MODEL, "curl"
    started, started_at = time.perf_counter(), jev.utc_now()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = (args.output_dir or ROOT / "decision_models/results/wikidata" / f"dataset{args.dataset}"
                 / f"openai-decisions-{api.MODEL}" / args.prompt_setting / timestamp).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "dataset": args.dataset, "requested_model": api.MODEL, "provider": args.provider, "endpoint": api.ENDPOINT,
        "status": "preparing", "started_at_utc": started_at, "experiment_variant": "strict",
        "prompt_setting": args.prompt_setting, "evaluation_mode": "live_bfs", "context_level": "path_simple",
        "examples_in_prompt": args.prompt_setting == "few_shot_manual", "prompt_examples_count": None,
        "python": platform.python_version(), "platform": platform.platform(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("rdflib", "pydantic")},
        "code_hashes": {shared.relative(path): jev.file_hash(path) for path in
                        (Path(__file__), Path(api.__file__), Path(shared.__file__), Path(jev.__file__), Path(wikidata.__file__))},
        "settings": {"batch_size": args.batch_size, "seed_workers": args.workers, "max_iterations": args.max_iterations,
                     "transport": "curl", "request_timeout_seconds": 60, "max_attempts": 3,
                     "primitive": "choice", "decision_rule": "provider Choice (argmax)",
                     "confidence_gating": False, "fallback_provider": None,
                     "prompt_cache_options": "provider defaults: implicit, 30m", "refusal_override": False},
        "traversal_policy": {"seeds": "included_without_model_decision", "expand": "INCLUDE_only",
                             "unlabeled_bridges": "existing_Wikidata_traversal", "unreached_scoring": "EXCLUDE",
                             "unreached_probabilities": None, "refusals": "stop_without_fabricating_decision"},
        "pricing": {"sources": [api.GUIDE, api.PRICING], "checked_on": api.PRICE_DATE,
                    "rates_usd_per_million": api.RATES},
    }
    jev.write_json(directory / "manifest.json", manifest)
    inference_started = None
    try:
        prepared = prepare(args.dataset, args.prompt_setting)
        jev.write_json(directory / "preflight.json", prepared["metadata"])
        shared.save_prompt_artifacts(prepared, directory, manifest)
        manifest.update(prompt_file=shared.relative(prepared["prompt_path"]),
                        prompt_file_sha256=jev.file_hash(prepared["prompt_path"]),
                        effective_prompt_sha256=prepared["prompt_sha256"], jev_run=prepared["metadata"]["jev_run"],
                        scored_rows=prepared["metadata"]["scored_rows"], aligned_rows=len(prepared["aligned_ids"]),
                        seed_count=len(prepared["by_seed"]))
        print(json.dumps({key: value for key, value in prepared["metadata"].items()
                          if key not in {"source_hashes", "baselines"}}, indent=2), flush=True)
        if args.prepare_only:
            manifest.update(status="prepared", finished_at_utc=jev.utc_now(), elapsed_seconds=time.perf_counter() - started)
            jev.write_json(directory / "manifest.json", manifest)
            return 0
        api_key = api.nalt.load_api_key("openai", ROOT / "config")
        manifest["transport_version"] = subprocess.run(
            ["curl.exe" if sys.platform == "win32" else "curl", "--version"], capture_output=True, text=True, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ).stdout.splitlines()[0]
        manifest.update(status="running", live_inference_started_at_utc=jev.utc_now())
        jev.write_json(directory / "manifest.json", manifest)
        inference_started = time.perf_counter()
        manifest["preparation_wall_seconds"] = inference_started - started

        def seed_runner(*positional, **keyword):
            return shared.run_seed(*positional, **keyword, api_key=api_key, output_dir=directory,
                                   classifier_factory=classifier_factory)

        seed_results, scored = wikidata.run_bfs_for_seeds(prepared["by_seed"], prepared["traversal_by_seed"],
                                                         prepared["graph"], prepared["prompt"], args, seed_runner=seed_runner)
        bfs_seconds = time.perf_counter() - inference_started
        views, usage = shared.export_results(seed_results, scored, prepared, args, directory, bfs_seconds,
                                            usage_summarizer=api.summarize_usage)
        manifest.update(status="complete", finished_at_utc=jev.utc_now(), elapsed_seconds=time.perf_counter() - started,
                        response_models=usage["response_models"])
        usage.update(total_run_wall_seconds=manifest["elapsed_seconds"],
                     preparation_wall_seconds=manifest["preparation_wall_seconds"],
                     postprocessing_wall_seconds=manifest["elapsed_seconds"] - manifest["preparation_wall_seconds"] - bfs_seconds)
        jev.write_json(directory / "usage.json", usage)
        jev.write_json(directory / "manifest.json", manifest)
        print(json.dumps({"output_dir": str(directory), "metrics": views, "usage": usage}, indent=2), flush=True)
        return 0
    except Exception as error:
        manifest.update(status="failed", finished_at_utc=jev.utc_now(), error_type=type(error).__name__,
                        elapsed_seconds=time.perf_counter() - started)
        jev.write_json(directory / "manifest.json", manifest)
        if inference_started is not None:
            jev.write_json(directory / "partial_usage.json", api.summarize_usage(directory, time.perf_counter() - inference_started))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
