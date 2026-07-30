"""Expansion-property helpers for hierarchy and arbitrary-property datasets."""

from __future__ import annotations


DEFAULT_INITIAL_PROPERTIES = ("P31", "P279", "(-)P279")
DEFAULT_NEXT_PROPERTIES = ("(-)P279",)


def initial_properties(custom_properties: list[str] | None) -> tuple[str, ...]:
    if custom_properties:
        return tuple(custom_properties)
    return DEFAULT_INITIAL_PROPERTIES


def next_properties(custom_properties: list[str] | None) -> tuple[str, ...]:
    if custom_properties:
        return tuple(custom_properties)
    return DEFAULT_NEXT_PROPERTIES


def reachable_targets(
    q_adjacency: dict | None,
    properties: tuple[str, ...] | list[str],
    allowed_qids: set[str],
) -> set[str]:
    if q_adjacency is None or "claims" not in q_adjacency:
        return set()

    targets: set[str] = set()
    claims = q_adjacency["claims"]
    for prop in properties:
        for obj in claims.get(prop, []):
            value = obj.get("value")
            if value in allowed_qids:
                targets.add(value)
    return targets
