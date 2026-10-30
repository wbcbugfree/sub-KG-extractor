"""Run selected NALT models, or repeat validation search, across all 20 splits."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
import platform
from pathlib import Path
import subprocess
import sys
import time

from common import (HERE, ROOT, RUNTIME, all_folds, file_hash, implementation_hashes,
                    input_hashes, load_protocol, load_results, model_directory,
                    read_json, write_json)


def environment():
    packages = {}
    for name in ("numpy", "tensorflow", "tensorflow-intel", "scikit-learn", "h5py", "torch"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fit_path(runtime, model, seed):
    return model_directory(runtime, model["algorithm"], model["fold"],
                           model["configuration"], seed) / "fit.json"


def execute(stage, model, seed, output, runtime):
    fold = model["fold"]
    directory = model_directory(runtime, model["algorithm"], fold, model["configuration"], seed)
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-u", str(HERE / "model_runner.py"), stage,
               "--algorithm", model["algorithm"], "--configuration", model["configuration"],
               "--test-branch", fold["test"], "--validation-branch", fold["validation"],
               "--seed", str(seed), "--output-dir", str(output), "--runtime-dir", str(runtime)]
    log = directory / f"{stage}.log"
    with log.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    if completed.returncode:
        raise RuntimeError(f"Failed {command}:\n{log.read_text(encoding='utf-8', errors='replace')[-4000:]}")
    print(f"{stage}: {fold['test']} / {fold['validation']} / {model['algorithm']} / seed {seed}", flush=True)


def run_tasks(tasks, workers, output, runtime):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(execute, stage, model, seed, output, runtime)
                   for stage, model, seed in tasks]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise


def compact_run(fit, seed, runtime, model):
    record = {key: value for key, value in fit.items() if key not in ("history", "threshold_trials")}
    record["seed"] = seed
    record["checkpoint"] = (fit_path(runtime, model, seed).parent / "best.weights.h5").relative_to(ROOT).as_posix()
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "run", "search"],
                        help="run reproduces selected configurations; search repeats all six candidates")
    parser.add_argument("--output-dir", type=Path, default=HERE,
                        help="Use a separate directory for a fresh experiment")
    parser.add_argument("--runtime-dir", type=Path, help="Optional cache directory under the repository tmp/")
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    identifier = hashlib.sha256(str(output).encode()).hexdigest()[:12]
    runtime = (args.runtime_dir or RUNTIME / "runs" / identifier).resolve()
    if ROOT / "tmp" not in runtime.parents:
        raise ValueError("Runtime caches must be under the repository tmp/ directory")
    inputs = input_hashes()
    if inputs["embedding_sha256"] != read_json(HERE / "results/embedding.json")["embedding"]["sha256"]:
        raise ValueError("Frozen embedding checksum differs")
    protocol = load_protocol()
    if args.stage == "prepare":
        print(json.dumps({"splits": len(all_folds(protocol)), "seeds": protocol["reporting_seeds"],
                          "inputs": inputs}), flush=True)
        return
    selected_path = output / "results/selected_models.json"
    metadata_path = output / "results/experiment.json"
    if metadata_path.exists() and read_json(metadata_path).get("status") == "complete":
        models, _ = load_results(output)
        print(f"Already complete: {len(models)} selected configurations, 120 predictions. No training performed.")
        return
    # The retained experiment is a reference, not a destination for replacement fits.
    if output == HERE:
        raise ValueError("Retained results are incomplete; restore them or use --output-dir tmp/<new-run>")
    started = time.perf_counter()
    metadata = {"schema_version": 2, "status": "training", "mode": args.stage,
                "inputs": inputs, "implementation": implementation_hashes(),
                "started_utc": utc_now(), "workers": args.workers,
                "python_executable": sys.executable, "environment": environment()}
    if metadata_path.exists():
        previous = read_json(metadata_path)
        for key in ("inputs", "implementation", "mode"):
            if previous[key] != metadata[key]:
                raise ValueError(f"Cannot resume with different {key}")
        if selected_path.exists() and previous["selected_models_sha256"] != file_hash(selected_path):
            raise ValueError("Selected configurations changed during resume")
    if selected_path.exists():
        models = read_json(selected_path)["models"]
    elif args.stage == "run":
        reference, _ = load_results(HERE)
        models = [{key: value for key, value in m.items() if key != "runs"} | {"runs": []} for m in reference]
    else:
        models = []
        candidates = [{"algorithm": algorithm, "fold": fold, "configuration": config["id"]}
                      for fold in all_folds(protocol) for algorithm in ("lstm", "pa")
                      for config in protocol[algorithm]["configurations"]]
        write_json(metadata_path, metadata)
        run_tasks([("train", m, protocol["selection_seed"]) for m in candidates], args.workers, output, runtime)
        for fold in all_folds(protocol):
            for algorithm in ("lstm", "pa"):
                choices = [m for m in candidates if m["fold"] == fold and m["algorithm"] == algorithm]
                trials = [read_json(fit_path(runtime, m, protocol["selection_seed"])) for m in choices]
                best = max(range(len(trials)), key=lambda i: (
                    trials[i]["validation"]["f1"], trials[i]["validation"]["precision"],
                    trials[i]["validation"]["recall"], -trials[i]["model_parameters"]))
                models.append({**choices[best], "selection_seed": protocol["selection_seed"],
                               "validation": trials[best]["validation"], "runs": []})
    write_json(selected_path, {"selection_uses_test_scores": False, "models": models})
    metadata["selected_models_sha256"] = file_hash(selected_path)
    write_json(metadata_path, metadata)
    run_tasks([("train", m, seed) for m in models for seed in protocol["reporting_seeds"]],
              args.workers, output, runtime)
    # Freeze every seed's validation threshold before any held-out evaluation.
    for model in models:
        model["runs"] = [compact_run(read_json(fit_path(runtime, model, seed)), seed, runtime, model)
                         for seed in protocol["reporting_seeds"]]
        model["validation"] = next(r["validation"] for r in model["runs"] if r["seed"] == protocol["selection_seed"])
        for run in model["runs"]:
            run.pop("test", None)
            run.pop("prediction_sha256", None)
    lock = runtime / "selection_before_evaluation.json"
    if lock.exists() and read_json(lock) != {"models": models}:
        raise ValueError("Training or validation choices changed after evaluation began")
    write_json(lock, {"models": models})
    metadata["pre_evaluation_selection_sha256"] = file_hash(runtime / "selection_before_evaluation.json")
    write_json(metadata_path, metadata)
    run_tasks([("evaluate", m, seed) for m in models for seed in protocol["reporting_seeds"]],
              args.workers, output, runtime)
    for model in models:
        model["runs"] = [compact_run(read_json(fit_path(runtime, model, seed)), seed, runtime, model)
                         for seed in protocol["reporting_seeds"]]
    write_json(selected_path, {"selection_uses_test_scores": False, "models": models})
    fits = [read_json(p) for p in (runtime / "models").rglob("fit.json")]
    metadata.update({"status": "complete", "completed_utc": utc_now(),
                     "splits": len(all_folds(protocol)), "selected_configurations": len(models),
                     "evaluations": sum(len(m["runs"]) for m in models),
                     "selected_models_sha256": file_hash(selected_path),
                     "costs": {"fits": len(fits), "training_seconds": sum(f["training_seconds"] for f in fits),
                               "peak_process_rss_mb": max(f["peak_process_rss_mb"] for f in fits)},
                     "invocation_wall_seconds": time.perf_counter() - started})
    write_json(metadata_path, metadata)
    load_results(output)
    print(json.dumps({"complete": True, "output": str(output), "fits": len(fits)}), flush=True)


if __name__ == "__main__":
    main()
