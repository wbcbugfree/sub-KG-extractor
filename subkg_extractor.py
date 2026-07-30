#!/usr/bin/env python3
"""Unified LLM-gated extraction of topic-specific RDF subgraphs.

The command accepts an RDF target graph, one or more seed nodes, and either:

* a small node-only labeled demonstration set used to generate a system prompt;
  or
* a user-provided system prompt.

The runner first resolves one effective allowed-relation list. A user-provided
list is used as the traversal override. Otherwise, SKOS traversal uses
``skos:broader``, ``skos:narrower``, and ``skos:related``, while KG traversal
uses every predicate that connects two labeled URI nodes. The same list is
used to reconstruct seed-to-demonstration paths and to run the gated BFS.

Use ``--dry-run`` to parse and classify the graph, resolve nodes, reconstruct
paths, and report the prepared extraction plan without creating an LLM client
or making any API request.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from pydantic import BaseModel, Field
from rdflib import Graph, Literal as RdfLiteral, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, SKOS


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "results" / "subkg_extractor"

DEFAULT_MODELS = {
    "openai": "gpt-5.4-mini",
    "gemini": "gemini-3.5-flash",
    "deepseek": "deepseek-v4-pro",
    "openrouter": "google/gemini-3.5-flash",
}

OPENROUTER_CHAT_COMPLETIONS_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)
DEEPSEEK_BETA_BASE_URL = "https://api.deepseek.com/beta"
DEEPSEEK_TOOL_NAME = "submit_subgraph_candidate_evaluations"
DEEPSEEK_PROTOCOL_RETRIES = 3
GEMINI_RETRIES = 8

SKOS_TRAVERSAL_PREDICATES = (
    SKOS.broader,
    SKOS.narrower,
    SKOS.related,
)

LABEL_PREDICATES = (
    SKOS.prefLabel,
    RDFS.label,
)

ALTERNATIVE_LABEL_PREDICATES = (
    SKOS.altLabel,
    SKOS.hiddenLabel,
)

ANNOTATION_PREDICATES = {
    SKOS.prefLabel,
    SKOS.altLabel,
    SKOS.hiddenLabel,
    SKOS.notation,
    SKOS.definition,
    SKOS.scopeNote,
    SKOS.historyNote,
    SKOS.changeNote,
    SKOS.editorialNote,
    SKOS.note,
    RDFS.label,
    RDFS.comment,
    DCTERMS.title,
    DCTERMS.description,
    DCTERMS.created,
    DCTERMS.modified,
    DCTERMS.source,
}

SKOS_NAMESPACE = str(SKOS)
NALT_SCHEME_IRIS = {
    URIRef("https://lod.nal.usda.gov/nalt"),
    URIRef("https://lod.nal.usda.gov/nalt-core"),
    URIRef("https://lod.nal.usda.gov/nalt-taxon"),
}

NALT_NON_TRAVERSAL_CONCEPT_PREDICATES = {
    "https://lod.nal.usda.gov/naltv#taxonomicRank",
    "https://lod.nal.usda.gov/naltv#hasProduct",
    "https://lod.nal.usda.gov/naltv#productOf",
}

COMMON_RELATION_LABELS = {
    str(RDF.type): "type",
    str(RDFS.subClassOf): "subclass of",
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

COMMON_INVERSE_RELATION_LABELS = {
    str(RDF.type): "has type instance",
    str(RDFS.subClassOf): "has subclass",
    "P31": "has instance",
    "P279": "has subclass",
    "P127": "owner of",
    "P1830": "owned by",
}

OUTPUT_FILENAMES = {
    "manifest": "run.json",
    "prompt": "system_prompt.md",
    "decisions": "decisions.csv",
    "included": "included.csv",
    "excluded": "excluded.csv",
    "visited": "visited.csv",
    "subgraph": "subgraph.ttl",
}


KG_META_PROMPT = """# Meta-Prompt: Path-Aware Sub-Knowledge Graph Prompt Generator

## Role

Write one complete Markdown system prompt for a downstream LLM that decides
whether candidate nodes belong in a focused sub-knowledge graph.

The labeled demonstrations below are the only task-specific evidence. The
complete seed set jointly defines one topic. Each demonstration contains that
seed set, a final candidate, a path reconstructed from one of the seeds, and an
INCLUDE or EXCLUDE decision. The first node in the path identifies the seed
from which that candidate was reached. Intermediate nodes are traversal
context; only the final candidate is the decision target. The downstream
traversal may also encounter allowed relations not represented in this small
demonstration set.

{LABELED_DEMONSTRATIONS}

## What to infer

Infer the target scope, relation and direction semantics, the effect of complete
paths, the role of intermediate bridge nodes, the practical INCLUDE/EXCLUDE
boundary, and the precision-recall tradeoff appropriate for prune-gated BFS.
Use contrasts across demonstrations rather than class frequency. Translate
repeated evidence into general rules; do not copy labels or paths into the
generated prompt and do not invent unsupported graph facts.

## Required generated prompt

Return one self-contained Markdown system prompt under 1200 words. Its headings
must explicitly include Role, INCLUDE, EXCLUDE, Decision, and Confidence. It
must explain that all supplied seed nodes jointly define the target topic, that
relation direction and sequence can change relevance, that intermediate nodes
are context rather than decision targets, that INCLUDE keeps future traversal
available, and that EXCLUDE can block deeper nodes. Path length alone is not a
decision rule. Confidence must mean certainty in the selected decision, not
relevance.

Do not include an Output, Response Format, JSON, or Structured Output section;
the runner enforces the response schema. Do not mention prompt generation,
benchmarks, evaluation splits, papers, or the supplied demonstrations.
"""


SKOS_META_PROMPT = """# Meta-Prompt: Label-Only SKOS Subgraph Prompt Generator

## Role

Write one complete Markdown system prompt for a downstream LLM that decides
whether candidate concepts belong in a focused SKOS subgraph.

The labeled demonstrations below are the only task-specific evidence. The
downstream classifier receives only the seed-topic labels and candidate
preferred and alternative labels. It receives no path or parent information,
and the downstream traversal may use SKOS relation families not represented in
this small demonstration set.

{LABELED_DEMONSTRATIONS}

## What to infer

Infer the central semantic scope, core in-scope concept families, adjacent
domains, valid and dangerous gateways, and the precision-recall tradeoff for
prune-gated BFS. Use INCLUDE/EXCLUDE contrasts rather than class frequency.
Translate repeated evidence into category-level rules. Do not copy example
labels or invent hidden graph structure or unsupported domain facts.

## Required generated prompt

Return one self-contained Markdown system prompt under 1200 words. Its headings
must explicitly include Role, INCLUDE, EXCLUDE, Decision, and Confidence. It
must say that decisions use the seed and supplied labels, lexical overlap is
not sufficient, broad gateways require care, INCLUDE enables later traversal,
and EXCLUDE can hide deeper concepts. Confidence must mean certainty in the
selected decision, not relevance.

