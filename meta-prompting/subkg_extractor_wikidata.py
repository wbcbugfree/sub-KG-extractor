#!/usr/bin/env python3
"""
Wikidata sub-KG extraction and prompt experiments.

This script supports the prompt-iteration workflow:

1. Build a small few-shot example file from a dataset.
2. Generate a dataset-specific system prompt from meta_prompt.md + examples.
3. Evaluate zero-shot/few-shot manual and zero-shot/few-shot meta-generated
   prompts with real LLM-gated BFS by default, excluding the example rows from
   scoring.

The default evaluation mode is prune-gated BFS: LLM INCLUDE predictions control
which candidate nodes are expanded in the next iteration. The legacy row-level
mode remains available with --eval-mode row for cheap prompt checks and for
reproducing earlier summaries.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import pickle
import random
import sys
import time
import urllib.error
import urllib.request
from copy import copy
from collections import defaultdict
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from datetime import datetime
from datetime import timezone
from pathlib import Path
import re
from typing import List, Literal

from pydantic import BaseModel, Field

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_DATA_DIR = REPO_ROOT / "wikidata"
DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_PROMPTS_DIR = SCRIPT_DIR / "prompts"
DEFAULT_SAMPLES_DIR = SCRIPT_DIR / "samples"
DEFAULT_EXAMPLES_DIR = SCRIPT_DIR / "examples"
DEFAULT_META_EXAMPLES_DIR = DEFAULT_EXAMPLES_DIR / "meta"
DEFAULT_BASELINE_RUNTIME_INPUT_DIR = (
    REPO_ROOT / "tmp" / "kgprune_bridge_path_analogy" / "inputs"
)
DEFAULT_FOLDS_DIR = REPO_ROOT / "reproduction" / "folds"
DEFAULT_BASELINE_DECISIONS_DIR = (
    REPO_ROOT / "reproduction" / "results" / "decisions"
)
DEFAULT_ALIGNED_METRICS_CSV = (
    DEFAULT_RESULTS_DIR / "wikidata_aligned_metric_views.csv"
)
DEFAULT_ALIGNED_METRICS_JSON = (
    DEFAULT_RESULTS_DIR / "wikidata_aligned_metric_views.json"
)
DEFAULT_OPENAI_MODEL = "gpt-5"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3.5-flash"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_GEMINI_RETRIES = 8
DEFAULT_DEEPSEEK_PROTOCOL_RETRIES = 3
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_BETA_BASE_URL = "https://api.deepseek.com/beta"
DEEPSEEK_EVALUATION_TOOL_NAME = "submit_candidate_evaluations"
PROMPT_GENERATION_REASONING = {
    "gpt-5": {
        "provider": "openai",
        "parameter": "reasoning.effort",
        "value": "high",
    },
    "gpt-5.4-mini": {
        "provider": "openai",
        "parameter": "reasoning.effort",
        "value": "xhigh",
    },
    "gemini-3.5-flash": {
        "provider": "gemini",
        "parameter": "thinkingConfig.thinkingLevel",
        "value": "high",
    },
    "google/gemini-3.5-flash": {
        "provider": "openrouter",
        "parameter": "reasoning.enabled",
        "value": True,
    },
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "parameter": "reasoning_effort",
        "value": "max",
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "parameter": "reasoning_effort",
        "value": "max",
    },
}

DEEPSEEK_EVALUATION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
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
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["idx", "classification", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["evaluations"],
    "additionalProperties": False,
}

BASELINE_DECISION_FILES = {
    1: "dataset1_gold_decisions_filtered.csv",
    2: "dataset2_gold_decisions_filtered.csv",
    3: "dataset3_gold_decisions_bridge_filtered.csv",
}

FOLD_FILES = {
    1: "dataset1_5_folds.pkl",
    2: "dataset2_5_folds.pkl",
    3: "dataset3_overlap_reduced_5_folds.pkl",
}

METRIC_NAMES = ("precision", "recall", "f1", "accuracy")

RETAINED_BASELINE_FILES = {
    1: {
        "lstm": "dataset1_lstm_dataset1_config.csv",
        "path_analogy": "dataset1_path_analogy_dataset1_config.csv",
    },
    2: {
        "lstm": "dataset2_lstm_dataset2_config.csv",
        "path_analogy": "dataset2_path_analogy_dataset2_config.csv",
    },
    3: {
        "lstm": "dataset3_lstm_tuned.csv",
        "path_analogy": "dataset3_path_analogy_tuned.csv",
    },
}

RETAINED_BASELINE_LABELS = {
    "lstm": "LSTM",
    "path_analogy": "Path Analogy",
}

RETAINED_LLM_MODELS = (
    "gemini-3.5-flash",
    "gpt-5.4-mini",
    "deepseek-v4-pro",
)

RETAINED_PROMPT_STRATEGIES = (
    "zero_shot_manual",
    "few_shot_manual",
    "zero_shot_meta",
    "few_shot_meta",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _find_single_file(pattern, root):
    matches = sorted(Path(root).glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No file matched {pattern!r} under {root}")
    if len(matches) > 1:
        raise RuntimeError(
            f"Expected one file for {pattern!r} under {root}, found: "
            + ", ".join(str(path) for path in matches)
        )
    return matches[0]


def uses_meta_examples(prompt_setting):
    setting = str(prompt_setting or "").lower()
    return setting == "generated" or setting.endswith("_meta")


def default_examples_file(dataset, prompt_setting, examples_dir=DEFAULT_EXAMPLES_DIR):
    dataset = normalize_dataset_id(dataset)
    examples_dir = Path(examples_dir)
    if uses_meta_examples(prompt_setting):
        return _find_single_file(
            f"dataset{dataset}_*_meta_examples.json",
            examples_dir / "meta",
        )
    return _find_single_file(f"dataset{dataset}_*_examples.json", examples_dir)


def resolve_examples_file(dataset, prompt_setting, examples_file=None,
                          examples_dir=DEFAULT_EXAMPLES_DIR):
    if examples_file:
        return Path(examples_file)
    return default_examples_file(dataset, prompt_setting, examples_dir)


def apply_examples_file_default(args, prompt_setting,
                                examples_dir=DEFAULT_EXAMPLES_DIR):
    args.examples_file = resolve_examples_file(
        args.dataset,
        prompt_setting,
        examples_file=args.examples_file,
        examples_dir=examples_dir,
    )
    return args.examples_file


DATASETS = {
    1: {
        "decisions_file": "dataset1_gold_decisions.csv",
        "seeds_file": "seeds_dataset1.csv",
        "properties_file": "properties_dataset1.csv",
        "ttl_file": "dataset1_subgraph_2022.ttl",
        "perseed_file": "dataset1_subgraph_2022_perseed.json",
    },
    2: {
        "decisions_file": "dataset2_gold_decisions.csv",
        "seeds_file": "seeds_dataset2.csv",
        "properties_file": "properties_dataset2.csv",
        "ttl_file": "dataset2_subgraph_2022.ttl",
        "perseed_file": "dataset2_subgraph_2022_perseed.json",
    },
    3: {
        "decisions_file": "dataset3_gold_decisions.csv",
        "seeds_file": "seeds_dataset3.csv",
        "properties_file": "properties_dataset3.csv",
        "ttl_file": "dataset3_subgraph_2022.ttl",
        "perseed_file": "dataset3_subgraph_2022_perseed.json",
    },
}

CONTEXT_LEVELS = ["labels", "depth", "property", "path", "path_simple"]


PROPERTY_LABELS = {
    "P31": "instance of",
    "P279": "subclass of",
    "P22": "father",
    "P25": "mother",
    "P26": "spouse",
    "P40": "child",
    "P106": "occupation",
    "P127": "owned by",
    "P921": "main subject",
    "P1343": "described by source",
    "P1830": "owner of",
    "P1840": "investigation of",
    "P3342": "significant person",
    "P5008": "on focus list of Wikimedia project",
}

INVERSE_PROPERTY_LABELS = {
    "P31": "has instance",
    "P279": "has subclass",
    "P127": "owner of",
    "P1830": "owned by",
}


class PromptDecision(BaseModel):
    idx: int = Field(ge=1)
    classification: Literal["INCLUDE", "EXCLUDE"]
    confidence: float = Field(ge=0.0, le=1.0)


class BatchResponse(BaseModel):
    evaluations: List[PromptDecision]


def normalize_dataset_id(dataset):
    if isinstance(dataset, str):
        dataset = dataset.strip()
        if dataset.isdigit():
            return int(dataset)
    return dataset


def parse_dataset_arg(value):
    dataset = normalize_dataset_id(value)
    if dataset not in DATASETS:
        valid = ", ".join(str(key) for key in DATASETS)
        raise argparse.ArgumentTypeError(f"unknown dataset '{value}'. Expected one of: {valid}")
    return dataset


def dataset_dir(dataset, data_dir):
    dataset = normalize_dataset_id(dataset)
    return Path(data_dir) / f"dataset{dataset}"


def load_config():
    config_path = REPO_ROOT / "config"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_api_key(provider="openai"):
    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        config_key = "gemini_api_key"
        env_hint = "GEMINI_API_KEY"
    elif provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        config_key = "deepseek_api_key"
        env_hint = "DEEPSEEK_API_KEY"
    elif provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        config_key = "openrouter_api_key"
        env_hint = "OPENROUTER_API_KEY"
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        config_key = "openai_api_key"
        env_hint = "OPENAI_API_KEY"
    if api_key:
        return api_key
    api_key = load_config().get(config_key)
    if not api_key:
        raise RuntimeError(f"Set {env_hint} or create config with {config_key}")
    return api_key


def get_openai_client():
    from openai import OpenAI

    return OpenAI(api_key=load_api_key("openai"))


def get_gemini_client():
    from google import genai

    return genai.Client(api_key=load_api_key("gemini"))


def get_deepseek_client():
    from openai import OpenAI

    return OpenAI(api_key=load_api_key("deepseek"), base_url=DEEPSEEK_BETA_BASE_URL)


class OpenRouterClient:
    def __init__(self, api_key, endpoint=OPENROUTER_CHAT_COMPLETIONS_URL,
                 post_json=None):
        self.api_key = api_key
        self.endpoint = endpoint
        self.post_json = post_json or self._post_json

    def _post_json(self, url, headers, payload):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenRouter request failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
        return json.loads(response_body)

    def chat_completion(self, model, messages):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "reasoning": {"enabled": True},
        }
        return self.post_json(self.endpoint, headers, payload)


def get_openrouter_client():
    return OpenRouterClient(api_key=load_api_key("openrouter"))


def resolve_provider(provider, model):
    if provider != "auto":
        return provider
    if model and str(model).startswith("deepseek-"):
        return "deepseek"
    if model and str(model).startswith("google/"):
        return "openrouter"
    if model and model.startswith("gemini-"):
        return "gemini"
    return "openai"


def default_model_for_provider(provider):
    if provider == "gemini":
        return DEFAULT_GEMINI_MODEL
    if provider == "deepseek":
        return DEFAULT_DEEPSEEK_MODEL
    if provider == "openrouter":
        return DEFAULT_OPENROUTER_MODEL
    return DEFAULT_OPENAI_MODEL


def resolve_model(model, provider):
    return model or default_model_for_provider(provider)


def prompt_generation_reasoning(model, provider, thinking_level=None):
    setting = PROMPT_GENERATION_REASONING.get(model)
    if not setting:
        supported = ", ".join(PROMPT_GENERATION_REASONING)
        raise ValueError(
            f"No prompt-generation reasoning policy for model '{model}'. "
            f"Expected one of: {supported}"
        )
    if setting["provider"] != provider:
        raise ValueError(
            f"Model '{model}' requires provider '{setting['provider']}', "
            f"not '{provider}'"
        )
    resolved = dict(setting)
    if thinking_level is not None:
        if provider != "gemini":
            raise ValueError("--thinking-level is only supported for Gemini prompt generation")
        if thinking_level == "default":
            resolved["value"] = None
            resolved["mode"] = "provider_default"
        else:
            resolved["value"] = thinking_level
            resolved["mode"] = "explicit"
    return resolved


def model_slug(model):
    return re.sub(r"[^a-z0-9]+", "", str(model).lower())


def prepare_llm_args(args):
    provider = resolve_provider(getattr(args, "provider", "auto"), getattr(args, "model", None))
    args.provider = provider
    args.model = resolve_model(getattr(args, "model", None), provider)
    return args


def model_prompts_dir(model):
    return DEFAULT_PROMPTS_DIR / str(model)


def model_results_dir(model):
    return DEFAULT_RESULTS_DIR / str(model)


def legacy_provider_prompts_dir(provider):
    return DEFAULT_PROMPTS_DIR / provider


def is_default_results_dir(path):
    return Path(path).resolve() == DEFAULT_RESULTS_DIR.resolve()


def default_generated_prompt_path(args, examples_payload):
    examples_file = Path(args.examples_file)
    example_stem = examples_file.stem
    if example_stem.endswith("_examples"):
        example_stem = example_stem[:-len("_examples")]
    context_level = examples_payload.get("context_level", args.context_level)
    if context_level not in example_stem:
        example_stem = f"dataset{args.dataset}_{context_level}_{example_stem}"
    return (
        model_prompts_dir(args.model)
        / (
            f"generated_{example_stem}_evidence_{model_slug(args.model)}.md"
        )
    )


def resolve_prompt_file_for_model(prompt_file, model, provider=None):
    prompt_path = Path(prompt_file)
    if prompt_path.exists():
        return prompt_path

    search_dirs = [model_prompts_dir(model)]
    if provider:
        search_dirs.append(legacy_provider_prompts_dir(provider))
    for prompt_dir in search_dirs:
        candidate = prompt_dir / prompt_path.name
        if candidate.exists():
            return candidate

    return prompt_path


def resolve_evaluation_output_dir(output_dir, model):
    if is_default_results_dir(output_dir):
        return model_results_dir(model)
    return Path(output_dir)


def result_filename_component(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-")


def evaluation_result_stem(args, prompt_setting, eval_mode):
    if args.sample_size is not None and args.sample_size <= 0:
        return (
            f"dataset{args.dataset}_{prompt_setting}_"
            f"{result_filename_component(args.model)}"
        )
    stem = (
        f"dataset{args.dataset}_{prompt_setting}_{Path(args.prompt_file).stem}_"
        f"{args.context_level}"
    )
    if eval_mode == "bfs":
        stem += "_bfs"
    return f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def ensure_result_paths_available(result_paths, overwrite=False):
    existing = [Path(path) for path in result_paths if Path(path).exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "Result files already exist: "
            + ", ".join(str(path) for path in existing)
            + ". Pass --overwrite to replace the canonical full-dataset result."
        )


def get_llm_client(provider):
    if provider == "gemini":
        return get_gemini_client()
    if provider == "deepseek":
        return get_deepseek_client()
    if provider == "openrouter":
        return get_openrouter_client()
    return get_openai_client()


def gemini_call_with_retries(callable_, retries=DEFAULT_GEMINI_RETRIES):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return callable_()
        except Exception as exc:
            last_error = exc
            status_code = getattr(exc, "status_code", None)
            message = str(exc).lower()
            retryable = (
                status_code in {429, 500, 502, 503, 504}
                or "unavailable" in message
                or "rate" in message
                or "temporarily" in message
            )
            if not retryable or attempt == retries:
                raise
            wait_seconds = min(5 * (2 ** (attempt - 1)), 60) + random.random() * 3
            print(
                f"Gemini call failed with retryable error "
                f"({type(exc).__name__}); retry {attempt}/{retries - 1} "
                f"in {wait_seconds:.1f}s ..."
            )
            time.sleep(wait_seconds)
    raise last_error


def file_sha256(path):
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_seeds(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def load_properties(path):
    forward, inverse = [], []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            prop = line.strip()
            if not prop:
                continue
            if prop.startswith("(-)"):
                inverse.append(prop[3:])
            else:
                forward.append(prop)
    return forward, inverse


def label_for_property(prop_id):
    if not prop_id:
        return ""
    clean = prop_id.removeprefix("(-)")
    base_label = PROPERTY_LABELS.get(clean, clean)
    if prop_id.startswith("(-)"):
        return INVERSE_PROPERTY_LABELS.get(clean, f"reverse {base_label}")
    return base_label


def parse_ttl_edges(path):
    edge_pattern = re.compile(r"wd:(\S+)\s+wdt:(\S+)\s+wd:(\S+)\s*\.")
    label_pattern = re.compile(r'wd:(\S+)\s+rdfs:label\s+"(.+?)"@en\s*\.')

    edges_by_pair = {}
    edges_by_subj = defaultdict(list)
    edges_by_obj = defaultdict(list)
    labels = {}

    with Path(path).open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue
            edge_match = edge_pattern.match(line)
            if edge_match:
                subj, prop, obj = edge_match.group(1), edge_match.group(2), edge_match.group(3)
                edges_by_pair[(subj, obj)] = prop
                edges_by_subj[subj].append((prop, obj))
                edges_by_obj[obj].append((prop, subj))
                continue
            label_match = label_pattern.match(line)
            if label_match:
                qid, label = label_match.group(1), label_match.group(2)
                labels[qid] = label.replace('\\"', '"').replace("\\n", "\n")

    return edges_by_pair, dict(edges_by_subj), dict(edges_by_obj), labels


def build_edge_indexes(edges):
    edges_by_subj = defaultdict(list)
    edges_by_obj = defaultdict(list)
    for subj, prop, obj in edges:
        edges_by_subj[subj].append((prop, obj))
        edges_by_obj[obj].append((prop, subj))
    return dict(edges_by_subj), dict(edges_by_obj)


def bfs_with_properties(seed, forward_props, inverse_props, edges_by_subj, edges_by_obj,
                        allowed_nodes=None):
    fwd_set = set(forward_props)
    inv_set = set(inverse_props)
    allowed = set(allowed_nodes) if allowed_nodes is not None else None
    visited = {seed: (0, None, None)}
    queue = deque([seed])

    while queue:
        node = queue.popleft()
        node_depth = visited[node][0]

        for prop, neighbor in edges_by_subj.get(node, []):
            if (
                prop in fwd_set
                and (allowed is None or neighbor in allowed)
                and neighbor not in visited
            ):
                visited[neighbor] = (node_depth + 1, prop, node)
                queue.append(neighbor)

        for prop, neighbor in edges_by_obj.get(node, []):
            if (
                prop in inv_set
                and (allowed is None or neighbor in allowed)
                and neighbor not in visited
            ):
                visited[neighbor] = (node_depth + 1, f"(-){prop}", node)
                queue.append(neighbor)

    return visited


def load_decision_rows(dataset, data_dir=DEFAULT_DATA_DIR):
    dataset = normalize_dataset_id(dataset)
    cfg = DATASETS[dataset]
    path = dataset_dir(dataset, data_dir) / cfg["decisions_file"]
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader):
            seed_qid = row.get("from", "").strip()
            qid = row.get("QID", "").strip()
            target_text = row.get("target", row.get("decision", "")).strip()
            if not seed_qid or not qid or target_text == "":
                continue
            rows.append({
                "row_index": index,
                "row_id": f"row{index}",
                "legacy_row_id": f"{seed_qid}|{qid}",
                "seed_qid": seed_qid,
                "seed_label": row.get("starting label", "").strip(),
                "qid": qid,
                "label": row.get("label", "").strip(),
                "csv_depth": int(row["depth"]) if row.get("depth", "").strip() else None,
                "target": int(target_text),
            })
    return rows


def load_baseline_pairs(dataset, runtime_input_dir=DEFAULT_BASELINE_RUNTIME_INPUT_DIR):
    dataset = normalize_dataset_id(dataset)
    filename = BASELINE_DECISION_FILES[dataset]
    path = Path(runtime_input_dir) / filename
    if not path.exists():
        raise FileNotFoundError(f"KGPrune runtime input file not found: {path}")
    pairs = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            seed_qid = row.get("from", "").strip()
            qid = row.get("QID", "").strip()
            if seed_qid and qid:
                pairs.add((seed_qid, qid))
    return pairs


def load_evaluation_fold_map(dataset, folds_dir=DEFAULT_FOLDS_DIR):
    """Return the benchmark test-fold index for every seed concept.

    The retained fold files define each fold by its test seeds.  A seed must
    occur in exactly one test fold; otherwise row-count-weighted fold metrics
    would not have a well-defined evaluation partition.
    """
    dataset = normalize_dataset_id(dataset)
    path = Path(folds_dir) / FOLD_FILES[dataset]
    if not path.exists():
        raise FileNotFoundError(f"Wikidata fold file not found: {path}")
    with path.open("rb") as handle:
        folds = pickle.load(handle)
    if not isinstance(folds, dict):
        raise TypeError(f"Expected a fold dictionary in {path}, found {type(folds)!r}")

    fold_by_seed = {}
    for fold_index, split in sorted(folds.items()):
        if not isinstance(split, dict) or "test" not in split:
            raise ValueError(f"Fold {fold_index!r} in {path} has no test seed list")
        for seed_qid in split["test"]:
            seed_qid = str(seed_qid).strip()
            if seed_qid in fold_by_seed:
                raise ValueError(
                    f"Seed {seed_qid} occurs in test folds "
                    f"{fold_by_seed[seed_qid]} and {fold_index} in {path}"
                )
            fold_by_seed[seed_qid] = int(fold_index)

    expected_folds = set(range(5))
    actual_folds = set(fold_by_seed.values())
    if actual_folds != expected_folds:
        raise ValueError(
            f"Expected test folds {sorted(expected_folds)} in {path}, "
            f"found {sorted(actual_folds)}"
        )
    return fold_by_seed


def row_seed_qid(row):
    seed_qid = row.get("seed_qid")
    if seed_qid:
        return seed_qid
    legacy = row.get("legacy_row_id", "")
    if "|" in legacy:
        return legacy.split("|", 1)[0]
    return ""


def rows_in_pairs(rows, pairs):
    return [
        row for row in rows
        if (row_seed_qid(row), row.get("qid", "")) in pairs
    ]


def augment_rows_with_graph_context(dataset, rows, data_dir=DEFAULT_DATA_DIR):
    dataset = normalize_dataset_id(dataset)
    cfg = DATASETS[dataset]
    ddir = dataset_dir(dataset, data_dir)
    seeds = load_seeds(ddir / cfg["seeds_file"])
    fwd_props, inv_props = load_properties(ddir / cfg["properties_file"])
    per_seed_path = ddir / cfg["perseed_file"]
    if per_seed_path.exists():
        per_seed_subgraphs = load_json_if_exists(per_seed_path).get("seeds", {})
        ttl_graph = None
    else:
        per_seed_subgraphs = {}
        ttl_graph = parse_ttl_edges(ddir / cfg["ttl_file"])

    by_seed = defaultdict(list)
    for row in rows:
        by_seed[row["seed_qid"]].append(row)

    for seed in seeds:
        if seed not in by_seed:
            continue
        if seed in per_seed_subgraphs:
            seed_info = per_seed_subgraphs[seed]
            labels = dict(seed_info.get("labels", {}))
            edges_by_subj, edges_by_obj = build_edge_indexes(seed_info.get("edges", []))
            allowed_nodes = set(labels)
        else:
            _, edges_by_subj, edges_by_obj, labels = ttl_graph
            labels = dict(labels)
            allowed_nodes = set(labels)

        for row in by_seed[seed]:
            if row["seed_label"]:
                labels.setdefault(row["seed_qid"], row["seed_label"])
            if row["label"]:
                labels.setdefault(row["qid"], row["label"])

        visited = bfs_with_properties(
            seed, fwd_props, inv_props, edges_by_subj, edges_by_obj, allowed_nodes
        )
        for row in by_seed[seed]:
            info = visited.get(row["qid"])
            if info:
                depth, prop, parent = info
                row["bfs_depth"] = depth
                row["reaching_property"] = prop
                row["reaching_property_label"] = label_for_property(prop)
                row["parent_qid"] = parent
                row["parent_label"] = labels.get(parent, parent or "")
                row["path"] = build_path_text(seed, row["qid"], visited, labels)
            else:
                row["bfs_depth"] = row["csv_depth"]
                row["reaching_property"] = ""
                row["reaching_property_label"] = ""
                row["parent_qid"] = ""
                row["parent_label"] = ""
                row["path"] = ""
    return rows


def load_graph_context(dataset, data_dir=DEFAULT_DATA_DIR):
    dataset = normalize_dataset_id(dataset)
    cfg = DATASETS[dataset]
    ddir = dataset_dir(dataset, data_dir)
    fwd_props, inv_props = load_properties(ddir / cfg["properties_file"])
    per_seed_path = ddir / cfg["perseed_file"]
    if per_seed_path.exists():
        return {
            "forward_props": fwd_props,
            "inverse_props": inv_props,
            "per_seed_subgraphs": load_json_if_exists(per_seed_path).get("seeds", {}),
            "ttl_graph": None,
        }
    return {
        "forward_props": fwd_props,
        "inverse_props": inv_props,
        "per_seed_subgraphs": {},
        "ttl_graph": parse_ttl_edges(ddir / cfg["ttl_file"]),
    }


def local_graph_qids(graph_context):
    per_seed_subgraphs = graph_context.get("per_seed_subgraphs") or {}
    if per_seed_subgraphs:
        qids = set()
        for seed_info in per_seed_subgraphs.values():
            qids.update(seed_info.get("labels", {}).keys())
        return qids

    ttl_graph = graph_context.get("ttl_graph")
    if ttl_graph:
        _, _, _, labels = ttl_graph
        return set(labels)
    return set()


def skip_rows_absent_from_local_graph(rows, graph_context):
    graph_qids = local_graph_qids(graph_context)
    if not graph_qids:
        return list(rows), 0
    filtered = [
        row for row in rows
        if row.get("seed_qid") in graph_qids and row.get("qid") in graph_qids
    ]
    return filtered, len(rows) - len(filtered)


def seed_graph_indexes(seed_qid, graph_context):
    per_seed_subgraphs = graph_context["per_seed_subgraphs"]
    if seed_qid in per_seed_subgraphs:
        seed_info = per_seed_subgraphs[seed_qid]
        labels = dict(seed_info.get("labels", {}))
        edges_by_subj, edges_by_obj = build_edge_indexes(seed_info.get("edges", []))
        allowed_nodes = set(labels)
        return labels, edges_by_subj, edges_by_obj, allowed_nodes

    _, edges_by_subj, edges_by_obj, labels = graph_context["ttl_graph"]
    labels = dict(labels)
    return labels, edges_by_subj, edges_by_obj, set(labels)


def decision_rows_by_qid(rows):
    by_qid = {}
    for row in rows:
        qid = row.get("qid")
        if qid and qid not in by_qid:
            by_qid[qid] = row
    return by_qid


def next_decision_candidate_rows(seed_qid, seed_label, parent_qid, visited_info, labels,
                                 forward_props, inverse_props, edges_by_subj, edges_by_obj,
                                 allowed_nodes, decision_by_qid):
    candidates = []
    bridge_count = 0
    fwd_set = set(forward_props)
    inv_set = set(inverse_props)
    bridge_queue = deque([parent_qid])

    def add_neighbor(source_qid, prop, neighbor):
        nonlocal bridge_count
        if neighbor == seed_qid or neighbor in visited_info:
            return
        if allowed_nodes is not None and neighbor not in allowed_nodes:
            return
        label = labels.get(neighbor)
        if not label:
            return
        parent_depth = visited_info[source_qid][0]
        visited_info[neighbor] = (parent_depth + 1, prop, source_qid)
        decision_row = decision_by_qid.get(neighbor)
        if decision_row is None:
            bridge_queue.append(neighbor)
            bridge_count += 1
            return
        candidates.append({
            "row_id": decision_row["row_id"],
            "legacy_row_id": decision_row["legacy_row_id"],
            "seed_qid": seed_qid,
            "seed_label": seed_label,
            "qid": neighbor,
            "label": label,
            "csv_depth": decision_row.get("csv_depth"),
            "bfs_depth": parent_depth + 1,
            "target": decision_row.get("target"),
            "reaching_property": prop,
            "reaching_property_label": label_for_property(prop),
            "parent_qid": source_qid,
            "parent_label": labels.get(source_qid, source_qid),
            "path": build_path_text(seed_qid, neighbor, visited_info, labels),
        })

    while bridge_queue:
        source_qid = bridge_queue.popleft()
        for prop, neighbor in edges_by_subj.get(source_qid, []):
            if prop in fwd_set:
                add_neighbor(source_qid, prop, neighbor)
        for prop, neighbor in edges_by_obj.get(source_qid, []):
            if prop in inv_set:
                add_neighbor(source_qid, f"(-){prop}", neighbor)

    return candidates, bridge_count


def build_path_text(seed_qid, qid, visited, labels):
    if qid not in visited:
        return ""
    chain = []
    current = qid
    while current != seed_qid and current in visited:
        depth, prop, parent = visited[current]
        if not parent:
            break
        chain.append((parent, prop, current))
        current = parent
    chain.reverse()
    if not chain:
        return labels.get(seed_qid, seed_qid)
    parts = [labels.get(seed_qid, seed_qid)]
    for _, prop, child in chain:
        parts.append(f"--{label_for_property(prop) or prop}-->")
        parts.append(labels.get(child, child))
    return " ".join(parts)


def filter_rows(rows, seed_label=None, exclude_ids=None):
    exclude_ids = set(exclude_ids or [])
    selected = [row for row in rows if row["row_id"] not in exclude_ids]
    if seed_label:
        needle = seed_label.lower()
        selected = [row for row in selected if row["seed_label"].lower() == needle]
    return selected


def balanced_sample(rows, sample_size, seed=42):
    if sample_size is None or sample_size <= 0 or sample_size >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    keeps = [row for row in rows if row["target"] == 1]
    prunes = [row for row in rows if row["target"] == 0]
    rng.shuffle(keeps)
    rng.shuffle(prunes)
    half = sample_size // 2
    selected = keeps[:half] + prunes[:sample_size - half]
    if len(selected) < sample_size:
        seen = {row["row_id"] for row in selected}
        rest = [row for row in rows if row["row_id"] not in seen]
        rng.shuffle(rest)
        selected.extend(rest[:sample_size - len(selected)])
    rng.shuffle(selected)
    return selected


def pick_examples(rows, seed_label=None, per_class=4, sample_seed=42):
    return pick_examples_by_counts(
        rows,
        seed_label=seed_label,
        num_keep=per_class,
        num_prune=per_class,
        sample_seed=sample_seed,
    )


def pick_examples_by_counts(rows, seed_label=None, num_keep=4, num_prune=4,
                            sample_seed=42, diversity_key="seed", selection_strategy="diverse"):
    candidates = filter_rows(rows, seed_label=seed_label)
    if not candidates and seed_label:
        raise ValueError(f"No rows found for seed label: {seed_label}")
    rng = random.Random(sample_seed)
    keeps = [row for row in candidates if row["target"] == 1]
    prunes = [row for row in candidates if row["target"] == 0]
    if seed_label:
        rng.shuffle(keeps)
        rng.shuffle(prunes)
        return keeps[:num_keep] + prunes[:num_prune]
    if selection_strategy == "contrastive_property":
        return contrastive_property_sample(candidates, num_keep, num_prune, rng)
    return (
        diverse_sample(keeps, num_keep, rng, diversity_key)
        + diverse_sample(prunes, num_prune, rng, diversity_key)
    )


def contrastive_property_sample(rows, num_keep, num_prune, rng):
    by_prop_target = defaultdict(lambda: {1: [], 0: []})
    for row in rows:
        prop = row.get("reaching_property") or "unknown"
        by_prop_target[prop][row["target"]].append(row)
    for groups in by_prop_target.values():
        rng.shuffle(groups[1])
        rng.shuffle(groups[0])

    mixed_props = [
        prop for prop, groups in by_prop_target.items()
        if groups[1] and groups[0]
    ]
    rng.shuffle(mixed_props)

    selected = []
    used = set()
    keep_count = 0
    prune_count = 0

    while mixed_props and (keep_count < num_keep or prune_count < num_prune):
        progressed = False
        for prop in list(mixed_props):
            groups = by_prop_target[prop]
            if keep_count < num_keep and groups[1]:
                row = groups[1].pop()
                selected.append(row)
                used.add(row["row_id"])
                keep_count += 1
                progressed = True
            if prune_count < num_prune and groups[0]:
                row = groups[0].pop()
                selected.append(row)
                used.add(row["row_id"])
                prune_count += 1
                progressed = True
            if keep_count >= num_keep and prune_count >= num_prune:
                break
        if not progressed:
            break

    if keep_count < num_keep:
        remaining_keeps = [
            row for row in rows
            if row["target"] == 1 and row["row_id"] not in used
        ]
        selected.extend(diverse_sample(remaining_keeps, num_keep - keep_count, rng, "property"))
    if prune_count < num_prune:
        remaining_prunes = [
            row for row in rows
            if row["target"] == 0 and row["row_id"] not in used
        ]
        selected.extend(diverse_sample(remaining_prunes, num_prune - prune_count, rng, "property"))

    return selected


def diverse_by_seed_sample(rows, limit, rng):
    return diverse_sample(rows, limit, rng, "seed")


def diverse_sample(rows, limit, rng, diversity_key="seed"):
    by_seed = defaultdict(list)
    for row in rows:
        by_seed[diversity_bucket(row, diversity_key)].append(row)
    for bucket_rows in by_seed.values():
        rng.shuffle(bucket_rows)
    bucket_order = list(by_seed)
    rng.shuffle(bucket_order)

    selected = []
    while len(selected) < limit:
        progressed = False
        for bucket in bucket_order:
            if by_seed[bucket]:
                selected.append(by_seed[bucket].pop())
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected


def diversity_bucket(row, diversity_key):
    if diversity_key == "property":
        return row.get("reaching_property") or "unknown"
    if diversity_key == "property_seed":
        return (
            row.get("reaching_property") or "unknown",
            row.get("seed_label") or row.get("seed_qid") or "unknown",
        )
    return row.get("seed_label") or row.get("seed_qid") or "unknown"


def pick_explicit_rows(rows, row_ids):
    by_id = {row["row_id"]: row for row in rows}
    by_legacy = defaultdict(list)
    for row in rows:
        by_legacy[row["legacy_row_id"]].append(row)
    examples = []
    missing = []
    for row_id in row_ids:
        if row_id in by_id:
            examples.append(by_id[row_id])
        elif row_id in by_legacy and len(by_legacy[row_id]) == 1:
            examples.append(by_legacy[row_id][0])
        elif row_id in by_legacy:
            matches = ", ".join(row["row_id"] for row in by_legacy[row_id][:10])
            raise ValueError(f"Legacy row ID is ambiguous, use rowN instead: {row_id} matches {matches}")
        else:
            missing.append(row_id)
    if missing:
        raise ValueError("Explicit row IDs not found: " + ", ".join(missing))
    return examples


def format_row_for_prompt(row, context_level="property", include_gold=False):
    if context_level == "path_simple":
        fields = [
            f"candidate={row['label']}",
            f"path={row.get('path') or 'unknown'}",
        ]
        if include_gold:
            fields.append("decision=" + ("INCLUDE" if row["target"] == 1 else "EXCLUDE"))
        return " | ".join(fields)

    depth = row.get("bfs_depth") or row.get("csv_depth")
    fields = [
        f"candidate={row['label']}",
    ]
    if context_level in {"depth", "property", "path"}:
        fields.append(f"depth={depth}")
    if context_level in {"property", "path"}:
        prop = row.get("reaching_property") or "unknown"
        prop_label = row.get("reaching_property_label") or "unknown"
        fields.append(f"reached_via={prop_label} ({prop})")
        if row.get("parent_label"):
            fields.append(f"parent={row['parent_label']}")
    if context_level == "path" and row.get("path"):
        fields.append(f"path={row['path']}")
    if include_gold:
        fields.append("decision=" + ("INCLUDE" if row["target"] == 1 else "EXCLUDE"))
    return " | ".join(fields)


def format_example_for_prompt(example, context_level="property"):
    if context_level == "path_simple":
        fields = [
            f"candidate={example['label']}",
            f"path={example.get('path') or 'unknown'}",
            f"decision={example['decision']}",
        ]
        return " | ".join(fields)

    fields = [
        f"candidate={example['label']}",
    ]
    if context_level in {"depth", "property", "path"}:
        fields.append(f"depth={example.get('depth')}")
    if context_level in {"property", "path"}:
        prop = example.get("reaching_property") or "unknown"
        prop_label = example.get("reaching_property_label") or "unknown"
        fields.append(f"reached_via={prop_label} ({prop})")
        if example.get("parent_label"):
            fields.append(f"parent={example['parent_label']}")
    if context_level == "path" and example.get("path"):
        fields.append(f"path={example['path']}")
    fields.append(f"decision={example['decision']}")
    return " | ".join(fields)


def format_labeled_example_block(example, context_level="property"):
    return "\n".join([
        f"- Seed: {example['seed_label']}",
        f"  Candidate: {format_example_for_prompt(example, context_level)}",
    ])


def build_examples_file(args):
    rows = augment_rows_with_graph_context(
        args.dataset, load_decision_rows(args.dataset, args.data_dir), args.data_dir
    )
    if args.row_id:
        examples = pick_explicit_rows(rows, args.row_id)
    else:
        num_keep = args.num_keep if args.num_keep is not None else args.per_class
        num_prune = args.num_prune if args.num_prune is not None else args.per_class
        examples = pick_examples_by_counts(
            rows,
            seed_label=args.seed_label,
            num_keep=num_keep,
            num_prune=num_prune,
            sample_seed=args.sample_seed,
            diversity_key=args.diversity_key,
            selection_strategy=args.selection_strategy,
        )
    if not examples:
        raise RuntimeError("No examples selected")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset": args.dataset,
        "context_level": args.context_level,
        "diversity_key": args.diversity_key if not args.row_id else "explicit",
        "selection_strategy": args.selection_strategy if not args.row_id else "explicit",
        "created_at": utc_now_iso(),
        "examples": [
            {
                "row_id": row["row_id"],
                "legacy_row_id": row["legacy_row_id"],
                "seed_qid": row["seed_qid"],
                "seed_label": row["seed_label"],
                "qid": row["qid"],
                "label": row["label"],
                "target": row["target"],
                "decision": "INCLUDE" if row["target"] == 1 else "EXCLUDE",
                "depth": row.get("bfs_depth") or row.get("csv_depth"),
                "reaching_property": row.get("reaching_property", ""),
                "reaching_property_label": row.get("reaching_property_label", ""),
                "parent_qid": row.get("parent_qid", ""),
                "parent_label": row.get("parent_label", ""),
                "path": row.get("path", ""),
                "prompt_line": format_labeled_example_block({
                    "seed_label": row["seed_label"],
                    "label": row["label"],
                    "decision": "INCLUDE" if row["target"] == 1 else "EXCLUDE",
                    "depth": row.get("bfs_depth") or row.get("csv_depth"),
                    "reaching_property": row.get("reaching_property", ""),
                    "reaching_property_label": row.get("reaching_property_label", ""),
                    "parent_label": row.get("parent_label", ""),
                    "path": row.get("path", ""),
                }, args.context_level),
            }
            for row in examples
        ],
    }
    with output.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"Examples saved: {output}")
    for example in payload["examples"]:
        print(f"  {example['seed_label']} :: {example['prompt_line']}")


def load_examples(path):
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def format_examples_for_meta_prompt(examples_payload):
    lines = []
    context_level = examples_payload.get("context_level", "property")
    for example in examples_payload["examples"]:
        lines.append(format_labeled_example_block(example, context_level))
    return "\n".join(lines)


def format_examples_for_extraction(examples_payload):
    lines = [
        "## Ground-Truth Keep/Prune Examples",
        "",
        "Use these labeled examples only to calibrate the INCLUDE/EXCLUDE boundary for this dataset.",
        "They are not candidates in the current batch; classify only the candidates supplied by the user message.",
        "",
    ]
    context_level = examples_payload.get("context_level", "property")
    for example in examples_payload["examples"]:
        lines.append(format_labeled_example_block(example, context_level))
    return "\n".join(lines)


def build_effective_prompt(base_prompt, examples_payload=None, include_examples=False):
    prompt = base_prompt.strip()
    if not include_examples:
        return prompt
    if not examples_payload or not examples_payload.get("examples"):
        raise ValueError("Few-shot evaluation requires --examples-file")
    return prompt + "\n\n" + format_examples_for_extraction(examples_payload)


def resolve_prompt_setting(args, prompt_file):
    if args.prompt_setting:
        prompt_setting = args.prompt_setting
    else:
        prompt_setting = (
            "generated"
            if Path(prompt_file).stem.startswith("generated_")
            else "manual"
        )

    include_examples = args.include_examples_in_prompt
    if prompt_setting in {"few_shot_manual", "few_shot_meta"}:
        include_examples = True
    elif prompt_setting in {"zero_shot_manual", "zero_shot_meta"}:
        include_examples = False
    return prompt_setting, include_examples


def extract_response_text(response):
    if hasattr(response, "output_text") and response.output_text:
        return response.output_text.strip()
    chunks = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def validate_generated_prompt_text(text):
    required_patterns = [
        r"^##?\s+.*role",
        r"^##?\s+.*include",
        r"^##?\s+.*exclude",
        r"^##?\s+.*decision",
        r"confidence",
    ]
    missing = [
        pattern for pattern in required_patterns
        if not re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    ]
    if missing:
        raise RuntimeError(
            "Generated prompt appears incomplete; missing expected section(s): "
            + ", ".join(missing)
        )
    if len(text) < 2500:
        raise RuntimeError(
            f"Generated prompt appears too short ({len(text)} chars); "
            "retry the model call."
        )
    disallowed_patterns = [
        r"\brow_id\b",
        r"\bqid\b",
        r"\bevaluations\b",
        r"\*\*Seed:\*\*",
        r"\|\s*\*\*Decision:\*\*",
        r"gold standards for your decision logic",
    ]
    present = [
        pattern for pattern in disallowed_patterns
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    ]
    if present:
        raise RuntimeError(
            "Generated prompt includes schema/identifier text that should be "
            "handled by structured output code instead: "
            + ", ".join(present)
        )


def validate_or_retry_prompt_generation(provider, client, model, content,
                                        generation_reasoning=None):
    retry_content = content
    last_error = None
    for attempt in range(1, 4):
        generated, response_metadata = generate_prompt_with_provider(
            provider,
            client,
            model,
            retry_content,
            generation_reasoning,
        )
        try:
            validate_generated_prompt_text(generated)
            response_metadata["generation_attempts"] = attempt
            return generated, response_metadata
        except RuntimeError as exc:
            last_error = exc
            if attempt == 3:
                raise
            retry_content = (
                content
                + "\n\nYour previous generated prompt failed validation: "
                + str(exc)
                + "\nRegenerate a complete Markdown system prompt with explicit headings "
                  "containing Role, INCLUDE, EXCLUDE, Decision, and Confidence. "
                  "Do not copy any few-shot example seed labels, candidate labels, "
                  "paths, or gold decisions into the generated prompt."
            )
    raise last_error


def generate_prompt_with_openai(client, model, content, generation_reasoning=None):
    generation_reasoning = generation_reasoning or prompt_generation_reasoning(
        model,
        "openai",
    )
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "You generate concise system prompts for LLM-based KG pruning."},
            {"role": "user", "content": content},
        ],
        reasoning={"effort": generation_reasoning["value"]},
    )
    generated = extract_response_text(response)
    if not generated:
        raise RuntimeError("The model returned an empty prompt")
    if getattr(response, "status", None) == "incomplete":
        details = getattr(response, "incomplete_details", None)
        detail_text = (
            details.model_dump()
            if hasattr(details, "model_dump")
            else details
        )
        raise RuntimeError(f"The model returned an incomplete prompt: {detail_text}")
    return generated, {
        "response_status": getattr(response, "status", None),
        "response_incomplete_details": (
            getattr(response, "incomplete_details", None).model_dump()
            if getattr(response, "incomplete_details", None)
            else None
        ),
    }


def generate_prompt_with_gemini(client, model, content, generation_reasoning=None):
    from google.genai import types

    generation_reasoning = generation_reasoning or prompt_generation_reasoning(
        model,
        "gemini",
    )
    config_kwargs = {
        "system_instruction": "You generate concise Markdown system prompts for LLM-based KG pruning.",
    }
    if generation_reasoning["value"] is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=generation_reasoning["value"],
        )
    response = gemini_call_with_retries(
        lambda: client.models.generate_content(
            model=model,
            contents=content,
            config=types.GenerateContentConfig(**config_kwargs),
        )
    )
    generated = (getattr(response, "text", None) or "").strip()
    if not generated:
        raise RuntimeError("The model returned an empty prompt")
    return generated, {
        "response_status": None,
        "response_incomplete_details": None,
    }


def generate_prompt_with_deepseek(client, model, content, generation_reasoning=None):
    generation_reasoning = generation_reasoning or prompt_generation_reasoning(
        model,
        "deepseek",
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You generate concise Markdown system prompts for LLM-based KG pruning.",
            },
            {"role": "user", "content": content},
        ],
        stream=False,
        reasoning_effort=generation_reasoning["value"],
        extra_body={"thinking": {"type": "enabled"}},
    )
    generated = (response.choices[0].message.content or "").strip()
    if not generated:
        raise RuntimeError("The model returned an empty prompt")
    return generated, {
        "response_status": None,
        "response_incomplete_details": None,
    }


def openrouter_message_content(payload):
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter response has no choices: {payload}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts).strip()
    return ""


def parse_batch_response_text(text):
    try:
        return BatchResponse.model_validate_json(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return BatchResponse.model_validate_json(text[start:end + 1])
        raise


def generate_prompt_with_openrouter(client, model, content,
                                    generation_reasoning=None):
    payload = client.chat_completion(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You generate concise Markdown system prompts for LLM-based KG pruning.",
            },
            {"role": "user", "content": content},
        ],
    )
    generated = openrouter_message_content(payload)
    if not generated:
        raise RuntimeError("The model returned an empty prompt")
    return generated, {
        "response_status": None,
        "response_incomplete_details": None,
    }


def generate_prompt_with_provider(provider, client, model, content,
                                  generation_reasoning=None):
    if provider == "gemini":
        return generate_prompt_with_gemini(
            client,
            model,
            content,
            generation_reasoning,
        )
    if provider == "deepseek":
        return generate_prompt_with_deepseek(
            client,
            model,
            content,
            generation_reasoning,
        )
    if provider == "openrouter":
        return generate_prompt_with_openrouter(
            client,
            model,
            content,
            generation_reasoning,
        )
    return generate_prompt_with_openai(client, model, content, generation_reasoning)


def generate_prompt(args):
    apply_examples_file_default(args, "generated")
    prepare_llm_args(args)
    examples_payload = load_examples(args.examples_file)
    if not examples_payload:
        raise ValueError("--examples-file is required for generate")
    meta_prompt = Path(args.meta_prompt).read_text(encoding="utf-8")
    content = (meta_prompt
               .replace("{FEW_SHOT_EXAMPLES}", format_examples_for_meta_prompt(examples_payload)))

    client = get_llm_client(args.provider)
    generation_reasoning = prompt_generation_reasoning(
        args.model,
        args.provider,
        getattr(args, "thinking_level", None),
    )
    generated, response_metadata = validate_or_retry_prompt_generation(
        args.provider,
        client,
        args.model,
        content,
        generation_reasoning,
    )
    output = Path(args.output) if args.output else default_generated_prompt_path(args, examples_payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generated, encoding="utf-8")
    metadata_path = output.with_suffix(".json")
    metadata = {
        "dataset": args.dataset,
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": (
            generation_reasoning["value"] if args.provider in {"openai", "deepseek"} else None
        ),
        "thinking_level": (
            (
                generation_reasoning["value"]
                if generation_reasoning["value"] is not None
                else "default"
            )
            if args.provider == "gemini" else None
        ),
        "prompt_generation_reasoning": generation_reasoning,
        "examples_file": str(args.examples_file),
        "examples_sha256": file_sha256(args.examples_file),
        "meta_prompt": str(args.meta_prompt),
        "meta_prompt_sha256": file_sha256(args.meta_prompt),
        "generation_input_sha256": text_sha256(content),
        "generated_prompt_sha256": file_sha256(output),
        "prompt_chars": len(generated),
        **response_metadata,
        "created_at": utc_now_iso(),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Generated prompt saved: {output}")
    print(f"Metadata saved: {metadata_path}")
    print("\nPreview:\n" + generated[:1200])


def evaluate_rows_prompt(args):
    prepare_llm_args(args)
    args.prompt_file = resolve_prompt_file_for_model(args.prompt_file, args.model, args.provider)
    examples_payload = load_examples(args.examples_file)
    exclude_ids = {
        example["row_id"] for example in examples_payload.get("examples", [])
    } if examples_payload else set()
    rows = augment_rows_with_graph_context(
        args.dataset, load_decision_rows(args.dataset, args.data_dir), args.data_dir
    )
    graph_context = load_graph_context(args.dataset, args.data_dir)
    candidates = filter_rows(rows, seed_label=args.seed_label, exclude_ids=exclude_ids)
    if args.sample_file:
        sample = sample_from_file(candidates, args.sample_file)
        sample_file = Path(args.sample_file)
        sample, skipped_absent_from_graph = skip_rows_absent_from_local_graph(sample, graph_context)
        if not sample:
            raise RuntimeError(
                "No sample rows remain after skipping rows absent from the local graph"
            )
    else:
        candidates, skipped_absent_from_graph = skip_rows_absent_from_local_graph(
            candidates, graph_context
        )
        sample = balanced_sample(candidates, args.sample_size, args.sample_seed)
        if args.sample_size is not None and 0 < args.sample_size < len(candidates):
            sample_file = write_evaluation_sample(
                sample,
                args.dataset,
                args.context_level,
                args.examples_file,
                args.sample_size,
                args.sample_seed,
                args.sample_output_dir,
            )
        else:
            sample_file = None
    base_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    prompt_setting, include_examples_in_prompt = resolve_prompt_setting(args, args.prompt_file)
    prompt = build_effective_prompt(
        base_prompt,
        examples_payload=examples_payload,
        include_examples=include_examples_in_prompt,
    )
    all_results = []
    batches = [
        sample[start:start + args.batch_size]
        for start in range(0, len(sample), args.batch_size)
    ]
    worker_count = min(max(args.workers, 1), max(len(batches), 1))

    if worker_count == 1:
        client = get_llm_client(args.provider)
        for batch_index, batch in enumerate(batches, start=1):
            batch_results = evaluate_batch(
                client,
                args.provider,
                prompt,
                batch,
                args.context_level,
                args.model,
            )
            all_results.extend(batch_results)
            print(f"Evaluated batch {batch_index} ({len(all_results)}/{len(sample)} rows)")
    else:
        completed_rows = 0
        batch_results_by_index = [None] * len(batches)
        print(f"Evaluating {len(batches)} batches with {worker_count} workers ...")
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(
                    evaluate_batch_with_new_client,
                    args.provider,
                    prompt,
                    batch,
                    args.context_level,
                    args.model,
                ): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                batch_results = future.result()
                batch_results_by_index[index] = batch_results
                completed_rows += len(batch_results)
                print(f"Evaluated batch {index + 1} ({completed_rows}/{len(sample)} rows)")
        for batch_results in batch_results_by_index:
            all_results.extend(batch_results or [])

    metrics = compute_metrics(all_results)
    metric_views = {
        "all_evaluated_rows": metrics,
    }
    metric_aggregations = {
        "all_evaluated_rows": pooled_metric_aggregation(all_results),
    }
    baseline_pairs = None
    baseline_subset_results = []
    if args.report_baseline_subset:
        baseline_pairs = load_baseline_pairs(args.dataset, args.runtime_input_dir)
        baseline_subset_results = rows_in_pairs(all_results, baseline_pairs)
        metric_views["kgprune_runtime_rows"] = compute_metrics(baseline_subset_results)
        metric_aggregations["kgprune_runtime_rows"] = compute_fold_metric_aggregation(
            baseline_subset_results,
            args.dataset,
        )

    output_dir = resolve_evaluation_output_dir(args.output_dir, args.model)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = evaluation_result_stem(args, prompt_setting, "row")
    result_json = output_dir / f"{stem}.json"
    result_csv = output_dir / f"{stem}.csv"
    ensure_result_paths_available(
        (result_json, result_csv),
        overwrite=getattr(args, "overwrite", False),
    )
    prompt_metadata_path = Path(args.prompt_file).with_suffix(".json")
    prompt_metadata = load_json_if_exists(prompt_metadata_path)
    sampling_metadata = {}
    if sample_file is not None:
        sampling_metadata["sample_file"] = str(sample_file)
        if not args.sample_file:
            sampling_metadata["sample_seed"] = args.sample_seed
    retained_prompt_metadata = {
        key: prompt_metadata.get(key)
        for key in (
            "model",
            "provider",
            "reasoning_effort",
            "thinking_level",
            "prompt_generation_reasoning",
            "examples_file",
            "examples_sha256",
            "meta_prompt",
            "meta_prompt_sha256",
            "generation_input_sha256",
            "generated_prompt_sha256",
            "created_at",
        )
        if key in prompt_metadata and prompt_metadata.get(key) is not None
    }
    result_json.write_text(json.dumps({
        "dataset": args.dataset,
        "prompt_setting": prompt_setting,
        "prompt_file": str(args.prompt_file),
        "prompt_sha256": file_sha256(args.prompt_file),
        "effective_prompt_sha256": text_sha256(prompt),
        "response_schema": "batch_index_classification_confidence_v1",
        "candidate_identifiers_in_prompt": False,
        "candidate_identifier_mode": "batch_index",
        **({"prompt_metadata": retained_prompt_metadata} if retained_prompt_metadata else {}),
        "examples_file": str(args.examples_file) if args.examples_file else None,
        "examples_sha256": file_sha256(args.examples_file) if args.examples_file else None,
        "examples_in_prompt": include_examples_in_prompt,
        "prompt_examples_count": len(examples_payload.get("examples", [])) if examples_payload else 0,
        "provider": args.provider,
        "model": args.model,
        "reasoning_configuration": "provider_default",
        "context_level": args.context_level,
        **sampling_metadata,
        "workers": worker_count,
        "row_count": len(sample),
        "evaluation_mode": "row",
        "excluded_example_rows": len(exclude_ids),
        "local_graph_filter": {
            "enabled": True,
            "skipped_rows_absent_from_local_graph": skipped_absent_from_graph,
        },
        "metrics_schema_version": 2,
        "metrics": metrics,
        "metric_views": metric_views,
        "metric_aggregations": metric_aggregations,
        "baseline_subset": {
            "enabled": bool(args.report_baseline_subset),
            "runtime_input_dir": str(args.runtime_input_dir) if args.report_baseline_subset else None,
            "runtime_input_file": BASELINE_DECISION_FILES[args.dataset] if args.report_baseline_subset else None,
            "runtime_pairs_total": len(baseline_pairs) if baseline_pairs is not None else None,
            "rows_evaluated": len(baseline_subset_results) if args.report_baseline_subset else None,
            "metrics": metric_views.get("kgprune_runtime_rows"),
            "metric_aggregations": metric_aggregations.get("kgprune_runtime_rows"),
        },
    }, indent=2), encoding="utf-8")
    write_result_csv(result_csv, all_results)

    print("\nPrompt evaluation")
    print(f"  Dataset: {args.dataset}")
    print(f"  Setting: {prompt_setting}")
    print(f"  Prompt: {args.prompt_file}")
    print(f"  Examples in prompt: {include_examples_in_prompt}")
    print(f"  Provider: {args.provider}")
    print(f"  Model: {args.model}")
    print(f"  Context: {args.context_level}")
    print(f"  Workers: {worker_count}")
    print(f"  Rows evaluated: {len(all_results)}")
    print(f"  Skipped absent from local graph: {skipped_absent_from_graph}")
    if sample_file is not None:
        print(f"  Sample CSV: {sample_file}")
    print(f"  TP={metrics['tp']} FP={metrics['fp']} FN={metrics['fn']} TN={metrics['tn']}")
    print(f"  Precision={metrics['precision']:.3f} Recall={metrics['recall']:.3f} "
          f"F1={metrics['f1']:.3f} Accuracy={metrics['accuracy']:.3f}")
    if args.report_baseline_subset:
        subset_metrics = metric_views["kgprune_runtime_rows"]
        print("  KGPrune/PBG runtime-input subset:")
        print(f"    Rows evaluated: {len(baseline_subset_results)} "
              f"(available pairs: {len(baseline_pairs)})")
        print(f"    TP={subset_metrics['tp']} FP={subset_metrics['fp']} "
              f"FN={subset_metrics['fn']} TN={subset_metrics['tn']}")
        print(f"    Precision={subset_metrics['precision']:.3f} "
              f"Recall={subset_metrics['recall']:.3f} "
              f"F1={subset_metrics['f1']:.3f} "
              f"Accuracy={subset_metrics['accuracy']:.3f}")
    print(f"  Results JSON: {result_json}")
    print(f"  Results CSV: {result_csv}")


def evaluate_batch_with_new_client(provider, prompt, batch, context_level, model):
    client = get_llm_client(provider)
    return evaluate_batch(
        client,
        provider,
        prompt,
        batch,
        context_level,
        model,
    )


def evaluate_candidate_rows(prompt, rows, context_level, provider, model, batch_size, workers):
    if not rows:
        return []
    batch_size = max(1, batch_size)
    batches = [
        rows[start:start + batch_size]
        for start in range(0, len(rows), batch_size)
    ]
    worker_count = min(max(workers, 1), max(len(batches), 1))
    if worker_count == 1:
        client = get_llm_client(provider)
        results = []
        for batch in batches:
            results.extend(evaluate_batch(
                client,
                provider,
                prompt,
                batch,
                context_level,
                model,
            ))
        return results

    results_by_index = [None] * len(batches)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(
                evaluate_batch_with_new_client,
                provider,
                prompt,
                batch,
                context_level,
                model,
            ): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            index = futures[future]
            results_by_index[index] = future.result()

    results = []
    for batch_results in results_by_index:
        results.extend(batch_results or [])
    return results


def run_bfs_for_seed(seed_qid, seed_label, gold_rows, graph_context, prompt, args,
                     decision_rows=None):
    labels, edges_by_subj, edges_by_obj, allowed_nodes = seed_graph_indexes(seed_qid, graph_context)
    labels.setdefault(seed_qid, seed_label)
    traversal_rows = list(decision_rows or gold_rows)
    for row in traversal_rows:
        if row.get("label"):
            labels.setdefault(row["qid"], row["label"])

    decision_row_by_qid = decision_rows_by_qid(traversal_rows)
    visited_info = {seed_qid: (0, None, None)}
    included_qids = {seed_qid}
    pruned_qids = set()
    frontier = [seed_qid]
    llm_decision_by_qid = {}
    raw_decisions = []
    iteration_details = []
    total_bridge_nodes_traversed = 0

    for iteration in range(1, args.max_iterations + 1):
        candidates = []
        bridge_nodes_traversed = 0
        for parent_qid in frontier:
            parent_candidates, parent_bridge_count = next_decision_candidate_rows(
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
                decision_row_by_qid,
            )
            candidates.extend(parent_candidates)
            bridge_nodes_traversed += parent_bridge_count
        total_bridge_nodes_traversed += bridge_nodes_traversed
        if not candidates:
            break

        batch_results = evaluate_candidate_rows(
            prompt,
            candidates,
            args.context_level,
            args.provider,
            args.model,
            args.batch_size,
            args.workers,
        )
        raw_decisions.extend(batch_results)

        next_frontier = []
        for result in batch_results:
            qid = result["qid"]
            llm_decision_by_qid[qid] = result
            if result["prediction"] == 1:
                included_qids.add(qid)
                next_frontier.append(qid)
            elif result["prediction"] == 0:
                pruned_qids.add(qid)

        iteration_details.append({
            "iteration": iteration,
            "evaluated": len(candidates),
            "included": len(next_frontier),
            "excluded": len(candidates) - len(next_frontier),
            "bridge_nodes_traversed": bridge_nodes_traversed,
        })
        if not next_frontier:
            break
        frontier = next_frontier

    scored_rows = []
    for row in gold_rows:
        qid = row["qid"]
        decision = llm_decision_by_qid.get(qid)
        if qid in included_qids:
            prediction = 1
            classification = "INCLUDE"
            confidence = decision.get("confidence") if decision else None
        else:
            prediction = 0
            if decision:
                classification = decision.get("classification", "EXCLUDE")
                confidence = decision.get("confidence")
            else:
                classification = "NOT_VISITED"
                confidence = None

        scored_rows.append({
            "row_id": row["row_id"],
            "legacy_row_id": row["legacy_row_id"],
            "seed_qid": row["seed_qid"],
            "seed_label": row["seed_label"],
            "qid": qid,
            "label": row["label"],
            "target": row["target"],
            "prediction": prediction,
            "classification": classification,
            "confidence": confidence,
            "depth": (decision or row).get("depth") or row.get("bfs_depth") or row.get("csv_depth"),
            "reaching_property": row.get("reaching_property", ""),
            "reaching_property_label": row.get("reaching_property_label", ""),
        })

    return {
        "seed_qid": seed_qid,
        "seed_label": seed_label,
        "gold_rows": scored_rows,
        "raw_decisions": raw_decisions,
        "included_qids": sorted(included_qids),
        "pruned_qids": sorted(pruned_qids),
        "visited_qids": sorted(visited_info),
        "bridge_nodes_traversed": total_bridge_nodes_traversed,
        "iterations": len(iteration_details),
        "iteration_details": iteration_details,
    }


def run_bfs_for_seeds(by_seed, traversal_by_seed, graph_context, prompt, args,
                      seed_runner=None):
    seed_runner = seed_runner or run_bfs_for_seed
    seed_items = sorted(by_seed.items())
    if not seed_items:
        return [], []

    worker_count = min(max(args.workers, 1), len(seed_items))
    seed_args = copy(args)
    seed_args.workers = args.workers if len(seed_items) == 1 else 1

    def run_seed(seed_qid, seed_rows):
        seed_label = seed_rows[0]["seed_label"]
        return seed_runner(
            seed_qid,
            seed_label,
            seed_rows,
            graph_context,
            prompt,
            seed_args,
            decision_rows=traversal_by_seed.get(seed_qid, seed_rows),
        )

    if worker_count == 1:
        seed_results = []
        for seed_qid, seed_rows in seed_items:
            print(
                f"Running BFS seed {seed_rows[0]['seed_label']} ({seed_qid}) "
                f"with {len(seed_rows)} gold rows ..."
            )
            seed_results.append(run_seed(seed_qid, seed_rows))
    else:
        print(
            f"Running {len(seed_items)} BFS seeds with {worker_count} "
            "parallel seed workers ..."
        )
        results_by_index = [None] * len(seed_items)
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(run_seed, seed_qid, seed_rows): index
                for index, (seed_qid, seed_rows) in enumerate(seed_items)
            }
            completed = 0
            for future in as_completed(futures):
                index = futures[future]
                results_by_index[index] = future.result()
                completed += 1
                print(f"Completed BFS seed {completed}/{len(seed_items)}")
        seed_results = results_by_index

    all_results = []
    for seed_result in seed_results:
        all_results.extend(seed_result["gold_rows"])
    return seed_results, all_results


def evaluate_bfs_prompt(args):
    prepare_llm_args(args)
    args.prompt_file = resolve_prompt_file_for_model(args.prompt_file, args.model, args.provider)
    examples_payload = load_examples(args.examples_file)
    exclude_ids = {
        example["row_id"] for example in examples_payload.get("examples", [])
    } if examples_payload else set()

    rows = augment_rows_with_graph_context(
        args.dataset, load_decision_rows(args.dataset, args.data_dir), args.data_dir
    )
    graph_context = load_graph_context(args.dataset, args.data_dir)
    traversal_candidates = filter_rows(rows, seed_label=args.seed_label)
    traversal_candidates, _ = skip_rows_absent_from_local_graph(
        traversal_candidates, graph_context
    )
    candidates = filter_rows(rows, seed_label=args.seed_label, exclude_ids=exclude_ids)
    if args.sample_file:
        sample = sample_from_file(candidates, args.sample_file)
        sample_file = Path(args.sample_file)
        sample, skipped_absent_from_graph = skip_rows_absent_from_local_graph(sample, graph_context)
        if not sample:
            raise RuntimeError(
                "No sample rows remain after skipping rows absent from the local graph"
            )
    else:
        candidates, skipped_absent_from_graph = skip_rows_absent_from_local_graph(
            candidates, graph_context
        )
        sample = balanced_sample(candidates, args.sample_size, args.sample_seed)
        if args.sample_size is not None and 0 < args.sample_size < len(candidates):
            sample_file = write_evaluation_sample(
                sample,
                args.dataset,
                args.context_level,
                args.examples_file,
                args.sample_size,
                args.sample_seed,
                args.sample_output_dir,
            )
        else:
            sample_file = None

    by_seed = defaultdict(list)
    for row in sample:
        by_seed[row["seed_qid"]].append(row)
    traversal_by_seed = defaultdict(list)
    for row in traversal_candidates:
        traversal_by_seed[row["seed_qid"]].append(row)

    base_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    prompt_setting, include_examples_in_prompt = resolve_prompt_setting(args, args.prompt_file)
    prompt = build_effective_prompt(
        base_prompt,
        examples_payload=examples_payload,
        include_examples=include_examples_in_prompt,
    )

    seed_results, all_results = run_bfs_for_seeds(
        by_seed,
        traversal_by_seed,
        graph_context,
        prompt,
        args,
    )

    metrics = compute_metrics(all_results)
    metric_views = {
        "all_evaluated_rows": metrics,
    }
    metric_aggregations = {
        "all_evaluated_rows": pooled_metric_aggregation(all_results),
    }
    baseline_pairs = None
    baseline_subset_results = []
    if args.report_baseline_subset:
        baseline_pairs = load_baseline_pairs(args.dataset, args.runtime_input_dir)
        baseline_subset_results = rows_in_pairs(all_results, baseline_pairs)
        metric_views["kgprune_runtime_rows"] = compute_metrics(baseline_subset_results)
        metric_aggregations["kgprune_runtime_rows"] = compute_fold_metric_aggregation(
            baseline_subset_results,
            args.dataset,
        )

    output_dir = resolve_evaluation_output_dir(args.output_dir, args.model)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = evaluation_result_stem(args, prompt_setting, "bfs")
    result_json = output_dir / f"{stem}.json"
    result_csv = output_dir / f"{stem}.csv"
    ensure_result_paths_available(
        (result_json, result_csv),
        overwrite=getattr(args, "overwrite", False),
    )
    prompt_metadata_path = Path(args.prompt_file).with_suffix(".json")
    prompt_metadata = load_json_if_exists(prompt_metadata_path)
    sampling_metadata = {}
    if sample_file is not None:
        sampling_metadata["sample_file"] = str(sample_file)
        if not args.sample_file:
            sampling_metadata["sample_seed"] = args.sample_seed
    retained_prompt_metadata = {
        key: prompt_metadata.get(key)
        for key in (
            "model",
            "provider",
            "reasoning_effort",
            "thinking_level",
            "prompt_generation_reasoning",
            "examples_file",
            "examples_sha256",
            "meta_prompt",
            "meta_prompt_sha256",
            "generation_input_sha256",
            "generated_prompt_sha256",
            "created_at",
        )
        if key in prompt_metadata and prompt_metadata.get(key) is not None
    }
    result_json.write_text(json.dumps({
        "dataset": args.dataset,
        "prompt_setting": prompt_setting,
        "prompt_file": str(args.prompt_file),
        "prompt_sha256": file_sha256(args.prompt_file),
        "effective_prompt_sha256": text_sha256(prompt),
        "evaluation_mode": "bfs",
        "response_schema": "batch_index_classification_confidence_v1",
        "candidate_identifiers_in_prompt": False,
        "candidate_identifier_mode": "batch_index",
        **({"prompt_metadata": retained_prompt_metadata} if retained_prompt_metadata else {}),
        "examples_file": str(args.examples_file) if args.examples_file else None,
        "examples_sha256": file_sha256(args.examples_file) if args.examples_file else None,
        "examples_in_prompt": include_examples_in_prompt,
        "prompt_examples_count": len(examples_payload.get("examples", [])) if examples_payload else 0,
        "provider": args.provider,
        "model": args.model,
        "reasoning_configuration": "provider_default",
        "context_level": args.context_level,
        "max_iterations": args.max_iterations,
        **sampling_metadata,
        "workers": args.workers,
        "row_count": len(sample),
        "selected_seed_count": len(by_seed),
        "traversal_decision_rows": sum(
            len(traversal_by_seed.get(seed_qid, [])) for seed_qid in by_seed
        ),
        "excluded_example_rows": len(exclude_ids),
        "bridge_policy": {
            "enabled": True,
            "unlabeled_nodes": "traverse_without_llm_decision",
            "decision_rows": "llm_classified_when_reached",
            "scored_rows": "sample_rows_only",
        },
        "local_graph_filter": {
            "enabled": True,
            "skipped_rows_absent_from_local_graph": skipped_absent_from_graph,
        },
        "metrics_schema_version": 2,
        "metrics": metrics,
        "metric_views": metric_views,
        "metric_aggregations": metric_aggregations,
        "baseline_subset": {
            "enabled": bool(args.report_baseline_subset),
            "runtime_input_dir": str(args.runtime_input_dir) if args.report_baseline_subset else None,
            "runtime_input_file": BASELINE_DECISION_FILES[args.dataset] if args.report_baseline_subset else None,
            "runtime_pairs_total": len(baseline_pairs) if baseline_pairs is not None else None,
            "rows_evaluated": len(baseline_subset_results) if args.report_baseline_subset else None,
            "metrics": metric_views.get("kgprune_runtime_rows"),
            "metric_aggregations": metric_aggregations.get("kgprune_runtime_rows"),
        },
        "seed_results": [
            {
                "seed_qid": result["seed_qid"],
                "seed_label": result["seed_label"],
                "gold_row_count": len(result["gold_rows"]),
                "iterations": result["iterations"],
                "iteration_details": result["iteration_details"],
                "included_count": len(result["included_qids"]),
                "pruned_count": len(result["pruned_qids"]),
                "visited_count": len(result["visited_qids"]),
                "bridge_nodes_traversed": result["bridge_nodes_traversed"],
            }
            for result in seed_results
        ],
    }, indent=2), encoding="utf-8")
    write_result_csv(result_csv, all_results)

    print("\nPrompt evaluation")
    print(f"  Dataset: {args.dataset}")
    print(f"  Mode: bfs")
    print(f"  Setting: {prompt_setting}")
    print(f"  Prompt: {args.prompt_file}")
    print(f"  Examples in prompt: {include_examples_in_prompt}")
    print(f"  Provider: {args.provider}")
    print(f"  Model: {args.model}")
    print(f"  Context: {args.context_level}")
    print(f"  Selected seeds: {len(by_seed)}")
    print(f"  Gold rows scored: {len(all_results)}")
    print(f"  Skipped absent from local graph: {skipped_absent_from_graph}")
    if sample_file is not None:
        print(f"  Sample CSV: {sample_file}")
    print(f"  TP={metrics['tp']} FP={metrics['fp']} FN={metrics['fn']} TN={metrics['tn']}")
    print(f"  Precision={metrics['precision']:.3f} Recall={metrics['recall']:.3f} "
          f"F1={metrics['f1']:.3f} Accuracy={metrics['accuracy']:.3f}")
    print(f"  Results JSON: {result_json}")
    print(f"  Results CSV: {result_csv}")


def evaluate_prompt(args):
    prompt_setting, _ = resolve_prompt_setting(args, args.prompt_file)
    apply_examples_file_default(args, prompt_setting)
    if args.eval_mode == "row":
        return evaluate_rows_prompt(args)
    return evaluate_bfs_prompt(args)


def evaluate_batch(client, provider, prompt, batch, context_level, model):
    user_message = build_eval_user_message(batch, context_level)
    if provider == "gemini":
        return evaluate_batch_gemini(client, prompt, user_message, batch, model)
    if provider == "deepseek":
        return evaluate_batch_deepseek(
            client,
            prompt,
            user_message,
            batch,
            model,
        )
    if provider == "openrouter":
        return evaluate_batch_openrouter(
            client,
            prompt,
            user_message,
            batch,
            model,
        )
    return evaluate_batch_openai(
        client,
        prompt,
        user_message,
        batch,
        model,
    )


def evaluate_batch_openai(client, prompt, user_message, batch, model):
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_message},
        ],
        text_format=BatchResponse,
    )
    return match_evaluations_by_index(batch, response.output_parsed.evaluations)


def evaluate_batch_gemini(client, prompt, user_message, batch, model):
    from google.genai import types

    response = gemini_call_with_retries(
        lambda: client.models.generate_content(
            model=model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=prompt,
                response_mime_type="application/json",
                response_schema=BatchResponse,
            ),
        )
    )
    parsed = BatchResponse.model_validate_json(response.text)
    return match_evaluations_by_index(batch, parsed.evaluations)


def deepseek_evaluation_tool():
    return {
        "type": "function",
        "function": {
            "name": DEEPSEEK_EVALUATION_TOOL_NAME,
            "description": (
                "Return candidate keep/prune decisions for the supplied "
                "numbered sub-knowledge-graph extraction candidates."
            ),
            "parameters": DEEPSEEK_EVALUATION_TOOL_SCHEMA,
            "strict": True,
        },
    }


def evaluate_batch_deepseek(client, prompt, user_message, batch, model):
    for attempt in range(1, DEFAULT_DEEPSEEK_PROTOCOL_RETRIES + 1):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_message},
            ],
            tools=[deepseek_evaluation_tool()],
            stream=False,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            arguments = tool_calls[0].function.arguments
            parsed = BatchResponse.model_validate_json(arguments)
            return match_evaluations_by_index(batch, parsed.evaluations)
        if attempt < DEFAULT_DEEPSEEK_PROTOCOL_RETRIES:
            print(
                "DeepSeek response omitted the required tool call; "
                f"retry {attempt}/{DEFAULT_DEEPSEEK_PROTOCOL_RETRIES - 1} ..."
            )
    raise RuntimeError("DeepSeek response did not include the required tool call")


def evaluate_batch_openrouter(client, prompt, user_message, batch, model):
    json_instruction = (
        "\n\nReturn only valid JSON with this shape: "
        "{\"evaluations\":[{\"idx\":1,\"classification\":\"INCLUDE\",\"confidence\":0.0}]}"
    )
    payload = client.chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_message + json_instruction},
        ],
    )
    parsed = parse_batch_response_text(openrouter_message_content(payload))
    return match_evaluations_by_index(batch, parsed.evaluations)


def build_eval_user_message(batch, context_level):
    lines = [
        f"Classify exactly {len(batch)} candidates as INCLUDE or EXCLUDE.",
        "Return one evaluation for each numbered candidate.",
        "Use the candidate number as idx. Each evaluation should contain only idx, classification, and confidence.",
        "",
    ]
    for index, row in enumerate(batch, start=1):
        lines.append(f"{index}. Seed: {row['seed_label']}")
        lines.append(f"   Candidate: {format_row_for_prompt(row, context_level, include_gold=False)}")
    return "\n".join(lines)


def match_evaluations_by_index(batch, evaluations):
    results = []
    by_index = {}
    duplicate_indexes = set()
    for ev in evaluations:
        if ev.idx in by_index:
            duplicate_indexes.add(ev.idx)
        by_index[ev.idx] = ev

    for index, row in enumerate(batch, start=1):
        ev = by_index.get(index)
        if ev is None or index in duplicate_indexes:
            results.append({
                "row_id": row["row_id"],
                "legacy_row_id": row["legacy_row_id"],
                "seed_qid": row["seed_qid"],
                "seed_label": row["seed_label"],
                "qid": row["qid"],
                "label": row["label"],
                "target": row["target"],
                "prediction": None,
                "classification": "NO_MATCH",
                "confidence": None,
                "depth": row.get("bfs_depth") or row.get("csv_depth"),
                "reaching_property": row.get("reaching_property", ""),
                "reaching_property_label": row.get("reaching_property_label", ""),
            })
            continue

        predicted = 1 if ev.classification.upper() == "INCLUDE" else 0
        results.append({
            "row_id": row["row_id"],
            "legacy_row_id": row["legacy_row_id"],
            "seed_qid": row["seed_qid"],
            "seed_label": row["seed_label"],
            "qid": row["qid"],
            "label": row["label"],
            "target": row["target"],
            "prediction": predicted,
            "classification": ev.classification.upper(),
            "confidence": ev.confidence,
            "depth": row.get("bfs_depth") or row.get("csv_depth"),
            "reaching_property": row.get("reaching_property", ""),
            "reaching_property_label": row.get("reaching_property_label", ""),
        })
    return results


def compute_metrics(results):
    valid = [r for r in results if r["prediction"] is not None]
    tp = sum(1 for r in valid if r["target"] == 1 and r["prediction"] == 1)
    fp = sum(1 for r in valid if r["target"] == 0 and r["prediction"] == 1)
    fn = sum(1 for r in valid if r["target"] == 1 and r["prediction"] == 0)
    tn = sum(1 for r in valid if r["target"] == 0 and r["prediction"] == 0)
    total = tp + fp + fn + tn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (tp + tn) / total if total else 0.0
    return {
        "total": total,
        "unmatched": len(results) - len(valid),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
    }


def pooled_metric_aggregation(results):
    """Represent a prediction set for which only pooled scoring is defined."""
    return {
        "pooled": compute_metrics(results),
        "fold_coverage": None,
        "folds": None,
        "weighted_fold": None,
    }


def compute_fold_metric_aggregation(
    results,
    dataset,
    folds_dir=DEFAULT_FOLDS_DIR,
):
    """Compute per-fold, row-weighted fold, and pooled prediction metrics.

    Fold weights are the numbers of scored decision rows in each fold.  The
    reported dispersion is the corresponding weighted population standard
    deviation.  If the supplied rows do not cover all five test folds, pooled
    metrics and the available per-fold metrics are still returned, while the
    five-fold aggregate is ``None``.
    """
    fold_by_seed = load_evaluation_fold_map(dataset, folds_dir)
    rows_by_fold = defaultdict(list)
    unknown_seeds = set()
    for row in results:
        seed_qid = row_seed_qid(row)
        fold_index = fold_by_seed.get(seed_qid)
        if fold_index is None:
            unknown_seeds.add(seed_qid or "<missing>")
            continue
        rows_by_fold[fold_index].append(row)
    if unknown_seeds:
        preview = ", ".join(sorted(unknown_seeds)[:10])
        raise ValueError(
            f"Rows contain {len(unknown_seeds)} seed(s) absent from the "
            f"Dataset {normalize_dataset_id(dataset)} fold definition: {preview}"
        )

    pooled_metrics = compute_metrics(results)
    fold_results = []
    for fold_index in range(5):
        fold_rows = rows_by_fold.get(fold_index, [])
        fold_metrics = compute_metrics(fold_rows)
        fold_results.append({
            "fold": fold_index,
            "rows": len(fold_rows),
            "scored_rows": fold_metrics["total"],
            "unmatched_rows": fold_metrics["unmatched"],
            "metrics": fold_metrics,
        })

    present_folds = [
        item["fold"] for item in fold_results if item["scored_rows"] > 0
    ]
    missing_folds = sorted(set(range(5)) - set(present_folds))
    weighted_fold = None
    if not missing_folds:
        total_weight = sum(item["scored_rows"] for item in fold_results)
        weighted_fold = {
            "weighting": "scored_decision_rows",
            "fold_count": len(fold_results),
            "total_weight": total_weight,
        }
        for metric_name in METRIC_NAMES:
            weighted_mean = sum(
                item["scored_rows"] * item["metrics"][metric_name]
                for item in fold_results
            ) / total_weight
            weighted_variance = sum(
                item["scored_rows"]
                * (item["metrics"][metric_name] - weighted_mean) ** 2
                for item in fold_results
            ) / total_weight
            weighted_fold[metric_name] = weighted_mean
            weighted_fold[f"std_{metric_name}"] = math.sqrt(weighted_variance)

        pooled_accuracy = pooled_metrics["accuracy"]
        if not math.isclose(
            weighted_fold["accuracy"],
            pooled_accuracy,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise AssertionError(
                "Row-count-weighted fold accuracy must equal pooled accuracy"
            )

    return {
        "pooled": pooled_metrics,
        "fold_coverage": {
            "expected": list(range(5)),
            "present": present_folds,
            "missing": missing_folds,
            "complete": not missing_folds,
        },
        "folds": fold_results,
        "weighted_fold": weighted_fold,
    }


def _read_retained_csv_rows(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Prediction file not found: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _index_unique_retained_rows(rows, path):
    indexed = {}
    for row in rows:
        row_id = row.get("row_id", "").strip()
        if not row_id:
            raise ValueError(f"A row in {path} has no row_id")
        if row_id in indexed:
            raise ValueError(f"Duplicate row_id {row_id} in {path}")
        indexed[row_id] = row
    return indexed


def _parse_retained_binary(value, field, row_id, path):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Non-integer {field}={value!r} for {row_id} in {path}"
        ) from exc
    if parsed not in (0, 1):
        raise ValueError(f"Non-binary {field}={parsed} for {row_id} in {path}")
    return parsed


def _retained_seed_qid(row):
    return (row.get("seed_qid") or row.get("from") or "").strip()


def _portable_repo_path(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _retained_baseline_path(dataset, baseline, decisions_dir):
    return Path(decisions_dir) / RETAINED_BASELINE_FILES[dataset][baseline]


def _retained_llm_path(dataset, model, strategy, results_dir):
    results_dir = Path(results_dir)
    if model == "deepseek-v4-pro":
        return (
            results_dir
            / model
            / f"dataset{dataset}_{strategy}_{model}.csv"
        )
    return (
        results_dir
        / model
        / f"dataset{dataset}"
        / strategy
        / "bfs_gated_predictions.csv"
    )


def _normalize_retained_rows(rows, path, expected_rows=None):
    indexed = _index_unique_retained_rows(rows, path)
    if expected_rows is None:
        selected_ids = list(indexed)
    else:
        selected_ids = list(expected_rows)
        missing = sorted(set(selected_ids) - set(indexed))
        if missing:
            preview = ", ".join(missing[:10])
            raise ValueError(
                f"{path} is missing {len(missing)} aligned row(s): {preview}"
            )

    normalized = []
    for row_id in selected_ids:
        row = indexed[row_id]
        seed_qid = _retained_seed_qid(row)
        if not seed_qid:
            raise ValueError(f"No seed QID for {row_id} in {path}")
        normalized.append({
            "row_id": row_id,
            "seed_qid": seed_qid,
            "target": _parse_retained_binary(
                row.get("target"), "target", row_id, path
            ),
            "prediction": _parse_retained_binary(
                row.get("prediction"), "prediction", row_id, path
            ),
        })
    return normalized, indexed


def _validate_retained_reference_universe(dataset, decisions_dir):
    reference_path = _retained_baseline_path(dataset, "lstm", decisions_dir)
    reference_raw = _read_retained_csv_rows(reference_path)
    reference_rows, reference_index = _normalize_retained_rows(
        reference_raw,
        reference_path,
    )
    reference_by_id = {row["row_id"]: row for row in reference_rows}
    reference_ids = list(reference_by_id)
    fold_map = load_evaluation_fold_map(dataset)

    expected_fold_by_row = {}
    for row_id in reference_ids:
        raw_row = reference_index[row_id]
        seed_qid = reference_by_id[row_id]["seed_qid"]
        expected_fold = fold_map.get(seed_qid)
        if expected_fold is None:
            raise ValueError(
                f"Reference seed {seed_qid} for {row_id} is absent from "
                f"Dataset {dataset} fold definitions"
            )
        recorded_fold = raw_row.get("fold", "").strip()
        if recorded_fold and int(recorded_fold) != expected_fold:
            raise ValueError(
                f"Fold mismatch for {row_id} in {reference_path}: "
                f"recorded {recorded_fold}, expected {expected_fold}"
            )
        expected_fold_by_row[row_id] = expected_fold

    path = _retained_baseline_path(dataset, "path_analogy", decisions_dir)
    raw_rows = _read_retained_csv_rows(path)
    rows, indexed = _normalize_retained_rows(raw_rows, path, reference_ids)
    if set(indexed) != set(reference_ids):
        extra = sorted(set(indexed) - set(reference_ids))
        missing = sorted(set(reference_ids) - set(indexed))
        raise ValueError(
            f"Baseline row universe mismatch in {path}: "
            f"{len(missing)} missing, {len(extra)} extra"
        )
    for row in rows:
        reference = reference_by_id[row["row_id"]]
        if row["target"] != reference["target"]:
            raise ValueError(
                f"Target mismatch for {row['row_id']} in {path}: "
                f"{row['target']} != {reference['target']}"
            )
        recorded_fold = indexed[row["row_id"]].get("fold", "").strip()
        expected_fold = expected_fold_by_row[row["row_id"]]
        if recorded_fold and int(recorded_fold) != expected_fold:
            raise ValueError(
                f"Fold mismatch for {row['row_id']} in {path}: "
                f"recorded {recorded_fold}, expected {expected_fold}"
            )

    return reference_ids, reference_by_id


def _validate_retained_targets(rows, reference_by_id, path):
    for row in rows:
        reference = reference_by_id[row["row_id"]]
        if row["seed_qid"] != reference["seed_qid"]:
            raise ValueError(
                f"Seed mismatch for {row['row_id']} in {path}: "
                f"{row['seed_qid']} != {reference['seed_qid']}"
            )
        if row["target"] != reference["target"]:
            raise ValueError(
                f"Target mismatch for {row['row_id']} in {path}: "
                f"{row['target']} != {reference['target']}"
            )


def _build_retained_metric_result(
    dataset,
    method_family,
    model,
    prompt_strategy,
    configuration_id,
    display_name,
    path,
    source_rows,
    aligned_rows,
):
    aggregation = compute_fold_metric_aggregation(aligned_rows, dataset)
    if not aggregation["fold_coverage"]["complete"]:
        raise ValueError(
            f"Aligned rows from {path} do not cover all five folds: "
            f"{aggregation['fold_coverage']}"
        )
    weighted = aggregation["weighted_fold"]
    pooled = aggregation["pooled"]
    if abs(weighted["accuracy"] - pooled["accuracy"]) > 1e-12:
        raise ValueError(
            f"Weighted and pooled accuracy disagree for {configuration_id}: "
            f"{weighted['accuracy']} != {pooled['accuracy']}"
        )
    return {
        "dataset": dataset,
        "method_family": method_family,
        "model": model,
        "prompt_strategy": prompt_strategy,
        "configuration_id": configuration_id,
        "display_name": display_name,
        "source_file": _portable_repo_path(path),
        "source_rows": source_rows,
        "aligned_rows": len(aligned_rows),
        "excluded_nonruntime_rows": source_rows - len(aligned_rows),
        "metric_aggregations": aggregation,
    }


def _flatten_retained_metric_result(result):
    aggregation = result["metric_aggregations"]
    weighted = aggregation["weighted_fold"]
    pooled = aggregation["pooled"]
    flat = {
        key: result[key]
        for key in (
            "dataset",
            "method_family",
            "model",
            "prompt_strategy",
            "configuration_id",
            "display_name",
            "source_file",
            "source_rows",
            "aligned_rows",
            "excluded_nonruntime_rows",
        )
    }
    for fold in aggregation["folds"]:
        flat[f"fold_{fold['fold']}_rows"] = fold["scored_rows"]
    for metric_name in METRIC_NAMES:
        flat[f"weighted_{metric_name}"] = weighted[metric_name]
        flat[f"weighted_std_{metric_name}"] = weighted[f"std_{metric_name}"]
    for name in (
        "total",
        "unmatched",
        "tp",
        "fp",
        "fn",
        "tn",
        "precision",
        "recall",
        "f1",
        "accuracy",
    ):
        flat[f"pooled_{name}"] = pooled[name]
    return flat


def collect_retained_metric_results(results_dir, decisions_dir):
    """Validate and aggregate all retained Wikidata benchmark predictions."""
    results = []
    validation = {}
    for dataset in (1, 2, 3):
        reference_ids, reference_by_id = _validate_retained_reference_universe(
            dataset,
            decisions_dir,
        )
        fold_counts = {str(index): 0 for index in range(5)}
        fold_map = load_evaluation_fold_map(dataset)
        for row in reference_by_id.values():
            fold_counts[str(fold_map[row["seed_qid"]])] += 1
        validation[str(dataset)] = {
            "aligned_rows": len(reference_ids),
            "fold_rows": fold_counts,
        }

        for baseline in ("lstm", "path_analogy"):
            path = _retained_baseline_path(dataset, baseline, decisions_dir)
            raw_rows = _read_retained_csv_rows(path)
            rows, indexed = _normalize_retained_rows(
                raw_rows,
                path,
                reference_ids,
            )
            if set(indexed) != set(reference_ids):
                raise ValueError(
                    f"Selected baseline {path} is not row-complete for "
                    f"Dataset {dataset}"
                )
            _validate_retained_targets(rows, reference_by_id, path)
            results.append(_build_retained_metric_result(
                dataset=dataset,
                method_family="baseline",
                model=RETAINED_BASELINE_LABELS[baseline],
                prompt_strategy="",
                configuration_id=f"baseline_{baseline}",
                display_name=RETAINED_BASELINE_LABELS[baseline],
                path=path,
                source_rows=len(raw_rows),
                aligned_rows=rows,
            ))

        for model in RETAINED_LLM_MODELS:
            for strategy in RETAINED_PROMPT_STRATEGIES:
                path = _retained_llm_path(dataset, model, strategy, results_dir)
                raw_rows = _read_retained_csv_rows(path)
                rows, _ = _normalize_retained_rows(
                    raw_rows,
                    path,
                    reference_ids,
                )
                _validate_retained_targets(rows, reference_by_id, path)
                results.append(_build_retained_metric_result(
                    dataset=dataset,
                    method_family="subkg_extractor",
                    model=model,
                    prompt_strategy=strategy,
                    configuration_id=f"subkg_{model}_{strategy}",
                    display_name=f"{model} / {strategy}",
                    path=path,
                    source_rows=len(raw_rows),
                    aligned_rows=rows,
                ))

    expected_count = 3 * (
        2 + len(RETAINED_LLM_MODELS) * len(RETAINED_PROMPT_STRATEGIES)
    )
    if len(results) != expected_count:
        raise AssertionError(
            f"Expected {expected_count} results, produced {len(results)}"
        )
    return results, validation


def write_retained_metric_outputs(
    results,
    validation,
    output_csv,
    output_json,
):
    output_csv = Path(output_csv)
    output_json = Path(output_json)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)

    flat_rows = [_flatten_retained_metric_result(result) for result in results]
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flat_rows[0]))
        writer.writeheader()
        writer.writerows(flat_rows)

    payload = {
        "schema_version": 1,
        "aggregation": {
            "weighted_fold": (
                "Five fold-level metric values weighted by the number of "
                "aligned scored decision rows in each fold; dispersion is "
                "the weighted population standard deviation."
            ),
            "pooled": (
                "Metrics computed once from the combined row-level "
                "predictions across all five folds."
            ),
        },
        "validation": validation,
        "result_count": len(results),
        "results": results,
    }
    output_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def aggregate_retained_results(args):
    """Reaggregate retained predictions without making model API calls."""
    results, validation = collect_retained_metric_results(
        args.results_dir,
        args.baseline_decisions_dir,
    )
    write_retained_metric_outputs(
        results,
        validation,
        args.output_csv,
        args.output_json,
    )
    print(f"Validated and aggregated {len(results)} method configurations")
    for dataset, details in validation.items():
        print(
            f"  Dataset {dataset}: {details['aligned_rows']} rows; "
            f"folds {details['fold_rows']}"
        )
    print(f"  CSV: {args.output_csv}")
    print(f"  JSON: {args.output_json}")


def _optional_int(value):
    if value in ("", None):
        return None
    return int(value)


def _optional_float(value):
    if value in ("", None):
        return None
    return float(value)


def load_result_csv(path):
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            row["target"] = _optional_int(row.get("target"))
            row["prediction"] = _optional_int(row.get("prediction"))
            row["depth"] = _optional_int(row.get("depth"))
            row["confidence"] = _optional_float(row.get("confidence"))
            rows.append(row)
    return rows


def error_kind(row):
    if row["prediction"] is None:
        return "unmatched"
    if row["target"] == 1 and row["prediction"] == 0:
        return "fn"
    if row["target"] == 0 and row["prediction"] == 1:
        return "fp"
    return "correct"


def compare_result_csvs(args):
    left_rows = load_result_csv(args.left)
    right_rows = load_result_csv(args.right)
    left_by_id = {row["row_id"]: row for row in left_rows}
    right_by_id = {row["row_id"]: row for row in right_rows}
    common_ids = sorted(set(left_by_id) & set(right_by_id))
    common_left = [left_by_id[row_id] for row_id in common_ids]
    common_right = [right_by_id[row_id] for row_id in common_ids]
    left_name = args.left_name or Path(args.left).stem
    right_name = args.right_name or Path(args.right).stem

    left_metrics = compute_metrics(common_left)
    right_metrics = compute_metrics(common_right)

    prop_stats = defaultdict(lambda: {
        "rows": 0,
        "gold_keep": 0,
        "gold_prune": 0,
        "left_fp": 0,
        "left_fn": 0,
        "right_fp": 0,
        "right_fn": 0,
        "disagree": 0,
    })
    left_better = []
    right_better = []
    for row_id in common_ids:
        left = left_by_id[row_id]
        right = right_by_id[row_id]
        prop = left.get("reaching_property_label") or left.get("reaching_property") or "unknown"
        stats = prop_stats[prop]
        stats["rows"] += 1
        if left["target"] == 1:
            stats["gold_keep"] += 1
        else:
            stats["gold_prune"] += 1

        left_error = error_kind(left)
        right_error = error_kind(right)
        if left_error == "fp":
            stats["left_fp"] += 1
        elif left_error == "fn":
            stats["left_fn"] += 1
        if right_error == "fp":
            stats["right_fp"] += 1
        elif right_error == "fn":
            stats["right_fn"] += 1
        if left["prediction"] != right["prediction"]:
            stats["disagree"] += 1
            if left_error == "correct" and right_error != "correct":
                left_better.append((left, right))
            elif right_error == "correct" and left_error != "correct":
                right_better.append((left, right))

    def metric_line(name, metrics):
        return (
            f"| {name} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} | {metrics['tn']} | "
            f"{metrics['precision']:.3f} | {metrics['recall']:.3f} | "
            f"{metrics['f1']:.3f} | {metrics['accuracy']:.3f} |"
        )

    lines = [
        "# Prompt Result Comparison",
        "",
        f"- Left: `{left_name}`",
        f"- Right: `{right_name}`",
        f"- Common rows: {len(common_ids)}",
        f"- Left-only rows: {len(set(left_by_id) - set(right_by_id))}",
        f"- Right-only rows: {len(set(right_by_id) - set(left_by_id))}",
        "",
        "## Metrics On Common Rows",
        "",
        "| Prompt | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        metric_line(left_name, left_metrics),
        metric_line(right_name, right_metrics),
        "",
        "## Error Profile By Reaching Property",
        "",
        "| Property | Rows | Gold INCLUDE | Gold EXCLUDE | "
        f"{left_name} FP/FN | {right_name} FP/FN | Disagreements |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for prop, stats in sorted(
        prop_stats.items(),
        key=lambda item: (
            -(item[1]["left_fp"] + item[1]["left_fn"] + item[1]["right_fp"] + item[1]["right_fn"]),
            item[0],
        ),
    ):
        lines.append(
            f"| {prop} | {stats['rows']} | {stats['gold_keep']} | {stats['gold_prune']} | "
            f"{stats['left_fp']}/{stats['left_fn']} | "
            f"{stats['right_fp']}/{stats['right_fn']} | {stats['disagree']} |"
        )

    def append_examples(title, examples):
        lines.extend(["", f"## {title}", ""])
        if not examples:
            lines.append("None.")
            return
        lines.append("| Row | Target | Property | Depth | Seed | Candidate | Left | Right |")
        lines.append("|---|---:|---|---:|---|---|---|---|")
        for left, right in examples[:args.max_examples]:
            lines.append(
                f"| {left['row_id']} | {left['target']} | "
                f"{left.get('reaching_property_label', '')} | {left.get('depth', '')} | "
                f"{left.get('seed_label', '')} | {left.get('label', '')} | "
                f"{left.get('classification', left.get('prediction'))} | "
                f"{right.get('classification', right.get('prediction'))} |"
            )

    append_examples(f"Rows Where {left_name} Is Correct And {right_name} Is Wrong", left_better)
    append_examples(f"Rows Where {right_name} Is Correct And {left_name} Is Wrong", right_better)

    output = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Comparison saved: {args.output}")
    else:
        print(output)


def write_result_csv(path, rows):
    fieldnames = [
        "row_id", "legacy_row_id", "seed_qid", "seed_label", "qid", "label", "target",
        "prediction", "classification", "confidence", "depth",
        "reaching_property", "reaching_property_label",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def sample_from_file(candidates, sample_file):
    by_row_id = {row["row_id"]: row for row in candidates}
    sample = []
    missing = []
    with Path(sample_file).open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            row_id = row.get("row_id", "").strip()
            if row_id in by_row_id:
                sample.append(by_row_id[row_id])
            elif row_id:
                missing.append(row_id)
    if missing:
        raise RuntimeError(
            f"{len(missing)} sample row_id values are not available after filtering: "
            + ", ".join(missing[:10])
        )
    if not sample:
        raise RuntimeError(f"No rows loaded from sample file: {sample_file}")
    return sample


def write_evaluation_sample(sample, dataset, context_level, examples_file,
                            sample_size, sample_seed, output_dir, output_path=None):
    if output_path:
        path = Path(output_path)
    else:
        examples_stem = Path(examples_file).stem if examples_file else "no_examples"
        path = Path(output_dir) / (
            f"dataset{dataset}_{context_level}_{examples_stem}_"
            f"sample{sample_size}_seed{sample_seed}.csv"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "row_id",
        "legacy_row_id",
        "seed_qid",
        "seed_label",
        "qid",
        "label",
        "target",
        "decision",
        "depth",
        "csv_depth",
        "bfs_depth",
        "reaching_property",
        "reaching_property_label",
        "parent_qid",
        "parent_label",
        "path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sample:
            writer.writerow({
                "row_id": row["row_id"],
                "legacy_row_id": row["legacy_row_id"],
                "seed_qid": row["seed_qid"],
                "seed_label": row["seed_label"],
                "qid": row["qid"],
                "label": row["label"],
                "target": row["target"],
                "decision": "INCLUDE" if row["target"] == 1 else "EXCLUDE",
                "depth": row.get("bfs_depth") or row.get("csv_depth"),
                "csv_depth": row.get("csv_depth"),
                "bfs_depth": row.get("bfs_depth"),
                "reaching_property": row.get("reaching_property", ""),
                "reaching_property_label": row.get("reaching_property_label", ""),
                "parent_qid": row.get("parent_qid", ""),
                "parent_label": row.get("parent_label", ""),
                "path": row.get("path", ""),
            })
    return path


def default_result_json_paths():
    if not DEFAULT_RESULTS_DIR.exists():
        return []
    paths = []
    for model_dir in sorted(DEFAULT_RESULTS_DIR.iterdir()):
        if model_dir.is_dir():
            paths.extend(sorted(model_dir.glob("*.json")))
            paths.extend(sorted(model_dir.glob("dataset*/*/summary.json")))
    paths.extend(sorted(DEFAULT_RESULTS_DIR.glob("*.json")))
    return sorted(dict.fromkeys(paths))


def display_result_path(result_path):
    path = Path(result_path)
    try:
        path = path.relative_to(DEFAULT_RESULTS_DIR)
    except ValueError:
        pass
    return str(path.with_suffix("")).replace("\\", "/")


def select_summary_metric_views(payload, requested_view):
    requested = requested_view or "bfs_gated"
    metric_aggregations = payload.get("metric_aggregations") or {}
    runtime_aggregation = (
        metric_aggregations.get("kgprune_runtime_rows")
        or metric_aggregations.get("bfs_gated_kgprune_runtime_rows")
    )
    if requested in {
        "kgprune_runtime_weighted_fold",
        "kgprune_runtime_pooled",
        "both_kgprune_aggregations",
    }:
        if not runtime_aggregation:
            raise ValueError(
                "Result does not contain explicit KGPrune runtime-row metric "
                "aggregations; rescore it with the current evaluator."
            )
        views = []
        if requested in {
            "kgprune_runtime_weighted_fold",
            "both_kgprune_aggregations",
        }:
            weighted = runtime_aggregation.get("weighted_fold")
            if weighted is None:
                raise ValueError(
                    "The runtime-row result does not have complete five-fold "
                    "coverage for a weighted-fold summary."
                )
            views.append(("kgprune_runtime_weighted_fold", weighted))
        if requested in {
            "kgprune_runtime_pooled",
            "both_kgprune_aggregations",
        }:
            views.append(("kgprune_runtime_pooled", runtime_aggregation["pooled"]))
        return views

    if requested in {"bfs_gated", "both_bfs_gated"}:
        metrics_block = payload.get("metrics") or {}
        views = []
        if isinstance(metrics_block, dict) and "bfs_gated" in metrics_block:
            views.append(("full_dataset_bfs_gated", metrics_block["bfs_gated"]))
            kgprune_metrics = metrics_block.get("bfs_gated_kgprune_runtime_rows")
            if kgprune_metrics:
                views.append(("kgprune_runtime_bfs_gated", kgprune_metrics))
            return views

        if payload.get("evaluation_mode") == "bfs":
            metric_views = payload.get("metric_views") or {}
            if "all_evaluated_rows" in metric_views:
                views.append(("all_evaluated_rows_bfs_gated", metric_views["all_evaluated_rows"]))
            if "kgprune_runtime_rows" in metric_views:
                views.append(("kgprune_runtime_bfs_gated", metric_views["kgprune_runtime_rows"]))
        return views

    if requested == "metrics":
        metrics = payload["metrics"]
        if "precision" not in metrics:
            raise ValueError(
                "Top-level 'metrics' does not contain a direct metric row; "
                "use --metric-view bfs_gated for batch summaries."
            )
        return [("metrics", metrics)]

    metrics_block = payload.get("metrics") or {}
    if requested in metrics_block:
        return [(requested, metrics_block[requested])]

    metric_views = payload.get("metric_views") or {}
    if requested in metric_views:
        suffix = "_bfs_gated" if payload.get("evaluation_mode") == "bfs" else ""
        return [(f"{requested}{suffix}", metric_views[requested])]

    available = set(metric_views)
    if isinstance(metrics_block, dict):
        available.update(metrics_block)
    available.update({
        "bfs_gated",
        "both_bfs_gated",
        "kgprune_runtime_weighted_fold",
        "kgprune_runtime_pooled",
        "both_kgprune_aggregations",
    })
    raise ValueError(
        f"Result does not contain metric view {requested!r}. "
        f"Available views: {', '.join(sorted(available)) or 'none'}"
    )


def summarize_results(args):
    rows = []
    result_paths = [Path(path) for path in args.result_json] if args.result_json else default_result_json_paths()
    if not result_paths:
        raise ValueError("No result JSON files found. Pass --result-json or add files under results/<model>/")
    for result_path in result_paths:
        with Path(result_path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        manifest = {}
        if Path(result_path).name == "summary.json":
            manifest = load_json_if_exists(Path(result_path).parent / "manifest.json")
        if "prompt_file" not in payload and "metrics" not in payload and "metric_views" not in payload:
            continue
        selected_views = select_summary_metric_views(payload, args.metric_view)
        if not selected_views:
            continue
        prompt_file = payload.get("prompt_file") or manifest.get("prompt_file") or ""
        prompt_name = Path(prompt_file).stem if prompt_file else ""
        prompt_kind = payload.get("prompt_setting") or manifest.get("strategy")
        if not prompt_kind:
            prompt_kind = "generated" if prompt_name.startswith("generated_") else "manual"
        prompt_metadata = payload.get("prompt_metadata") or {}
        if not prompt_metadata.get("meta_prompt_sha256"):
            if prompt_file:
                sidecar_path = resolve_prompt_file_for_model(
                    prompt_file,
                    payload.get("model") or manifest.get("model") or payload.get("provider", "openai") or DEFAULT_OPENAI_MODEL,
                    payload.get("provider") or manifest.get("provider", "openai"),
                )
                sidecar_metadata = load_json_if_exists(sidecar_path.with_suffix(".json"))
                if sidecar_metadata:
                    prompt_metadata = sidecar_metadata
        meta_prompt_sha = prompt_metadata.get("meta_prompt_sha256", "")
        prompt_sha = payload.get("prompt_sha256") or manifest.get("prompt_sha256", "")
        model = payload.get("model") or manifest.get("model") or prompt_metadata.get("model") or payload.get("provider", "openai")
        reasoning_effort = (
            payload.get("reasoning_effort")
            or prompt_metadata.get("reasoning_effort")
            or prompt_metadata.get("thinking_level")
            or ""
        )
        examples_file = payload.get("examples_file") or manifest.get("examples_file")
        for metric_view, metrics in selected_views:
            rows.append({
                "dataset": payload.get("dataset") or manifest.get("dataset"),
                "model": model,
                "reasoning_effort": reasoning_effort,
                "prompt": prompt_kind,
                "prompt_file": prompt_name,
                "examples_file": Path(examples_file).stem if examples_file else "",
                "examples_in_prompt": payload.get("examples_in_prompt", manifest.get("examples_in_prompt", "")),
                "result_file": display_result_path(result_path),
                "context": payload.get("context_level") or manifest.get("context_level"),
                "example_count": (
                    payload.get("prompt_examples_count")
                    if payload.get("prompt_examples_count") is not None
                    else manifest.get("prompt_examples_count", payload.get("excluded_example_rows", ""))
                ),
                "metric_view": metric_view,
                "metric_rows": metrics.get("total", metrics.get("total_weight", "")),
                "meta_prompt_sha": meta_prompt_sha[:12] if meta_prompt_sha else "",
                "prompt_sha": prompt_sha[:12] if prompt_sha else "",
                "precision": metrics["precision"],
                "recall": metrics["recall"],
                "f1": metrics["f1"],
                "accuracy": metrics["accuracy"],
                "path": str(result_path),
            })
    if not rows:
        raise ValueError("No evaluation result JSON files found in the selected paths")

    setting_order = {
        "zero_shot_manual": 0,
        "few_shot_manual": 1,
        "zero_shot_meta": 2,
        "few_shot_meta": 3,
        "manual": 4,
        "generated": 5,
    }
    rows.sort(key=lambda row: (
        str(row["dataset"]),
        row["model"],
        row["reasoning_effort"],
        row["context"],
        setting_order.get(row["prompt"], 99),
        row["prompt_file"],
    ))
    lines = [
        "| Dataset | Model | Reasoning | Setting | Prompt File | Examples | Examples In Prompt | Result File | Context | Metric View | Rows | Example Count | Meta SHA | Prompt SHA | Precision | Recall | F1 | Accuracy |",
        "|---|---|---|---|---|---|---:|---|---|---|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        examples = f"`{row['examples_file']}`" if row["examples_file"] else ""
        lines.append(
            f"| {row['dataset']} | {row['model']} | {row['reasoning_effort']} | {row['prompt']} | "
            f"`{row['prompt_file']}` | {examples} | "
            f"{row['examples_in_prompt']} | "
            f"`{row['result_file']}` | "
            f"{row['context']} | "
            f"{row['metric_view']} | "
            f"{row['metric_rows']} | {row['example_count']} | "
            f"{row['meta_prompt_sha']} | {row['prompt_sha']} | "
            f"{row['precision']:.3f} | {row['recall']:.3f} | "
            f"{row['f1']:.3f} | {row['accuracy']:.3f} |"
        )
    text = "\n".join(lines)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"Summary saved: {output}")
    print(text)


def fingerprint_files(args):
    for file_path in args.file:
        sha = file_sha256(file_path)
        if sha:
            print(f"{sha}  {file_path}")
        else:
            print(f"MISSING  {file_path}")


def annotate_prompt_metadata(args):
    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        raise FileNotFoundError(prompt_path)
    metadata_path = prompt_path.with_suffix(".json")
    metadata = load_json_if_exists(metadata_path)
    metadata.update({
        "prompt_file": str(prompt_path),
        "generated_prompt_sha256": file_sha256(prompt_path),
        "annotated_at": utc_now_iso(),
    })
    if args.examples_file:
        metadata["examples_file"] = str(args.examples_file)
        metadata["examples_sha256"] = file_sha256(args.examples_file)
    if args.meta_prompt:
        metadata["meta_prompt"] = str(args.meta_prompt)
        metadata["meta_prompt_sha256"] = file_sha256(args.meta_prompt)
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Annotated metadata saved: {metadata_path}")


def add_common_dataset_args(parser):
    parser.add_argument("--dataset", type=parse_dataset_arg, required=True)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--context-level", choices=CONTEXT_LEVELS, default="property")


def main():
    parser = argparse.ArgumentParser(
        description="Run Wikidata sub-KG extraction and prompt experiments"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-examples")
    add_common_dataset_args(build)
    build.add_argument("--seed-label")
    build.add_argument("--row-id", action="append", default=[],
                       help="Explicit example row ID, preferably rowN. Repeat as needed.")
    build.add_argument("--per-class", type=int, default=4)
    build.add_argument("--num-keep", type=int,
                       help="Number of INCLUDE examples to sample. Overrides --per-class for INCLUDE.")
    build.add_argument("--num-prune", type=int,
                       help="Number of EXCLUDE examples to sample. Overrides --per-class for EXCLUDE.")
    build.add_argument("--sample-seed", type=int, default=42)
    build.add_argument("--diversity-key", choices=["seed", "property", "property_seed"],
                       default="seed",
                       help="Round-robin bucket used when sampling examples without --row-id.")
    build.add_argument("--selection-strategy", choices=["diverse", "contrastive_property"],
                       default="diverse",
                       help="Example sampling strategy when --row-id is not supplied.")
    build.add_argument("--output", required=True)

    gen = sub.add_parser("generate")
    add_common_dataset_args(gen)
    gen.add_argument(
        "--examples-file",
        help=("Meta-prompting examples file. Defaults to the dataset pack under "
              "meta-prompting/examples/meta/."),
    )
    gen.add_argument("--meta-prompt", type=Path, default=DEFAULT_PROMPTS_DIR / "meta_prompt.md")
    gen.add_argument("--provider", choices=["auto", "openai", "gemini", "openrouter", "deepseek"], default="auto",
                     help=("LLM provider. 'auto' uses Gemini for gemini-* models, "
                           "DeepSeek for deepseek-* models, OpenRouter for google/* "
                           "models, and OpenAI otherwise."))
    gen.add_argument("--model",
                     help=f"Generation model. Defaults to {DEFAULT_OPENAI_MODEL} for OpenAI "
                          f"{DEFAULT_GEMINI_MODEL} for Gemini, {DEFAULT_OPENROUTER_MODEL} "
                          f"for OpenRouter, or {DEFAULT_DEEPSEEK_MODEL} for DeepSeek. "
                          "Prompt generation uses the model's configured reasoning setting.")
    gen.add_argument(
        "--thinking-level",
        choices=["default", "high"],
        help=(
            "Gemini-only prompt-generation override. Use 'default' to omit "
            "thinking_config, or 'high' to request high thinking."
        ),
    )
    gen.add_argument(
        "--output",
        type=Path,
        help="Generated prompt path. Defaults to prompts/<model>/generated_<examples>_evidence_<model>.md.",
    )

    eval_p = sub.add_parser("evaluate")
    add_common_dataset_args(eval_p)
    eval_p.add_argument("--eval-mode", choices=["bfs", "row"], default="bfs",
                        help="Use real LLM-gated BFS or legacy independent row-level evaluation.")
    eval_p.add_argument("--prompt-file", required=True)
    eval_p.add_argument(
        "--examples-file",
        help=("Examples file. Defaults to the fixed manual pack for manual settings "
              "and the meta-only pack for meta settings."),
    )
    eval_p.add_argument("--prompt-setting",
                        choices=[
                            "zero_shot_manual",
                            "few_shot_manual",
                            "zero_shot_meta",
                            "few_shot_meta",
                        ],
                        help="Named experiment group to record in result metadata.")
    eval_p.add_argument("--include-examples-in-prompt", action="store_true",
                        help="Append labeled examples from --examples-file to the system prompt.")
    eval_p.add_argument("--seed-label")
    eval_p.add_argument("--sample-size", type=int, default=30,
                        help="Number of balanced rows to evaluate; use 0 for all rows after exclusions.")
    eval_p.add_argument("--sample-seed", type=int, default=42)
    eval_p.add_argument("--sample-file", type=Path,
                        help="Existing sample CSV whose row_id order should be reused exactly.")
    eval_p.add_argument("--batch-size", type=int, default=10)
    eval_p.add_argument("--max-iterations", type=int, default=10,
                        help="Maximum prune-gated BFS iterations in --eval-mode bfs.")
    eval_p.add_argument("--workers", type=int, default=1,
                        help="Number of parallel LLM evaluation batch calls.")
    eval_p.add_argument("--provider", choices=["auto", "openai", "gemini", "openrouter", "deepseek"], default="auto",
                        help=("LLM provider. 'auto' uses Gemini for gemini-* models, "
                              "DeepSeek for deepseek-* models, OpenRouter for google/* "
                              "models, and OpenAI otherwise."))
    eval_p.add_argument("--model",
                        help=f"Evaluation model. Defaults to {DEFAULT_OPENAI_MODEL} for OpenAI "
                             f"{DEFAULT_GEMINI_MODEL} for Gemini, {DEFAULT_OPENROUTER_MODEL} "
                             f"for OpenRouter, or {DEFAULT_DEEPSEEK_MODEL} for DeepSeek. "
                             "Provider-default reasoning is always used during extraction.")
    eval_p.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    eval_p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing canonical full-dataset result.",
    )
    eval_p.add_argument("--sample-output-dir", type=Path, default=DEFAULT_SAMPLES_DIR,
                        help="Directory for the deterministic sampled rows evaluated.")
    eval_p.add_argument("--report-baseline-subset", action="store_true",
                        help="Also report metrics on rows present in KGPrune/PBG runtime inputs.")
    eval_p.add_argument("--runtime-input-dir", type=Path,
                        default=DEFAULT_BASELINE_RUNTIME_INPUT_DIR,
                        help="Directory containing KGPrune/PBG-filtered runtime input CSVs.")

    summary = sub.add_parser("summarize")
    summary.add_argument(
        "--result-json",
        action="append",
        help=("Result JSON file to include. Repeat as needed. "
              "Defaults to all JSON files under results/<model>/."),
    )
    summary.add_argument("--metric-view", default="bfs_gated",
                         help=("Metric view to summarize. Default 'bfs_gated' emits only BFS-gated "
                               "all-row and KGPrune runtime-row views when available. Use a specific "
                               "view name only for targeted debugging. Current rescored outputs also "
                               "support 'kgprune_runtime_weighted_fold', "
                               "'kgprune_runtime_pooled', and 'both_kgprune_aggregations'."))
    summary.add_argument("--output")

    compare = sub.add_parser("compare")
    compare.add_argument("--left", type=Path, required=True,
                         help="First result CSV to compare.")
    compare.add_argument("--right", type=Path, required=True,
                         help="Second result CSV to compare.")
    compare.add_argument("--left-name",
                         help="Display name for the first result CSV.")
    compare.add_argument("--right-name",
                         help="Display name for the second result CSV.")
    compare.add_argument("--max-examples", type=int, default=20,
                         help="Maximum row-level examples per disagreement section.")
    compare.add_argument("--output", type=Path)

    fingerprint = sub.add_parser("fingerprint")
    fingerprint.add_argument("file", nargs="+", help="File path to hash with SHA-256.")

    annotate = sub.add_parser("annotate-prompt-metadata")
    annotate.add_argument("--prompt-file", required=True)
    annotate.add_argument("--examples-file")
    annotate.add_argument("--meta-prompt")

    aggregate = sub.add_parser(
        "aggregate-retained",
        help=(
            "Validate and rescore all retained Wikidata baseline and Sub-KG "
            "Extractor predictions without making model API calls."
        ),
    )
    aggregate.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Root directory containing the retained Sub-KG prediction CSVs.",
    )
    aggregate.add_argument(
        "--baseline-decisions-dir",
        type=Path,
        default=DEFAULT_BASELINE_DECISIONS_DIR,
        help="Directory containing the retained row-complete baseline CSVs.",
    )
    aggregate.add_argument(
        "--output-csv",
        type=Path,
        default=DEFAULT_ALIGNED_METRICS_CSV,
    )
    aggregate.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_ALIGNED_METRICS_JSON,
    )

    args = parser.parse_args()
    if args.command == "build-examples":
        build_examples_file(args)
    elif args.command == "generate":
        generate_prompt(args)
    elif args.command == "evaluate":
        evaluate_prompt(args)
    elif args.command == "summarize":
        summarize_results(args)
    elif args.command == "compare":
        compare_result_csvs(args)
    elif args.command == "fingerprint":
        fingerprint_files(args)
    elif args.command == "annotate-prompt-metadata":
        annotate_prompt_metadata(args)
    elif args.command == "aggregate-retained":
        aggregate_retained_results(args)


if __name__ == "__main__":
    main()
