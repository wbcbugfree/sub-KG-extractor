"""Run USDA NALT extraction, prompt generation, and prompt validation.

The traversal starts from one or more English seed concept labels and expands
over skos:broader, skos:narrower, and skos:related. The configured branch root
is used only to define the gold-standard positive hierarchy; it is ignored
during BFS. The same CLI also manages the labeled demonstration sets and
system prompts used by the NALT experiments.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Sequence, Set
from urllib.request import urlopen

import rdflib
from openai import OpenAI
from pydantic import BaseModel, Field
from rdflib import URIRef
from rdflib.namespace import RDF, SKOS


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "results"
DEFAULT_CONFIG_PATH = REPO_ROOT / "config"
DEFAULT_PROMPT_DIR = SCRIPT_DIR / "prompts"
DEFAULT_META_EXAMPLES_DIR = DEFAULT_PROMPT_DIR / "examples"
DEFAULT_META_PROMPT = DEFAULT_PROMPT_DIR / "meta_prompt_label_only.md"
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_OPENAI_MODEL = DEFAULT_MODEL
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_BATCH_SIZE = 20
DEFAULT_DEEPSEEK_PROTOCOL_RETRIES = 3
PROMPT_GENERATION_ATTEMPTS = 3
DEEPSEEK_BETA_BASE_URL = "https://api.deepseek.com/beta"
DEEPSEEK_EXTRACTION_TOOL_NAME = "submit_nalt_candidate_evaluations"
RESULT_MODES = (
    "zero_shot_generic",
    "zero_shot_manual",
    "zero_shot_meta",
)
RESULT_ARTIFACT_SUFFIXES = (
    "_INCLUDED.csv",
    "_PRUNED.csv",
    "_VISITED.csv",
    "_METRICS.json",
)
REQUIRED_PROMPT_HEADINGS = ("Role", "INCLUDE", "EXCLUDE", "Decision", "Confidence")
SHARED_PROMPT_DIR_NAMES = {"examples", "manual"}

NALT_SOIL_URI = "https://lod.nal.usda.gov/nalt/63334"
NALT_SOIL_SCIENCE_URI = "https://lod.nal.usda.gov/nalt/956"
NALT_BENCHMARK_PRESETS = {
    "soil_science": {
        "gold_root_label": "Soil science",
        "gold_root_uri": NALT_SOIL_SCIENCE_URI,
        "seed_labels": ["soil"],
        "output_subdir": "soil",
    },
    "pest_management": {
        "gold_root_label": "Pest management",
        "gold_root_uri": "https://lod.nal.usda.gov/nalt/34011",
        "seed_labels": ["pest control", "pesticides"],
        "output_subdir": "pest",
    },
    "plant_health": {
        "gold_root_label": "Plant health",
        "gold_root_uri": "https://lod.nal.usda.gov/nalt/941",
        "seed_labels": ["plant diseases and disorders", "plant protection"],
        "output_subdir": "plant",
    },
    "immunology": {
        "gold_root_label": "Immunology",
        "gold_root_uri": "https://lod.nal.usda.gov/nalt/17475",
        "seed_labels": ["immunity"],
        "output_subdir": "immunology",
    },
    "cell_biology": {
        "gold_root_label": "Cell biology",
        "gold_root_uri": "https://lod.nal.usda.gov/nalt/17472",
        "seed_labels": ["cells"],
        "output_subdir": "cell",
    },
}
NALT_TTL_FILENAME = "nalt-full_dwn_20240716.ttl"
NALT_TTL_DOWNLOAD_URL = (
    "https://lod.nal.usda.gov/nalt/en/rest/v1/nalt/data?format=text%2Fturtle"
)
DEFAULT_NALT_DOWNLOAD_PATH = SCRIPT_DIR / NALT_TTL_FILENAME

DEFAULT_NALT_TTL_CANDIDATES = [
    DEFAULT_NALT_DOWNLOAD_PATH,
    SCRIPT_DIR / "source" / NALT_TTL_FILENAME,
    SCRIPT_DIR / "target" / NALT_TTL_FILENAME,
    SCRIPT_DIR / "target" / "nalt-full.ttl",
    Path(tempfile.gettempdir()) / "subkg-vocab-connectivity" / "nalt" / NALT_TTL_FILENAME,
]

TRAVERSAL_RELATIONS = (SKOS.broader, SKOS.narrower, SKOS.related)
HIERARCHY_RELATIONS = (SKOS.broader, SKOS.narrower)


SYSTEM_PROMPT_NALT = """# Sub-Knowledge Graph Node Relevance Classifier

## Task

You are a knowledge graph curator extracting a coherent, topic-focused subgraph.
Evaluate every candidate concept against the provided Seed Topic and decide whether
the concept belongs in that subgraph.

## Input Format

- **Seed Topic**: the central concept defining the intended scope
- **Candidate Concepts**: a numbered list formatted as prefLabel
  (altLabel1, altLabel2, ...)

Use only the English labels provided. Consider both the preferred label and all
alternative labels when interpreting a candidate.

## INCLUDE Criteria

Classify a candidate as INCLUDE when its primary meaning represents one or more of:

1. A core type, component, property, or characteristic of the Seed Topic
2. An intrinsic process that defines, transforms, or directly affects it
3. A direct measurement, observation, or assessment of it
4. A specific subtype of the Seed Topic itself
5. A material, method, or activity whose primary purpose is specific to the Seed Topic

## EXCLUDE Criteria

Classify a candidate as EXCLUDE when its primary meaning is:

1. Semantically irrelevant or connected only through a weak association
2. Overly broad and likely to introduce many unrelated concepts
3. Primarily part of an adjacent but distinct domain
4. Generic across many domains without a specific connection to the Seed Topic
5. Relevant only because it occurs in, inhabits, affects, benefits, harms, or is
   studied within the Seed Topic
6. A named organism, taxonomic group, biological lineage, or functional organism
   group whose primary identity is biological rather than a concept of the Seed Topic

## Decision Framework

Judge primary subject, not contextual association. For a domain-focused subgraph,
include concepts that are about the Seed Topic itself; exclude entities that merely
interact with it. Biological agents and taxa should be excluded unless the Seed Topic
explicitly asks for a biological taxonomy. Because every INCLUDE decision enables further
graph expansion, conservatively EXCLUDE ambiguous gateway-like concepts that could open
a broad adjacent domain.

A lexical match to the Seed Topic is useful evidence but is not sufficient by itself.
Conversely, a concept may be included without a lexical match when its primary meaning
is clearly integral to the topic.

## Output Requirements

Return exactly one decision for every numbered candidate. Use the candidate's
1-based number as `idx`; do not replace it with a label. Each decision must contain:

- `idx`: candidate number
- `classification`: INCLUDE or EXCLUDE
- `confidence`: number from 0.0 to 1.0 indicating confidence in that classification

## Confidence Score Guidelines

- **0.9-1.0**: clear and unambiguous fit or misfit
- **0.7-0.89**: strong evidence with limited uncertainty
- **0.5-0.69**: borderline; reasonable arguments exist for both decisions
- **0.3-0.49**: weak preference with substantial uncertainty
- **0.0-0.29**: highly uncertain because the labels provide insufficient context