Do not include an Output, Response Format, JSON, or Structured Output section;
the runner enforces the response schema. Do not mention prompt generation,
benchmarks, evaluation splits, papers, or the supplied demonstrations.
"""


class CandidateDecision(BaseModel):
    """One structured LLM decision, keyed by the displayed candidate index."""

    idx: int = Field(description="1-based candidate number")
    classification: Literal["INCLUDE", "EXCLUDE"]
    confidence: float = Field(ge=0.0, le=1.0)


class CandidateBatchResponse(BaseModel):
    evaluations: list[CandidateDecision]


DEEPSEEK_TOOL_SCHEMA = {
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


@dataclass(frozen=True)
class RelationSpec:
    """One directed use of an RDF predicate during traversal."""

    predicate: URIRef
    inverse: bool = False


@dataclass(frozen=True)
class PathStep:
    source: URIRef
    target: URIRef
    relation: RelationSpec


@dataclass(frozen=True)
class RawExample:
    node_reference: str
    decision: str


@dataclass(frozen=True)
class PreparedExample:
    node: URIRef
    decision: str
    seed: URIRef
    path: tuple[PathStep, ...]


@dataclass(frozen=True)
class Candidate:
    node: URIRef
    seed: URIRef
    path: tuple[PathStep, ...]


@dataclass(frozen=True)
class ClassifiedCandidate:
    candidate: Candidate
    classification: str
    confidence: float
    warning: str = ""


@dataclass(frozen=True)
class GraphClassification:
    graph_type: str
    reason: str
    diagnostics: dict


@dataclass
class PreparedRun:
    index: "RdfGraphIndex"
    detected_graph: GraphClassification
    graph_type: str
    graph_type_source: str
    seeds: list[URIRef]
    examples: list[PreparedExample]
    relation_specs: list[RelationSpec]
    relation_source: str
    system_prompt_path: Path | None
    meta_prompt_text: str | None
    meta_prompt_source: str | None
    prompt_generation_input: str | None
    warnings: list[str]


class NodeResolutionError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_decision(value: object) -> str:
    decision = str(value or "").strip().upper()
    mapping = {
        "INCLUDE": "INCLUDE",
        "KEEP": "INCLUDE",
        "1": "INCLUDE",
        "TRUE": "INCLUDE",
        "EXCLUDE": "EXCLUDE",
        "PRUNE": "EXCLUDE",
        "0": "EXCLUDE",
        "FALSE": "EXCLUDE",
    }
    if decision not in mapping:
        raise ValueError(
            f"Unknown decision '{value}'. Expected INCLUDE or EXCLUDE."
        )
    return mapping[decision]


def local_name(uri: URIRef | str) -> str:
    value = str(uri)
    return re.split(r"[/#]", value.rstrip("/#"))[-1]


def language_rank(value: RdfLiteral, language: str) -> tuple[int, str]:
    lang = (value.language or "").casefold()
    wanted = language.casefold()
    if lang == wanted:
        priority = 0
    elif not lang:
        priority = 1
    else:
        priority = 2
    return priority, str(value).casefold()


class RdfGraphIndex:
    """A label-aware, deterministic index over one RDF graph."""

    def __init__(
        self,
        graph: Graph,
        source_path: Path | None = None,
        language: str = "en",
    ):
        self.graph = graph
        self.source_path = Path(source_path) if source_path else None
        self.language = language
        (
            self.preferred_labels,
            self.alternative_labels,
            self.preferred_label_triples,
            self.alternative_label_triples,
        ) = self._build_labels()
        self._label_index = self._build_label_index()
        self._local_name_index = self._build_local_name_index()
        self._namespace_map = {
            str(prefix): str(namespace)
            for prefix, namespace in self.graph.namespaces()
            if prefix is not None
        }
        self._structural_predicates_cache: list[URIRef] | None = None
        self._adjacency_cache: dict[
            tuple[tuple[str, bool], ...],
            dict[URIRef, tuple[tuple[URIRef, RelationSpec], ...]],
        ] = {}

    @classmethod
    def from_path(
        cls,
        path: Path,
        graph_format: str | None = None,
        language: str = "en",
    ) -> "RdfGraphIndex":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Target graph does not exist: {path}")
        graph = Graph()
        try:
            graph.parse(path, format=graph_format)
        except Exception as exc:
            format_hint = graph_format or "automatic format detection"
            raise RuntimeError(
                f"Could not parse RDF graph {path} using {format_hint}: {exc}"
            ) from exc
        if not graph:
            raise ValueError(f"Target graph is empty: {path}")
        return cls(graph, source_path=path, language=language)

    def _literal_values(
        self,
        subject: URIRef,
        predicates: Sequence[URIRef],
    ) -> list[RdfLiteral]:
        values = {
            value
            for predicate in predicates
            for value in self.graph.objects(subject, predicate)
            if isinstance(value, RdfLiteral) and str(value).strip()
        }
        wanted = self.language.casefold()
        preferred_language = {
            value
            for value in values
            if (value.language or "").casefold() == wanted
        }
        untagged = {value for value in values if not value.language}
        selected = preferred_language | untagged
        if not selected:
            selected = values
        return sorted(
            selected,
            key=lambda value: language_rank(value, self.language),
        )

    def _build_labels(
        self,
    ) -> tuple[
        dict[URIRef, str],
        dict[URIRef, list[str]],
        dict[URIRef, tuple[URIRef, RdfLiteral]],
        dict[URIRef, list[tuple[URIRef, RdfLiteral]]],
    ]:
        subjects = {
            subject
            for predicate in LABEL_PREDICATES + ALTERNATIVE_LABEL_PREDICATES
            for subject in self.graph.subjects(predicate, None)
            if isinstance(subject, URIRef)
        }
        preferred: dict[URIRef, str] = {}
        alternatives: dict[URIRef, list[str]] = {}
        preferred_triples: dict[
            URIRef,
            tuple[URIRef, RdfLiteral],
        ] = {}
        alternative_triples: dict[
            URIRef,
            list[tuple[URIRef, RdfLiteral]],
        ] = {}
        for subject in subjects:
            preferred_entry: tuple[URIRef, RdfLiteral] | None = None
            for predicate in LABEL_PREDICATES:
                values = self._literal_values(subject, (predicate,))
                if values:
                    preferred_entry = (predicate, values[0])
                    break
            alternative_entries = [
                (predicate, value)
                for predicate in ALTERNATIVE_LABEL_PREDICATES
                for value in self._literal_values(subject, (predicate,))
            ]
            if preferred_entry is None and alternative_entries:
                preferred_entry = alternative_entries.pop(0)
            if preferred_entry is None:
                continue

            preferred[subject] = str(preferred_entry[1])
            preferred_triples[subject] = preferred_entry
            seen = {preferred[subject].casefold()}
            alternatives[subject] = []
            alternative_triples[subject] = []
            for predicate, value in alternative_entries:
                text = str(value)
                if text.casefold() not in seen:
                    alternatives[subject].append(text)
                    alternative_triples[subject].append((predicate, value))
                    seen.add(text.casefold())
        return (
            preferred,
            alternatives,
            preferred_triples,
            alternative_triples,
        )

    def _build_label_index(self) -> dict[str, list[URIRef]]:
        index: dict[str, list[URIRef]] = defaultdict(list)
        for node, label in self.preferred_labels.items():
            index[label.casefold()].append(node)
            for alternative in self.alternative_labels.get(node, []):
                index[alternative.casefold()].append(node)
        return {
            label: sorted(set(nodes), key=str)
            for label, nodes in index.items()
        }

    def _build_local_name_index(self) -> dict[str, list[URIRef]]:
        index: dict[str, list[URIRef]] = defaultdict(list)
        for node in self.preferred_labels:
            name = local_name(node)
            if name:
                index[name.casefold()].append(node)
        return {
            name: sorted(set(nodes), key=str)
            for name, nodes in index.items()
        }

    def node_exists(self, node: URIRef) -> bool:
        return any(self.graph.triples((node, None, None))) or any(
            self.graph.triples((None, None, node))
        )

    def resolve_node(self, reference: str) -> URIRef:
        value = str(reference or "").strip()
        if not value:
            raise NodeResolutionError("Node reference cannot be empty")

        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1].strip()

        if re.match(r"^https?://", value):
            uri = URIRef(value)
            if not self.node_exists(uri):
                raise NodeResolutionError(f"Node IRI is absent from the graph: {value}")
            self.require_label(uri)
            return uri

        if ":" in value and not re.match(r"^[A-Za-z]:[\\/]", value):
            prefix, suffix = value.split(":", 1)
            namespace = self._namespace_map.get(prefix)
            if namespace:
                uri = URIRef(namespace + suffix)
                if self.node_exists(uri):
                    self.require_label(uri)
                    return uri

        by_local_name = self._local_name_index.get(value.casefold(), [])
        if len(by_local_name) == 1:
            self.require_label(by_local_name[0])
            return by_local_name[0]
        if len(by_local_name) > 1:
            raise NodeResolutionError(
                f"Local identifier '{value}' matches multiple graph nodes; "
                "use a full IRI: "
                + ", ".join(str(node) for node in by_local_name[:5])
            )

        by_label = self._label_index.get(value.casefold(), [])
        if len(by_label) == 1:
            return by_label[0]
        if len(by_label) > 1:
            raise NodeResolutionError(
                f"Label '{value}' is ambiguous; use a full IRI: "
                + ", ".join(str(node) for node in by_label[:5])
            )
        raise NodeResolutionError(
            f"Could not resolve node reference '{value}' by IRI, prefixed name, "
            "local identifier, or label."
        )

    def require_label(self, node: URIRef) -> str:
        label = self.preferred_labels.get(node)
        if not label:
            raise NodeResolutionError(
                f"Node {node} has no usable {self.language!r} or untagged label"
            )
        return label

    def label(self, node: URIRef) -> str:
        return self.preferred_labels.get(node, local_name(node))

    def display_label(self, node: URIRef) -> str:
        preferred = self.label(node)
        alternatives = self.alternative_labels.get(node, [])
        if alternatives:
            return f"{preferred} ({', '.join(alternatives)})"
        return preferred

    def qname(self, uri: URIRef) -> str:
        try:
            return self.graph.namespace_manager.qname(uri)
        except Exception:
            return str(uri)

    def relation_label(self, predicate: URIRef) -> str:
        if predicate == SKOS.broader:
            return "broader"
        if predicate == SKOS.narrower:
            return "narrower"
        if predicate == SKOS.related:
            return "related"

        common = COMMON_RELATION_LABELS.get(str(predicate))
        if common:
            return common
        common = COMMON_RELATION_LABELS.get(local_name(predicate))
        if common:
            return common

        direct_property_prefix = "http://www.wikidata.org/prop/direct/"
        if str(predicate).startswith(direct_property_prefix):
            property_entity = URIRef(
                "http://www.wikidata.org/entity/" + local_name(predicate)
            )
            if property_entity in self.preferred_labels:
                return self.preferred_labels[property_entity]

        if predicate in self.preferred_labels:
            return self.preferred_labels[predicate]
        qname = self.qname(predicate)
        return qname if not qname.startswith("<") else local_name(predicate)

    def relation_text(self, relation: RelationSpec) -> str:
        name = self.relation_label(relation.predicate)
        if not relation.inverse:
            return name
        if relation.predicate == SKOS.broader:
            return "narrower"
        if relation.predicate == SKOS.narrower:
            return "broader"
        if relation.predicate == SKOS.related:
            return "related"
        inverse = COMMON_INVERSE_RELATION_LABELS.get(str(relation.predicate))
        if inverse is None:
            inverse = COMMON_INVERSE_RELATION_LABELS.get(
                local_name(relation.predicate)
            )
        return inverse or f"inverse {name}"

    def relation_token(self, relation: RelationSpec) -> str:
        token = self.qname(relation.predicate)
        if token.startswith("<") and token.endswith(">"):
            token = str(relation.predicate)
        return f"^{token}" if relation.inverse else token

    def path_text(self, path: Sequence[PathStep]) -> str:
        if not path:
            return "(seed node)"
        parts = [self.label(path[0].source)]
        for step in path:
            parts.append(
                f"--{self.relation_text(step.relation)}--> "
                f"{self.label(step.target)}"
            )
        return " ".join(parts)

    def is_structural_triple(
        self,
        subject: object,
        predicate: object,
        obj: object,
    ) -> bool:
        return (
            isinstance(subject, URIRef)
            and isinstance(predicate, URIRef)
            and isinstance(obj, URIRef)
            and predicate not in ANNOTATION_PREDICATES
            and subject in self.preferred_labels
            and obj in self.preferred_labels
        )

    def structural_predicates(self) -> list[URIRef]:
        if self._structural_predicates_cache is None:
            self._structural_predicates_cache = sorted(
                {
                    predicate
                    for subject, predicate, obj in self.graph
                    if self.is_structural_triple(subject, predicate, obj)
                },
                key=str,
            )
        return list(self._structural_predicates_cache)

    def all_structural_relation_specs(self) -> list[RelationSpec]:
        return [
            RelationSpec(predicate, inverse)
            for predicate in self.structural_predicates()
            for inverse in (False, True)
        ]

    @staticmethod
    def _spec_key(specs: Iterable[RelationSpec]) -> tuple[tuple[str, bool], ...]:
        return tuple(sorted({(str(spec.predicate), spec.inverse) for spec in specs}))

    def adjacency(
        self,
        specs: Sequence[RelationSpec],
    ) -> dict[URIRef, tuple[tuple[URIRef, RelationSpec], ...]]:
        key = self._spec_key(specs)
        cached = self._adjacency_cache.get(key)
        if cached is not None:
            return cached

        by_predicate: dict[URIRef, set[bool]] = defaultdict(set)
        for spec in specs:
            by_predicate[spec.predicate].add(spec.inverse)

        adjacency: dict[URIRef, set[tuple[URIRef, RelationSpec]]] = defaultdict(set)
        for predicate, directions in by_predicate.items():
            for subject, obj in self.graph.subject_objects(predicate):
                if not self.is_structural_triple(subject, predicate, obj):
                    continue
                if False in directions:
                    adjacency[subject].add((obj, RelationSpec(predicate, False)))
                if True in directions:
                    adjacency[obj].add((subject, RelationSpec(predicate, True)))

        frozen = {
            node: tuple(
                sorted(
                    neighbors,
                    key=lambda item: (
                        self.label(item[0]).casefold(),
                        str(item[0]),
                        str(item[1].predicate),
                        item[1].inverse,
                    ),
                )
            )
            for node, neighbors in adjacency.items()
        }
        self._adjacency_cache[key] = frozen
        return frozen

    def shortest_path(
        self,
        seeds: Sequence[URIRef],
        target: URIRef,
        specs: Sequence[RelationSpec],
    ) -> tuple[URIRef, tuple[PathStep, ...]] | None:
        sorted_seeds = sorted(set(seeds), key=str)
        if target in sorted_seeds:
            return target, tuple()
        adjacency = self.adjacency(specs)
        queue = deque(sorted_seeds)
        parents: dict[URIRef, tuple[URIRef, RelationSpec] | None] = {
            seed: None for seed in sorted_seeds
        }
        origins = {seed: seed for seed in sorted_seeds}

        while queue:
            current = queue.popleft()
            for neighbor, relation in adjacency.get(current, ()):
                if neighbor in parents:
                    continue
                parents[neighbor] = (current, relation)
                origins[neighbor] = origins[current]
                if neighbor == target:
                    steps: list[PathStep] = []
                    cursor = target
                    while parents[cursor] is not None:
                        parent, edge_relation = parents[cursor]
                        steps.append(PathStep(parent, cursor, edge_relation))
                        cursor = parent
                    steps.reverse()
                    return origins[target], tuple(steps)
                queue.append(neighbor)
        return None

    @staticmethod
    def source_triple(step: PathStep) -> tuple[URIRef, URIRef, URIRef]:
        """Return the source-graph orientation of one traversal step."""

        if step.relation.inverse:
            return step.target, step.relation.predicate, step.source
        return step.source, step.relation.predicate, step.target

    def llm_label_triples(
        self,
        node: URIRef,
        graph_type: str,
        seeds: set[URIRef],
    ) -> list[tuple[URIRef, URIRef, RdfLiteral]]:
        """Return exactly the graph labels exposed for this retained node."""

        preferred = self.preferred_label_triples.get(node)
        if preferred is None:
            raise RuntimeError(f"Included node has no tracked LLM label: {node}")
        triples = [(node, preferred[0], preferred[1])]
        if graph_type == "skos" and node not in seeds:
            triples.extend(
                (node, predicate, value)
                for predicate, value in self.alternative_label_triples.get(
                    node,
                    [],
                )
            )
        return triples

    def traversal_subgraph(
        self,
        included_nodes: Iterable[URIRef],
        traversed_edges: Iterable[tuple[URIRef, URIRef, URIRef]],
        graph_type: str,
        seeds: Sequence[URIRef],
    ) -> tuple[Graph, int, int]:
        """Build the retained traversal edges plus the labels sent to the LLM."""

        included = set(included_nodes)
        seed_set = set(seeds)
        output = Graph()
        for prefix, namespace in self.graph.namespaces():
            output.bind(prefix, namespace)

        retained_edges = set(traversed_edges)
        for subject, predicate, obj in retained_edges:
            if subject not in included or obj not in included:
                raise RuntimeError(
                    "A retained traversal edge refers to a node outside the "
                    "included node set"
                )
            if (subject, predicate, obj) not in self.graph:
                raise RuntimeError(
                    "A retained traversal edge is absent from the source graph: "
                    f"{subject} {predicate} {obj}"
                )
            output.add((subject, predicate, obj))

        label_triples = {
            triple
            for node in included
            for triple in self.llm_label_triples(node, graph_type, seed_set)
        }
        for triple in label_triples:
            output.add(triple)
        return output, len(retained_edges), len(label_triples)


def classify_graph(index: RdfGraphIndex) -> GraphClassification:
    """Conservatively distinguish a SKOS traversal profile from a general KG.

    Generic graphs are labeled SKOS only when nearly all labeled resources are
    ``skos:Concept`` instances and concept-to-concept predicates stay within
    SKOS. NALT Full is an explicit, closed profile: its known taxonomic-rank and
    product predicates are non-traversal data, while extraction topology is
    defined by the SKOS broader, narrower, and related relations. Any unknown
    non-SKOS concept predicate makes even a NALT-scheme graph a KG.
    """

    concept_nodes = {
        node
        for node in index.graph.subjects(RDF.type, SKOS.Concept)
        if isinstance(node, URIRef)
    }
    labeled_nodes = set(index.preferred_labels)
    labeled_concepts = concept_nodes & labeled_nodes
    concept_coverage = (
        len(labeled_concepts) / len(labeled_nodes) if labeled_nodes else 0.0
    )

    concept_predicate_counts: dict[str, int] = defaultdict(int)
    skos_traversal_edges = 0
    for subject, predicate, obj in index.graph:
        if subject not in concept_nodes or obj not in concept_nodes:
            continue
        if predicate == RDF.type:
            continue
        concept_predicate_counts[str(predicate)] += 1
        if predicate in SKOS_TRAVERSAL_PREDICATES:
            skos_traversal_edges += 1

    non_skos_concept_predicates = {
        predicate: count
        for predicate, count in concept_predicate_counts.items()
        if not predicate.startswith(SKOS_NAMESPACE)
    }
    schemes = {
        obj
        for obj in index.graph.objects(None, SKOS.inScheme)
        if isinstance(obj, URIRef)
    }
    nalt_profile = bool(schemes & NALT_SCHEME_IRIS)
    nalt_profile_predicates_valid = bool(
        nalt_profile
        and set(non_skos_concept_predicates)
        <= NALT_NON_TRAVERSAL_CONCEPT_PREDICATES
    )

    diagnostics = {
        "triple_count": len(index.graph),
        "labeled_resource_count": len(labeled_nodes),
        "skos_concept_count": len(concept_nodes),
        "labeled_skos_concept_count": len(labeled_concepts),
        "labeled_concept_coverage": round(concept_coverage, 6),
        "skos_traversal_edge_count": skos_traversal_edges,
        "non_skos_concept_predicates": non_skos_concept_predicates,
        "recognized_nalt_profile": nalt_profile,
        "nalt_profile_predicates_valid": nalt_profile_predicates_valid,
    }

    if (
        nalt_profile
        and nalt_profile_predicates_valid
        and concept_coverage >= 0.95
        and skos_traversal_edges > 0
    ):
        return GraphClassification(
            "skos",
            "recognized USDA NALT SKOS traversal profile",
            diagnostics,
        )

    if (
        concept_coverage >= 0.95
        and skos_traversal_edges > 0
        and not non_skos_concept_predicates
    ):
        return GraphClassification(
            "skos",
            "SKOS concepts dominate and no non-SKOS concept relation is used",
            diagnostics,
        )

    if not concept_nodes:
        reason = "the graph contains no skos:Concept instances"
    elif concept_coverage < 0.95:
        reason = (
            "SKOS concepts do not cover at least 95% of labeled resources"
        )
    elif non_skos_concept_predicates:
        reason = "the graph uses non-SKOS relations between SKOS concepts"
    else:
        reason = "the graph has no SKOS traversal edges"
    return GraphClassification("kg", reason, diagnostics)


def load_examples(path: Path) -> list[RawExample]:
    """Load the deliberately small node-only demonstration format."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Demonstration file does not exist: {path}")

    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"Demonstration CSV has no header: {path}")
            normalized_headers = {header.strip() for header in reader.fieldnames}
            if normalized_headers != {"node", "decision"}:
                raise ValueError(
                    "Demonstration CSV must contain exactly the columns "
                    "'node' and 'decision'"
                )
            rows = list(reader)
    else:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            if set(payload) != {"examples"}:
                raise ValueError(
                    "Demonstration JSON may contain only the top-level key "
                    "'examples'"
                )
            rows = payload["examples"]
        elif isinstance(payload, list):
            rows = payload
        else:
            raise ValueError(
                "Demonstration JSON must be a list or {'examples': [...]}"
            )

    if not isinstance(rows, list) or not rows:
        raise ValueError("At least one labeled demonstration is required")

    examples: list[RawExample] = []
    seen: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or set(row) != {"node", "decision"}:
            raise ValueError(
                f"Demonstration {index} must contain exactly 'node' and "
                "'decision'; paths are reconstructed from the target graph"
            )
        node = str(row["node"] or "").strip()
        if not node:
            raise ValueError(f"Demonstration {index} has an empty node")
        decision = normalize_decision(row["decision"])
        key = node.casefold()
        if key in seen:
            if seen[key] != decision:
                raise ValueError(
                    f"Demonstration node '{node}' has conflicting decisions"
                )
            raise ValueError(f"Duplicate demonstration node: {node}")
        seen[key] = decision
        examples.append(RawExample(node, decision))
    return examples


