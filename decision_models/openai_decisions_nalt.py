"""Run OpenAI Decisions with the frozen Jev NALT prompts and gated BFS protocol."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decision_models import jev_nalt as shared

nalt = shared.nalt
read_csv, write_csv, write_json = shared.read_csv, shared.write_csv, shared.write_json
file_hash, utc_now = shared.file_hash, shared.utc_now
MODEL = "gpt-6-luna"
ENDPOINT = "https://api.openai.com/v1/decisions"
PRICE_DATE = "2026-10-28"
GUIDE = "https://developers.openai.com/api/docs/guides/decisions"
PRICING = "https://developers.openai.com/api/docs/pricing"
RATES = {"uncached_input": 0.10, "cached_input": 0.01, "cache_write_30m": 0.125, "output": 0.0}
BRANCHES = ("plant_health", "immunology", "cell_biology", "pest_management", "soil_science")
FINAL_RUN_INDEX = ROOT / "decision_models/results/nalt/summary/runs.json"
DESALINATION_URI = "https://lod.nal.usda.gov/nalt/136092"
DESALINATION_OVERRIDE_REASON = "User-authorized EXCLUDE for desalination only when the provider returns refusal"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def jev_runs() -> dict[str, Path]:
    runs = read_json(FINAL_RUN_INDEX)["runs"]
    selected = [item for item in runs if item["provider"] == "typesafe" and item["model"] == "jev-1.14.0"]
    if len(selected) != len(BRANCHES) or {item["branch"] for item in selected} != set(BRANCHES):
        raise ValueError("Expected one retained Jev run per NALT branch")
    return {item["branch"]: ROOT / item["run_dir"] for item in selected}


def build_request(model: str, seed_label: str, batch: list[dict], system_prompt: str) -> dict:
    """Only translate API containers: preserve every policy, selector and option text."""
    jev = shared.build_request(model, seed_label, batch, system_prompt)
    return {"model": model, "input": jev["state"], "questions": [
        {"type": "choice", "name": name,
         "instructions": question["instructions"]["system_prompt"] + "\n\n" + question["instructions"]["task"],
         "choices": [{"value": value, "description": description} for value, description in question["criteria"].items()]}
        for name, question in jev["questions"].items()]}


def parse_response(payload: dict, batch: list[dict], iteration: int, batch_number: int,
                   exclude_desalination_on_refusal: bool = False) -> list[dict]:
    """Validate answers; an opt-in, URI-specific refusal override has no probabilities."""
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
        raise ValueError("Expected an answers array")
    if not isinstance(payload.get("model"), str):
        raise ValueError("Missing response model")
    expected = {f"candidate_{number}": node for number, node in enumerate(batch, 1)}
    answers, overrides, seen = {}, {}, set()
    for answer in payload["answers"]:
        if not isinstance(answer, dict):
            raise ValueError("Expected an answer object")
        name = answer.get("name")
        if not isinstance(name, str) or name not in expected or name in seen:
            raise ValueError("Missing, unknown or duplicate answer name")
        seen.add(name)
        if answer.get("type") == "refusal" and exclude_desalination_on_refusal and expected[name]["uri"] == DESALINATION_URI:
            overrides[name] = answer
            continue
        if answer.get("type") != "choice":
            raise ValueError("Refused or non-choice decision")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, list):
            raise ValueError("Missing probabilities array")
        distribution = {}
        for item in probabilities:
            if not isinstance(item, dict) or item.get("value") not in {"INCLUDE", "EXCLUDE"} or item["value"] in distribution:
                raise ValueError("Invalid or duplicate probability option")
            distribution[item["value"]] = item.get("probability")
        answers[name] = {**answer, "probabilities": distribution}
    if seen != set(expected):
        raise ValueError("Response does not contain exactly the requested candidates")
    rows = []
    for number, (name, node) in enumerate(expected.items(), 1):
        if name in overrides:
            row = {**node, "classification": "EXCLUDE", "confidence": None, "p_include": None, "p_exclude": None,
                   "selected_probability": None, "decision_source": "user_refusal_override", "provider_answer_type": "refusal",
                   "override_reason": DESALINATION_OVERRIDE_REASON, "iteration": iteration, "batch": batch_number,
                   "response_model": payload["model"]}
        else:
            row = shared.parse_response({"model": payload["model"], "answers": {"candidate_1": answers[name]}},
                                        [node], iteration, batch_number)[0]
            row.update(decision_source="openai_decisions", provider_answer_type="choice", override_reason=None)
        row["candidate_number"] = number
        rows.append(row)
    return rows


def parse_http_output(output: bytes) -> tuple[bytes, dict]:
    """Preserve rate-limit token headers while redacting authentication and cookies."""
    response, transfer = output.rsplit(shared.CURL_METADATA_MARKER, 1)
    metadata = {"curl": json.loads(transfer), "response_header_blocks": []}
    while response.startswith(b"HTTP/"):
        block, separator, response = response.partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("Incomplete HTTP header block")
        lines = block.decode("iso-8859-1").split("\r\n")
        headers = []
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            sensitive = any(word in name.casefold() for word in ("cookie", "authorization", "api-key", "secret", "auth-token", "access-token"))
            headers.append({"name": name, "value": "[REDACTED]" if sensitive else value.strip()})
        metadata["response_header_blocks"].append({"status_line": lines[0], "headers": headers})
    return response, metadata


def attach_usage(record: dict) -> None:
    """Cache counts are subsets of input_tokens; unknown fields remain missing."""
    payload = record.get("response", {})
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details", {})
    details = details if isinstance(details, dict) else {}
    record["usage"] = usage
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        record[key] = value if type(value) is int and value >= 0 else None
    for key in ("cached_tokens", "cache_write_tokens"):
        value = details.get(key)
        record[key] = value if type(value) is int and value >= 0 else None
    record["usage_available"] = record["input_tokens"] is not None and record["output_tokens"] is not None
    record["cost_available"] = record["usage_available"] and all(record[key] is not None for key in ("cached_tokens", "cache_write_tokens"))
    record["uncached_input_tokens"] = record["estimated_cost_usd"] = None
    if record["cost_available"]:
        regular = record["input_tokens"] - record["cached_tokens"] - record["cache_write_tokens"]
        if regular < 0:
            raise ValueError("Cache counts exceed total input usage")
        record["uncached_input_tokens"] = regular
        record["estimated_cost_usd"] = (regular * RATES["uncached_input"] + record["cached_tokens"] * RATES["cached_input"]
                                         + record["cache_write_tokens"] * RATES["cache_write_30m"]) / 1_000_000


class DecisionsClassifier:
    def __init__(self, api_key: str, output_dir: Path, exclude_desalination_on_refusal: bool = False,
                 request_builder=None):
        if not api_key or any(char in api_key for char in "\r\n"):
            raise ValueError("Invalid credential")
        self.api_key, self.output_dir, self.iteration = api_key, output_dir, 0
        self.exclude_desalination_on_refusal = exclude_desalination_on_refusal
        self.request_builder = request_builder or build_request

    def post(self, body_path: Path, client_request_id: str, metadata: dict) -> tuple[int, bytes]:
        headers = (f"Authorization: Bearer {self.api_key}\nContent-Type: application/json\nAccept: application/json\n"
                   f"X-Client-Request-Id: {client_request_id}\n")
        completed = subprocess.run(
            ["curl.exe" if sys.platform == "win32" else "curl", "--silent", "--show-error", "--connect-timeout", "10",
             "--max-time", "60", "--request", "POST", "--header", "@-", "--data-binary", f"@{body_path}",
             "--dump-header", "-", "--write-out", shared.CURL_METADATA_MARKER.decode() + "%{json}", ENDPOINT],
            input=headers.encode("utf-8"), capture_output=True, timeout=70,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        body = b""
        if shared.CURL_METADATA_MARKER in completed.stdout:
            body, details = parse_http_output(completed.stdout)
            metadata.update(details)
        metadata["curl_exit_code"] = completed.returncode
        if completed.returncode:
            metadata["curl_error"] = completed.stderr.decode("utf-8", errors="replace").replace(self.api_key, "[REDACTED]")
            raise OSError(f"curl transport exited with code {completed.returncode}")
        return int(metadata["curl"]["http_code"]), body

    def request_batch(self, model, seed_label, batch, system_prompt, iteration, number):
        payload = self.request_builder(model, seed_label, batch, system_prompt)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        prefix = self.output_dir / "requests" / f"iteration_{iteration:02d}_batch_{number:03d}"
        write_json(prefix.with_suffix(".request.json"), {"request": payload, "candidate_uris": [row["uri"] for row in batch],
                                                       "request_sha256": hashlib.sha256(body).hexdigest()})
        body_path = prefix.with_suffix(".body.json")
        body_path.write_bytes(body)
        for attempt in range(1, 4):
            client_id = str(uuid.uuid4())
            record = {"started_at_utc": utc_now(), "iteration": iteration, "batch": number, "attempt": attempt,
                      "candidate_count": len(batch), "endpoint": ENDPOINT, "method": "POST", "transport": "curl",
                      "requested_model": model, "client_request_id": client_id,
                      "request_headers": {"Authorization": "[REDACTED]", "Content-Type": "application/json", "Accept": "application/json", "X-Client-Request-Id": client_id},
                      "request_body_bytes": len(body), "request_sha256": hashlib.sha256(body).hexdigest(),
                      "http_metadata": {}, "price_checked_on": PRICE_DATE, "rates_usd_per_million": RATES}
            started, retry_after, retryable = time.perf_counter(), 2 ** attempt, False
            try:
                status, raw = self.post(body_path, client_id, record["http_metadata"])
                record["http_status"] = status
                sanitized = raw.replace(self.api_key.encode(), b"[REDACTED]")
                response_path = prefix.with_suffix(f".attempt_{attempt:02d}.response.json")
                response_path.write_bytes(sanitized)
                record.update(response_file=response_path.name, response_body_bytes=len(raw),
                              response_sha256=hashlib.sha256(raw).hexdigest(), response_redacted=sanitized != raw)
                try:
                    record["response"] = json.loads(sanitized)
                except ValueError:
                    record["response_text"] = sanitized.decode("utf-8", errors="replace")
                if status == 200:
                    rows = parse_response(record.get("response"), batch, iteration, number,
                                          self.exclude_desalination_on_refusal)
                    record["refusal_overrides"] = [{key: row[key] for key in
                        ("uri", "classification", "candidate_number", "decision_source", "override_reason")}
                        for row in rows if row["decision_source"] == "user_refusal_override"]
                    record["status"] = "success"
                else:
                    record["status"] = "http_error"
                    retryable = status in {408, 429, 500, 502, 503, 504, 529}
                    delay = shared.response_headers(record["http_metadata"]).get("retry-after", "")
                    if delay:
                        try:
                            retry_after = max(retry_after, float(delay))
                        except ValueError:
                            pass
            except (OSError, subprocess.TimeoutExpired) as error:
                record.update(status="connection_error", error_type=type(error).__name__,
                              error_message=str(error).replace(self.api_key, "[REDACTED]"))
                retryable = True
            except (ValueError, TypeError) as error:
                record.update(status="invalid_response", error_type=type(error).__name__, error_message=str(error))
            record.update(elapsed_seconds=time.perf_counter() - started, finished_at_utc=utc_now())
            attach_usage(record)
            write_json(prefix.with_suffix(f".attempt_{attempt:02d}.json"), record)
            if record["status"] == "success":
                return rows
            if not retryable or attempt == 3 or retry_after > 30:
                raise RuntimeError(f"OpenAI Decisions failed: layer {iteration}, batch {number}, {record['status']}, HTTP {record.get('http_status')}")
            time.sleep(retry_after)
        raise RuntimeError("Attempts exhausted")

    def __call__(self, *, model, seed_label, nodes, batch_size, max_workers, system_prompt, **_):
        started_at, started = utc_now(), time.perf_counter()
        self.iteration += 1
        iteration = self.iteration
        batches = [nodes[start:start + batch_size] for start in range(0, len(nodes), batch_size)]
        print(f"Decisions layer {iteration}: {len(nodes)} candidates in {len(batches)} live requests", flush=True)
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
            futures = [executor.submit(self.request_batch, model, seed_label, batch, system_prompt, iteration, number)
                       for number, batch in enumerate(batches, 1)]
            rows = [row for future in futures for row in future.result()]
        write_json(self.output_dir / "layers" / f"layer_{iteration:02d}.json", rows)
        write_json(self.output_dir / "timing" / f"layer_{iteration:02d}.json", {
            "iteration": iteration, "started_at_utc": started_at, "finished_at_utc": utc_now(),
            "wall_seconds": time.perf_counter() - started, "candidate_count": len(nodes),
            "logical_requests": len(batches), "workers": min(max_workers, len(batches))})
        return rows


def summarize_usage(directory: Path, bfs_seconds: float | None) -> dict:
    attempt_paths = [path for path in sorted(directory.glob("**/requests/*.attempt_*.json"))
                     if not path.name.endswith(".response.json")]
    attempts = [read_json(path) for path in attempt_paths]
    table = []
    for path, record in zip(attempt_paths, attempts):
        attach_usage(record)
        headers = shared.response_headers(record["http_metadata"])
        transfer = record["http_metadata"].get("curl", {})
        table.append({key: record.get(key) for key in (
            "iteration", "batch", "attempt", "client_request_id", "started_at_utc", "finished_at_utc", "elapsed_seconds",
            "candidate_count", "status", "http_status", "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
            "cache_write_tokens", "uncached_input_tokens", "estimated_cost_usd")} | {
                "source": path.relative_to(directory).as_posix(),
                "request_id": headers.get("x-request-id"), "response_model": record.get("response", {}).get("model"),
                "refusal_override_count": len(record.get("refusal_overrides", [])),
                "provider_refusal_count": sum(answer.get("type") == "refusal"
                    for answer in record.get("response", {}).get("answers", []) if isinstance(answer, dict)),
                "server_processing_ms": headers.get("openai-processing-ms"),
                **{key: transfer.get(field) for key, field in {
                    "dns_seconds": "time_namelookup", "connect_seconds": "time_connect", "tls_seconds": "time_appconnect",
                    "first_byte_seconds": "time_starttransfer", "transfer_seconds": "time_total",
                    "upload_bytes": "size_upload", "download_bytes": "size_download"}.items()}})
    write_csv(directory / "api_calls.csv", table)
    success = [row for row in attempts if row["status"] == "success"]
    logical = len(list(directory.glob("**/requests/*.request.json")))
    latency = sorted(row["elapsed_seconds"] for row in success)
    totals = {key: sum(row[key] for row in attempts if row[key] is not None) for key in (
        "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "cache_write_tokens", "uncached_input_tokens", "estimated_cost_usd")}
    return {**totals, "live_bfs_wall_seconds": bfs_seconds, "logical_requests": logical, "request_attempts": len(attempts),
            "requests_with_refusal_overrides": sum(bool(row.get("refusal_overrides")) for row in attempts),
            "refusal_override_decisions": sum(len(row.get("refusal_overrides", [])) for row in attempts),
            "provider_refusal_answers": sum(row["provider_refusal_count"] for row in table),
            "successful_requests": len(success), "retries": len(attempts) - logical, "failed_attempts": len(attempts) - len(success),
            "attempts_without_usage": sum(not row["usage_available"] for row in attempts),
            "attempts_without_cost": sum(not row["cost_available"] for row in attempts),
            "summed_attempt_latency_seconds": sum(row["elapsed_seconds"] for row in attempts),
            "median_success_latency_seconds": statistics.median(latency) if latency else None,
            "p95_success_latency_seconds": latency[math.ceil(len(latency) * .95) - 1] if latency else None,
            "max_success_latency_seconds": max(latency) if latency else None,
            "response_models": sorted({row["response"]["model"] for row in success}),
            "price_checked_on": PRICE_DATE, "price_sources": [GUIDE, PRICING], "rates_usd_per_million": RATES,
            "cost_note": "Estimate for global endpoint, short context, default 30m cache TTL; unknown usage is not zero. Output is free. Cache reads/writes are subsets of input tokens."}


def replay_jev(index, preset, prompt_path, exclusions, examples_path, directory):
    manifest, detail = read_json(directory / "manifest.json"), read_json(directory / "metrics.json")
    if manifest["source_sha256"] != shared.SOURCE_SHA256 or manifest["prompt_file_sha256"] != file_hash(prompt_path):
        raise ValueError("Jev source or prompt differs")
    if manifest["examples_sha256"] != file_hash(examples_path) or set(detail["exclude_evaluation_uris"]) != set(map(str, exclusions)):
        raise ValueError("Jev held-out examples differ")
    rows = read_csv(directory / "decisions.csv")
    mapping = {row["uri"]: row for row in rows}

    def cached(*, nodes, **_):
        return [{**node, "classification": mapping[node["uri"]]["classification"],
                 "confidence": float(mapping[node["uri"]]["provider_confidence"])} for node in nodes]

    replay = shared.run_bfs(index, preset, prompt_path, exclusions, examples_path, cached, "jev-1.14.0", "typesafe")
    if replay["metrics"] != detail["metrics"] or replay["iterations"] != detail["iterations"]:
        raise ValueError("Jev replay differs")
    if {row["uri"] for row in replay["all_visited"]} != set(mapping):
        raise ValueError("Jev visited set differs")
    return {"model": "jev-1.14.0", "metrics": detail["metrics"],
            "included": sorted(row["uri"] for row in rows if row["classification"] == "INCLUDE"), "visited": sorted(mapping),
            "artifact": (directory / "metrics.json").relative_to(ROOT).as_posix(),
            "hashes": {path.relative_to(ROOT).as_posix(): file_hash(path) for path in directory.rglob("*")
                       if path.is_file() and path.suffix in {".json", ".csv"}},
            "source_and_bfs_replay_exact": True}


def export_results(index, results, directory, baselines, manifest, bfs_seconds):
    decisions = {row["uri"]: dict(row) for row in results["all_included"] + results["all_pruned"]}
    seeds, excluded, gold = map(set, (results["seed_uris"], results["exclude_evaluation_uris"], results["gold_uris"]))
    for uri in seeds:
        decisions[uri].update(decision_source="seed", iteration=0, p_include=None, p_exclude=None,
                              selected_probability=None, confidence=None, batch=None, candidate_number=None, response_model=None)
    ordered = [decisions[row["uri"]] for row in results["all_visited"]]
    rows = [{"uri": row["uri"], "prefLabel": row["prefLabel"], "altLabels": "; ".join(row["altLabels"]),
             "classification": row["classification"], "p_include": row.get("p_include"), "p_exclude": row.get("p_exclude"),
             "selected_probability": row.get("selected_probability"), "provider_confidence": row.get("confidence"),
             "decision_source": row["decision_source"], "iteration": row["iteration"], "batch": row.get("batch"),
             "provider_answer_type": row.get("provider_answer_type"), "override_reason": row.get("override_reason"),
             "candidate_number": row.get("candidate_number"), "response_model": row.get("response_model"),
             "in_hierarchy_gold": row["uri"] in gold, "scored": row["uri"] not in excluded,
             "relations": "; ".join(row["relations"])} for row in ordered]
    write_csv(directory / "decisions.csv", rows)
    for name, decision in (("included", "INCLUDE"), ("pruned", "EXCLUDE")):
        write_csv(directory / f"{name}.csv", [row for row in rows if row["classification"] == decision])
    included = {uri for uri, row in decisions.items() if row["classification"] == "INCLUDE"}
    visited = set(decisions)
    pooled = (gold | visited | {uri for baseline in baselines for uri in baseline["visited"]}) - excluded
    comparison = []
    for model, metrics, predicted in [(item["model"], item["metrics"], set(item["included"])) for item in baselines] + [
            (MODEL, results["metrics"], included)]:
        fixed = nalt.calculate_precision_recall_f1(predicted & pooled, gold & pooled, pruned_uris=pooled - predicted)
        comparison.append({"model": model, "fresh_inference": model == MODEL,
                           "experiment_variant": manifest.get("experiment_variant", "strict") if model == MODEL else "retained_baseline",
                           "refusal_override_count": sum(row["decision_source"] == "user_refusal_override" for row in rows) if model == MODEL else 0,
                           **metrics,
                           "pooled_accuracy": fixed["accuracy"], "pooled_true_negative": fixed["true_negative"],
                           "pooled_evaluation_total": len(pooled)})
    write_json(directory / "comparison.json", comparison)
    write_csv(directory / "comparison.csv", comparison)
    write_csv(directory / "pooled_evaluation.csv", [{"uri": uri, "prefLabel": index.pref_labels[nalt.URIRef(uri)],
        "gold_include": uri in gold, "visited": uri in visited, "final_classification": "INCLUDE" if uri in included else "EXCLUDE",
        "model_classification": decisions.get(uri, {}).get("classification")
            if decisions.get(uri, {}).get("decision_source") == "openai_decisions" else None,
        "decision_source": decisions.get(uri, {}).get("decision_source", "unreached"),
        "p_include": decisions.get(uri, {}).get("p_include"), "p_exclude": decisions.get(uri, {}).get("p_exclude")}
        for uri in sorted(pooled)])
    missed = (gold - included) - excluded
    errors = {"false_positive": sorted((included - gold) - excluded),
              "false_negative_explicit_exclude": sorted(missed & visited), "false_negative_unreached": sorted(missed - visited)}
    write_json(directory / "errors.json", {name: [{"uri": uri, "label": index.pref_labels[nalt.URIRef(uri)],
                                                   "p_include": decisions.get(uri, {}).get("p_include")} for uri in uris]
                                           for name, uris in errors.items()})
    judged = [row for row in rows if row["decision_source"] == "openai_decisions" and row["scored"]]
    overrides = [row for row in rows if row["decision_source"] == "user_refusal_override"]
    write_json(directory / "refusal_overrides.json", overrides)
    audit = shared.audit_live_bfs(index, results, directory)
    audit["candidate_decisions"] = audit["model_decisions"]
    audit["model_decisions"] -= len(overrides)
    audit["refusal_override_count"] = len(overrides)
    audit["refusal_override_scope_valid"] = all(
        manifest["settings"]["exclude_desalination_on_refusal"] and row["uri"] == DESALINATION_URI
        and row["classification"] == "EXCLUDE" and row["provider_answer_type"] == "refusal"
        and all(row[key] is None for key in ("p_include", "p_exclude", "selected_probability", "provider_confidence"))
        for row in overrides)
    if not audit["refusal_override_scope_valid"]:
        raise ValueError("Refusal override provenance failed")
    prompt = nalt.resolve_system_prompt(Path(manifest["prompt_path"]))
    audit["prompt_text_preserved"] = all(question["instructions"].startswith(prompt + "\n\n")
        for path in (directory / "requests").glob("*.request.json") for question in read_json(path)["request"]["questions"])
    audit["baseline_files_unchanged"] = all(file_hash(ROOT / relative) == digest
        for baseline in baselines for relative, digest in baseline["hashes"].items())
    if not audit["prompt_text_preserved"] or not audit["baseline_files_unchanged"]:
        raise ValueError("Input preservation failed")
    write_json(directory / "audit.json", audit)
    write_json(directory / "metrics.json", {"metrics": results["metrics"], "iterations": results["iterations"],
        "seed_uris": results["seed_uris"], "gold_root_uri": results["gold_root_uri"], "exclude_evaluation_uris": sorted(excluded),
        "unreachable_gold_uris": sorted(results["unreachable_gold_uris"]),
        "visited_candidate_brier_score": statistics.mean((row["p_include"] - int(row["in_hierarchy_gold"])) ** 2 for row in judged) if judged else None,
        "visited_candidate_brier_count": len(judged), "false_negative_explicit_exclude_count": len(errors["false_negative_explicit_exclude"]),
        "experiment_variant": manifest.get("experiment_variant", "strict"), "refusal_override_count": len(overrides),
        "probability_metrics_exclude_overrides": True,
        "false_negative_unreached_count": len(errors["false_negative_unreached"]), "complete_traversal": audit["frontier_exhausted"]})
    return summarize_usage(directory, bfs_seconds), audit


def run_branch(branch: str, directory: Path, prepare_only: bool = False,
               exclude_desalination_on_refusal: bool = False) -> int:
    if exclude_desalination_on_refusal and branch != "soil_science":
        raise ValueError("The desalination refusal override is restricted to Soil science")
    started = time.perf_counter()
    directory.mkdir(parents=True, exist_ok=False)
    key = nalt.load_api_key("openai", ROOT / "config") if not prepare_only else None
    preset = nalt.resolve_benchmark_config(branch)
    prompt_path = ROOT / "usecase/prompts/manual" / f"{branch}.md"
    examples = ROOT / "usecase/prompts/examples" / f"{branch}_label_meta_examples.json"
    old_run = jev_runs()[branch]
    old_manifest = read_json(old_run / "manifest.json")
    manifest = {"started_at_utc": utc_now(), "status": "preparing", "benchmark_name": branch,
                "experiment_variant": "desalination_refusal_exclude" if exclude_desalination_on_refusal else "strict",
                "requested_model": MODEL, "endpoint": ENDPOINT, "source_path": str(shared.SOURCE), "source_sha256": file_hash(shared.SOURCE),
                "prompt_path": str(prompt_path), "prompt_file_sha256": file_hash(prompt_path),
                "transmitted_prompt_sha256": hashlib.sha256(nalt.resolve_system_prompt(prompt_path).encode()).hexdigest(),
                "examples_path": str(examples), "examples_sha256": file_hash(examples), "preset": preset,
                "jev_run": old_run.relative_to(ROOT).as_posix(),
                "settings": {**old_manifest["settings"], "transport": "curl", "prompt_cache_options": "provider defaults: implicit, 30m",
                             "exclude_desalination_on_refusal": exclude_desalination_on_refusal,
                             "refusal_override_uri": DESALINATION_URI if exclude_desalination_on_refusal else None,
                             "refusal_override_reason": DESALINATION_OVERRIDE_REASON if exclude_desalination_on_refusal else None,
                             "prompt_mapping": "Original system prompt + two newlines + identical Jev selector in each question; original Jev state in input."},
                "python": platform.python_version(), "platform": platform.platform(),
                "dependencies": {name: importlib.metadata.version(name) for name in ("rdflib", "pydantic", "openai")},
                "pricing": {"checked_on": PRICE_DATE, "sources": [GUIDE, PRICING], "rates_usd_per_million": RATES},
                "code_hashes": {path.relative_to(ROOT).as_posix(): file_hash(path) for path in
                    (Path(__file__).resolve(), ROOT / "decision_models/jev_nalt.py", ROOT / "usecase/subkg_extractor_nalt.py")}}
    manifest["transport_version"] = subprocess.run(["curl.exe" if sys.platform == "win32" else "curl", "--version"],
        capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout.splitlines()[0]
    write_json(directory / "manifest.json", manifest)
    (directory / "system_prompt.md").write_bytes(prompt_path.read_bytes())
    inference_started = None
    try:
        if manifest["source_sha256"] != shared.SOURCE_SHA256:
            raise ValueError("Unexpected NALT source")
        print(f"Preparing {branch}: fingerprinted NALT and exact baseline replay", flush=True)
        stage = time.perf_counter()
        graph = nalt.rdflib.Graph().parse(shared.SOURCE, format="xml")
        index = nalt.NaltSkosIndex(graph, ignored_uris={preset["gold_root_uri"]})
        manifest["graph_parse_and_index_seconds"] = time.perf_counter() - stage
        exclusions = nalt.resolve_example_candidate_uris(index, examples)
        stage = time.perf_counter()
        baselines = shared.replay_baselines(index, preset, prompt_path, exclusions, examples)
        baselines.append(replay_jev(index, preset, prompt_path, exclusions, examples, old_run))
        manifest["baseline_replay_seconds"] = time.perf_counter() - stage
        write_json(directory / "preflight.json", {"source_triples": len(graph), "baselines": baselines,
                                                   "exclusions": sorted(map(str, exclusions)), "same_jev_settings": True})
        if prepare_only:
            manifest.update(status="prepared", finished_at_utc=utc_now(), elapsed_seconds=time.perf_counter() - started)
            write_json(directory / "manifest.json", manifest)
            return 0
        manifest.update(status="running", live_inference_started_at_utc=utc_now())
        write_json(directory / "manifest.json", manifest)
        classifier = DecisionsClassifier(key, directory, exclude_desalination_on_refusal)
        inference_started = time.perf_counter()
        manifest["preparation_wall_seconds"] = inference_started - started
        results = shared.run_bfs(index, preset, prompt_path, exclusions, examples, classifier, MODEL, "openai")
        bfs_seconds = time.perf_counter() - inference_started
        usage, audit = export_results(index, results, directory, baselines, manifest, bfs_seconds)
        manifest.update(status="complete" if audit["frontier_exhausted"] else "iteration_limit_reached", finished_at_utc=utc_now(),
                        elapsed_seconds=time.perf_counter() - started, response_models=usage["response_models"])
        usage.update(total_run_wall_seconds=manifest["elapsed_seconds"], preparation_wall_seconds=manifest["preparation_wall_seconds"],
                     graph_parse_and_index_seconds=manifest["graph_parse_and_index_seconds"], baseline_replay_seconds=manifest["baseline_replay_seconds"],
                     postprocessing_wall_seconds=manifest["elapsed_seconds"] - manifest["preparation_wall_seconds"] - bfs_seconds)
        write_json(directory / "usage.json", usage)
        write_json(directory / "manifest.json", manifest)
        print(json.dumps({"branch": branch, "status": manifest["status"], "metrics": results["metrics"], "usage": usage}), flush=True)
        return 0 if audit["frontier_exhausted"] else 2
    except Exception as error:
        manifest.update(status="failed", finished_at_utc=utc_now(), elapsed_seconds=time.perf_counter() - started,
                        error_type=type(error).__name__, error_message=str(error).replace(key or "__no_key__", "[REDACTED]"))
        write_json(directory / "manifest.json", manifest)
        if (directory / "requests").exists():
            write_json(directory / "partial_usage.json", summarize_usage(directory, time.perf_counter() - inference_started if inference_started else None))
        raise


def run_study(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=False)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    old_files = {path.relative_to(ROOT).as_posix(): file_hash(path) for directory in jev_runs().values()
                 for path in directory.rglob("*") if path.is_file()}
    study = {"started_at_utc": utc_now(), "status": "running", "model": MODEL, "endpoint": ENDPOINT, "execution": "sequential",
             "branch_order": list(BRANCHES), "fresh_runs": [], "jev_run_index": FINAL_RUN_INDEX.relative_to(ROOT).as_posix(),
             "jev_artifact_hashes": old_files, "runner_sha256": file_hash(Path(__file__).resolve())}
    write_json(output / "study.json", study)
    (output / "logs").mkdir()
    started = time.perf_counter()
    for branch in BRANCHES:
        directory = ROOT / "decision_models/results/nalt" / branch / "openai-decisions-gpt-6-luna" / timestamp
        command = [sys.executable, "-u", str(Path(__file__).resolve()), "--benchmark", branch, "--output-dir", str(directory)]
        entry = {"branch": branch, "run_dir": directory.relative_to(ROOT).as_posix(), "command": command, "started_at_utc": utc_now()}
        branch_started = time.perf_counter()
        print(f"Starting {branch}", flush=True)
        with (output / "logs" / f"{branch}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith(("Preparing", "Decisions layer", "{", "Traceback", "RuntimeError", "ValueError")):
                    print(f"[{branch}] {line.rstrip()}", flush=True)
            entry["exit_code"] = process.wait()
        entry.update(finished_at_utc=utc_now(), process_wall_seconds=time.perf_counter() - branch_started)
        manifest = directory / "manifest.json"
        entry["status"] = read_json(manifest)["status"] if manifest.exists() else "failed_before_manifest"
        study["fresh_runs"].append(entry)
        write_json(output / "study.json", study)
        if entry["exit_code"] or entry["status"] != "complete":
            break
    preserved = all(file_hash(ROOT / relative) == digest for relative, digest in old_files.items())
    complete = len(study["fresh_runs"]) == len(BRANCHES) and all(item["status"] == "complete" for item in study["fresh_runs"])
    study.update(status="complete" if complete and preserved else "incomplete", finished_at_utc=utc_now(),
                 process_wall_seconds=time.perf_counter() - started, jev_artifacts_unchanged=preserved)
    write_json(output / "study.json", study)
    print(json.dumps({"status": study["status"], "study": str(output), "wall_seconds": study["process_wall_seconds"]}), flush=True)
    return 0 if complete and preserved else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=BRANCHES)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--exclude-desalination-on-refusal", action="store_true",
                        help="Soil only: apply the user-authorized EXCLUDE if desalination receives a refusal; preserve missing probabilities")
    args = parser.parse_args()
    if args.exclude_desalination_on_refusal and args.benchmark != "soil_science":
        parser.error("--exclude-desalination-on-refusal requires --benchmark soil_science")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.benchmark:
        directory = args.output_dir or ROOT / "decision_models/results/nalt" / args.benchmark / "openai-decisions-gpt-6-luna" / stamp
        return run_branch(args.benchmark, directory, args.prepare_only, args.exclude_desalination_on_refusal)
    if args.prepare_only:
        parser.error("--prepare-only requires --benchmark")
    return run_study(args.output_dir or ROOT / "decision_models/results/nalt" / f"openai_study_{stamp}")


if __name__ == "__main__":
    raise SystemExit(main())
