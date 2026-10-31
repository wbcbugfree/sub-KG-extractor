"""Run a live Jev classifier inside the existing NALT gated BFS."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from usecase import subkg_extractor_nalt as nalt


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
SOURCE = ROOT / "tmp/nalt-baseline-source/nalt-full_dwn_20240716.rdf"
SOURCE_SHA256 = "71a3e2e5ca84e5f680e1ebbf3fa7a5892415c58de716334b5add27b54af6d168"
INPUT_USD_PER_MILLION = 0.042
PRICE_CHECKED_ON = "2026-10-28"
PRICE_SOURCE = "https://docs.typesafe.ai/models"
CURL_METADATA_MARKER = b"\n__JEV_CURL_METADATA__"
RECORDS_FILE = "records.jsonl"
_RECORD_LOCK = threading.Lock()


class NoRedirect(HTTPRedirectHandler):
    """Keep the bearer credential on the explicitly selected API endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_api_key() -> str:
    """Read TypeSafe credentials without printing their contents."""
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if api_key:
        return api_key
    try:
        config = json.loads((ROOT / "config").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("Set TYPESAFE_API_KEY or configure typesafe_api_key in root config.") from None
    api_key = config.get("typesafe_api_key") if isinstance(config, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("No nonempty typesafe_api_key found in root config.")
    return api_key.strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def write_record(directory: Path, source: str, value) -> None:
    """Append one JSON line under a lock shared by all seed workers."""
    source = PurePosixPath(source.replace("\\", "/"))
    if source.is_absolute() or ".." in source.parts:
        raise ValueError("Record paths must be relative to the run")
    line = json.dumps({"path": source.as_posix(), "data": value}, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    with _RECORD_LOCK:
        with (directory / RECORDS_FILE).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)


def iter_records(directory: Path):
    """Read the compact log, or a legacy run's separate JSON trace files."""
    compact = directory / RECORDS_FILE
    if compact.exists():
        with compact.open(encoding="utf-8") as handle:
            for line in handle:
                entry = json.loads(line)
                yield entry["path"], entry["data"]
        return
    for name in ("requests", "layers", "timing", "seeds"):
        for path in sorted((directory / name).rglob("*.json")):
            if path.name.endswith((".body.json", ".response.json")):
                continue
            yield path.relative_to(directory).as_posix(), json.loads(path.read_text(encoding="utf-8"))


def records_in(directory: Path, folder: str, pattern: str = "*.json") -> list[tuple[str, object]]:
    """Select logical record paths; no extraction or scratch files are needed."""
    return sorted((source, value) for source, value in iter_records(directory)
                  if PurePosixPath(source).parent.name == folder
                  and fnmatch.fnmatchcase(PurePosixPath(source).name, pattern))


@contextmanager
def request_body_file(body: bytes):
    """Give curl a short-lived payload file in the ignored runtime directory."""
    runtime = ROOT / "tmp/decision-model-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="request-", suffix=".json", dir=runtime)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
        yield path
    finally:
        path.unlink(missing_ok=True)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def safe_headers(items) -> list[dict]:
    """Retain response metadata while removing cookie and credential values."""
    return [{"name": name, "value": "[REDACTED]" if any(
        token in name.casefold() for token in ("cookie", "authorization", "api-key", "token", "secret")
    ) else value} for name, value in items]


def parse_curl_output(output: bytes) -> tuple[bytes, dict]:
    """Separate all HTTP header blocks, response body, and curl transfer metrics."""
    response, transfer = output.rsplit(CURL_METADATA_MARKER, 1)
    metadata = {"curl": json.loads(transfer), "response_header_blocks": []}
    while response.startswith(b"HTTP/"):
        block, separator, remaining = response.partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("Incomplete HTTP header block")
        lines = block.decode("iso-8859-1").split("\r\n")
        items = [tuple(line.split(":", 1)) for line in lines[1:] if ":" in line]
        metadata["response_header_blocks"].append({
            "status_line": lines[0], "headers": safe_headers((name, value.strip()) for name, value in items),
        })
        response = remaining
    return response, metadata


def response_headers(metadata: dict) -> dict:
    blocks = metadata.get("response_header_blocks", [])
    return {item["name"].casefold(): item["value"] for item in blocks[-1]["headers"]} if blocks else {}


def attach_usage(record: dict) -> None:
    """Record known usage even if a response fails validation or has an HTTP error."""
    payload = record.get("response") or record.get("error_response") or {}
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    complete = all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens"))
    record["usage_available"] = complete
    record["input_tokens"] = usage.get("input_tokens") if complete else None
    record["output_tokens"] = usage.get("output_tokens") if complete else None
    record["estimated_cost_usd"] = usage["input_tokens"] * INPUT_USD_PER_MILLION / 1_000_000 if complete else None


def summarize_usage(output_dir: Path, bfs_seconds: float) -> dict:
    requests = records_in(output_dir, "requests")
    attempt_records = [(source, record) for source, record in requests if ".attempt_" in source]
    attempts = [record for _, record in attempt_records]
    for record in attempts:
        attach_usage(record)
    successes = [record for record in attempts if record["status"] == "success"]
    known = [record for record in attempts if record["usage_available"]]
    latencies = sorted(record["elapsed_seconds"] for record in successes)
    input_tokens = sum(record["input_tokens"] for record in known)
    output_tokens = sum(record["output_tokens"] for record in known)
    logical_requests = sum(source.endswith(".request.json") for source, _ in requests)
    table = []
    for source, record in attempt_records:
        headers = response_headers(record.get("http_metadata", {}))
        transfer = record.get("http_metadata", {}).get("curl", {})
        table.append({key: record.get(key) for key in (
            "iteration", "batch", "attempt", "started_at_utc", "finished_at_utc", "elapsed_seconds",
            "candidate_count", "status", "http_status", "input_tokens", "output_tokens", "estimated_cost_usd",
        )} | {"source": source,
             "request_id": headers.get("x-typesafe-request-id"),
             "response_model": record.get("response", {}).get("model"),
             "dns_seconds": transfer.get("time_namelookup"), "connect_seconds": transfer.get("time_connect"),
             "tls_seconds": transfer.get("time_appconnect"), "first_byte_seconds": transfer.get("time_starttransfer"),
             "transfer_seconds": transfer.get("time_total"), "upload_bytes": transfer.get("size_upload"),
             "download_bytes": transfer.get("size_download")})
    write_csv(output_dir / "api_calls.csv", table)
    return {
        "live_bfs_wall_seconds": bfs_seconds, "logical_requests": logical_requests,
        "successful_requests": len(successes), "request_attempts": len(attempts),
        "retries": len(attempts) - logical_requests, "failed_attempts": len(attempts) - len(successes),
        "attempts_without_usage": len(attempts) - len(known),
        "summed_attempt_latency_seconds": sum(record["elapsed_seconds"] for record in attempts),
        "median_success_latency_seconds": statistics.median(latencies) if latencies else None,
        "p95_success_latency_seconds": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
        "max_success_latency_seconds": max(latencies) if latencies else None,
        "usage_complete_for_successes": all(record["usage_available"] for record in successes),
        "usage_complete_for_attempts": len(known) == len(attempts),
        "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
        "successful_input_tokens": sum(record["input_tokens"] for record in successes if record["usage_available"]),
        "successful_output_tokens": sum(record["output_tokens"] for record in successes if record["usage_available"]),
        "input_usd_per_million": INPUT_USD_PER_MILLION, "output_usd_per_million": 0,
        "estimated_recorded_cost_usd": input_tokens * INPUT_USD_PER_MILLION / 1_000_000,
        "estimated_success_cost_usd": sum(record["estimated_cost_usd"] for record in successes if record["usage_available"]),
        "cost_basis": f"Published price checked {PRICE_CHECKED_ON}; estimate, not an invoice. Missing usage is not zero usage.",
        "price_source": PRICE_SOURCE, "price_checked_on": PRICE_CHECKED_ON,
        "response_models": sorted({record["response"]["model"] for record in successes}),
    }


def build_request(model: str, seed_label: str, batch: list[dict], system_prompt: str) -> dict:
    """Retain the original system text and the exact legacy user-message format."""
    state = f"""Seed topic: {seed_label}

Classify exactly {len(batch)} NALT candidate concepts as INCLUDE or EXCLUDE.

Candidate concepts:
{nalt.format_candidates_for_prompt(batch)}
"""
    return {
        "model": model,
        "state": state,
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


def probability(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Probability must be numeric")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Probability must be finite and between zero and one")
    return float(value)


def parse_response(payload: dict, batch: list[dict], iteration: int, batch_number: int) -> list[dict]:
    """Fail on malformed answers; never convert a provider failure to EXCLUDE."""
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        raise ValueError("Missing response model")
    expected = {f"candidate_{number}" for number in range(1, len(batch) + 1)}
    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) != expected:
        raise ValueError("Response does not contain exactly the requested candidates")
    rows = []
    for number, node in enumerate(batch, 1):
        answer = answers[f"candidate_{number}"]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ValueError("Expected a Choice answer")
        distribution = answer.get("probabilities")
        if not isinstance(distribution, dict) or set(distribution) != {"INCLUDE", "EXCLUDE"}:
            raise ValueError("Missing binary probability distribution")
        p_include = probability(distribution["INCLUDE"])
        p_exclude = probability(distribution["EXCLUDE"])
        if not math.isclose(p_include + p_exclude, 1.0, abs_tol=1e-5):
            raise ValueError("Probabilities do not sum to one")
        choice = answer.get("choice")
        if choice not in distribution or distribution[choice] < max(p_include, p_exclude) - 1e-6:
            raise ValueError("Returned Choice is not a maximum-probability option")
        rows.append({
            **node,
            "classification": choice,
            "confidence": probability(answer.get("confidence")),
            "p_include": p_include,
            "p_exclude": p_exclude,
            "selected_probability": distribution[choice],
            "decision_source": "jev",
            "iteration": iteration,
            "batch": batch_number,
            "candidate_number": number,
            "response_model": payload["model"],
        })
    return rows


class JevClassifier:
    """One binary Choice per reached candidate, batched within each BFS layer."""

    def __init__(self, api_key: str, output_dir: Path, transport: str = "curl", request_builder=None,
                 record_prefix: str = ""):
        self.api_key = api_key
        self.output_dir = output_dir
        self.transport = transport
        self.request_builder = request_builder or build_request
        self.record_prefix = record_prefix
        self.iteration = 0

    def post(self, body: bytes, metadata: dict | None = None) -> tuple[int, bytes]:
        """Use verified TLS; curl receives credentials on stdin, never argv or disk."""
        metadata = metadata if metadata is not None else {}
        if self.transport == "urllib":
            request = Request(ENDPOINT, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json", "Accept": "application/json",
            })
            with build_opener(NoRedirect).open(request, timeout=60) as response:
                metadata["response_header_blocks"] = [{"status_line": str(response.status),
                                                        "headers": safe_headers(response.headers.items())}]
                return response.status, response.read(4_000_000)
        headers = f"Authorization: Bearer {self.api_key}\nContent-Type: application/json\nAccept: application/json\n"
        if any(character in self.api_key for character in "\r\n"):
            raise ValueError("Invalid credential header")
        with request_body_file(body) as body_path:
            completed = subprocess.run(
                ["curl.exe" if sys.platform == "win32" else "curl", "--silent", "--show-error",
                 "--connect-timeout", "10", "--max-time", "60", "--request", "POST",
                 "--header", "@-", "--data-binary", f"@{body_path}",
                 "--dump-header", "-", "--write-out", CURL_METADATA_MARKER.decode() + "%{json}", ENDPOINT],
                input=headers.encode("utf-8"), capture_output=True, timeout=70,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        if CURL_METADATA_MARKER in completed.stdout:
            response_body, details = parse_curl_output(completed.stdout)
            metadata.update(details)
        if completed.returncode:
            metadata["curl_exit_code"] = completed.returncode
            metadata["curl_error"] = completed.stderr.decode("utf-8", errors="replace").replace(self.api_key, "[REDACTED]")
            raise OSError(f"curl transport exited with code {completed.returncode}")
        status = int(metadata["curl"]["http_code"])
        if status != 200:
            raise HTTPError(ENDPOINT, status, "TypeSafe HTTP error",
                            {"Retry-After": response_headers(metadata).get("retry-after", "")}, io.BytesIO(response_body))
        return status, response_body

    def request_batch(self, model, seed_label, batch, system_prompt, iteration, number):
        payload = self.request_builder(model, seed_label, batch, system_prompt)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        prefix = f"{self.record_prefix}requests/iteration_{iteration:02d}_batch_{number:03d}"
        write_record(self.output_dir, prefix + ".request.json", {
            "request": payload,
            "candidate_uris": [row["uri"] for row in batch],
            "request_sha256": hashlib.sha256(body).hexdigest(),
        })
        for attempt in range(1, 4):
            record = {"started_at_utc": utc_now(), "iteration": iteration, "batch": number,
                      "attempt": attempt, "candidate_count": len(batch), "endpoint": ENDPOINT,
                      "method": "POST", "transport": self.transport, "requested_model": model,
                      "request_body_bytes": len(body), "request_sha256": hashlib.sha256(body).hexdigest(),
                      "request_headers": {"Authorization": "[REDACTED]", "Content-Type": "application/json", "Accept": "application/json"},
                      "http_metadata": {}, "price_checked_on": PRICE_CHECKED_ON,
                      "input_usd_per_million": INPUT_USD_PER_MILLION, "output_usd_per_million": 0}
            started = time.perf_counter()
            retry_after = 2 ** attempt
            try:
                record["http_status"], response_body = self.post(body, record["http_metadata"])
                record["response_body_bytes"] = len(response_body)
                record["response_sha256"] = hashlib.sha256(response_body).hexdigest()
                answer = json.loads(response_body)
                record["response"] = answer
                rows = parse_response(answer, batch, iteration, number)
                record["status"] = "success"
            except HTTPError as error:
                record.update(status="http_error", http_status=error.code)
                # No authorization header is persisted; redact any reflected credential.
                record["error_body"] = error.read().decode("utf-8", errors="replace").replace(self.api_key, "[REDACTED]")
                if not record["http_metadata"]:
                    record["http_metadata"]["response_header_blocks"] = [{"status_line": str(error.code),
                                                                          "headers": safe_headers(error.headers.items())}]
                try:
                    parsed_error = json.loads(record["error_body"])
                    if isinstance(parsed_error, dict):
                        record["error_response"] = parsed_error
                except ValueError:
                    pass
                delay = error.headers.get("Retry-After", "")
                if delay:
                    try:
                        retry_after = max(retry_after, float(delay))
                    except ValueError:
                        pass
                error.close()
                retryable = error.code in {408, 429, 500, 502, 503, 504, 529}
            except (URLError, OSError, subprocess.TimeoutExpired) as error:
                record["status"] = "connection_error"
                record["error_type"] = type(error).__name__
                record["error_message"] = str(error).replace(self.api_key, "[REDACTED]")
                retryable = True
            except (ValueError, TypeError):
                record["status"] = "invalid_response"
                retryable = False
            record["elapsed_seconds"] = time.perf_counter() - started
            record["finished_at_utc"] = utc_now()
            attach_usage(record)
            write_record(self.output_dir, prefix + f".attempt_{attempt:02d}.json", record)
            if record["status"] == "success":
                return rows
            if not retryable or attempt == 3 or retry_after > 30:
                raise RuntimeError(f"Jev request failed: iteration {iteration}, batch {number}, {record['status']}")
            time.sleep(retry_after)
        raise RuntimeError("Jev attempts exhausted")

    def __call__(self, *, model, seed_label, nodes, batch_size, max_workers, system_prompt, **_):
        started_at = utc_now()
        started = time.perf_counter()
        self.iteration += 1
        iteration = self.iteration
        batches = [nodes[start:start + batch_size] for start in range(0, len(nodes), batch_size)]
        print(f"Jev layer {iteration}: {len(nodes)} candidates in {len(batches)} live requests", flush=True)
        with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
            futures = [executor.submit(self.request_batch, model, seed_label, batch, system_prompt, iteration, number)
                       for number, batch in enumerate(batches, 1)]
            rows = [row for future in futures for row in future.result()]
        write_record(self.output_dir, f"{self.record_prefix}layers/layer_{iteration:02d}.json", rows)
        write_record(self.output_dir, f"{self.record_prefix}timing/layer_{iteration:02d}.json", {
            "iteration": iteration, "started_at_utc": started_at, "finished_at_utc": utc_now(),
            "wall_seconds": time.perf_counter() - started, "candidate_count": len(nodes),
            "logical_requests": len(batches), "workers": min(max_workers, len(batches)),
        })
        return rows


def run_bfs(index, preset, prompt_path, exclusions, examples_path, classifier, model, provider):
    return nalt.iterative_nalt_extraction(
        index=index, seed_labels=preset["seed_labels"], gold_root_uri=preset["gold_root_uri"],
        benchmark_name=preset["benchmark_name"], gold_root_label=preset["gold_root_label"],
        output_subdir=preset["output_subdir"], max_iterations=20, batch_size=20,
        llm_model=model, llm_provider=provider, llm_max_workers=5,
        system_prompt=nalt.resolve_system_prompt(prompt_path), prompt_file=prompt_path,
        result_mode="zero_shot_manual", exclude_evaluation_uris=exclusions,
        exclude_examples_files=[examples_path], classifier=classifier,
    )


def replay_baselines(index, preset, prompt_path, exclusions, examples_path):
    """Verify the source, held-out set, metrics, and complete saved BFS traces."""
    baselines = []
    for path in sorted((ROOT / "usecase/results").glob(f"*/{preset['output_subdir']}/zero_shot_manual/*_METRICS.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        prefix = path.name.removesuffix("_METRICS.json")
        related = {kind: path.with_name(f"{prefix}_{kind}.csv") for kind in ("INCLUDED", "PRUNED", "VISITED")}
        rows = {kind: read_csv(csv_path) for kind, csv_path in related.items()}
        decisions = {row["uri"]: row for row in rows["INCLUDED"] + rows["PRUNED"]}

        def cached_classifier(*, nodes, **_):
            return [{**node, "classification": decisions[node["uri"]]["classification"],
                     "confidence": float(decisions[node["uri"]]["confidence"])} for node in nodes]

        if set(map(str, exclusions)) != set(saved["exclude_evaluation_uris"]):
            raise ValueError("Baseline demonstration exclusions differ")
        saved_prompt = Path(saved["run_config"]["prompt_file"].replace("\\", "/"))
        if saved_prompt.is_absolute():
            saved_prompt = Path("usecase/prompts/manual") / prompt_path.name
        if (ROOT / saved_prompt).resolve() != prompt_path.resolve():
            raise ValueError("Baseline uses a different prompt")
        replay = run_bfs(index, preset, prompt_path, exclusions, examples_path,
                         cached_classifier, saved["llm_model"], saved["llm_provider"])
        for key, kind in (("all_included", "INCLUDED"), ("all_pruned", "PRUNED"), ("all_visited", "VISITED")):
            if {row["uri"] for row in replay[key]} != {row["uri"] for row in rows[kind]}:
                raise ValueError(f"Baseline replay mismatch: {path.name} {kind}")
        if replay["iterations"] != saved["iterations"]:
            raise ValueError("Baseline BFS layers differ")
        for key, value in saved["metrics"].items():
            if key in replay["metrics"] and not math.isclose(replay["metrics"][key], value, abs_tol=1e-12):
                raise ValueError(f"Baseline metric differs: {key}")
        for row in rows["VISITED"]:
            current = index.node_info(row["uri"])
            if current["displayName"] != row["displayName"] or (row["in_hierarchy_gold"].lower() == "true") != (row["uri"] in replay["gold_uris"]):
                raise ValueError("Baseline labels differ from the recovered source")
        baselines.append({
            "model": saved["llm_model"], "metrics": saved["metrics"],
            "included": sorted(row["uri"] for row in rows["INCLUDED"]),
            "visited": sorted(row["uri"] for row in rows["VISITED"]),
            "artifact": path.relative_to(ROOT).as_posix(),
            "hashes": {str(item.relative_to(ROOT).as_posix()): file_hash(item) for item in [path, *related.values()]},
            "source_and_bfs_replay_exact": True,
        })
    if len(baselines) != 3:
        raise ValueError("Expected three retained human-authored LLM baselines")
    return baselines


def audit_live_bfs(index, results, output_dir):
    """Independently replay reachable frontiers and verify the actual request sets."""
    seen = set(results["seed_uris"])
    included = set(seen)
    pending = {row["uri"] for uri in seen for row in index.one_hop_neighbors(uri)} - seen
    layers = records_in(output_dir, "layers", "layer_*.json")
    request_records = records_in(output_dir, "requests", "*.request.json")
    traces = []
    for number, (_, layer) in enumerate(layers, 1):
        layer_uris = {row["uri"] for row in layer}
        if layer_uris != pending or len(layer) != len(layer_uris):
            raise ValueError("Live decisions are not exactly the reachable frontier")
        requests = [record for source, record in request_records
                    if PurePosixPath(source).name.startswith(f"iteration_{number:02d}_batch_")]
        requested = [uri for request in requests for uri in request["candidate_uris"]]
        if len(requested) != len(layer) or set(requested) != layer_uris:
            raise ValueError("Live requests do not match the BFS layer")
        kept = {row["uri"] for row in layer if row["classification"] == "INCLUDE"}
        seen.update(layer_uris)
        included.update(kept)
        traces.append({"iteration": number, "evaluated": len(layer), "included": len(kept), "pruned": len(layer) - len(kept)})
        pending = {row["uri"] for uri in kept for row in index.one_hop_neighbors(uri)} - seen
    if seen != {row["uri"] for row in results["all_visited"]} or included != {row["uri"] for row in results["all_included"]}:
        raise ValueError("Live replay disagrees with extraction outputs")
    if traces != results["iterations"]:
        raise ValueError("Live replay disagrees with recorded layers")
    return {"exact_frontier_requests": True, "included_only_expansion": True,
            "visited_once": True, "iteration_trace_exact": True,
            "remaining_frontier_count": len(pending), "frontier_exhausted": not pending,
            "model_decisions": len(seen) - len(results["seed_uris"])}


def export_results(index, results, output_dir, baselines, run_manifest, bfs_seconds):
    decisions = {row["uri"]: dict(row) for row in results["all_included"] + results["all_pruned"]}
    seeds, excluded, gold = map(set, (results["seed_uris"], results["exclude_evaluation_uris"], results["gold_uris"]))
    for uri in seeds:
        decisions[uri].update(decision_source="seed", iteration=0, p_include=None, p_exclude=None,
                              selected_probability=None, confidence=None, batch=None, candidate_number=None, response_model=None)
    ordered = [decisions[row["uri"]] for row in results["all_visited"]]
    csv_rows = [{
        "uri": row["uri"], "prefLabel": row["prefLabel"], "altLabels": "; ".join(row["altLabels"]),
        "classification": row["classification"], "p_include": row.get("p_include"),
        "p_exclude": row.get("p_exclude"), "selected_probability": row.get("selected_probability"),
        "provider_confidence": row.get("confidence"), "decision_source": row["decision_source"],
        "iteration": row["iteration"], "batch": row.get("batch"), "candidate_number": row.get("candidate_number"),
        "response_model": row.get("response_model"), "in_hierarchy_gold": row["uri"] in gold,
        "scored": row["uri"] not in excluded, "relations": "; ".join(row["relations"]),
    } for row in ordered]
    write_csv(output_dir / "decisions.csv", csv_rows)
    included = {uri for uri, row in decisions.items() if row["classification"] == "INCLUDE"}
    visited = set(decisions)
    pooled = (gold | visited | {uri for baseline in baselines for uri in baseline["visited"]}) - excluded
    comparison = []
    for model, metrics, predicted in [(item["model"], item["metrics"], set(item["included"])) for item in baselines] + [
        (results["llm_model"], results["metrics"], included)
    ]:
        fixed = nalt.calculate_precision_recall_f1(predicted & pooled, gold & pooled,
                                                  pruned_uris=pooled - predicted)
        comparison.append({"model": model, "fresh_inference": model == results["llm_model"],
                           **metrics, "pooled_accuracy": fixed["accuracy"],
                           "pooled_true_negative": fixed["true_negative"], "pooled_evaluation_total": len(pooled)})
    write_csv(output_dir / "comparison.csv", comparison)
    evaluation = []
    for uri in sorted(pooled, key=lambda value: (index.pref_labels[nalt.URIRef(value)].casefold(), value)):
        node = decisions.get(uri, {})
        evaluation.append({"uri": uri, "prefLabel": index.pref_labels[nalt.URIRef(uri)], "gold_include": uri in gold,
                           "visited": uri in visited, "final_classification": "INCLUDE" if uri in included else "EXCLUDE",
                           "model_classification": node.get("classification") if uri not in seeds else None,
                           "decision_source": node.get("decision_source", "unreached"), "p_include": node.get("p_include"),
                           "p_exclude": node.get("p_exclude")})
    write_csv(output_dir / "pooled_evaluation.csv", evaluation)
    false_negatives = (gold - included) - excluded
    errors = {
        "false_positive": sorted((included - gold) - excluded),
        "false_negative_explicit_exclude": sorted(false_negatives & visited),
        "false_negative_unreached": sorted(false_negatives - visited),
    }
    write_record(output_dir, "errors.json", {kind: [{"uri": uri, "label": index.pref_labels[nalt.URIRef(uri)],
                       "p_include": decisions.get(uri, {}).get("p_include")} for uri in uris] for kind, uris in errors.items()})
    judged = [row for row in csv_rows if row["decision_source"] == "jev" and row["scored"]]
    brier = statistics.mean((row["p_include"] - int(row["in_hierarchy_gold"])) ** 2 for row in judged) if judged else None
    costs = summarize_usage(output_dir, bfs_seconds)
    audit = audit_live_bfs(index, results, output_dir)
    audit["prompt_text_preserved"] = all(
        question["instructions"]["system_prompt"] == nalt.resolve_system_prompt(Path(run_manifest["prompt_path"]))
        for _, record in records_in(output_dir, "requests", "*.request.json")
        for question in record["request"]["questions"].values()
    )
    audit["baseline_files_unchanged"] = all(file_hash(ROOT / relative) == digest
                                             for baseline in baselines for relative, digest in baseline["hashes"].items())
    if not audit["prompt_text_preserved"] or not audit["baseline_files_unchanged"]:
        raise ValueError("Input preservation audit failed")
    write_record(output_dir, "audit.json", audit)
    write_json(output_dir / "usage.json", costs)
    write_json(output_dir / "metrics.json", {"metrics": results["metrics"], "iterations": results["iterations"],
               "seed_uris": results["seed_uris"], "gold_root_uri": results["gold_root_uri"],
               "exclude_evaluation_uris": sorted(excluded), "unreachable_gold_uris": sorted(results["unreachable_gold_uris"]),
               "visited_candidate_brier_score": brier, "visited_candidate_brier_count": len(judged),
               "false_negative_explicit_exclude_count": len(errors["false_negative_explicit_exclude"]),
               "false_negative_unreached_count": len(errors["false_negative_unreached"]),
               "complete_traversal": audit["frontier_exhausted"]})
    return comparison, costs, audit, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=sorted(nalt.NALT_BENCHMARK_PRESETS), default="plant_health")
    parser.add_argument("--model", default="jev-1.14.0")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--transport", choices=("curl", "urllib"), default="curl")
    parser.add_argument("--prepare-only", action="store_true", help="Validate inputs and replay baselines without calling Jev.")
    args = parser.parse_args()
    started = time.perf_counter()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or ROOT / "decision_models/results/nalt" / args.benchmark / args.model / timestamp
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"started_at_utc": utc_now(), "status": "preparing", "benchmark_name": args.benchmark,
                "requested_model": args.model, "source_path": str(args.source), "source_sha256": file_hash(args.source),
                "python": platform.python_version(), "platform": platform.platform(),
                "dependencies": {name: importlib.metadata.version(name) for name in ("rdflib", "pydantic", "openai")},
                "code_hashes": {str(path.relative_to(ROOT).as_posix()): file_hash(path) for path in
                                [Path(__file__).resolve(), ROOT / "usecase/subkg_extractor_nalt.py"]},
                "pricing": {"checked_on": PRICE_CHECKED_ON, "source": PRICE_SOURCE,
                            "input_usd_per_million": INPUT_USD_PER_MILLION, "output_usd_per_million": 0},
                "settings": {"batch_size": 20, "max_workers": 5, "max_iterations": 20, "request_timeout_seconds": 60,
                             "transport": args.transport,
                             "max_attempts": 3, "primitive": "choice", "decision_rule": "provider Choice (argmax)",
                             "confidence_gating": False, "fallback_provider": None}}
    manifest["records_file"] = RECORDS_FILE
    write_json(output_dir / "manifest.json", manifest)
    try:
        manifest["transport_version"] = subprocess.run(
            ["curl.exe" if sys.platform == "win32" else "curl", "--version"],
            capture_output=True, text=True, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ).stdout.splitlines()[0] if args.transport == "curl" else platform.python_version()
        if manifest["source_sha256"] != SOURCE_SHA256:
            raise ValueError("NALT source does not match the fingerprinted July 2024 release")
        preset = nalt.resolve_benchmark_config(args.benchmark)
        prompt_path = ROOT / "usecase/prompts/manual" / f"{args.benchmark}.md"
        examples_path = ROOT / "usecase/prompts/examples" / f"{args.benchmark}_label_meta_examples.json"
        prompt = nalt.resolve_system_prompt(prompt_path)
        (output_dir / "system_prompt.md").write_bytes(prompt_path.read_bytes())
        manifest.update(prompt_path=str(prompt_path), prompt_file_sha256=file_hash(prompt_path),
                        transmitted_prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        examples_path=str(examples_path), examples_sha256=file_hash(examples_path), preset=preset)
        write_json(output_dir / "manifest.json", manifest)
        print("Parsing fingerprinted NALT RDF/XML source", flush=True)
        graph_started = time.perf_counter()
        graph = nalt.rdflib.Graph().parse(args.source, format="xml")
        index = nalt.NaltSkosIndex(graph, ignored_uris={preset["gold_root_uri"]})
        manifest["graph_parse_and_index_seconds"] = time.perf_counter() - graph_started
        exclusions = nalt.resolve_example_candidate_uris(index, examples_path)
        description = nalt.describe_benchmark(index, seed_labels=preset["seed_labels"], gold_root_uri=preset["gold_root_uri"])
        branch_sizes = []
        for branch in nalt.NALT_BENCHMARK_PRESETS:
            path = next((ROOT / "usecase/results/gpt-5.4-mini" / nalt.NALT_BENCHMARK_PRESETS[branch]["output_subdir"] / "zero_shot_manual").glob("*_METRICS.json"))
            metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]
            branch_sizes.append({"branch": branch, "raw_gold": metrics["raw_gold_positive_count"], "evaluation_gold": metrics["gold_positive_count"]})
        print("Replaying all three retained human-authored LLM runs offline", flush=True)
        replay_started = time.perf_counter()
        baselines = replay_baselines(index, preset, prompt_path, exclusions, examples_path)
        manifest["baseline_replay_seconds"] = time.perf_counter() - replay_started
        write_json(output_dir / "preflight.json", {"source_triples": len(graph), "description": description,
                   "branch_sizes": branch_sizes, "smallest_by_evaluation_gold": min(branch_sizes, key=lambda row: row["evaluation_gold"])["branch"],
                   "exclusions": sorted(map(str, exclusions)), "baselines": baselines})
        if args.prepare_only:
            manifest.update(status="prepared", finished_at_utc=utc_now(), elapsed_seconds=time.perf_counter() - started)
            write_json(output_dir / "manifest.json", manifest)
            print(f"Prepared without API calls: {output_dir}", flush=True)
            return 0
        manifest.update(status="running", live_inference_started_at_utc=utc_now())
        write_json(output_dir / "manifest.json", manifest)
        classifier = JevClassifier(load_api_key(), output_dir, transport=args.transport)
        inference_started = time.perf_counter()
        manifest["preparation_wall_seconds"] = inference_started - started
        results = run_bfs(index, preset, prompt_path, exclusions, examples_path, classifier, args.model, "typesafe")
        bfs_seconds = time.perf_counter() - inference_started
        comparison, costs, audit, errors = export_results(index, results, output_dir, baselines, manifest, bfs_seconds)
        manifest.update(status="complete" if audit["frontier_exhausted"] else "iteration_limit_reached",
                        finished_at_utc=utc_now(), elapsed_seconds=time.perf_counter() - started,
                        response_models=costs["response_models"])
        costs.update(total_run_wall_seconds=manifest["elapsed_seconds"],
                     preparation_wall_seconds=manifest["preparation_wall_seconds"],
                     graph_parse_and_index_seconds=manifest["graph_parse_and_index_seconds"],
                     baseline_replay_seconds=manifest["baseline_replay_seconds"],
                     postprocessing_wall_seconds=manifest["elapsed_seconds"] - manifest["preparation_wall_seconds"] - bfs_seconds)
        write_json(output_dir / "usage.json", costs)
        write_json(output_dir / "manifest.json", manifest)
        print(json.dumps({"metrics": results["metrics"], "usage": costs, "audit": audit, "output_dir": str(output_dir)}, indent=2), flush=True)
        return 0 if audit["frontier_exhausted"] else 2
    except Exception as error:
        manifest.update(status="failed", finished_at_utc=utc_now(), error_type=type(error).__name__)
        write_json(output_dir / "manifest.json", manifest)
        if (output_dir / RECORDS_FILE).exists():
            write_json(output_dir / "partial_usage.json", summarize_usage(output_dir, time.perf_counter() - inference_started))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