def expand_relation_iri(index: RdfGraphIndex, value: str) -> URIRef:
    token = value.strip()
    if token.startswith("<") and token.endswith(">"):
        token = token[1:-1].strip()
    if re.match(r"^https?://", token):
        return URIRef(token)
    if ":" in token:
        prefix, suffix = token.split(":", 1)
        namespace = index._namespace_map.get(prefix)
        if namespace:
            return URIRef(namespace + suffix)
    matches = [
        predicate
        for predicate in index.structural_predicates()
        if local_name(predicate).casefold() == token.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Relation '{value}' is ambiguous; use a full IRI: "
            + ", ".join(str(match) for match in matches)
        )
    raise ValueError(
        f"Could not resolve relation '{value}' as an IRI, prefixed name, or "
        "unique local name"
    )


def parse_relation_specs(
    index: RdfGraphIndex,
    values: Sequence[str],
    graph_type: str,
) -> list[RelationSpec]:
    specs: set[RelationSpec] = set()
    for raw_value in values:
        value = raw_value.strip()
        inverse = False
        if value.startswith("^"):
            inverse = True
            value = value[1:].strip()
        elif value.startswith("(-)"):
            inverse = True
            value = value[3:].strip()
        predicate = expand_relation_iri(index, value)
        if predicate not in index.structural_predicates():
            raise ValueError(
                f"Allowed relation {predicate} has no traversable labeled-node "
                "edge in the target graph"
            )
        if graph_type == "skos":
            if predicate not in SKOS_TRAVERSAL_PREDICATES:
                raise ValueError(
                    "The SKOS adapter accepts only skos:broader, "
                    "skos:narrower, and skos:related. Use --graph-type kg "
                    "for advanced relations."
                )
            specs.add(RelationSpec(predicate, False))
            specs.add(RelationSpec(predicate, True))
        else:
            specs.add(RelationSpec(predicate, inverse))
    return sorted(specs, key=lambda spec: (str(spec.predicate), spec.inverse))


