"""Verify saved experiments, validate inputs, or reproduce one run using project code."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PROCESS_START = time.perf_counter()
sys.dont_write_bytecode = True


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def current_code(archive):
    return [{"path": record["path"], "sha256": sha256(ROOT / record["path"])}
            for record in archive["implementation_files"]]


def verify_archive(archive):
    records = archive["support_files"] + archive["demonstration_count_files"]
    for run in archive["runs"]:
        records.extend(run["artifacts"])
    for record in records + archive["input_files"]:
        path = ROOT / record["path"]
        if not path.is_file() or sha256(path) != record["sha256"]:
            raise ValueError(f"Experiment artifact or input is missing or changed: {path}")
    previous = {record["path"]: record["sha256"] for record in archive["implementation_files"]}
    changed = [record["path"] for record in current_code(archive)
               if previous[record["path"]] != record["sha256"]]
    return dict(runs=len(archive["runs"]), artifacts=len(records),
                input_files=len(archive["input_files"]), code_changes_since_layout_update=changed)


def execute_run(experiment, run, prompt, output):
    key = experiment.n.load_api_key(run["provider"])
    started = time.perf_counter()
    status = dict(run_id=run["id"], protocol=run["protocol"], provider=run["provider"],
                  model=run["model"], condition=run["condition"], status="running",
                  started_utc=experiment.utc_now(), manifest_sha256=sha256(output / "manifest.json"))
    experiment.save_json(output / "status.json", status)
    api_type = (experiment.FixedCandidatesAPI if run["protocol"] == "fixed_candidates"
                else experiment.RecordedAPI)
    api = api_type(output, run["provider"], run["model"], key,
                   experiment.batch_runner.batch_response_schema_for_openai(),
                   experiment.batch_runner.batch_response_schema_for_gemini_jsonl())
    try:
        if run["protocol"] == "fixed_candidates":
            runner = experiment.run_fixed_candidates
        elif run["benchmark"] == "wikidata_dataset2":
            runner = experiment.run_wikidata
        else:
            runner = experiment.run_nalt
        result = runner(run, api, prompt, output)
        experiment.save_json(output / "result.json", result)
        metrics = result.get("metrics", result.get("ablation"))
        status.update(status="completed", metrics=metrics,
                      preparation_seconds=result["preparation_seconds"])
        for field in ("traversal_seconds", "inference_wall_seconds", "execution_wall_seconds"):
            if field in result:
                status[field] = result[field]
        if result.get("incomplete_frontier_count") or result.get("seeds_at_iteration_limit"):
            status["completion_caveat"] = "Iteration limit reached; inspect the remaining frontier."
    except Exception as exc:
        status.update(status="failed", error=str(exc).replace(key, "[REDACTED]")[:700])
    finally:
        api.close()
        status.update(ended_utc=experiment.utc_now(), wall_seconds=time.perf_counter() - started,
                      telemetry=api.summary())
        experiment.save_json(output / "status.json", status)
    print(json.dumps({"status": status["status"], "output": str(output)}))
    exit_code = 0 if status["status"] == "completed" else 1
    experiment.save_json(output / "process_time.json", {
        "wall_seconds": time.perf_counter() - PROCESS_START,
        "exit_code": exit_code,
        "measurement": "internal_process_timer",
        "source": "run_experiment.py:PROCESS_START",
    })
    return exit_code


def main():
    archive = read_json(HERE / "experiments.json")
    runs = {run["id"]: run for run in archive["runs"]}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "validate", "run"))
    parser.add_argument("run_id", nargs="?", choices=sorted(runs))
    parser.add_argument("--output-dir", type=Path,
                        help="New directory under tmp/ for validation or fresh inference.")
    args = parser.parse_args()
    if args.command != "verify" and (args.run_id is None or args.output_dir is None):
        parser.error("validate and run require a run ID and --output-dir")
    print(json.dumps(verify_archive(archive)))
    if args.command == "verify":
        return 0
    output = args.output_dir.resolve()
    scratch_root = (ROOT / "tmp").resolve()
    if not output.is_relative_to(scratch_root) or output == scratch_root:
        parser.error("--output-dir must be a new subdirectory of the repository's tmp/")
    output.mkdir(parents=True, exist_ok=False)
    # The experiment module imports the canonical project modules normally.
    import path_prompt as experiment
    run = runs[args.run_id]
    prompt = (ROOT / run["prompt"]).read_text(encoding="utf-8")
    experiment.save_json(output / "manifest.json", {
        "source_run": run["id"], "created_utc": experiment.utc_now(), "python": sys.version,
        "configuration": {key: value for key, value in run.items() if key != "artifacts"},
        "original_protocol": archive["protocols"][run["protocol"]],
        "implementation_files": current_code(archive), "input_files": archive["input_files"],
        "prompt_sha256": sha256(ROOT / run["prompt"]),
        "accounting": "October 2026 API tariffs retained for comparable estimates",
    })
    if args.command == "validate":
        checks = experiment.validate_run(run, prompt, output)
        experiment.save_json(output / "validation.json", dict(passed=True, **checks))
        print(json.dumps(dict(condition=run["condition"], **checks)))
        return 0
    return execute_run(experiment, run, prompt, output)


if __name__ == "__main__":
    raise SystemExit(main())
