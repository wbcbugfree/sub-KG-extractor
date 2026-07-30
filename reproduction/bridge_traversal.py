"""Bridge-aware traversal helpers for KGPrune reproduction inputs.

The traversal may pass through unlabeled bridge nodes, but only labeled decision
QIDs are emitted as candidate decisions. When keep-gated mode is enabled, gold
or predicted PRUNE decision nodes are emitted but not expanded.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


Edge = tuple[str, str, str]
Adjacency = dict[str, dict[str, set[str]]]
WIKIDATA_ENTITY_PREFIX = "<http://www.wikidata.org/entity/"


@dataclass(frozen=True)
class DecisionPath:
    qid: str
    depth: int
    path: tuple[str, ...]


def embedding_qids_from_transaction(transaction) -> set[str]:
    qids: set[str] = set()
    for raw_key, _ in transaction.cursor():
        key = raw_key.decode("ascii")
        if key.startswith(WIKIDATA_ENTITY_PREFIX) and key.endswith(">"):
            qids.add(key[len(WIKIDATA_ENTITY_PREFIX) : -1])
        elif key.startswith("Q"):
            qids.add(key)
    return qids


def build_property_adjacency(edges: Iterable[Edge]) -> Adjacency:
    adjacency: Adjacency = defaultdict(lambda: defaultdict(set))
    for subject, predicate, obj in edges:
        adjacency[subject][predicate].add(obj)
        adjacency[obj][f"(-){predicate}"].add(subject)
    return adjacency


def property_targets(
    adjacency: Mapping[str, Mapping[str, set[str]]],
    qid: str,
    properties: Iterable[str],
) -> set[str]:
    targets: set[str] = set()
    node_claims = adjacency.get(qid, {})
    for prop in properties:
        targets.update(node_claims.get(prop, set()))
    return targets


def claim_value_targets(record: Mapping[str, object] | None, properties: Iterable[str]) -> set[str]:
    if not record:
        return set()
    claims = record.get("claims", {})
    if not isinstance(claims, Mapping):
        return set()

    targets: set[str] = set()
    for prop in properties:
        values = claims.get(prop, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, Mapping) and isinstance(value.get("value"), str):
                targets.add(value["value"])
            elif isinstance(value, str):
                targets.add(value)
    return targets


def legacy_reachable_targets(
    record: Mapping[str, object] | None,
    properties: Iterable[str],
    allowed_qids: set[str],
) -> set[str]:
    if not record or "claims" not in record:
        return set()
    claims = record["claims"]
    if not isinstance(claims, Mapping):
        return set()

    targets: set[str] = set()
    for prop in properties:
        values = claims.get(prop, [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            qid = value.get("value")
            if isinstance(qid, str) and qid in allowed_qids:
                targets.add(qid)
    return targets


def reachable_decision_paths_from_records(
    *,
    get_record,
    seed_qid: str,
    decision_qids: set[str],
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
    decision_targets: Mapping[str, str] | None = None,
    keep_gated: bool = False,
    allowed_qids: set[str] | None = None,
) -> list[DecisionPath]:
    seen = {seed_qid}
    queue: deque[tuple[str, tuple[str, ...]]] = deque()
    paths: list[DecisionPath] = []

    for target in sorted(claim_value_targets(get_record(seed_qid), initial_properties)):
        if allowed_qids is not None and target not in allowed_qids:
            continue
        if target not in seen:
            seen.add(target)
            queue.append((target, (seed_qid, target)))

    while queue:
        qid, path = queue.popleft()
        is_decision = qid in decision_qids
        if is_decision:
            paths.append(DecisionPath(qid=qid, depth=len(path) - 1, path=path))

        if keep_gated and is_decision and (decision_targets or {}).get(qid) != "1":
            continue

        for target in sorted(claim_value_targets(get_record(qid), next_properties)):
            if allowed_qids is not None and target not in allowed_qids:
                continue
            if target not in seen:
                seen.add(target)
                queue.append((target, (*path, target)))

    return paths


def legacy_decision_paths_from_records(
    *,
    get_record,
    seed_qid: str,
    decision_qids: set[str],
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
    decision_targets: Mapping[str, str] | None = None,
    keep_gated: bool = False,
) -> list[DecisionPath]:
    """Reproduce the original decision-node-only, set-layer traversal."""
    seen = {seed_qid}
    classes = legacy_reachable_targets(get_record(seed_qid), initial_properties, decision_qids)
    path_by_qid = {qid: (seed_qid, qid) for qid in classes}
    paths: list[DecisionPath] = []

    while classes:
        seen |= classes
        new_classes: set[str] = set()

        for qid in classes:
            path = path_by_qid[qid]
            paths.append(DecisionPath(qid=qid, depth=len(path) - 1, path=path))

            if keep_gated and (decision_targets or {}).get(qid) != "1":
                continue

            sub_classes = legacy_reachable_targets(get_record(qid), next_properties, decision_qids)
            sub_classes -= seen
            for target in sub_classes:
                path_by_qid[target] = (*path, target)
            new_classes |= sub_classes

        classes = new_classes

    return paths


def reachable_decision_paths_for_mode(
    *,
    get_record,
    seed_qid: str,
    decision_qids: set[str],
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
    allow_bridge_nodes: bool,
    available_embedding_qids: set[str] | None = None,
    decision_targets: Mapping[str, str] | None = None,
    keep_gated: bool = False,
) -> list[DecisionPath]:
    if not allow_bridge_nodes:
        return legacy_decision_paths_from_records(
            get_record=get_record,
            seed_qid=seed_qid,
            decision_qids=decision_qids,
            initial_properties=initial_properties,
            next_properties=next_properties,
            decision_targets=decision_targets,
            keep_gated=keep_gated,
        )

    return reachable_decision_paths_from_records(
        get_record=get_record,
        seed_qid=seed_qid,
        decision_qids=decision_qids,
        initial_properties=initial_properties,
        next_properties=next_properties,
        decision_targets=decision_targets,
        keep_gated=keep_gated,
        allowed_qids=available_embedding_qids,
    )


def reachable_decision_depths(
    *,
    edges: Iterable[Edge],
    seed_qid: str,
    decision_qids: set[str],
    initial_properties: tuple[str, ...],
    next_properties: tuple[str, ...],
    decision_targets: Mapping[str, str] | None = None,
    keep_gated: bool = False,
) -> dict[str, int]:
    adjacency = build_property_adjacency(edges)
    seen = {seed_qid}
    queue: deque[tuple[str, int]] = deque()
    depths: dict[str, int] = {}

    for target in property_targets(adjacency, seed_qid, initial_properties):
        if target not in seen:
            seen.add(target)
            queue.append((target, 1))

    while queue:
        qid, depth = queue.popleft()
        is_decision = qid in decision_qids
        if is_decision:
            depths.setdefault(qid, depth)

        if keep_gated and is_decision and (decision_targets or {}).get(qid) != "1":
            continue

        for target in property_targets(adjacency, qid, next_properties):
            if target not in seen:
                seen.add(target)
                queue.append((target, depth + 1))

    return depths