def skos_candidate_relation_specs() -> list[RelationSpec]:
    return [
        RelationSpec(predicate, inverse)
        for predicate in SKOS_TRAVERSAL_PREDICATES
        for inverse in (False, True)
    ]


def resolve_allowed_relations(
    index: RdfGraphIndex,
    graph_type: str,
    relation_overrides: Sequence[str],
) -> tuple[list[RelationSpec], str, list[str]]:
    """Resolve the final relation list before any path reconstruction."""

    if relation_overrides:
        return (
            parse_relation_specs(index, relation_overrides, graph_type),
            "user_override",
            [],
        )
    if graph_type == "skos":
        return skos_candidate_relation_specs(), "skos_default", []

    specs = index.all_structural_relation_specs()
    if not specs:
        raise ValueError(
            "The KG has no relation between labeled URI nodes. Provide labels "
            "or an explicit --allowed-relation."
        )
    return specs, "all_labeled_node_relations", []


def prepare_examples_and_relations(
    index: RdfGraphIndex,
    graph_type: str,
    seeds: Sequence[URIRef],
    raw_examples: Sequence[RawExample],
    relation_overrides: Sequence[str],
) -> tuple[list[PreparedExample], list[RelationSpec], str, list[str]]:
    allowed, relation_source, warnings = resolve_allowed_relations(
        index,
        graph_type,
        relation_overrides,
    )

    prepared: list[PreparedExample] = []
    seed_set = set(seeds)
    for raw in raw_examples:
        node = index.resolve_node(raw.node_reference)
        if node in seed_set:
            raise ValueError(
                f"Demonstration node {raw.node_reference!r} is also a seed; "
                "seed nodes are mandatory and are never classified"
            )
        result = index.shortest_path(seeds, node, allowed)
        if result is None:
            raise ValueError(
                f"Demonstration node '{raw.node_reference}' is not reachable "
                "from any seed using the effective allowed-relation list"
            )
        seed, path = result
        if not path:
            raise ValueError(
                f"Demonstration node '{raw.node_reference}' has no traversal path"
            )
        prepared.append(PreparedExample(node, raw.decision, seed, path))
    return prepared, allowed, relation_source, warnings