Confidence measures certainty in the selected classification, whether INCLUDE or
EXCLUDE; it is not a relevance score.
"""


class NaltNodeDecision(BaseModel):
    idx: int = Field(description="1-based candidate number")
    classification: Literal["INCLUDE", "EXCLUDE"]
    confidence: float = Field(ge=0.0, le=1.0)


class NaltBatchResponse(BaseModel):
    evaluations: List[NaltNodeDecision]


DEEPSEEK_EXTRACTION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "minimum": 1},
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


def load_api_key(provider="openai", config_path=DEFAULT_CONFIG_PATH):
    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        config_key = "gemini_api_key"
        env_hint = "GEMINI_API_KEY"
    elif provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        config_key = "deepseek_api_key"
        env_hint = "DEEPSEEK_API_KEY"
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        config_key = "openai_api_key"
        env_hint = "OPENAI_API_KEY"
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    if api_key:
        return api_key

    config_path = Path(config_path)
    candidates = [config_path, SCRIPT_DIR / "config", Path.cwd() / "config"]
    for candidate in candidates:
        if candidate.exists():
            with candidate.open("r", encoding="utf-8") as handle:
                api_key = json.load(handle).get(config_key)
            if api_key:
                return api_key
    raise ValueError(f"Set {env_hint} or provide a config file with {config_key}")


def get_openai_client(config_path=DEFAULT_CONFIG_PATH):
    return OpenAI(api_key=load_api_key("openai", config_path=config_path))


def get_gemini_client(config_path=DEFAULT_CONFIG_PATH):
    from google import genai

    return genai.Client(api_key=load_api_key("gemini", config_path=config_path))


def get_deepseek_client(config_path=DEFAULT_CONFIG_PATH):
    return OpenAI(
        api_key=load_api_key("deepseek", config_path=config_path),
        base_url=DEEPSEEK_BETA_BASE_URL,
        timeout=180.0,
        max_retries=2,
    )


def resolve_provider(provider, model):
    if provider != "auto":
        if provider not in {"openai", "gemini", "deepseek"}:
            raise ValueError(f"Unsupported LLM provider: {provider}")
        return provider
    if model and model.startswith("deepseek-"):
        return "deepseek"
    if model and model.startswith("gemini-"):
        return "gemini"
    return "openai"


def default_model_for_provider(provider):
    if provider == "gemini":
        return DEFAULT_GEMINI_MODEL
    if provider == "deepseek":
        return DEFAULT_DEEPSEEK_MODEL
    if provider == "openai":
        return DEFAULT_OPENAI_MODEL
    raise ValueError(f"Unsupported LLM provider: {provider}")


def normalize_seed_labels(seed_label=None, seed_labels=None):
    if seed_labels is None:
        if seed_label is None:
            labels = ["soil"]
        elif isinstance(seed_label, str):
            labels = [seed_label]
        else:
            labels = list(seed_label)
    elif isinstance(seed_labels, str):
        labels = [seed_labels]
    else:
        labels = list(seed_labels)

    labels = [label.strip() for label in labels if label and label.strip()]
    if not labels:
        raise ValueError("At least one seed label is required")
    return labels


def format_seed_topic(seed_labels):
    return "; ".join(normalize_seed_labels(seed_labels=seed_labels))


def slugify_label(value):
    chars = []
    previous_underscore = False
    for char in value.casefold():
        if char.isalnum():
            chars.append(char)
            previous_underscore = False
        elif not previous_underscore:
            chars.append("_")
            previous_underscore = True
    return "".join(chars).strip("_") or "custom"


def benchmark_output_subdir(benchmark_name):
    preset = NALT_BENCHMARK_PRESETS.get(benchmark_name or "")
    if not preset:
        return None
    return preset.get("output_subdir")


def model_folder_name(model):
    value = str(model or DEFAULT_MODEL).strip()
    chars = []
    previous_separator = False
    for char in value:
        if char.isalnum() or char in {".", "-"}:
            chars.append(char.casefold())
            previous_separator = False
        elif not previous_separator:
            chars.append("_")
            previous_separator = True
    return "".join(chars).strip("_") or DEFAULT_MODEL


def infer_result_mode(prompt_file):
    if not prompt_file:
        return "zero_shot_generic"
    prompt_parts = {part.casefold() for part in Path(prompt_file).parts}
    if "manual" in prompt_parts:
        return "zero_shot_manual"
    return "zero_shot_meta"


def resolve_results_output_dir(output_dir, run_config, llm_model):
    output_dir = Path(output_dir)
    benchmark_name = run_config.get("benchmark_name") or "custom"
    output_subdir = run_config.get("output_subdir")
    if output_subdir is None:
        output_subdir = benchmark_output_subdir(benchmark_name)
    if not output_subdir:
        output_subdir = slugify_label(benchmark_name)
    result_mode = (
        run_config.get("result_mode")
        or infer_result_mode(run_config.get("prompt_file"))
    )
    if result_mode not in RESULT_MODES:
        raise ValueError(f"Unsupported result mode: {result_mode}")
    return (
        output_dir
        / model_folder_name(llm_model)
        / output_subdir
        / result_mode
    )


def is_result_artifact(path):
    path = Path(path)
    return (
        path.is_file()
        and path.name.startswith("subkg_")
        and path.name.endswith(RESULT_ARTIFACT_SUFFIXES)
    )


def validate_results_output_dir(output_dir, overwrite=False):
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise NotADirectoryError(f"Result path is not a directory: {output_dir}")

    existing = list(output_dir.iterdir())
    if not existing:
        return
    unsupported = [path for path in existing if not is_result_artifact(path)]
    if unsupported:
        raise RuntimeError(
            "Refusing to overwrite unrecognized result artifacts: "
            + ", ".join(str(path) for path in unsupported)
        )
    if not overwrite:
        raise FileExistsError(
            f"Result directory already contains artifacts: {output_dir}. "
            "Pass --overwrite to replace the canonical strategy run."
        )


def initialize_results_output_dir(output_dir, overwrite=False):
    output_dir = Path(output_dir)
    validate_results_output_dir(output_dir, overwrite=overwrite)
    output_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for path in list(output_dir.iterdir()):
            path.unlink()


def resolve_benchmark_config(
    benchmark_name=None,
    seed_labels=None,
    gold_root_uri=None,
):
    if benchmark_name:
        if benchmark_name not in NALT_BENCHMARK_PRESETS:
            raise ValueError(f"Unknown NALT benchmark preset: {benchmark_name}")
        config = dict(NALT_BENCHMARK_PRESETS[benchmark_name])
        config["benchmark_name"] = benchmark_name
    else:
        config = {
            "benchmark_name": "custom",
            "gold_root_label": None,
            "gold_root_uri": NALT_SOIL_SCIENCE_URI,
            "seed_labels": ["soil"],
        }

    if seed_labels is not None:
        config["seed_labels"] = normalize_seed_labels(seed_labels=seed_labels)
    else:
        config["seed_labels"] = normalize_seed_labels(
            seed_labels=config["seed_labels"]
        )
    if gold_root_uri is not None:
        config["gold_root_uri"] = str(gold_root_uri)

    return config


def get_llm_client(provider, config_path=DEFAULT_CONFIG_PATH):
    if provider == "gemini":
        return get_gemini_client(config_path=config_path)
    if provider == "deepseek":
        return get_deepseek_client(config_path=config_path)
    if provider == "openai":
        return get_openai_client(config_path=config_path)
    raise ValueError(f"Unsupported LLM provider: {provider}")


def english_literals(graph, subject, predicate):
    return sorted({
        str(value)
        for value in graph.objects(subject, predicate)
        if getattr(value, "language", None) == "en"
    })


def relation_name(predicate):
    if predicate == SKOS.broader:
        return "skos:broader"
    if predicate == SKOS.narrower:
        return "skos:narrower"
    if predicate == SKOS.related:
        return "skos:related"
    return str(predicate)


def normalize_uri_set(values: Iterable):
    return {str(value) for value in values}


def resolve_system_prompt(prompt_file=None, default_prompt=SYSTEM_PROMPT_NALT):
    if not prompt_file:
        return default_prompt
    return Path(prompt_file).read_text(encoding="utf-8")


def resolve_example_candidate_uris(index, examples_file):
    if not examples_file:
        return set()
    payload = validate_example_pack(load_examples(examples_file))
    candidate_uris = set()
    for example in payload["examples"]:
        if example.get("candidate_uri"):
            candidate_uris.add(URIRef(str(example["candidate_uri"])))
        else:
            candidate_uris.add(
                index.find_uri_by_english_pref_label(example["candidate_label"])
            )
    return candidate_uris


def reachable_uris_from_seeds(index, seed_uris):
    seen = set()
    queue = deque(URIRef(str(uri)) for uri in seed_uris)

    while queue:
        current = queue.popleft()
        if current in seen or not index.is_traversable(current):
            continue
        seen.add(current)
        for neighbor in index.adjacency.get(current, {}):
            neighbor = URIRef(str(neighbor))
            if neighbor not in seen and index.is_traversable(neighbor):
                queue.append(neighbor)

    return seen


class NaltSkosIndex:
    """Local RDF index for NALT SKOS traversal using English labels only."""

    def __init__(self, graph, ignored_uris=None):
        self.graph = graph
        self.ignored_uris = {URIRef(str(uri)) for uri in (ignored_uris or set())}
        self.pref_labels = self._build_pref_labels()
        self.alt_labels = self._build_alt_labels()
        self.adjacency = self._build_adjacency()

    @classmethod
    def from_ttl(cls, ttl_path, ignored_uris=None):
        graph = rdflib.Graph()
        graph.parse(Path(ttl_path), format="turtle")
        return cls(graph, ignored_uris=ignored_uris)

    def _candidate_concepts(self):
        concepts = set(self.graph.subjects(RDF.type, SKOS.Concept))
        concepts |= {
            subject
            for subject, _, _ in self.graph.triples((None, SKOS.prefLabel, None))
            if isinstance(subject, URIRef)
        }
        return concepts

    def _build_pref_labels(self):
        labels = {}
        for concept in self._candidate_concepts():
            english = english_literals(self.graph, concept, SKOS.prefLabel)
            if english:
                labels[concept] = english[0]
        return labels

    def _build_alt_labels(self):
        return {
            concept: english_literals(self.graph, concept, SKOS.altLabel)
            for concept in self.pref_labels
        }

    def _build_adjacency(self):
        adjacency = defaultdict(lambda: defaultdict(set))
        visible = set(self.pref_labels)
        for predicate in TRAVERSAL_RELATIONS:
            label = relation_name(predicate)
            for subject, obj in self.graph.subject_objects(predicate):
                if not isinstance(subject, URIRef) or not isinstance(obj, URIRef):
                    continue
                if subject not in visible or obj not in visible:
                    continue
                adjacency[subject][obj].add(label)
                adjacency[obj][subject].add(label)
        return adjacency

    def is_traversable(self, uri):
        uri = URIRef(str(uri))
        return uri in self.pref_labels and uri not in self.ignored_uris

    def find_uri_by_english_pref_label(self, label):
        label_lower = label.casefold()
        matches = [
            uri for uri, pref_label in self.pref_labels.items()
            if pref_label.casefold() == label_lower and uri not in self.ignored_uris
        ]
        if not matches:
            raise ValueError(f"No NALT concept found with English prefLabel: {label}")
        return sorted(matches, key=str)[0]

    def node_info(self, uri, relations=None, confidence=None):
        uri = URIRef(str(uri))
        pref_label = self.pref_labels[uri]
        alt_labels = self.alt_labels.get(uri, [])
        node = {
            "uri": str(uri),
            "prefLabel": pref_label,
            "altLabels": alt_labels,
            "displayName": (
                f"{pref_label} ({', '.join(alt_labels)})"
                if alt_labels else pref_label
            ),
            "relations": sorted(relations or []),
        }
        if confidence is not None:
            node["confidence"] = confidence
        return node

    def one_hop_neighbors(self, uri):
        uri = URIRef(str(uri))
        neighbors = []
        for neighbor, relations in self.adjacency.get(uri, {}).items():
            if not self.is_traversable(neighbor):
                continue
            neighbors.append(self.node_info(neighbor, relations=relations))
        return sorted(neighbors, key=lambda row: (row["prefLabel"].casefold(), row["uri"]))

    def hierarchy_descendants(self, root_uri, exclude=None):
        root_uri = URIRef(str(root_uri))
        exclude = {URIRef(str(uri)) for uri in (exclude or set())}
        seen = {root_uri}
        queue = deque([root_uri])

        while queue:
            current = queue.popleft()
            children = set(self.graph.objects(current, SKOS.narrower))
            children |= set(self.graph.subjects(SKOS.broader, current))
            for child in children:
                if not isinstance(child, URIRef) or child not in self.pref_labels:
                    continue
                if child in seen:
                    continue
                seen.add(child)
                queue.append(child)

        return seen - exclude


def calculate_precision_recall_f1(
    included_uris,
    gold_uris,
    pruned_uris=None,
    exclude_uris=None,
):
    included = normalize_uri_set(included_uris)
    gold = normalize_uri_set(gold_uris)
    pruned = normalize_uri_set(pruned_uris or set())
    excluded = normalize_uri_set(exclude_uris or set())
    excluded_evaluation_count = len((included | pruned | gold) & excluded)
    included -= excluded
    pruned -= excluded
    gold -= excluded
    pruned -= included

    true_positive = len(included & gold)
    false_positive = len(included - gold)
    false_negative = len(gold - included)
    true_negative = len(pruned - gold)
    evaluation_total = (
        true_positive
        + false_positive
        + false_negative
        + true_negative
    )

    precision = true_positive / (true_positive + false_positive) if included else 0.0
    recall = true_positive / (true_positive + false_negative) if gold else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall else 0.0
    )
    accuracy = (
        (true_positive + true_negative) / evaluation_total
        if evaluation_total else 0.0
    )

    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
        "evaluation_total": evaluation_total,
        "excluded_evaluation_count": excluded_evaluation_count,
    }


def format_candidates_for_prompt(nodes):
    lines = []
    for index, node in enumerate(nodes, start=1):
        lines.append(f"{index}. {node['displayName']}")
    return "\n".join(lines)


def deepseek_extraction_tool():
    return {
        "type": "function",
        "function": {
            "name": DEEPSEEK_EXTRACTION_TOOL_NAME,
            "description": (
                "Return INCLUDE or EXCLUDE decisions with confidence scores "
                "for all numbered NALT candidate concepts."
            ),
            "parameters": DEEPSEEK_EXTRACTION_TOOL_SCHEMA,
            "strict": True,
        },
    }


def validate_batch_response(parsed, expected_count):
    indices = [decision.idx for decision in parsed.evaluations]
    expected_indices = list(range(1, expected_count + 1))
    if sorted(indices) != expected_indices:
        raise ValueError(
            "Structured output must contain exactly one decision for every "
            f"candidate index; received {indices}, expected {expected_indices}"
        )
    return parsed


def classify_batch_deepseek(
    client,
    model,
    system_prompt,
    user_prompt,
    expected_count,
):
    last_error = None
    for attempt in range(1, DEFAULT_DEEPSEEK_PROTOCOL_RETRIES + 1):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=[deepseek_extraction_tool()],
            stream=False,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            try:
                arguments = tool_calls[0].function.arguments
                parsed = NaltBatchResponse.model_validate_json(arguments)
                return validate_batch_response(parsed, expected_count)
            except Exception as exc:
                last_error = exc
        else:
            last_error = RuntimeError(
                "DeepSeek response omitted the required tool call"
            )
        if attempt < DEFAULT_DEEPSEEK_PROTOCOL_RETRIES:
            print(
                "DeepSeek structured output failed; "
                f"retry {attempt}/{DEFAULT_DEEPSEEK_PROTOCOL_RETRIES - 1} ..."
            )
    raise RuntimeError(
        "DeepSeek did not return valid structured candidate evaluations"
    ) from last_error


def classify_batch(
    client,
    model,
    seed_label,
    batch,
    provider="openai",
    system_prompt=SYSTEM_PROMPT_NALT,
):
    user_prompt = f"""Seed topic: {seed_label}

