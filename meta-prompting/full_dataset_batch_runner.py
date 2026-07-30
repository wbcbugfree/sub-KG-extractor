#!/usr/bin/env python3
"""
Batch API runner for full Wikidata prompt experiments.

This runner scores every evaluable dataset row with row-level batch requests,
then applies BFS-consistent branch gating offline. Datasets 1 and 2 use labelled
parent decisions; Dataset 3 replays its per-seed graph so unlabeled bridge nodes
are traversed without becoming decisions.
"""

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from datetime import timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import subkg_extractor_wikidata as pe  # noqa: E402

DEFAULT_BATCH_SIZE = 20
DEFAULT_CACHE_TTL = "86400s"
DEFAULT_OPENAI_COMPLETION_WINDOW = "24h"
OPENAI_BATCH_ENDPOINT = "/v1/responses"

CACHE_MINIMUM_APPENDIX = """

## Cached Batch Response Contract

This appendix exists only to make the reusable cached prompt long enough for
Gemini context caching. It contains no labelled examples, no candidate labels,
and no dataset-specific keep/prune decisions.

For each future batch, classify only the numbered candidate items supplied in
the user message. Use the seed label, candidate label, relation names, and full
path evidence in that batch. Do not infer hidden QIDs, row identifiers, or
unstated metadata. Do not classify any node that appears only as an intermediate
path bridge unless it is the final candidate in a numbered item.

Return one structured evaluation per numbered item. The `batch_index` must copy
the item number from the batch prompt. The `classification` must be exactly
`INCLUDE` or `EXCLUDE`. The `confidence` should be a number from 0.0 to 1.0
reflecting decision strength. If a candidate is ambiguous, prefer the decision
that best preserves a focused seed-specific subgraph rather than a broad,
drifting neighborhood.

Apply the manual relevance policy above consistently across all future batches.
"""

STRATEGIES = (
    "zero_shot_manual",
    "few_shot_manual",
    "zero_shot_meta",
    "few_shot_meta",
)

RUN_ARTIFACT_FILENAMES = {
    "batch_results.jsonl",
    "bfs_gated_predictions.csv",
    "manifest.json",
    "requests.jsonl",
    "rows.csv",
    "summary.json",
}


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def file_sha256(path):
    return pe.file_sha256(path) if path else None


def text_sha256(text):
    return pe.text_sha256(text)


def cache_too_small_error(exc):
    text = str(exc)
    return "Cached content is too small" in text and "min_total_token_count" in text


def cacheable_prompt(prompt):
    return prompt.rstrip() + CACHE_MINIMUM_APPENDIX