def default_relations_without_examples(
    index: RdfGraphIndex,
    graph_type: str,
    relation_overrides: Sequence[str],
) -> tuple[list[RelationSpec], str, list[str]]:
    return resolve_allowed_relations(
        index,
        graph_type,
        relation_overrides,
    )


def format_demonstrations(
    index: RdfGraphIndex,
    graph_type: str,
    seeds: Sequence[URIRef],
    examples: Sequence[PreparedExample],
) -> str:
    if graph_type == "skos":
        seed_topic = "; ".join(index.label(seed) for seed in seeds)
        lines = []
        for example in examples:
            lines.append(f"- Seed Topic: {seed_topic}")
            lines.append(
                "  Candidate: candidate="
                f"{index.display_label(example.node)} | "
                f"decision={example.decision}"
            )
        return "\n".join(lines)

    seed_set = "; ".join(index.label(seed) for seed in seeds)
    lines = []
    for example in examples:
        lines.append(f"- Subgraph Seed Set: {seed_set}")
        lines.append(
            "  Candidate: candidate="
            f"{index.label(example.node)} | "
            f"path={index.path_text(example.path)} | "
            f"decision={example.decision}"
        )
    return "\n".join(lines)


def render_meta_prompt(template: str, demonstrations: str) -> str:
    placeholders = ("{LABELED_DEMONSTRATIONS}", "{FEW_SHOT_EXAMPLES}")
    present = [placeholder for placeholder in placeholders if placeholder in template]
    if not present:
        raise ValueError(
            "Meta-prompt must contain {LABELED_DEMONSTRATIONS} or "
            "{FEW_SHOT_EXAMPLES}"
        )
    rendered = template
    for placeholder in present:
        rendered = rendered.replace(placeholder, demonstrations)
    return rendered


def validate_generated_prompt(text: str) -> None:
    if len(text.strip()) < 500:
        raise ValueError("Generated system prompt is unexpectedly short")
    required = ("role", "include", "exclude", "decision", "confidence")
    missing = [
        heading
        for heading in required
        if not re.search(
            rf"^##?\s+.*\b{heading}\b",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    ]
    if missing:
        raise ValueError(
            "Generated system prompt lacks required heading(s): "
            + ", ".join(missing)
        )


def prepare_run(args: argparse.Namespace) -> PreparedRun:
    index = RdfGraphIndex.from_path(
        args.graph,
        graph_format=args.graph_format,
        language=args.language,
    )
    detected = classify_graph(index)
    if args.graph_type == "auto":
        graph_type = detected.graph_type
        graph_type_source = "classifier"
    else:
        graph_type = args.graph_type
        graph_type_source = "user_override"

    warnings: list[str] = []
    if graph_type_source == "user_override" and graph_type != detected.graph_type:
        warnings.append(
            f"Graph type override '{graph_type}' differs from classifier result "
            f"'{detected.graph_type}': {detected.reason}."
        )

    seeds: list[URIRef] = []
    for reference in args.seed:
        seed = index.resolve_node(reference)
        if seed not in seeds:
            seeds.append(seed)
    if not seeds:
        raise ValueError("At least one seed node is required")

    if args.examples:
        raw_examples = load_examples(args.examples)
        decisions = {example.decision for example in raw_examples}
        if not args.system_prompt and decisions != {"INCLUDE", "EXCLUDE"}:
            raise ValueError(
                "Meta-prompt generation requires at least one INCLUDE and one "
                "EXCLUDE demonstration"
            )
        examples, relation_specs, relation_source, relation_warnings = (
            prepare_examples_and_relations(
                index,
                graph_type,
                seeds,
                raw_examples,
                args.allowed_relation,
            )
        )
        warnings.extend(relation_warnings)
    else:
        examples = []
        relation_specs, relation_source, relation_warnings = (
            default_relations_without_examples(
                index,
                graph_type,
                args.allowed_relation,
            )
        )
        warnings.extend(relation_warnings)

    if args.system_prompt:
        system_prompt_path = Path(args.system_prompt)
        if not system_prompt_path.is_file():
            raise FileNotFoundError(
                f"System prompt does not exist: {system_prompt_path}"
            )
        meta_prompt_text = None
        meta_prompt_source = None
        generation_input = None
    else:
        if not examples:
            raise ValueError(
                "--examples is mandatory when --system-prompt is not supplied"
            )
        system_prompt_path = None
        if args.meta_prompt:
            meta_prompt_path = Path(args.meta_prompt)
            if not meta_prompt_path.is_file():
                raise FileNotFoundError(
                    f"Meta-prompt does not exist: {meta_prompt_path}"
                )
            meta_prompt_text = meta_prompt_path.read_text(encoding="utf-8")
            meta_prompt_source = str(meta_prompt_path)
        else:
            meta_prompt_text = (
                SKOS_META_PROMPT if graph_type == "skos" else KG_META_PROMPT
            )
            meta_prompt_source = f"built_in_{graph_type}"
        demonstrations = format_demonstrations(
            index,
            graph_type,
            seeds,
            examples,
        )
        generation_input = render_meta_prompt(meta_prompt_text, demonstrations)

    return PreparedRun(
        index=index,
        detected_graph=detected,
        graph_type=graph_type,
        graph_type_source=graph_type_source,
        seeds=seeds,
        examples=examples,
        relation_specs=relation_specs,
        relation_source=relation_source,
        system_prompt_path=system_prompt_path,
        meta_prompt_text=meta_prompt_text,
        meta_prompt_source=meta_prompt_source,
        prompt_generation_input=generation_input,
        warnings=warnings,
    )


def load_api_key(provider: str, config_path: Path) -> str:
    settings = {
        "openai": ("OPENAI_API_KEY", "openai_api_key"),
        "gemini": ("GEMINI_API_KEY", "gemini_api_key"),
        "deepseek": ("DEEPSEEK_API_KEY", "deepseek_api_key"),
        "openrouter": ("OPENROUTER_API_KEY", "openrouter_api_key"),
    }
    if provider not in settings:
        raise ValueError(f"Unsupported LLM provider: {provider}")
    environment_name, config_name = settings[provider]
    if provider == "gemini":
        key = os.environ.get(environment_name) or os.environ.get("GOOGLE_API_KEY")
    else:
        key = os.environ.get(environment_name)
    if key:
        return key

    candidates = [Path(config_path), REPO_ROOT / "config", Path.cwd() / "config"]
    checked: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in checked or not candidate.is_file():
            continue
        checked.add(resolved)
        with candidate.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        key = payload.get(config_name)
        if key:
            return str(key)
    raise RuntimeError(
        f"Set {environment_name} or provide a config file containing "
        f"'{config_name}'"
    )


def resolve_provider(provider: str, model: str | None) -> str:
    if provider != "auto":
        return provider
    value = str(model or "")
    if value.startswith("deepseek-"):
        return "deepseek"
    if value.startswith("gemini-"):
        return "gemini"
    if value.startswith("google/") or "/" in value:
        return "openrouter"
    return "openai"


def resolve_model(provider: str, model: str | None) -> str:
    return model or DEFAULT_MODELS[provider]


def resolve_prompt_reasoning(
    provider: str,
    model: str,
    requested: str,
) -> str | None:
    if requested == "none":
        return None
    if requested != "auto":
        value = requested
    elif provider == "openai":
        value = "xhigh" if str(model).startswith("gpt-5.4") else "high"
    elif provider == "gemini":
        value = "high"
    elif provider == "deepseek":
        value = "max"
    else:
        value = "high"

    accepted = {
        "openai": {"low", "medium", "high", "xhigh"},
        "gemini": {"low", "medium", "high"},
        "deepseek": {"low", "medium", "high", "max"},
        "openrouter": {"low", "medium", "high"},
    }[provider]
    if value not in accepted:
        raise ValueError(
            f"Prompt reasoning '{value}' is not valid for {provider}; "
            f"choose one of: {', '.join(sorted(accepted))}, none, auto"
        )
    return value


def gemini_call_with_retries(
    callable_: Callable[[], object],
    retries: int = GEMINI_RETRIES,
) -> object:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return callable_()
        except Exception as exc:
            last_error = exc
            status_code = getattr(exc, "status_code", None)
            message = str(exc).casefold()
            retryable = (
                status_code in {429, 500, 502, 503, 504}
                or "unavailable" in message
                or "rate" in message
                or "temporarily" in message
                or "deadline" in message
            )
            if not retryable or attempt == retries:
                raise
            time.sleep(min(2 ** (attempt - 1), 30))
    if last_error:
        raise last_error
    raise RuntimeError("Gemini retry loop ended without a response")


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str = OPENROUTER_CHAT_COMPLETIONS_URL,
    ):
        self.api_key = api_key
        self.endpoint = endpoint

    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        reasoning: dict | None = None,
    ) -> dict:
        payload: dict = {"model": model, "messages": messages}
        if reasoning is not None:
            payload["reasoning"] = reasoning
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenRouter request failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
        return json.loads(body)