Classify exactly {len(batch)} NALT candidate concepts as INCLUDE or EXCLUDE.

Candidate concepts:
{format_candidates_for_prompt(batch)}
"""
    if provider == "gemini":
        from google.genai import types

        response = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=NaltBatchResponse,
            ),
        )
        parsed = NaltBatchResponse.model_validate_json(response.text)
        parsed = validate_batch_response(parsed, len(batch))
    elif provider == "deepseek":
        parsed = classify_batch_deepseek(
            client,
            model,
            system_prompt,
            user_prompt,
            len(batch),
        )
    elif provider == "openai":
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text_format=NaltBatchResponse,
        )
        parsed = response.output_parsed
        parsed = validate_batch_response(parsed, len(batch))
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    decisions = {decision.idx: decision for decision in parsed.evaluations}

    rows = []
    for index, node in enumerate(batch, start=1):
        decision = decisions[index]
        output = dict(node)
        output["classification"] = decision.classification.upper()
        output["confidence"] = decision.confidence
        rows.append(output)
    return rows


def classify_candidates(
    client,
    model,
    seed_label,
    nodes,
    batch_size=DEFAULT_BATCH_SIZE,
    max_workers=5,
    provider="openai",
    system_prompt=SYSTEM_PROMPT_NALT,
):
    batches = [
        nodes[start:start + batch_size]
        for start in range(0, len(nodes), batch_size)
    ]
    if not batches:
        return []

    results_by_batch = {}
    workers = min(max_workers, len(batches))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index = {
            executor.submit(
                classify_batch,
                client,
                model,
                seed_label,
                batch,
                provider,
                system_prompt,
            ): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(future_to_index):
            batch_index = future_to_index[future]
            results_by_batch[batch_index] = future.result()

    classified = []
    for batch_index in range(len(batches)):
        classified.extend(results_by_batch[batch_index])
    return classified


def iterative_nalt_extraction(
    index,
    seed_label="soil",
    seed_labels=None,
    gold_root_uri=NALT_SOIL_SCIENCE_URI,
    benchmark_name="custom",
    gold_root_label=None,
    output_subdir=None,
    max_iterations=20,
    batch_size=DEFAULT_BATCH_SIZE,
    llm_model=None,
    llm_provider="auto",
    llm_max_workers=5,
    config_path=DEFAULT_CONFIG_PATH,
    system_prompt=SYSTEM_PROMPT_NALT,
    prompt_file=None,
    result_mode=None,
    exclude_evaluation_uris=None,
    exclude_examples_files=None,
):
    resolved_provider = resolve_provider(llm_provider, llm_model)
    resolved_model = llm_model or default_model_for_provider(resolved_provider)
    resolved_result_mode = result_mode or infer_result_mode(prompt_file)
    if resolved_result_mode not in RESULT_MODES:
        raise ValueError(f"Unsupported result mode: {resolved_result_mode}")
    seed_labels = normalize_seed_labels(
        seed_label=seed_label,
        seed_labels=seed_labels,
    )
    seed_topic = format_seed_topic(seed_labels)
    seed_uris = [
        index.find_uri_by_english_pref_label(label)
        for label in seed_labels
    ]
    gold_root = URIRef(str(gold_root_uri))
    raw_gold_uris = index.hierarchy_descendants(gold_root, exclude={gold_root})
    structurally_reachable_uris = reachable_uris_from_seeds(index, seed_uris)
    gold_uris = raw_gold_uris & structurally_reachable_uris
    unreachable_gold_uris = raw_gold_uris - structurally_reachable_uris
    exclude_evaluation_uris = {
        URIRef(str(uri)) for uri in (exclude_evaluation_uris or set())
    }

    all_visited = {}
    all_included = {}
    all_pruned = {}
    iteration_details = []

    for seed_uri in seed_uris:
        seed_node = index.node_info(seed_uri, confidence=1.0)
        seed_node["classification"] = "INCLUDE"
        all_visited[str(seed_uri)] = seed_node
        all_included[str(seed_uri)] = seed_node

    first_frontier = {}
    for seed_uri in seed_uris:
        for neighbor in index.one_hop_neighbors(seed_uri):
            if neighbor["uri"] not in all_visited:
                first_frontier[neighbor["uri"]] = neighbor
    nodes_to_evaluate = sorted(
        first_frontier.values(),
        key=lambda row: (row["prefLabel"].casefold(), row["uri"]),
    )
    client = None
    if max_iterations > 0:
        client = get_llm_client(resolved_provider, config_path=config_path)

    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        new_nodes = [
            node for node in nodes_to_evaluate
            if node["uri"] not in all_visited
        ]
        if not new_nodes:
            break

        for node in new_nodes:
            all_visited[node["uri"]] = node

        classified_nodes = classify_candidates(
            client=client,
            model=resolved_model,
            seed_label=seed_topic,
            nodes=new_nodes,
            batch_size=batch_size,
            max_workers=llm_max_workers,
            provider=resolved_provider,
            system_prompt=system_prompt,
        )

        iteration_included = []
        iteration_pruned = []
        for node in classified_nodes:
            node_uri = node["uri"]
            if node["classification"] == "INCLUDE":
                all_included[node_uri] = node
                iteration_included.append(node)
            else:
                all_pruned[node_uri] = node
                iteration_pruned.append(node)

        iteration_details.append({
            "iteration": iteration,
            "evaluated": len(new_nodes),
            "included": len(iteration_included),
            "pruned": len(iteration_pruned),
        })
        print(
            f"Iteration {iteration}: evaluated={len(new_nodes)} "
            f"included={len(iteration_included)} "
            f"pruned={len(iteration_pruned)}",
            flush=True,
        )

        if not iteration_included:
            break

        next_nodes = {}
        for node in iteration_included:
            for neighbor in index.one_hop_neighbors(URIRef(node["uri"])):
                if neighbor["uri"] not in all_visited:
                    next_nodes[neighbor["uri"]] = neighbor
        nodes_to_evaluate = sorted(
            next_nodes.values(),
            key=lambda row: (row["prefLabel"].casefold(), row["uri"]),
        )

    evaluated_included_uris = (
        normalize_uri_set(all_included.keys())
        - normalize_uri_set(exclude_evaluation_uris)
    )
    evaluated_gold_uris = (
        normalize_uri_set(gold_uris)
        - normalize_uri_set(exclude_evaluation_uris)
    )
    evaluated_pruned_uris = (
        normalize_uri_set(all_pruned.keys())
        - normalize_uri_set(exclude_evaluation_uris)
    )
    evaluated_visited_uris = (
        normalize_uri_set(all_visited.keys())
        - normalize_uri_set(exclude_evaluation_uris)
    )
    evaluated_included_count = len(evaluated_included_uris)
    metrics = calculate_precision_recall_f1(
        all_included.keys(),
        gold_uris,
        pruned_uris=all_pruned.keys(),
        exclude_uris=exclude_evaluation_uris,
    )
    metrics.update({
        "gold_positive_count": len(evaluated_gold_uris),
        "reachable_gold_positive_count": len(gold_uris),
        "raw_gold_positive_count": len(raw_gold_uris),
        "unreachable_gold_positive_count": len(unreachable_gold_uris),
        "included_count": evaluated_included_count,
        "raw_included_count": len(all_included),
        "pruned_count": len(evaluated_pruned_uris),
        "raw_pruned_count": len(all_pruned),
        "visited_count": len(evaluated_visited_uris),
        "raw_visited_count": len(all_visited),
        "iterations": iteration,
    })

    return {
        "seed_uris": [str(seed_uri) for seed_uri in seed_uris],
        "gold_root_uri": str(gold_root),
        "llm_provider": resolved_provider,
        "llm_model": resolved_model,
        "run_config": {
            "benchmark_name": benchmark_name,
            "gold_root_label": gold_root_label,
            "output_subdir": output_subdir,
            "seed_label": seed_labels[0],
            "seed_labels": seed_labels,
            "seed_topic": seed_topic,
            "max_iterations": max_iterations,
            "batch_size": batch_size,
            "llm_max_workers": llm_max_workers,
            "prompt_file": str(prompt_file) if prompt_file else None,
            "result_mode": resolved_result_mode,
            "exclude_examples_files": [
                str(path) for path in (exclude_examples_files or [])
            ],
            "excluded_evaluation_uri_count": len(exclude_evaluation_uris),
        },
        "raw_gold_uris": {str(uri) for uri in raw_gold_uris},
        "gold_uris": {str(uri) for uri in gold_uris},
        "unreachable_gold_uris": {str(uri) for uri in unreachable_gold_uris},
        "exclude_evaluation_uris": {
            str(uri) for uri in exclude_evaluation_uris
        },
        "all_visited": list(all_visited.values()),
        "all_included": list(all_included.values()),
        "all_pruned": list(all_pruned.values()),
        "iterations": iteration_details,
        "metrics": metrics,
    }


def download_nalt_ttl(url, destination, opener=urlopen):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial_path = destination.with_name(f"{destination.name}.part")

    try:
        with opener(url, timeout=120) as response:
            with partial_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        if partial_path.stat().st_size == 0:
            raise ValueError("Downloaded NALT Turtle file is empty")
        partial_path.replace(destination)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    return destination


def resolve_nalt_ttl(
    user_path=None,
    candidates=None,
    download_path=DEFAULT_NALT_DOWNLOAD_PATH,
    downloader=download_nalt_ttl,
):
    if user_path:
        path = Path(user_path)
        if path.exists():
            return path
        raise FileNotFoundError(f"NALT TTL not found: {path}")

    candidates = DEFAULT_NALT_TTL_CANDIDATES if candidates is None else candidates
    for candidate in candidates:
        candidate = Path(candidate)
        if candidate.exists():
            return candidate

    download_path = Path(download_path)
    print(f"No local NALT Full TTL found; downloading to: {download_path}")
    return Path(downloader(NALT_TTL_DOWNLOAD_URL, download_path))


def write_csv(path, rows, gold_uris):
    fieldnames = [
        "prefLabel",
        "altLabels",
        "displayName",
        "uri",
        "confidence",
        "in_hierarchy_gold",
        "relations",
        "classification",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output = dict(row)
            output["altLabels"] = "; ".join(row.get("altLabels", []))
            output["relations"] = "; ".join(row.get("relations", []))
            output["in_hierarchy_gold"] = str(row["uri"] in gold_uris)
            writer.writerow(output)


def save_results(
    results,
    output_dir=DEFAULT_OUTPUT_DIR,
    timestamp=None,
    overwrite=False,
):
    timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_config = results.get("run_config", {})
    output_dir = resolve_results_output_dir(
        output_dir,
        run_config,
        results.get("llm_model"),
    )
    initialize_results_output_dir(output_dir, overwrite=overwrite)
    benchmark_name = run_config.get("benchmark_name") or "custom"
    if benchmark_name == "custom":
        benchmark_name = slugify_label(run_config.get("seed_topic", "custom"))
    prefix = f"subkg_{benchmark_name}_NALT_{timestamp}"

    gold_uris = results["gold_uris"]
    included_path = output_dir / f"{prefix}_INCLUDED.csv"
    pruned_path = output_dir / f"{prefix}_PRUNED.csv"
    visited_path = output_dir / f"{prefix}_VISITED.csv"
    metrics_path = output_dir / f"{prefix}_METRICS.json"

    write_csv(included_path, results["all_included"], gold_uris)
    write_csv(pruned_path, results["all_pruned"], gold_uris)
    write_csv(visited_path, results["all_visited"], gold_uris)

    retained_run_config = {
        key: run_config[key]
        for key in (
            "benchmark_name",
            "gold_root_label",
            "seed_labels",
            "max_iterations",
            "batch_size",
            "llm_max_workers",
            "prompt_file",
            "result_mode",
            "exclude_examples_files",
        )
        if key in run_config
    }
    metrics_payload = {
        "seed_uris": results["seed_uris"],
        "gold_root_uri": results["gold_root_uri"],
        "llm_provider": results["llm_provider"],
        "llm_model": results["llm_model"],
        "run_config": retained_run_config,
        "metrics": results["metrics"],
        "unreachable_gold_uris": sorted(results["unreachable_gold_uris"]),
        "exclude_evaluation_uris": sorted(results.get("exclude_evaluation_uris", [])),
        "iterations": results["iterations"],
    }
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with metrics_path.open("w", encoding="utf-8") as handle:
        json.dump(metrics_payload, handle, indent=2)

    return {
        "included": included_path,
        "pruned": pruned_path,
        "visited": visited_path,
        "metrics": metrics_path,
    }


def describe_benchmark(index, seed_label="soil", seed_labels=None, gold_root_uri=NALT_SOIL_SCIENCE_URI):
    seed_labels = normalize_seed_labels(
        seed_label=seed_label,
        seed_labels=seed_labels,
    )
    seed_uris = [
        index.find_uri_by_english_pref_label(label)
        for label in seed_labels
    ]
    gold_root = URIRef(str(gold_root_uri))
    raw_gold_uris = index.hierarchy_descendants(gold_root, exclude={gold_root})
    structurally_reachable_uris = reachable_uris_from_seeds(index, seed_uris)
    gold_uris = raw_gold_uris & structurally_reachable_uris
    unreachable_gold_uris = raw_gold_uris - structurally_reachable_uris
    seed_neighbors = {}
    for seed_uri in seed_uris:
        for neighbor in index.one_hop_neighbors(seed_uri):
            seed_neighbors[neighbor["uri"]] = neighbor
    seed_neighbor_uris = set(seed_neighbors)
    raw_gold_strings = {str(uri) for uri in raw_gold_uris}

    return {
        "seed_label": seed_labels[0],
        "seed_labels": seed_labels,
        "seed_topic": format_seed_topic(seed_labels),
        "seed_uri": str(seed_uris[0]),
        "seed_uris": [str(seed_uri) for seed_uri in seed_uris],
        "ignored_root_uri": str(gold_root),
        "visible_concepts": len(index.pref_labels),
        "traversal_relations": [relation_name(relation) for relation in TRAVERSAL_RELATIONS],
        "seed_one_hop_neighbors_after_root_ignore": len(seed_neighbors),
        "gold_positive_count_excluding_root": len(raw_gold_uris),
        "reachable_gold_positive_count": len(gold_uris),
        "unreachable_gold_positive_count": len(unreachable_gold_uris),
        "seed_in_gold": {
            label: str(uri) in raw_gold_strings
            for label, uri in zip(seed_labels, seed_uris)
        },
        "root_reachable_from_seed_after_ignore": str(gold_root) in seed_neighbor_uris,
    }


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now_iso():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_examples(path):
    return load_json(path)


def validate_example_pack(payload):
    benchmark_name = payload.get("benchmark_name")
    if not benchmark_name:
        raise ValueError("Example pack requires benchmark_name")
    if payload.get("context_level") != "labels":
        raise ValueError("NALT meta examples must use context_level='labels'")
    seed_labels = payload.get("seed_labels")
    if not isinstance(seed_labels, list) or not seed_labels:
        raise ValueError("Example pack requires a non-empty seed_labels list")
    examples = payload.get("examples")
    if not isinstance(examples, list) or not examples:
        raise ValueError("Example pack requires a non-empty examples list")

    seen_ids = set()
    decisions = set()
    for example in examples:
        for key in ("id", "seed_label", "candidate_label", "decision"):
            if not example.get(key):
                raise ValueError(f"Example in {benchmark_name} missing {key}")
        if example["id"] in seen_ids:
            raise ValueError(f"Duplicate example id: {example['id']}")
        seen_ids.add(example["id"])
        decision = str(example["decision"]).upper()
        if decision not in {"INCLUDE", "EXCLUDE"}:
            raise ValueError(f"Invalid decision for {example['id']}: {decision}")
        example["decision"] = decision
        decisions.add(decision)
        for disallowed in ("path", "parent", "reaching_property"):
            if disallowed in example:
                raise ValueError(
                    f"Label-only examples must not contain {disallowed}: "
                    f"{example['id']}"
                )

    if decisions != {"INCLUDE", "EXCLUDE"}:
        raise ValueError(
            f"Example pack for {benchmark_name} must contain INCLUDE and "
            "EXCLUDE examples"
        )
    return payload


def format_examples_for_meta_prompt(examples_payload):
    validate_example_pack(examples_payload)
    lines = []
    for example in examples_payload["examples"]:
        lines.append(f"- Seed Topic: {example['seed_label']}")
        lines.append(
            "  Candidate: "
            f"candidate={example['candidate_label']} | "
            f"decision={example['decision']}"
        )
    return "\n".join(lines)


def render_meta_prompt(meta_prompt_text, examples_payload):
    rendered_examples = format_examples_for_meta_prompt(examples_payload)
    if "{FEW_SHOT_EXAMPLES}" not in meta_prompt_text:
        raise ValueError("Meta prompt must contain {FEW_SHOT_EXAMPLES}")
    return meta_prompt_text.replace("{FEW_SHOT_EXAMPLES}", rendered_examples)


def prompt_has_heading(text, heading):
    heading_lower = heading.casefold()
    for line in text.splitlines():
        normalized = line.strip().lstrip("#").strip().casefold()
        if normalized == heading_lower or normalized.startswith(f"{heading_lower} "):
            return True
    return False


def validate_manual_prompt(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    missing = [
        heading
        for heading in REQUIRED_PROMPT_HEADINGS
        if not prompt_has_heading(text, heading)
    ]
    if missing:
        raise ValueError(f"{path} missing required headings: {', '.join(missing)}")
    return {
        "path": str(path),
        "word_count": len(text.split()),
    }


def validate_generated_prompt_text(text):
    if not text.strip():
        raise ValueError("Generated prompt is empty")
    missing = [
        heading
        for heading in REQUIRED_PROMPT_HEADINGS
        if not prompt_has_heading(text, heading)
    ]
    if missing:
        raise ValueError(
            "Generated prompt missing required headings: " + ", ".join(missing)
        )
    if "```" in text:
        raise ValueError("Generated prompt must be Markdown, not a fenced block")
    return text.strip()


def validate_meta_prompt(path=DEFAULT_META_PROMPT):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if "{FEW_SHOT_EXAMPLES}" not in text:
        raise ValueError(f"{path} missing {{FEW_SHOT_EXAMPLES}} placeholder")
    missing = [
        heading
        for heading in (
            "Role",
            "Prompt-Generation Input",
            "Generated Prompt Requirements",
        )
        if heading not in text
    ]
    if missing:
        raise ValueError(f"{path} missing required sections: {', '.join(missing)}")
    return {
        "path": str(path),
        "word_count": len(text.split()),
    }


def validate_repository_artifacts(
    prompt_dir=DEFAULT_PROMPT_DIR,
    report_path=None,
):
    prompt_dir = Path(prompt_dir)
    manual_dir = prompt_dir / "manual"
    examples_dir = prompt_dir / "examples"
    benchmarks = sorted(
        path.stem
        for path in manual_dir.glob("*.md")
        if path.stem in NALT_BENCHMARK_PRESETS
    )
    if not benchmarks:
        raise FileNotFoundError(f"No manual prompt files found under {manual_dir}")

    report = {
        "prompt_dir": str(prompt_dir),
        "benchmarks": benchmarks,
        "meta_prompt": validate_meta_prompt(
            prompt_dir / "meta_prompt_label_only.md"
        ),
        "manual_prompts": [],
        "example_packs": [],
        "model_prompts": [],
    }

    for benchmark in benchmarks:
        manual_path = manual_dir / f"{benchmark}.md"
        if not manual_path.exists():
            raise FileNotFoundError(manual_path)
        report["manual_prompts"].append({
            "benchmark_name": benchmark,
            **validate_manual_prompt(manual_path),
        })

        examples_path = examples_dir / f"{benchmark}_label_meta_examples.json"
        if not examples_path.exists():
            raise FileNotFoundError(examples_path)
        payload = validate_example_pack(load_examples(examples_path))
        report["example_packs"].append({
            "benchmark_name": benchmark,
            "path": str(examples_path),
            "example_count": len(payload["examples"]),
            "include_count": sum(
                example["decision"] == "INCLUDE"
                for example in payload["examples"]
            ),
            "exclude_count": sum(
                example["decision"] == "EXCLUDE"
                for example in payload["examples"]
            ),
        })

    model_dirs = sorted(
        path
        for path in prompt_dir.iterdir()
        if path.is_dir() and path.name not in SHARED_PROMPT_DIR_NAMES
    )
    for model_dir in model_dirs:
        for prompt_path in sorted(model_dir.glob("*.md")):
            if prompt_path.stem not in NALT_BENCHMARK_PRESETS:
                continue
            report["model_prompts"].append({
                "model": model_dir.name,
                "benchmark_name": prompt_path.stem,
                **validate_manual_prompt(prompt_path),
            })

    if report_path:
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def render_meta_input(args):
    examples_payload = load_examples(args.examples_file)
    meta_prompt = Path(args.meta_prompt).read_text(encoding="utf-8")
    rendered = render_meta_prompt(meta_prompt, examples_payload)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Rendered meta input saved: {output}")
    else:
        print(rendered)


def generate_prompt_with_openai(client, model, content):
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "You generate complete Markdown system prompts for "
                    "LLM-based knowledge-graph pruning."
                ),
            },
            {"role": "user", "content": content},
        ],
        reasoning={"effort": "xhigh"},
    )
    generated = (getattr(response, "output_text", None) or "").strip()
    if not generated:
        raise RuntimeError("OpenAI returned an empty generated prompt")
    return generated, {
        "parameter": "reasoning.effort",
        "value": "xhigh",
    }


def generate_prompt_with_gemini(client, model, content):
    from google.genai import types

    response = client.models.generate_content(
        model=model,
        contents=content,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You generate complete Markdown system prompts for "
                "LLM-based knowledge-graph pruning."
            ),
            thinking_config=types.ThinkingConfig(thinking_level="high"),
        ),
    )
    generated = (getattr(response, "text", None) or "").strip()
    if not generated:
        raise RuntimeError("Gemini returned an empty generated prompt")
    return generated, {
        "parameter": "thinkingConfig.thinkingLevel",
        "value": "high",
    }


def generate_prompt_with_deepseek(client, model, content):
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate complete Markdown system prompts for "
                    "LLM-based knowledge-graph pruning."
                ),
            },
            {"role": "user", "content": content},
        ],
        stream=False,
        reasoning_effort="max",
        extra_body={"thinking": {"type": "enabled"}},
    )
    generated = (response.choices[0].message.content or "").strip()
    if not generated:
        raise RuntimeError("DeepSeek returned an empty generated prompt")
    return generated, {
        "parameter": "reasoning_effort",
        "value": "max",
        "thinking": {"type": "enabled"},
    }


def generate_prompt_with_provider(provider, client, model, content):
    if provider == "gemini":
        return generate_prompt_with_gemini(client, model, content)
    if provider == "deepseek":
        return generate_prompt_with_deepseek(client, model, content)
    return generate_prompt_with_openai(client, model, content)


def generate_validated_prompt(provider, client, model, content):
    retry_content = content
    last_error = None
    for attempt in range(1, PROMPT_GENERATION_ATTEMPTS + 1):
        generated, reasoning = generate_prompt_with_provider(
            provider,
            client,
            model,
            retry_content,
        )
        try:
            return validate_generated_prompt_text(generated), reasoning, attempt
        except ValueError as exc:
            last_error = exc
            retry_content = (
                content
                + "\n\nThe previous prompt failed validation: "
                + str(exc)
                + "\nRegenerate a complete Markdown prompt with explicit Role, "
                "INCLUDE, EXCLUDE, Decision, and Confidence headings."
            )
    raise RuntimeError("Generated prompt failed validation") from last_error


def generate_prompt(args):
    if args.benchmark not in NALT_BENCHMARK_PRESETS:
        raise ValueError(f"Unknown NALT benchmark: {args.benchmark}")

    examples_file = (
        Path(args.examples_file)
        if args.examples_file
        else DEFAULT_META_EXAMPLES_DIR
        / f"{args.benchmark}_label_meta_examples.json"
    )
    examples_payload = validate_example_pack(load_examples(examples_file))
    if examples_payload["benchmark_name"] != args.benchmark:
        raise ValueError(
            f"Example pack is for {examples_payload['benchmark_name']}, "
            f"not {args.benchmark}"
        )
    meta_prompt_path = Path(args.meta_prompt)
    meta_prompt_text = meta_prompt_path.read_text(encoding="utf-8")
    content = render_meta_prompt(meta_prompt_text, examples_payload)

    provider = resolve_provider(args.provider, args.model)
    model = args.model or default_model_for_provider(provider)
    client = get_llm_client(provider, config_path=args.config_path)
    generated, reasoning, attempts = generate_validated_prompt(
        provider,
        client,
        model,
        content,
    )

    output = (
        Path(args.output)
        if args.output
        else DEFAULT_PROMPT_DIR / model / f"{args.benchmark}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generated + "\n", encoding="utf-8")

    metadata_path = output.with_suffix(".json")
    metadata = {
        "benchmark_name": args.benchmark,
        "provider": provider,
        "model": model,
        "prompt_generation_reasoning": reasoning,
        "generation_attempts": attempts,
        "examples_file": str(examples_file),
        "examples_sha256": file_sha256(examples_file),
        "meta_prompt": str(meta_prompt_path),
        "meta_prompt_sha256": file_sha256(meta_prompt_path),
        "generation_input_sha256": text_sha256(content),
        "generated_prompt_sha256": file_sha256(output),
        "created_at": utc_now_iso(),
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated prompt saved: {output}")
    print(f"Metadata saved: {metadata_path}")


def validate_gateway_probe(payload, benchmark_name):
    if payload.get("benchmark_name") != benchmark_name:
        raise ValueError(
            f"Gateway probe is for {payload.get('benchmark_name')}, "
            f"not {benchmark_name}"
        )
    seed_labels = payload.get("seed_labels")
    if not isinstance(seed_labels, list) or not seed_labels:
        raise ValueError("Gateway probe requires a non-empty seed_labels list")
    gateways = payload.get("gateways")
    if not isinstance(gateways, list) or not gateways:
        raise ValueError("Gateway probe requires a non-empty gateways list")
    labels = [gateway.get("label") for gateway in gateways]
    if any(not label for label in labels):
        raise ValueError("Every gateway requires a label")
    if len(set(labels)) != len(labels):
        raise ValueError("Gateway labels must be unique")
    for gateway in gateways:
        expected = str(
            gateway.get("expected_classification", "EXCLUDE")
        ).upper()
        if expected not in {"INCLUDE", "EXCLUDE"}:
            raise ValueError(
                f"Invalid expected classification for {gateway['label']}: "
                f"{expected}"
            )
        gateway["expected_classification"] = expected
    return payload


def probe_gateways(args):
    payload = validate_gateway_probe(
        load_json(args.gateway_file),
        args.benchmark,
    )
    resolved_ttl = resolve_nalt_ttl(args.nalt_ttl)
    print(f"Loading NALT TTL: {resolved_ttl}", flush=True)
    index = NaltSkosIndex.from_ttl(resolved_ttl)

    nodes = []
    for gateway in payload["gateways"]:
        uri = index.find_uri_by_english_pref_label(gateway["label"])
        node = index.node_info(uri)
        node["probe_weight"] = gateway.get("weight", 1)
        node["probe_critical"] = bool(gateway.get("critical", False))
        node["probe_expected_classification"] = gateway[
            "expected_classification"
        ]
        nodes.append(node)

    provider = resolve_provider(args.provider, args.model)
    model = args.model or default_model_for_provider(provider)
    client = get_llm_client(provider, config_path=args.config_path)
    seed_topic = format_seed_topic(payload["seed_labels"])
    total_weight = sum(node["probe_weight"] for node in nodes)
    results = []

    for prompt_path in args.prompt_file:
        prompt_path = Path(prompt_path)
        system_prompt = validate_generated_prompt_text(
            prompt_path.read_text(encoding="utf-8")
        )
        classified = classify_candidates(
            client=client,
            model=model,
            seed_label=seed_topic,
            nodes=nodes,
            batch_size=len(nodes),
            max_workers=1,
            provider=provider,
            system_prompt=system_prompt,
        )
        excluded = [
            node for node in classified if node["classification"] == "EXCLUDE"
        ]
        included = [
            node for node in classified if node["classification"] == "INCLUDE"
        ]
        correct = [
            node
            for node in classified
            if node["classification"] == node["probe_expected_classification"]
        ]
        errors = [
            node
            for node in classified
            if node["classification"] != node["probe_expected_classification"]
        ]
        critical_failures = [
            node["prefLabel"] for node in errors if node["probe_critical"]
        ]
        excluded_weight = sum(node["probe_weight"] for node in excluded)
        correct_weight = sum(node["probe_weight"] for node in correct)
        max_errors = payload.get("pass_criteria", {}).get("max_errors", 1)
        passed = len(errors) <= max_errors and not critical_failures
        result = {
            "prompt_file": str(prompt_path),
            "prompt_sha256": file_sha256(prompt_path),
            "excluded_count": len(excluded),
            "included_count": len(included),
            "correct_count": len(correct),
            "error_count": len(errors),
            "exclusion_rate": len(excluded) / len(classified),
            "weighted_exclusion_rate": (
                excluded_weight / total_weight if total_weight else 0.0
            ),
            "weighted_accuracy": (
                correct_weight / total_weight if total_weight else 0.0
            ),
            "critical_failures": critical_failures,
            "passed": passed,
            "evaluations": [
                {
                    "label": node["prefLabel"],
                    "uri": node["uri"],
                    "classification": node["classification"],
                    "expected_classification": node[
                        "probe_expected_classification"
                    ],
                    "correct": (
                        node["classification"]
                        == node["probe_expected_classification"]
                    ),
                    "confidence": node["confidence"],
                    "weight": node["probe_weight"],
                    "critical": node["probe_critical"],
                }
                for node in classified
            ],
        }
        results.append(result)
        print(
            f"{prompt_path.name}: correct={len(correct)}/{len(classified)} "
            f"weighted={result['weighted_accuracy']:.3f} "
            f"critical_failures={len(critical_failures)} passed={passed}",
            flush=True,
        )

    report = {
        "benchmark_name": args.benchmark,
        "provider": provider,
        "model": model,
        "seed_labels": payload["seed_labels"],
        "gateway_file": str(args.gateway_file),
        "gateway_file_sha256": file_sha256(args.gateway_file),
        "nalt_ttl": str(resolved_ttl),
        "created_at": utc_now_iso(),
        "pass_criteria": payload.get("pass_criteria", {"max_errors": 1}),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Gateway probe saved: {args.output}")


def add_extraction_arguments(parser):
    parser.add_argument("--nalt-ttl", type=Path, default=None,
                        help=(
                            "Path to NALT Full Turtle file. If omitted, common local paths are "
                            "checked before downloading the official Turtle export."
                        ))
    parser.add_argument(
        "--benchmark",
        choices=sorted(NALT_BENCHMARK_PRESETS),
        default=None,
        help="Named NALT benchmark preset. Omit for a custom run.",
    )
    parser.add_argument(
        "--list-benchmarks",
        action="store_true",
        help="Print available NALT benchmark presets and exit.",
    )
    parser.add_argument(
        "--seed-label",
        action="append",
        default=None,
        help=(
            "English prefLabel to use as a seed. Repeat for multiple seeds. "
            "Defaults to 'soil' for custom runs."
        ),
    )
    parser.add_argument("--gold-root-uri", default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Result root. Runs are stored below "
            "<model>/<branch>/<mode>."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing canonical model/branch/mode result directory.",
    )
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--llm-provider",
        choices=("auto", "openai", "gemini", "deepseek"),
        default="auto",
        help=(
            "LLM provider. Auto infers Gemini from gemini-* and DeepSeek "
            "from deepseek-* model names."
        ),
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help=(
            f"Model name. Defaults to {DEFAULT_OPENAI_MODEL} for OpenAI or "
            f"{DEFAULT_GEMINI_MODEL} for Gemini, or {DEFAULT_DEEPSEEK_MODEL} "
            "for DeepSeek."
        ),
    )
    parser.add_argument("--llm-max-workers", type=int, default=5)
    parser.add_argument("--config-path", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help=(
            "Markdown system prompt to use for classification. Defaults to the "
            "built-in generic NALT prompt."
        ),
    )
    parser.add_argument(
        "--result-mode",
        choices=RESULT_MODES,
        default=None,
        help=(
            "Result grouping mode. By default, manual prompt paths map to "
            "zero_shot_manual, other prompt files to zero_shot_meta, and the "
            "built-in prompt to zero_shot_generic."
        ),
    )
    parser.add_argument(
        "--exclude-examples-file",
        type=Path,
        action="append",
        default=None,
        help=(
            "Label-only example pack used for prompt calibration. Candidate "
            "nodes in the pack are excluded from evaluation metrics. Repeat "
            "for multiple packs."
        ),
    )
    parser.add_argument("--describe-only", action="store_true",
                        help="Parse NALT and print benchmark statistics without calling the LLM.")


def build_cli_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run USDA NALT Full subgraph extraction and prompt experiments."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract",
        help="Run an LLM-gated NALT extraction benchmark.",
    )
    add_extraction_arguments(extract)

    validate = subparsers.add_parser(
        "validate-artifacts",
        help="Validate checked-in prompts and labeled demonstration sets.",
    )
    validate.add_argument(
        "--prompt-dir",
        type=Path,
        default=DEFAULT_PROMPT_DIR,
    )
    validate.add_argument("--report-path", type=Path, default=None)

    render = subparsers.add_parser(
        "render-meta-input",
        help="Render a meta-prompt with a labeled demonstration set.",
    )
    render.add_argument("--examples-file", type=Path, required=True)
    render.add_argument("--meta-prompt", type=Path, default=DEFAULT_META_PROMPT)
    render.add_argument("--output", type=Path, default=None)

    generate = subparsers.add_parser(
        "generate",
        help="Generate and validate a benchmark-specific system prompt.",
    )
    generate.add_argument(
        "--benchmark",
        choices=sorted(NALT_BENCHMARK_PRESETS),
        required=True,
    )
    generate.add_argument("--examples-file", type=Path, default=None)
    generate.add_argument(
        "--meta-prompt",
        type=Path,
        default=DEFAULT_META_PROMPT,
    )
    generate.add_argument(
        "--provider",
        choices=("auto", "openai", "gemini", "deepseek"),
        default="auto",
        help=(
            "Auto selects Gemini for gemini-* and DeepSeek for deepseek-* "
            "model names. DeepSeek prompt generation uses max reasoning."
        ),
    )
    generate.add_argument(
        "--model",
        default=None,
        help=(
            f"Defaults to {DEFAULT_OPENAI_MODEL}, {DEFAULT_GEMINI_MODEL}, or "
            f"{DEFAULT_DEEPSEEK_MODEL} according to provider."
        ),
    )
    generate.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    generate.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Defaults to prompts/<model>/<benchmark>.md.",
    )

    probe = subparsers.add_parser(
        "probe-gateways",
        help="Compare prompts on a fixed set of consequential gateway nodes.",
    )
    probe.add_argument(
        "--benchmark",
        choices=sorted(NALT_BENCHMARK_PRESETS),
        required=True,
    )
    probe.add_argument("--gateway-file", type=Path, required=True)
    probe.add_argument(
        "--prompt-file",
        type=Path,
        action="append",
        required=True,
    )
    probe.add_argument("--nalt-ttl", type=Path, default=None)
    probe.add_argument(
        "--provider",
        choices=("auto", "openai", "gemini", "deepseek"),
        default="auto",
    )
    probe.add_argument("--model", default=None)
    probe.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    probe.add_argument("--output", type=Path, required=True)
    return parser


def normalize_cli_argv(argv):
    argv = list(argv)
    commands = {
        "extract",
        "validate-artifacts",
        "render-meta-input",
        "generate",
        "probe-gateways",
    }
    if argv and argv[0] not in commands and argv[0] not in {"-h", "--help"}:
        return ["extract", *argv]
    return argv


def run_extraction(args):

    if args.list_benchmarks:
        print(json.dumps(NALT_BENCHMARK_PRESETS, indent=2))
        return

    benchmark_config = resolve_benchmark_config(
        benchmark_name=args.benchmark,
        seed_labels=args.seed_label,
        gold_root_uri=args.gold_root_uri,
    )

    if not args.describe_only:
        resolved_provider = resolve_provider(args.llm_provider, args.llm_model)
        resolved_model = args.llm_model or default_model_for_provider(
            resolved_provider
        )
        resolved_result_mode = args.result_mode or infer_result_mode(
            args.prompt_file
        )
        planned_output_dir = resolve_results_output_dir(
            args.output_dir,
            {
                "benchmark_name": benchmark_config["benchmark_name"],
                "output_subdir": benchmark_config.get("output_subdir"),
                "prompt_file": (
                    str(args.prompt_file) if args.prompt_file else None
                ),
                "result_mode": resolved_result_mode,
            },
            resolved_model,
        )
        validate_results_output_dir(
            planned_output_dir,
            overwrite=args.overwrite,
        )

    ttl_path = resolve_nalt_ttl(args.nalt_ttl)
    ignored_uris = {URIRef(str(benchmark_config["gold_root_uri"]))}

    print(f"Loading NALT TTL: {ttl_path}")
    index = NaltSkosIndex.from_ttl(ttl_path, ignored_uris=ignored_uris)
    description = describe_benchmark(
        index,
        seed_labels=benchmark_config["seed_labels"],
        gold_root_uri=benchmark_config["gold_root_uri"],
    )
    description["benchmark_name"] = benchmark_config["benchmark_name"]
    description["gold_root_label"] = benchmark_config["gold_root_label"]
    print(json.dumps(description, indent=2))

    if args.describe_only:
        return

    system_prompt = resolve_system_prompt(args.prompt_file)
    exclude_evaluation_uris = set()
    for examples_file in args.exclude_examples_file or []:
        exclude_evaluation_uris.update(
            resolve_example_candidate_uris(index, examples_file)
        )

    print("Starting LLM-gated NALT BFS extraction")
    results = iterative_nalt_extraction(
        index=index,
        seed_labels=benchmark_config["seed_labels"],
        gold_root_uri=benchmark_config["gold_root_uri"],
        benchmark_name=benchmark_config["benchmark_name"],
        gold_root_label=benchmark_config["gold_root_label"],
        output_subdir=benchmark_config.get("output_subdir"),
        max_iterations=args.max_iterations,
        batch_size=args.batch_size,
        llm_model=args.llm_model,
        llm_provider=args.llm_provider,
        llm_max_workers=args.llm_max_workers,
        config_path=args.config_path,
        system_prompt=system_prompt,
        prompt_file=args.prompt_file,
        result_mode=args.result_mode,
        exclude_evaluation_uris=exclude_evaluation_uris,
        exclude_examples_files=args.exclude_examples_file,
    )
    output_paths = save_results(
        results,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
    )

    print("NALT benchmark complete")
    print("Primary hierarchy metrics:")
    print(json.dumps(results["metrics"], indent=2))
    print("Output files:")
    for name, path in output_paths.items():
        print(f"  {name}: {path}")


def main(argv=None):
    parser = build_cli_parser()
    cli_argv = normalize_cli_argv(
        sys.argv[1:] if argv is None else argv
    )
    args = parser.parse_args(cli_argv)

    if args.command == "extract":
        run_extraction(args)
    elif args.command == "validate-artifacts":
        report = validate_repository_artifacts(
            prompt_dir=args.prompt_dir,
            report_path=args.report_path,
        )
        print(json.dumps(report, indent=2))
    elif args.command == "render-meta-input":
        render_meta_input(args)
    elif args.command == "generate":
        generate_prompt(args)
    elif args.command == "probe-gateways":
        probe_gateways(args)


if __name__ == "__main__":
    main()