def find_single(pattern, root):
    matches = sorted(Path(root).glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No file matched {pattern!r} under {root}")
    if len(matches) > 1:
        raise RuntimeError(
            f"Expected one file for {pattern!r} under {root}, found: "
            + ", ".join(str(path) for path in matches)
        )
    return matches[0]


def default_examples_file(dataset, examples_dir, strategy="manual"):
    return pe.default_examples_file(
        dataset,
        strategy,
        examples_dir=examples_dir,
    )


def default_evaluation_examples_file(dataset, examples_dir):
    """Return the fixed example exclusion pack shared by all strategies."""
    return default_examples_file(
        dataset,
        examples_dir,
        strategy="zero_shot_meta",
    )


def default_output_dir(model):
    return SCRIPT_DIR / "results" / model


def strategy_run_dir(output_dir, dataset, strategy):
    return Path(output_dir) / f"dataset{dataset}" / strategy


def initialize_run_dir(run_dir, overwrite=False):
    run_dir = Path(run_dir)
    if not run_dir.exists():
        run_dir.mkdir(parents=True)
        return
    if not run_dir.is_dir():
        raise NotADirectoryError(f"Run path is not a directory: {run_dir}")

    existing = list(run_dir.iterdir())
    if not existing:
        return
    if not overwrite:
        raise FileExistsError(
            f"Run directory already contains artifacts: {run_dir}. "
            "Pass --overwrite to replace the canonical strategy run."
        )

    unsupported = [
        path for path in existing
        if not path.is_file() or path.name not in RUN_ARTIFACT_FILENAMES
    ]
    if unsupported:
        raise RuntimeError(
            "Refusing to overwrite unrecognized run artifacts: "
            + ", ".join(str(path) for path in unsupported)
        )
    for path in existing:
        path.unlink()


def default_prompt_file(dataset, strategy, prompts_dir, model):
    prompts_dir = Path(prompts_dir)
    if strategy.endswith("_manual"):
        return prompts_dir / f"manual_dataset{dataset}.md"
    return find_single(f"generated_dataset{dataset}_*.md", prompts_dir / model)


def load_effective_prompt(dataset, strategy, prompts_dir, examples_dir,
                          model, prompt_file=None, examples_file=None):
    prompt_path = (
        Path(prompt_file)
        if prompt_file
        else default_prompt_file(dataset, strategy, prompts_dir, model)
    )
    examples_path = (
        Path(examples_file)
        if examples_file
        else default_examples_file(dataset, examples_dir, strategy)
    )
    base_prompt = prompt_path.read_text(encoding="utf-8")
    examples_payload = pe.load_examples(examples_path)
    include_examples = strategy.startswith("few_shot")
    prompt = pe.build_effective_prompt(
        base_prompt,
        examples_payload=examples_payload,
        include_examples=include_examples,
    )
    context_level = examples_payload.get("context_level", "path_simple")
    return {
        "prompt": prompt,
        "prompt_file": prompt_path,
        "examples_file": examples_path,
        "examples_payload": examples_payload,
        "examples_in_prompt": include_examples,
        "context_level": context_level,
    }


def load_full_dataset_rows(dataset, evaluation_examples_payload,
                           data_dir=pe.DEFAULT_DATA_DIR):
    rows = pe.augment_rows_with_graph_context(
        dataset,
        pe.load_decision_rows(dataset, data_dir),
        data_dir,
    )
    graph_context = pe.load_graph_context(dataset, data_dir)
    rows, skipped_absent = pe.skip_rows_absent_from_local_graph(rows, graph_context)
    exclude_ids = {
        example["row_id"]
        for example in evaluation_examples_payload.get("examples", [])
        if example.get("row_id")
    }
    rows = pe.filter_rows(rows, exclude_ids=exclude_ids)
    rows.sort(key=lambda row: (
        row["seed_qid"],
        row.get("bfs_depth") or row.get("csv_depth") or 9999,
        row.get("row_index", 0),
    ))
    return rows, {
        "skipped_rows_absent_from_local_graph": skipped_absent,
        "excluded_example_rows": len(exclude_ids),
    }


def chunk_rows(rows, batch_size):
    batch_size = max(1, int(batch_size))
    for start in range(0, len(rows), batch_size):
        yield rows[start:start + batch_size]


def batch_response_schema_for_gemini_jsonl():
    return {
        "type": "OBJECT",
        "properties": {
            "evaluations": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "idx": {
                            "type": "INTEGER",
                            "minimum": 1.0,
                            "title": "Idx",
                        },
                        "classification": {
                            "type": "STRING",
                            "enum": ["INCLUDE", "EXCLUDE"],
                            "title": "Classification",
                        },
                        "confidence": {
                            "type": "NUMBER",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "title": "Confidence",
                        },
                    },
                    "propertyOrdering": ["idx", "classification", "confidence"],
                    "required": ["idx", "classification", "confidence"],
                    "title": "PromptDecision",
                },
                "title": "Evaluations",
            }
        },
        "required": ["evaluations"],
        "title": "BatchResponse",
    }


def batch_response_schema_for_openai():
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "idx": {
                            "type": "integer",
                            "minimum": 1,
                        },
                        "classification": {
                            "type": "string",
                            "enum": ["INCLUDE", "EXCLUDE"],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["idx", "classification", "confidence"],
                },
            },
        },
        "required": ["evaluations"],
    }


def openai_text_format():
    return {
        "format": {
            "type": "json_schema",
            "name": "batch_response",
            "strict": True,
            "schema": batch_response_schema_for_openai(),
        },
    }


def default_openai_prompt_cache_key(dataset, strategy, model):
    return f"subkg-d{dataset}-{strategy}-{model}"


def build_gemini_batch_request(request_key, batch_rows, context_level, cached_content):
    user_message = pe.build_eval_user_message(batch_rows, context_level)
    return {
        "key": request_key,
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_message}],
                }
            ],
            "cachedContent": cached_content,
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": batch_response_schema_for_gemini_jsonl(),
            },
        },
    }


def build_openai_batch_request(request_key, batch_rows, context_level, prompt,
                               model, prompt_cache_key=None,
                               prompt_cache_retention=None):
    user_message = pe.build_eval_user_message(batch_rows, context_level)
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_message},
        ],
        "text": openai_text_format(),
    }
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    if prompt_cache_retention:
        body["prompt_cache_retention"] = prompt_cache_retention
    return {
        "custom_id": request_key,
        "method": "POST",
        "url": OPENAI_BATCH_ENDPOINT,
        "body": body,
    }


def write_gemini_batch_jsonl(rows, context_level, cached_content, output_path,
                             dataset, strategy, batch_size):
    requests = []
    with Path(output_path).open("w", encoding="utf-8", newline="\n") as handle:
        for batch_index, batch in enumerate(chunk_rows(rows, batch_size), start=1):
            request_key = f"dataset{dataset}_{strategy}_batch{batch_index:06d}"
            request = build_gemini_batch_request(
                request_key=request_key,
                batch_rows=batch,
                context_level=context_level,
                cached_content=cached_content,
            )
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
            requests.append({
                "key": request_key,
                "row_ids": [row["row_id"] for row in batch],
            })
    return requests