def create_client(provider: str, config_path: Path) -> object:
    api_key = load_api_key(provider, config_path)
    if provider == "gemini":
        from google import genai

        return genai.Client(api_key=api_key)
    if provider == "deepseek":
        from openai import OpenAI

        return OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BETA_BASE_URL,
            timeout=180.0,
            max_retries=2,
        )
    if provider == "openrouter":
        return OpenRouterClient(api_key)
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def extract_openai_response_text(response: object) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text).strip()
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks).strip()


def openrouter_message_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter response contains no choices")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts).strip()
    return ""


def generate_prompt_once(
    client: object,
    provider: str,
    model: str,
    content: str,
    reasoning: str | None,
) -> str:
    system_instruction = (
        "Generate one rigorous Markdown system prompt for LLM-gated "
        "sub-knowledge-graph extraction."
    )
    if provider == "gemini":
        from google.genai import types

        config: dict = {"system_instruction": system_instruction}
        if reasoning:
            config["thinking_config"] = types.ThinkingConfig(
                thinking_level=reasoning
            )
        response = gemini_call_with_retries(
            lambda: client.models.generate_content(
                model=model,
                contents=content,
                config=types.GenerateContentConfig(**config),
            )
        )
        return str(getattr(response, "text", "") or "").strip()

    if provider == "deepseek":
        kwargs: dict = {}
        if reasoning:
            kwargs["reasoning_effort"] = reasoning
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": content},
            ],
            stream=False,
            **kwargs,
        )
        return str(response.choices[0].message.content or "").strip()

    if provider == "openrouter":
        reasoning_payload = (
            {"enabled": True, "effort": reasoning} if reasoning else None
        )
        response = client.chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": content},
            ],
            reasoning=reasoning_payload,
        )
        return openrouter_message_text(response)

    kwargs = {}
    if reasoning:
        kwargs["reasoning"] = {"effort": reasoning}
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": content},
        ],
        **kwargs,
    )
    if getattr(response, "status", None) == "incomplete":
        raise RuntimeError(
            "OpenAI prompt-generation response was incomplete: "
            f"{getattr(response, 'incomplete_details', None)}"
        )
    return extract_openai_response_text(response)


def generate_system_prompt(
    client: object,
    provider: str,
    model: str,
    content: str,
    reasoning: str | None,
) -> str:
    retry_content = content
    last_error: Exception | None = None
    for attempt in range(1, 4):
        generated = generate_prompt_once(
            client,
            provider,
            model,
            retry_content,
            reasoning,
        )
        if not generated:
            last_error = RuntimeError("Prompt-generation model returned no text")
        else:
            try:
                validate_generated_prompt(generated)
                return generated
            except ValueError as exc:
                last_error = exc
        if attempt < 3:
            retry_content = (
                content
                + "\n\nRegenerate the prompt. The previous response failed "
                + f"validation: {last_error}. Include explicit Role, INCLUDE, "
                "EXCLUDE, Decision, and Confidence headings without copying "
                "demonstration labels or paths."
            )
    raise RuntimeError("Could not generate a valid system prompt") from last_error


def parse_json_object(text: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Structured model response must be a JSON object")
    return payload


def validate_batch_response(
    parsed: CandidateBatchResponse,
    expected_count: int,
) -> CandidateBatchResponse:
    indices = [decision.idx for decision in parsed.evaluations]
    expected = list(range(1, expected_count + 1))
    if sorted(indices) != expected or len(indices) != len(set(indices)):
        raise ValueError(
            "The model must return exactly one decision for every candidate; "
            f"received indices {indices}, expected {expected}"
        )
    return parsed


def deepseek_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": DEEPSEEK_TOOL_NAME,
            "description": (
                "Return INCLUDE or EXCLUDE decisions and confidence scores for "
                "all numbered subgraph candidates."
            ),
            "parameters": DEEPSEEK_TOOL_SCHEMA,
            "strict": True,
        },
    }


def build_candidate_user_message(
    index: RdfGraphIndex,
    graph_type: str,
    seeds: Sequence[URIRef],
    batch: Sequence[Candidate],
) -> str:
    lines = [
        f"Classify exactly {len(batch)} candidates as INCLUDE or EXCLUDE.",
        "Return one evaluation for each numbered candidate and use its number as idx.",
        "Each evaluation contains only idx, classification, and confidence.",
        "",
    ]
    if graph_type == "skos":
        seed_topic = "; ".join(index.label(seed) for seed in seeds)
        lines.append(f"Seed Topic: {seed_topic}")
        lines.append("")
        for number, candidate in enumerate(batch, start=1):
            lines.append(
                f"{number}. Candidate: {index.display_label(candidate.node)}"
            )
    else:
        seed_set = "; ".join(index.label(seed) for seed in seeds)
        lines.append(f"Subgraph Seed Set: {seed_set}")
        lines.append("")
        for number, candidate in enumerate(batch, start=1):
            lines.append(f"{number}. Candidate: {index.label(candidate.node)}")
            lines.append(f"   Path: {index.path_text(candidate.path)}")
    return "\n".join(lines)


def classify_batch_with_provider(
    client: object,
    provider: str,
    model: str,
    system_prompt: str,
    user_message: str,
    expected_count: int,
) -> CandidateBatchResponse:
    if provider == "gemini":
        from google.genai import types

        response = gemini_call_with_retries(
            lambda: client.models.generate_content(
                model=model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=CandidateBatchResponse,
                ),
            )
        )
        parsed = CandidateBatchResponse.model_validate_json(response.text)
        return validate_batch_response(parsed, expected_count)

    if provider == "deepseek":
        last_error: Exception | None = None
        for _attempt in range(1, DEEPSEEK_PROTOCOL_RETRIES + 1):
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                tools=[deepseek_tool()],
                stream=False,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                last_error = RuntimeError(
                    "DeepSeek response omitted the structured tool call"
                )
                continue
            try:
                parsed = CandidateBatchResponse.model_validate_json(
                    tool_calls[0].function.arguments
                )
                return validate_batch_response(parsed, expected_count)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(
            "DeepSeek did not return valid structured candidate decisions"
        ) from last_error

    if provider == "openrouter":
        json_instruction = (
            "\n\nReturn only JSON in this shape: "
            '{"evaluations":[{"idx":1,"classification":"INCLUDE",'
            '"confidence":0.0}]}.'
        )
        payload = client.chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message + json_instruction},
            ],
        )
        parsed = CandidateBatchResponse.model_validate(
            parse_json_object(openrouter_message_text(payload))
        )
        return validate_batch_response(parsed, expected_count)

    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        text_format=CandidateBatchResponse,
    )
    return validate_batch_response(response.output_parsed, expected_count)


def make_llm_classifier(
    index: RdfGraphIndex,
    graph_type: str,
    seeds: Sequence[URIRef],
    client: object,
    provider: str,
    model: str,
    system_prompt: str,
    batch_size: int,
    workers: int,
) -> Callable[[Sequence[Candidate]], list[ClassifiedCandidate]]:
    def classify(candidates: Sequence[Candidate]) -> list[ClassifiedCandidate]:
        batches = [
            list(candidates[start : start + batch_size])
            for start in range(0, len(candidates), batch_size)
        ]
        if not batches:
            return []

        def run_batch(batch: Sequence[Candidate]) -> list[ClassifiedCandidate]:
            message = build_candidate_user_message(
                index,
                graph_type,
                seeds,
                batch,
            )
            parsed = classify_batch_with_provider(
                client,
                provider,
                model,
                system_prompt,
                message,
                len(batch),
            )
            by_index = {decision.idx: decision for decision in parsed.evaluations}
            return [
                ClassifiedCandidate(
                    candidate=candidate,
                    classification=by_index[number].classification,
                    confidence=by_index[number].confidence,
                )
                for number, candidate in enumerate(batch, start=1)
            ]

        if len(batches) == 1 or workers == 1:
            return [item for batch in batches for item in run_batch(batch)]

        results: dict[int, list[ClassifiedCandidate]] = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as executor:
            future_to_index = {
                executor.submit(run_batch, batch): batch_index
                for batch_index, batch in enumerate(batches)
            }
            for future in as_completed(future_to_index):
                results[future_to_index[future]] = future.result()
        return [
            item
            for batch_index in range(len(batches))
            for item in results[batch_index]
        ]

    return classify


def candidate_sort_key(index: RdfGraphIndex, candidate: Candidate) -> tuple:
    return (
        len(candidate.path),
        index.label(candidate.node).casefold(),
        str(candidate.node),
        str(candidate.seed),
        index.path_text(candidate.path),
    )


def select_candidate_path(
    index: RdfGraphIndex,
    existing: Candidate | None,
    proposed: Candidate,
) -> Candidate:
    if existing is None:
        return proposed
    return min((existing, proposed), key=lambda value: candidate_sort_key(index, value))


def run_gated_bfs(
    index: RdfGraphIndex,
    seeds: Sequence[URIRef],
    relation_specs: Sequence[RelationSpec],
    classify: Callable[[Sequence[Candidate]], list[ClassifiedCandidate]],
    max_depth: int,
    progress: bool = True,
) -> dict:
    """Run deterministic breadth-first traversal gated by classifier decisions."""

    adjacency = index.adjacency(relation_specs)
    visited: dict[URIRef, dict] = {}
    included: dict[URIRef, dict] = {}
    excluded: dict[URIRef, dict] = {}
    retained_traversal_edges: set[tuple[URIRef, URIRef, URIRef]] = set()
    seed_set = set(seeds)
    for seed in seeds:
        row = {
            "node": seed,
            "seed": seed,
            "classification": "SEED",
            "confidence": 1.0,
            "depth": 0,
            "path": tuple(),
            "warning": "",
        }
        visited[seed] = row
        included[seed] = row

    frontier: dict[URIRef, Candidate] = {}
    for seed in sorted(seed_set, key=str):
        for neighbor, relation in adjacency.get(seed, ()):
            if neighbor in seed_set:
                continue
            candidate = Candidate(
                neighbor,
                seed,
                (PathStep(seed, neighbor, relation),),
            )
            frontier[neighbor] = select_candidate_path(
                index,
                frontier.get(neighbor),
                candidate,
            )

    iterations: list[dict] = []
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        candidates = sorted(
            (
                candidate
                for node, candidate in frontier.items()
                if node not in visited
            ),
            key=lambda value: candidate_sort_key(index, value),
        )
        if not candidates:
            break
        classified = classify(candidates)
        if len(classified) != len(candidates):
            raise RuntimeError(
                "Classifier returned a different number of decisions than candidates"
            )

        included_this_level: list[Candidate] = []
        for result in classified:
            candidate = result.candidate
            classification = result.classification.upper()
            if classification not in {"INCLUDE", "EXCLUDE"}:
                raise RuntimeError(
                    f"Classifier returned invalid decision: {classification}"
                )
            row = {
                "node": candidate.node,
                "seed": candidate.seed,
                "classification": classification,
                "confidence": float(result.confidence),
                "depth": len(candidate.path),
                "path": candidate.path,
                "warning": result.warning,
            }
            visited[candidate.node] = row
            if classification == "INCLUDE":
                included[candidate.node] = row
                included_this_level.append(candidate)
                retained_traversal_edges.update(
                    index.source_triple(step) for step in candidate.path
                )
            else:
                excluded[candidate.node] = row

        iteration = {
            "depth": depth,
            "evaluated": len(candidates),
            "included": len(included_this_level),
            "excluded": len(candidates) - len(included_this_level),
        }
        iterations.append(iteration)
        if progress:
            print(
                f"Depth {depth}: evaluated={iteration['evaluated']} "
                f"included={iteration['included']} "
                f"excluded={iteration['excluded']}",
                flush=True,
            )
        if not included_this_level:
            break

        next_frontier: dict[URIRef, Candidate] = {}
        for parent in included_this_level:
            for neighbor, relation in adjacency.get(parent.node, ()):
                if neighbor in visited or neighbor in seed_set:
                    continue
                proposed = Candidate(
                    node=neighbor,
                    seed=parent.seed,
                    path=parent.path
                    + (PathStep(parent.node, neighbor, relation),),
                )
                next_frontier[neighbor] = select_candidate_path(
                    index,
                    next_frontier.get(neighbor),
                    proposed,
                )
        frontier = next_frontier

    return {
        "visited": visited,
        "included": included,
        "excluded": excluded,
        "traversed_edges": retained_traversal_edges,
        "iterations": iterations,
        "stopped_at_depth_limit": bool(frontier and depth >= max_depth),
    }


def serialize_relation_specs(
    index: RdfGraphIndex,
    specs: Sequence[RelationSpec],
) -> list[dict]:
    return [
        {
            "predicate": str(spec.predicate),
            "qname": index.qname(spec.predicate),
            "label": index.relation_label(spec.predicate),
            "direction": "inverse" if spec.inverse else "forward",
            "token": index.relation_token(spec),
        }
        for spec in sorted(specs, key=lambda value: (str(value.predicate), value.inverse))
    ]


def prepared_run_summary(
    prepared: PreparedRun,
    args: argparse.Namespace,
    provider: str,
    model: str,
    reasoning: str | None,
) -> dict:
    index = prepared.index
    return {
        "mode": "dry_run" if args.dry_run else "extraction",
        "llm_calls_made": False if args.dry_run else None,
        "graph": str(Path(args.graph).resolve()),
        "graph_sha256": file_sha256(args.graph),
        "detected_graph_type": prepared.detected_graph.graph_type,
        "effective_graph_type": prepared.graph_type,
        "graph_type_source": prepared.graph_type_source,
        "graph_classification_reason": prepared.detected_graph.reason,
        "graph_diagnostics": prepared.detected_graph.diagnostics,
        "seeds": [
            {
                "reference": reference,
                "uri": str(seed),
                "label": index.label(seed),
            }
            for reference, seed in zip(args.seed, prepared.seeds)
        ],
        "demonstrations": [
            {
                "node": str(example.node),
                "label": index.label(example.node),
                "decision": example.decision,
                "nearest_seed": str(example.seed),
                "path_depth": len(example.path),
                "reconstructed_path": index.path_text(example.path),
            }
            for example in prepared.examples
        ],
        "examples_file": str(Path(args.examples).resolve()) if args.examples else None,
        "examples_sha256": file_sha256(args.examples) if args.examples else None,
        "allowed_relation_source": prepared.relation_source,
        "allowed_relations": serialize_relation_specs(
            index,
            prepared.relation_specs,
        ),
        "system_prompt_source": (
            str(prepared.system_prompt_path.resolve())
            if prepared.system_prompt_path
            else "generated_from_meta_prompt"
        ),
        "meta_prompt_source": prepared.meta_prompt_source,
        "prompt_generation_input_sha256": (
            text_sha256(prepared.prompt_generation_input)
            if prepared.prompt_generation_input
            else None
        ),
        "provider": provider,
        "model": model,
        "prompt_generation_reasoning": reasoning,
        "batch_size": args.batch_size,
        "workers": args.workers,
        "max_depth": args.max_depth,
        "warnings": prepared.warnings,
    }


def output_row(index: RdfGraphIndex, row: dict) -> dict:
    path = row["path"]
    return {
        "node": str(row["node"]),
        "label": index.label(row["node"]),
        "classification": row["classification"],
        "confidence": row["confidence"],
        "seed": str(row["seed"]),
        "seed_label": index.label(row["seed"]),
        "depth": row["depth"],
        "path": index.path_text(path),
        "warning": row.get("warning", ""),
    }