def write_openai_batch_jsonl(rows, context_level, prompt, output_path,
                             dataset, strategy, batch_size, model,
                             prompt_cache_key=None,
                             prompt_cache_retention=None):
    requests = []
    with Path(output_path).open("w", encoding="utf-8", newline="\n") as handle:
        for batch_index, batch in enumerate(chunk_rows(rows, batch_size), start=1):
            request_key = f"dataset{dataset}_{strategy}_batch{batch_index:06d}"
            request = build_openai_batch_request(
                request_key=request_key,
                batch_rows=batch,
                context_level=context_level,
                prompt=prompt,
                model=model,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
            requests.append({
                "key": request_key,
                "row_ids": [row["row_id"] for row in batch],
            })
    return requests


def write_rows_csv(path, rows):
    fieldnames = [
        "row_id", "legacy_row_id", "seed_qid", "seed_label", "qid", "label", "target",
        "csv_depth", "bfs_depth", "parent_qid", "parent_label", "reaching_property",
        "reaching_property_label", "path",
    ]
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def read_rows_csv(path):
    int_fields = {"target", "csv_depth", "bfs_depth"}
    rows = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            for field in int_fields:
                if row.get(field) not in ("", None):
                    row[field] = int(row[field])
                else:
                    row[field] = None
            rows.append(row)
    return rows


def create_context_cache(client, model, prompt, ttl, display_name):
    from google.genai import types

    cache = client.caches.create(
        model=model,
        config=types.CreateCachedContentConfig(
            display_name=display_name,
            system_instruction=prompt,
            ttl=ttl,
        ),
    )
    return cache


def create_gemini_client():
    return pe.get_gemini_client()


def create_openai_client():
    return pe.get_openai_client()


def submit_gemini_batch_job(client, model, jsonl_path, display_name):
    from google.genai import types

    uploaded_file = client.files.upload(
        file=str(jsonl_path),
        config=types.UploadFileConfig(
            display_name=display_name,
            mime_type="jsonl",
        ),
    )
    job = client.batches.create(
        model=model,
        src=uploaded_file.name,
        config={"display_name": display_name},
    )
    return uploaded_file, job


def submit_openai_batch_job(client, jsonl_path, display_name, completion_window):
    with Path(jsonl_path).open("rb") as handle:
        uploaded_file = client.files.create(
            file=handle,
            purpose="batch",
        )
    job = client.batches.create(
        input_file_id=uploaded_file.id,
        endpoint=OPENAI_BATCH_ENDPOINT,
        completion_window=completion_window,
        metadata={"description": display_name},
    )
    return uploaded_file, job


def maybe_state_name(state):
    return getattr(state, "name", state)


def download_gemini_batch_results(client, job_name, output_path):
    job = client.batches.get(name=job_name)
    state = maybe_state_name(job.state)
    if state != "JOB_STATE_SUCCEEDED":
        raise RuntimeError(f"Batch job is not complete: {job_name} state={state}")
    if not job.dest or not getattr(job.dest, "file_name", None):
        raise RuntimeError(f"Batch job has no result file: {job_name}")
    content = client.files.download(file=job.dest.file_name)
    Path(output_path).write_bytes(content)
    return job.dest.file_name


def download_openai_batch_results(client, job_name, output_path):
    job = client.batches.retrieve(job_name)
    if job.status != "completed":
        raise RuntimeError(f"Batch job is not complete: {job_name} status={job.status}")
    if not job.output_file_id:
        raise RuntimeError(f"Batch job has no output file: {job_name}")
    file_response = client.files.content(job.output_file_id)
    if hasattr(file_response, "write_to_file"):
        file_response.write_to_file(str(output_path))
    elif hasattr(file_response, "content"):
        Path(output_path).write_bytes(file_response.content)
    else:
        text = getattr(file_response, "text", None)
        if callable(text):
            text = text()
        Path(output_path).write_text(text or str(file_response), encoding="utf-8")
    return job.output_file_id


def extract_output_text_from_openai_body(payload):
    texts = []
    if payload.get("output_text"):
        texts.append(payload["output_text"])
    for item in payload.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                texts.append(part["text"])
            elif part.get("text"):
                texts.append(part["text"])
    if not texts:
        choices = payload.get("choices") or []
        for choice in choices:
            message = choice.get("message") or {}
            content = message.get("content")
            if content:
                texts.append(content)
    return "\n".join(texts)


def extract_response_text(payload):
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if "body" in payload and isinstance(payload["body"], dict):
        return extract_response_text(payload["body"])
    if "output" in payload or "output_text" in payload or "choices" in payload:
        return extract_output_text_from_openai_body(payload)
    if "text" in payload and isinstance(payload["text"], str):
        return payload["text"]
    candidates = payload.get("candidates") or []
    texts = []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if text:
                texts.append(text)
    return "\n".join(texts)


def parse_result_line(line):
    payload = json.loads(line)
    if "custom_id" in payload:
        key = payload.get("custom_id")
        error = payload.get("error")
        response_payload = payload.get("response") or {}
        if error:
            return key, "", error
        status_code = response_payload.get("status_code")
        if status_code and int(status_code) >= 400:
            return key, "", {
                "status_code": status_code,
                "body": response_payload.get("body"),
            }
        return key, extract_response_text(response_payload.get("body")), None

    key = payload.get("key") or payload.get("metadata", {}).get("key")
    if "response" in payload:
        response_payload = payload["response"]
    elif "inlineResponse" in payload:
        response_payload = payload["inlineResponse"].get("response")
    else:
        response_payload = payload
    error = payload.get("error") or payload.get("status")
    return key, extract_response_text(response_payload), error


def load_batch_predictions(result_jsonl_path, manifest):
    requests = {request["key"]: request["row_ids"] for request in manifest["requests"]}
    predictions = {}
    errors = {}
    with Path(result_jsonl_path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            key, text, error = parse_result_line(line)
            if not key:
                errors[f"line_{line_number}"] = f"Missing response key: {line[:200]}"
                continue
            row_ids = requests.get(key)
            if not row_ids:
                errors[key] = "Response key not found in manifest"
                continue
            if error:
                errors[key] = json.dumps(error, ensure_ascii=False)
                continue
            try:
                parsed = pe.BatchResponse.model_validate_json(text)
            except Exception as exc:
                errors[key] = f"Could not parse response JSON: {exc}; text={text[:300]}"
                continue
            by_idx = {evaluation.idx: evaluation for evaluation in parsed.evaluations}
            for index, row_id in enumerate(row_ids, start=1):
                evaluation = by_idx.get(index)
                if evaluation is None:
                    predictions[row_id] = {
                        "prediction": None,
                        "classification": "NO_MATCH",
                        "confidence": None,
                    }
                    continue
                classification = evaluation.classification.upper()
                predictions[row_id] = {
                    "prediction": 1 if classification == "INCLUDE" else 0,
                    "classification": classification,
                    "confidence": evaluation.confidence,
                }
    return predictions, errors


def apply_dataset3_bridge_aware_gating(rows, raw_predictions, graph_context):
    rows_by_seed = defaultdict(list)
    for row in rows:
        rows_by_seed[row["seed_qid"]].append(row)

    effective_by_seed_qid = {}
    decision_raw_by_seed_qid = {}
    for seed_qid, seed_rows in rows_by_seed.items():
        labels, edges_by_subj, edges_by_obj, allowed_nodes = pe.seed_graph_indexes(
            seed_qid, graph_context
        )
        seed_label = seed_rows[0].get("seed_label") or seed_qid
        labels.setdefault(seed_qid, seed_label)
        for row in seed_rows:
            if row.get("label"):
                labels.setdefault(row["qid"], row["label"])

        decision_by_qid = pe.decision_rows_by_qid(seed_rows)
        visited_info = {seed_qid: (0, None, None)}
        frontier = [seed_qid]
        effective_by_qid = {seed_qid: 1}
        decision_raw_by_qid = {}

        while frontier:
            candidates = []
            for parent_qid in frontier:
                parent_candidates, _ = pe.next_decision_candidate_rows(
                    seed_qid,
                    seed_label,
                    parent_qid,
                    visited_info,
                    labels,
                    graph_context["forward_props"],
                    graph_context["inverse_props"],
                    edges_by_subj,
                    edges_by_obj,
                    allowed_nodes,
                    decision_by_qid,
                )
                candidates.extend(parent_candidates)

            next_frontier = []
            for candidate in candidates:
                qid = candidate["qid"]
                raw = raw_predictions.get(candidate["row_id"], {
                    "prediction": None,
                    "classification": "NO_MATCH",
                    "confidence": None,
                })
                raw_prediction = raw.get("prediction")
                prediction = int(raw_prediction) if raw_prediction is not None else 0
                effective_by_qid[qid] = prediction
                decision_raw_by_qid[qid] = raw
                if prediction == 1:
                    next_frontier.append(qid)
            frontier = next_frontier

        effective_by_seed_qid[seed_qid] = effective_by_qid
        decision_raw_by_seed_qid[seed_qid] = decision_raw_by_qid

    gated_rows = []
    for row in rows:
        seed_qid = row["seed_qid"]
        qid = row["qid"]
        raw = raw_predictions.get(row["row_id"], {
            "prediction": None,
            "classification": "NO_MATCH",
            "confidence": None,
        })
        decision_raw = decision_raw_by_seed_qid[seed_qid].get(qid)
        if qid not in effective_by_seed_qid[seed_qid]:
            prediction = 0
            classification = "PRUNED_BY_ANCESTOR"
            confidence = None
        elif decision_raw is None or decision_raw.get("prediction") is None:
            prediction = 0
            classification = "NO_MATCH"
            confidence = None
        else:
            prediction = effective_by_seed_qid[seed_qid][qid]
            classification = decision_raw.get("classification") or (
                "INCLUDE" if prediction == 1 else "EXCLUDE"
            )
            confidence = decision_raw.get("confidence")

        gated = dict(row)
        gated.update({
            "raw_prediction": raw.get("prediction"),
            "raw_classification": raw.get("classification"),
            "raw_confidence": raw.get("confidence"),
            "prediction": prediction,
            "classification": classification,
            "confidence": confidence,
        })
        gated_rows.append(gated)
    return gated_rows


def apply_bfs_gating(rows, raw_predictions, dataset=None, graph_context=None):
    if dataset is not None and int(dataset) == 3:
        graph_context = graph_context or pe.load_graph_context(3)
        return apply_dataset3_bridge_aware_gating(rows, raw_predictions, graph_context)

    rows_by_seed = defaultdict(list)
    for row in rows:
        rows_by_seed[row["seed_qid"]].append(row)

    gated_rows = []
    for seed_qid, seed_rows in rows_by_seed.items():
        effective_by_qid = {seed_qid: 1}
        ordered_rows = sorted(
            seed_rows,
            key=lambda row: (
                row.get("bfs_depth") or row.get("csv_depth") or 9999,
                row.get("row_index", row.get("row_id", "")),
            ),
        )
        pending = ordered_rows[:]
        while pending:
            progressed = False
            next_pending = []
            for row in pending:
                parent_qid = row.get("parent_qid") or seed_qid
                if parent_qid not in effective_by_qid:
                    next_pending.append(row)
                    continue

                raw = raw_predictions.get(row["row_id"], {
                    "prediction": None,
                    "classification": "NO_MATCH",
                    "confidence": None,
                })
                raw_prediction = raw.get("prediction")
                parent_included = effective_by_qid[parent_qid] == 1
                if parent_included and raw_prediction is not None:
                    prediction = int(raw_prediction)
                    classification = raw.get("classification") or (
                        "INCLUDE" if prediction == 1 else "EXCLUDE"
                    )
                    confidence = raw.get("confidence")
                elif parent_included:
                    prediction = 0
                    classification = "NO_MATCH"
                    confidence = None
                else:
                    prediction = 0
                    classification = "PRUNED_BY_ANCESTOR"
                    confidence = None

                effective_by_qid[row["qid"]] = prediction
                gated = dict(row)
                gated.update({
                    "raw_prediction": raw_prediction,
                    "raw_classification": raw.get("classification"),
                    "raw_confidence": raw.get("confidence"),
                    "prediction": prediction,
                    "classification": classification,
                    "confidence": confidence,
                })
                gated_rows.append(gated)
                progressed = True
            if not progressed:
                for row in next_pending:
                    raw = raw_predictions.get(row["row_id"], {})
                    gated = dict(row)
                    gated.update({
                        "raw_prediction": raw.get("prediction"),
                        "raw_classification": raw.get("classification", "NO_PARENT"),
                        "raw_confidence": raw.get("confidence"),
                        "prediction": 0,
                        "classification": "NO_PARENT",
                        "confidence": None,
                    })
                    gated_rows.append(gated)
                break
            pending = next_pending
    return gated_rows


def raw_prediction_rows(rows, raw_predictions):
    output = []
    for row in rows:
        raw = raw_predictions.get(row["row_id"], {
            "prediction": None,
            "classification": "NO_MATCH",
            "confidence": None,
        })
        item = dict(row)
        item.update({
            "prediction": raw.get("prediction"),
            "classification": raw.get("classification"),
            "confidence": raw.get("confidence"),
        })
        output.append(item)
    return output


def write_scored_csv(path, rows):
    fieldnames = [
        "row_id", "legacy_row_id", "seed_qid", "seed_label", "qid", "label", "target",
        "prediction", "classification", "confidence", "raw_prediction",
        "raw_classification", "raw_confidence", "csv_depth", "bfs_depth", "parent_qid",
        "parent_label", "reaching_property", "reaching_property_label", "path",
    ]
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def prepare_job(args, submit=False):
    if args.strategy not in STRATEGIES:
        raise ValueError(f"Unsupported strategy: {args.strategy}")
    args.model = pe.resolve_model(args.model, args.provider)

    prompt_info = load_effective_prompt(
        dataset=args.dataset,
        strategy=args.strategy,
        prompts_dir=args.prompts_dir,
        examples_dir=args.examples_dir,
        model=args.model,
        prompt_file=args.prompt_file,
        examples_file=args.examples_file,
    )
    evaluation_examples_path = (
        Path(args.evaluation_examples_file)
        if args.evaluation_examples_file
        else default_evaluation_examples_file(args.dataset, args.examples_dir)
    )
    evaluation_examples_payload = pe.load_examples(evaluation_examples_path)
    rows, filter_stats = load_full_dataset_rows(
        args.dataset,
        evaluation_examples_payload,
        data_dir=args.data_dir,
    )

    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(args.model)
    run_dir = strategy_run_dir(output_dir, args.dataset, args.strategy)
    initialize_run_dir(run_dir, overwrite=getattr(args, "overwrite", False))
    rows_csv = run_dir / "rows.csv"
    jsonl_path = run_dir / "requests.jsonl"
    manifest_path = run_dir / "manifest.json"
    write_rows_csv(rows_csv, rows)

    cache_name = args.cache_name
    cache_prompt = prompt_info["prompt"]
    cache_padding_applied = False
    uploaded_file_name = None
    job_name = None
    result_file_name = None
    prompt_cache_key = None
    prompt_cache_retention = None
    completion_window = None
    provider = args.provider

    if provider == "openai":
        prompt_cache_key = (
            args.prompt_cache_key
            or default_openai_prompt_cache_key(args.dataset, args.strategy, args.model)
        )
        prompt_cache_retention = args.prompt_cache_retention
        completion_window = args.completion_window
        requests = write_openai_batch_jsonl(
            rows,
            prompt_info["context_level"],
            prompt_info["prompt"],
            jsonl_path,
            args.dataset,
            args.strategy,
            args.batch_size,
            args.model,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
        if submit:
            client = create_openai_client()
            display_name = f"subkg-d{args.dataset}-{args.strategy}-{timestamp_slug()}"
            uploaded_file, job = submit_openai_batch_job(
                client,
                jsonl_path,
                display_name,
                completion_window,
            )
            uploaded_file_name = uploaded_file.id
            job_name = job.id
    elif submit:
        client = create_gemini_client()
        display_name = f"subkg-d{args.dataset}-{args.strategy}-{timestamp_slug()}"
        if not cache_name:
            try:
                cache = create_context_cache(
                    client,
                    args.model,
                    cache_prompt,
                    args.cache_ttl,
                    display_name,
                )
            except Exception as exc:
                if not cache_too_small_error(exc):
                    raise
                cache_prompt = cacheable_prompt(prompt_info["prompt"])
                cache_padding_applied = True
                cache = create_context_cache(
                    client,
                    args.model,
                    cache_prompt,
                    args.cache_ttl,
                    display_name,
                )
            cache_name = cache.name
        requests = write_gemini_batch_jsonl(
            rows,
            prompt_info["context_level"],
            cache_name,
            jsonl_path,
            args.dataset,
            args.strategy,
            args.batch_size,
        )
        uploaded_file, job = submit_gemini_batch_job(client, args.model, jsonl_path, display_name)
        uploaded_file_name = uploaded_file.name
        job_name = job.name
    else:
        if not cache_name:
            raise RuntimeError("--cache-name is required for prepare-jsonl without submitting")
        requests = write_gemini_batch_jsonl(
            rows,
            prompt_info["context_level"],
            cache_name,
            jsonl_path,
            args.dataset,
            args.strategy,
            args.batch_size,
        )

    manifest = {
        "created_at": utc_now_iso(),
        "dataset": args.dataset,
        "data_dir": str(args.data_dir),
        "strategy": args.strategy,
        "provider": provider,
        "model": args.model,
        "context_level": prompt_info["context_level"],
        "prompt_file": str(prompt_info["prompt_file"]),
        "prompt_sha256": file_sha256(prompt_info["prompt_file"]),
        "effective_prompt_sha256": text_sha256(prompt_info["prompt"]),
        "examples_file": str(prompt_info["examples_file"]),
        "examples_sha256": file_sha256(prompt_info["examples_file"]),
        "examples_in_prompt": prompt_info["examples_in_prompt"],
        "prompt_examples_count": len(prompt_info["examples_payload"].get("examples", [])),
        "evaluation_exclusion_examples_file": str(evaluation_examples_path),
        "evaluation_exclusion_examples_sha256": file_sha256(evaluation_examples_path),
        "evaluation_exclusion_example_count": len(
            evaluation_examples_payload.get("examples", [])
        ),
        "row_count": len(rows),
        "filter_stats": filter_stats,
        "uploaded_file_name": uploaded_file_name,
        "job_name": job_name,
        "result_file_name": result_file_name,
        "run_dir": str(run_dir),
        "rows_csv": str(rows_csv),
        "requests_jsonl": str(jsonl_path),
        "requests": requests,
    }
    if provider == "openai":
        manifest["endpoint"] = OPENAI_BATCH_ENDPOINT
        if prompt_cache_key is not None:
            manifest["prompt_cache_key"] = prompt_cache_key
        if prompt_cache_retention is not None:
            manifest["prompt_cache_retention"] = prompt_cache_retention
        if completion_window is not None:
            manifest["completion_window"] = completion_window
    else:
        manifest.update({
            "cache_ttl": args.cache_ttl,
            "cache_padding_applied": cache_padding_applied,
            "cached_prompt_sha256": text_sha256(cache_prompt),
        })
        if cache_name is not None:
            manifest["cache_name"] = cache_name
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Run directory: {run_dir}")
    print(f"Rows: {len(rows)}")
    print(f"Requests: {len(requests)}")
    print(f"Manifest: {manifest_path}")
    if job_name:
        print(f"Batch job: {job_name}")
        if provider == "gemini":
            print(f"Cache: {cache_name}")
        else:
            print(f"Prompt cache key: {prompt_cache_key}")
            print(f"Prompt cache retention: {prompt_cache_retention}")
    return manifest


def cmd_prepare(args):
    prepare_job(args, submit=False)


def cmd_submit(args):
    prepare_job(args, submit=True)


def cmd_status(args):
    provider = resolve_status_provider(args.provider, args.job_name)
    if provider == "openai":
        client = create_openai_client()
        job = client.batches.retrieve(args.job_name)
        print(json.dumps({
            "id": job.id,
            "status": job.status,
            "errors": job.errors.model_dump() if getattr(job.errors, "model_dump", None) else job.errors,
            "output_file_id": job.output_file_id,
            "error_file_id": job.error_file_id,
            "request_counts": (
                job.request_counts.model_dump()
                if getattr(job.request_counts, "model_dump", None)
                else job.request_counts
            ),
        }, indent=2))
    else:
        client = create_gemini_client()
        job = client.batches.get(name=args.job_name)
        print(json.dumps({
            "name": job.name,
            "state": maybe_state_name(job.state),
            "error": str(job.error) if getattr(job, "error", None) else None,
            "dest_file_name": getattr(job.dest, "file_name", None) if getattr(job, "dest", None) else None,
        }, indent=2))


def resolve_status_provider(provider, job_name):
    if provider != "auto":
        return provider
    return "gemini" if str(job_name).startswith("batches/") else "openai"


def load_manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_manifest(path, manifest):
    Path(path).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def build_score_summary(manifest, row_count, prediction_count, parse_errors,
                        bfs_metrics, bfs_baseline_metrics,
                        bfs_metric_aggregations,
                        bfs_baseline_metric_aggregations,
                        baseline_error, bfs_csv):
    return {
        "created_at": utc_now_iso(),
        "metrics_schema_version": 2,
        "dataset": manifest["dataset"],
        "strategy": manifest["strategy"],
        "provider": manifest.get("provider", "gemini"),
        "model": manifest["model"],
        "context_level": manifest["context_level"],
        "row_count": row_count,
        "prediction_count": prediction_count,
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors,
        "metrics": {
            "bfs_gated": bfs_metrics,
            "bfs_gated_kgprune_runtime_rows": bfs_baseline_metrics,
        },
        "metric_aggregations": {
            "bfs_gated": bfs_metric_aggregations,
            "bfs_gated_kgprune_runtime_rows": bfs_baseline_metric_aggregations,
        },
        "baseline_subset": {
            "available": baseline_error is None,
            "error": baseline_error,
        },
        "outputs": {
            "bfs_csv": str(bfs_csv),
        },
    }


def cmd_download(args):
    manifest = load_manifest(args.manifest)
    job_name = args.job_name or manifest.get("job_name")
    if not job_name:
        raise RuntimeError("Provide --job-name or use a manifest with job_name")
    output_path = Path(args.output) if args.output else Path(manifest["run_dir"]) / "batch_results.jsonl"
    provider = manifest.get("provider", "gemini")
    if provider == "openai":
        client = create_openai_client()
        result_file_name = download_openai_batch_results(client, job_name, output_path)
    else:
        client = create_gemini_client()
        result_file_name = download_gemini_batch_results(client, job_name, output_path)
    manifest["result_file_name"] = result_file_name
    manifest["result_jsonl"] = str(output_path)
    save_manifest(args.manifest, manifest)
    print(f"Downloaded result file {result_file_name} to {output_path}")


def cmd_score(args):
    manifest = load_manifest(args.manifest)
    result_jsonl = Path(args.result_jsonl or manifest.get("result_jsonl", ""))
    if not result_jsonl.exists():
        raise FileNotFoundError(f"Result JSONL not found: {result_jsonl}")
    rows = read_rows_csv(manifest["rows_csv"])
    raw_predictions, parse_errors = load_batch_predictions(result_jsonl, manifest)
    dataset = int(manifest["dataset"])
    graph_context = None
    if dataset == 3:
        graph_context = pe.load_graph_context(
            dataset,
            manifest.get("data_dir", pe.DEFAULT_DATA_DIR),
        )
    bfs_rows = apply_bfs_gating(
        rows,
        raw_predictions,
        dataset=dataset,
        graph_context=graph_context,
    )

    run_dir = Path(manifest["run_dir"])
    bfs_csv = run_dir / "bfs_gated_predictions.csv"
    summary_json = run_dir / "summary.json"
    write_scored_csv(bfs_csv, bfs_rows)

    bfs_metrics = pe.compute_metrics(bfs_rows)
    bfs_metric_aggregations = pe.pooled_metric_aggregation(bfs_rows)
    baseline_error = None
    try:
        baseline_pairs = pe.load_baseline_pairs(manifest["dataset"])
        bfs_baseline_rows = pe.rows_in_pairs(bfs_rows, baseline_pairs)
        bfs_baseline_metrics = pe.compute_metrics(bfs_baseline_rows)
        bfs_baseline_metric_aggregations = pe.compute_fold_metric_aggregation(
            bfs_baseline_rows,
            dataset,
        )
    except FileNotFoundError as exc:
        baseline_error = str(exc)
        bfs_baseline_metrics = None
        bfs_baseline_metric_aggregations = None
    summary = build_score_summary(
        manifest=manifest,
        row_count=len(rows),
        prediction_count=len(raw_predictions),
        parse_errors=parse_errors,
        bfs_metrics=bfs_metrics,
        bfs_baseline_metrics=bfs_baseline_metrics,
        bfs_metric_aggregations=bfs_metric_aggregations,
        bfs_baseline_metric_aggregations=bfs_baseline_metric_aggregations,
        baseline_error=baseline_error,
        bfs_csv=bfs_csv,
    )
    summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest["summary_json"] = str(summary_json)
    manifest.pop("raw_csv", None)
    manifest["bfs_csv"] = str(bfs_csv)
    save_manifest(args.manifest, manifest)

    print("Full-dataset batch scoring")
    print(f"  Dataset: {manifest['dataset']}")
    print(f"  Strategy: {manifest['strategy']}")
    print(f"  Rows: {len(rows)}")
    print(f"  BFS-gated F1: {bfs_metrics['f1']:.3f}, Acc: {bfs_metrics['accuracy']:.3f}")
    if bfs_baseline_metrics:
        print(
            "  BFS-gated KGPrune runtime rows "
            f"F1: {bfs_baseline_metrics['f1']:.3f}, "
            f"Acc: {bfs_baseline_metrics['accuracy']:.3f}"
        )
    print(f"  Summary: {summary_json}")


def add_common_submit_args(parser):
    parser.add_argument("--dataset", type=int, required=True, choices=[1, 2, 3])
    parser.add_argument("--strategy", required=True, choices=STRATEGIES)
    parser.add_argument("--provider", choices=["gemini", "openai"], default="gemini")
    parser.add_argument("--model",
                        help="Model name. Defaults to the provider default if omitted.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--cache-ttl", default=DEFAULT_CACHE_TTL)
    parser.add_argument("--completion-window", default=DEFAULT_OPENAI_COMPLETION_WINDOW,
                        help="OpenAI Batch API completion window. Currently 24h.")
    parser.add_argument("--prompt-cache-key",
                        help="OpenAI prompt_cache_key. Defaults to a dataset/strategy/model key.")
    parser.add_argument("--prompt-cache-retention",
                        help="Optional OpenAI prompt_cache_retention, for example 24h.")
    parser.add_argument("--data-dir", type=Path, default=pe.DEFAULT_DATA_DIR)
    parser.add_argument("--prompts-dir", type=Path, default=pe.DEFAULT_PROMPTS_DIR)
    parser.add_argument(
        "--examples-dir",
        type=Path,
        default=SCRIPT_DIR / "examples",
        help=("Root examples directory containing fixed manual packs and the "
              "meta/ subdirectory."),
    )
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument(
        "--examples-file",
        type=Path,
        help=("Explicit examples override. Defaults by strategy: manual packs for "
               "manual runs and examples/meta packs for meta runs."),
    )
    parser.add_argument(
        "--evaluation-examples-file",
        type=Path,
        help=("Fixed row-exclusion pack used only to define the common evaluation "
              "universe. Defaults to the dataset's canonical meta example pack."),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing canonical dataset/strategy run directory.",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run full-dataset Wikidata prompt tests through Gemini or OpenAI Batch APIs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare-jsonl",
        help="Write rows, manifest, and batch JSONL without submitting a batch job.",
    )
    add_common_submit_args(prepare)
    prepare.add_argument("--cache-name",
                         help="Gemini cached_content name. Required for Gemini prepare-jsonl.")
    prepare.set_defaults(func=cmd_prepare)

    submit = subparsers.add_parser(
        "submit",
        help="Create provider cache context if needed, upload JSONL, and submit a batch job.",
    )
    add_common_submit_args(submit)
    submit.add_argument("--cache-name")
    submit.set_defaults(func=cmd_submit)

    status = subparsers.add_parser("status", help="Print batch job status.")
    status.add_argument("--job-name", required=True)
    status.add_argument("--provider", choices=["auto", "gemini", "openai"], default="auto")
    status.set_defaults(func=cmd_status)

    download = subparsers.add_parser(
        "download",
        help="Download completed batch results and update the manifest.",
    )
    download.add_argument("--manifest", required=True, type=Path)
    download.add_argument("--job-name")
    download.add_argument("--output", type=Path)
    download.set_defaults(func=cmd_download)

    score = subparsers.add_parser(
        "score",
        help="Parse downloaded batch results and compute BFS-gated metrics.",
    )
    score.add_argument("--manifest", required=True, type=Path)
    score.add_argument("--result-jsonl", type=Path)
    score.set_defaults(func=cmd_score)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