def validate_output_dir(path: Path, overwrite: bool) -> None:
    path = Path(path)
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {path}")
    existing = [path / name for name in OUTPUT_FILENAMES.values() if (path / name).exists()]
    unknown = []
    if path.exists():
        known_names = set(OUTPUT_FILENAMES.values())
        unknown = [child for child in path.iterdir() if child.name not in known_names]
    if (existing or unknown) and not overwrite:
        raise FileExistsError(
            f"Output directory is not empty: {path}. Pass --overwrite to "
            "replace recognized unified-extractor artifacts."
        )
    if unknown and overwrite:
        raise RuntimeError(
            "Refusing to overwrite a directory containing unrecognized files: "
            + ", ".join(str(item) for item in unknown)
        )


def initialize_output_dir(path: Path, overwrite: bool) -> None:
    path = Path(path)
    validate_output_dir(path, overwrite)
    existing = [path / name for name in OUTPUT_FILENAMES.values() if (path / name).exists()]
    path.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for artifact in existing:
            artifact.unlink()


def write_rows(path: Path, index: RdfGraphIndex, rows: Sequence[dict]) -> None:
    fieldnames = [
        "node",
        "label",
        "classification",
        "confidence",
        "seed",
        "seed_label",
        "depth",
        "path",
        "warning",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(output_row(index, row))


def save_results(
    prepared: PreparedRun,
    args: argparse.Namespace,
    system_prompt: str,
    provider: str,
    model: str,
    reasoning: str | None,
    traversal: dict,
) -> dict[str, Path]:
    output_dir = Path(args.output_dir)
    initialize_output_dir(output_dir, args.overwrite)
    paths = {
        name: output_dir / filename
        for name, filename in OUTPUT_FILENAMES.items()
    }
    index = prepared.index

    sort_key = lambda row: (
        row["depth"],
        index.label(row["node"]).casefold(),
        str(row["node"]),
    )
    visited_rows = sorted(traversal["visited"].values(), key=sort_key)
    included_rows = sorted(traversal["included"].values(), key=sort_key)
    excluded_rows = sorted(traversal["excluded"].values(), key=sort_key)
    write_rows(paths["decisions"], index, visited_rows)
    write_rows(paths["visited"], index, visited_rows)
    write_rows(paths["included"], index, included_rows)
    write_rows(paths["excluded"], index, excluded_rows)

    subgraph, traversal_edge_count, label_triple_count = (
        index.traversal_subgraph(
            traversal["included"].keys(),
            traversal["traversed_edges"],
            prepared.graph_type,
            prepared.seeds,
        )
    )
    subgraph.serialize(destination=paths["subgraph"], format="turtle")
    paths["prompt"].write_text(system_prompt.rstrip() + "\n", encoding="utf-8")

    manifest = prepared_run_summary(prepared, args, provider, model, reasoning)
    extraction_decision_count = sum(
        iteration["evaluated"] for iteration in traversal["iterations"]
    )
    manifest.update(
        {
            "mode": "extraction",
            "llm_calls_made": bool(
                prepared.system_prompt_path is None
                or extraction_decision_count > 0
            ),
            "created_at": utc_now_iso(),
            "system_prompt_sha256": text_sha256(system_prompt),
            "visited_count": len(visited_rows),
            "included_count": len(included_rows),
            "excluded_count": len(excluded_rows),
            "retained_traversal_edge_count": traversal_edge_count,
            "label_triple_count": label_triple_count,
            "subgraph_triple_count": len(subgraph),
            "iterations": traversal["iterations"],
            "stopped_at_depth_limit": traversal["stopped_at_depth_limit"],
            "output_files": {
                name: str(path.resolve()) for name, path in paths.items()
            },
        }
    )
    paths["manifest"].write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return paths


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract a topic-specific RDF subgraph using one unified "
            "meta-prompted, LLM-gated BFS pipeline."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--graph",
        type=Path,
        required=True,
        help="RDF target graph (for example Turtle, RDF/XML, or N-Triples).",
    )
    parser.add_argument(
        "--graph-format",
        default=None,
        help="Optional rdflib parser format; file-extension detection is the default.",
    )
    parser.add_argument(
        "--graph-type",
        choices=("auto", "kg", "skos"),
        default="auto",
        help="Override the conservative automatic graph classifier.",
    )
    parser.add_argument(
        "--seed",
        action="append",
        required=True,
        help=(
            "Mandatory seed node as a full IRI, prefixed name, unique local "
            "identifier, or unambiguous label. Repeat for multiple seeds."
        ),
    )
    parser.add_argument(
        "--examples",
        type=Path,
        default=None,
        help=(
            "JSON or CSV demonstrations containing only node and decision. "
            "Mandatory unless --system-prompt is supplied."
        ),
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        default=None,
        help=(
            "Use this system prompt instead of generating one. Demonstrations "
            "then become optional."
        ),
    )
    parser.add_argument(
        "--meta-prompt",
        type=Path,
        default=None,
        help=(
            "Optional meta-prompt override containing "
            "{LABELED_DEMONSTRATIONS} or {FEW_SHOT_EXAMPLES}."
        ),
    )
    parser.add_argument(
        "--allowed-relation",
        action="append",
        default=[],
        help=(
            "Optional allowed-relation override as an IRI, prefixed name, or "
            "local name. Prefix with ^ (or legacy (-)) for inverse KG "
            "traversal. Repeat as needed. Without overrides, SKOS uses its "
            "three traversal predicates and KG uses every labeled URI-to-URI "
            "relation in both directions."
        ),
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Preferred graph-label language, with untagged/other fallback.",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "openai", "gemini", "deepseek", "openrouter"),
        default="auto",
        help="LLM provider; auto infers it from a supplied model name.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Provider model; otherwise the provider-specific default is used.",
    )
    parser.add_argument(
        "--prompt-reasoning",
        choices=("auto", "none", "low", "medium", "high", "xhigh", "max"),
        default="auto",
        help="Reasoning setting for prompt generation only.",
    )
    parser.add_argument("--batch-size", type=positive_int, default=20)
    parser.add_argument("--workers", type=positive_int, default=5)
    parser.add_argument("--max-depth", type=positive_int, default=20)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Ignored JSON file containing provider API keys.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Canonical output directory for one extraction.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace recognized artifacts in the output directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Prepare and print the complete graph/relation/prompt plan without "
            "creating an LLM client, making API calls, or writing results."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> dict | None:
    if not args.system_prompt and not args.examples:
        raise ValueError(
            "--examples is mandatory unless --system-prompt is supplied"
        )
    if args.system_prompt and args.meta_prompt:
        raise ValueError(
            "--meta-prompt cannot be combined with --system-prompt because no "
            "prompt generation occurs"
        )

    provider = resolve_provider(args.provider, args.model)
    model = resolve_model(provider, args.model)
    reasoning = (
        None
        if args.system_prompt
        else resolve_prompt_reasoning(
            provider,
            model,
            args.prompt_reasoning,
        )
    )

    print(f"Loading target graph: {args.graph}", flush=True)
    prepared = prepare_run(args)
    summary = prepared_run_summary(prepared, args, provider, model, reasoning)
    if args.dry_run:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return summary

    validate_output_dir(args.output_dir, args.overwrite)
    client = create_client(provider, args.config)
    if prepared.system_prompt_path:
        system_prompt = prepared.system_prompt_path.read_text(encoding="utf-8")
        if not system_prompt.strip():
            raise ValueError("The supplied system prompt is empty")
    else:
        print(
            f"Generating the {prepared.graph_type} system prompt with "
            f"{model} ({provider}) ...",
            flush=True,
        )
        system_prompt = generate_system_prompt(
            client,
            provider,
            model,
            prepared.prompt_generation_input or "",
            reasoning,
        )

    print("Starting LLM-gated BFS extraction", flush=True)
    classifier = make_llm_classifier(
        prepared.index,
        prepared.graph_type,
        prepared.seeds,
        client,
        provider,
        model,
        system_prompt,
        args.batch_size,
        args.workers,
    )
    traversal = run_gated_bfs(
        prepared.index,
        prepared.seeds,
        prepared.relation_specs,
        classifier,
        args.max_depth,
    )
    paths = save_results(
        prepared,
        args,
        system_prompt,
        provider,
        model,
        reasoning,
        traversal,
    )
    print("Extraction complete")
    print(
        json.dumps(
            {
                "visited": len(traversal["visited"]),
                "included": len(traversal["included"]),
                "excluded": len(traversal["excluded"]),
                "output_dir": str(Path(args.output_dir).resolve()),
                "manifest": str(paths["manifest"].resolve()),
            },
            indent=2,
        )
    )
    return None


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
