"""Build compact Wikidata LMDB artifacts from local benchmark TTL files."""

from __future__ import annotations

import argparse
import csv
import pickle
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable


Edge = tuple[str, str, str]
Labels = dict[str, str]

EDGE_RE = re.compile(r"^wd:(Q\d+)\s+wdt:(P\d+)\s+wd:(Q\d+)\s+\.\s*$")
LABEL_RE = re.compile(r'^wd:(Q\d+)\s+rdfs:label\s+"((?:\\.|[^"\\])*)"@en\s+\.\s*$')
ESCAPE_RE = re.compile(r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)")


def _unescape_turtle_literal(value: str) -> str:
    escape_map = {
        "t": "\t",
        "b": "\b",
        "n": "\n",
        "r": "\r",
        "f": "\f",
        '"': '"',
        "'": "'",
        "\\": "\\",
    }

    def replace(match: re.Match[str]) -> str:
        escaped = match.group(1)
        if escaped.startswith(("u", "U")):
            return chr(int(escaped[1:], 16))
        return escape_map.get(escaped, escaped)

    return ESCAPE_RE.sub(replace, value)


def parse_ttl(path: Path) -> tuple[list[Edge], Labels]:
    edges: list[Edge] = []
    labels: Labels = {}

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            edge_match = EDGE_RE.match(stripped)
            if edge_match:
                edges.append(edge_match.groups())
                continue

            label_match = LABEL_RE.match(stripped)
            if label_match:
                qid, label = label_match.groups()
                labels[qid] = _unescape_turtle_literal(label)

    return edges, labels


def build_analogical_pruning_records(edges: Iterable[Edge], labels: Labels) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}

    def ensure(qid: str) -> dict[str, object]:
        return records.setdefault(qid, {"claims": defaultdict(list), "labels": {}})

    for subject, prop, obj in edges:
        ensure(subject)["claims"][prop].append({"value": obj})
        ensure(obj)["claims"][f"(-){prop}"].append({"value": subject})

    for qid, label in labels.items():
        ensure(qid)["labels"]["en"] = label

    for entity in records.values():
        entity["claims"] = dict(entity["claims"])

    return records


def build_kgprune_records(edges: Iterable[Edge], labels: Labels) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}

    def ensure(qid: str) -> dict[str, object]:
        return records.setdefault(qid, defaultdict(list))

    for subject, prop, obj in edges:
        ensure(subject)[prop].append(obj)
        ensure(obj)[f"(-){prop}"].append(subject)

    for qid, label in labels.items():
        ensure(qid)["labels"] = {"en": {"value": label}}

    return {qid: dict(entity) for qid, entity in records.items()}


def write_lmdb(records: dict[str, dict[str, object]], output: Path, map_size: int) -> None:
    try:
        import lmdb
    except ImportError as exc:
        raise SystemExit(
            "The 'lmdb' package is required to write LMDB artifacts. "
            "Install it in the active environment with: python -m pip install lmdb"
        ) from exc

    output.mkdir(parents=True, exist_ok=True)
    env = lmdb.open(str(output), map_size=map_size)
    with env.begin(write=True) as txn:
        for qid, entity in records.items():
            txn.put(qid.encode("ascii"), pickle.dumps(entity))
    env.close()


def read_required_qids(path: Path) -> list[str]:
    qids: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            for column in ("from", "QID"):
                qid = row.get(column, "").strip()
                if qid:
                    qids.add(qid)
    return sorted(qids)


def write_qids(path: Path, qids: Iterable[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        for qid in qids:
            handle.write(f"{qid}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ttl", type=Path, required=True, help="Local benchmark TTL file to parse.")
    parser.add_argument(
        "--format",
        choices=("analogical-pruning", "kgprune"),
        default="analogical-pruning",
        help="LMDB value shape to write.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output LMDB directory.")
    parser.add_argument("--map-size", type=int, default=1_000_000_000)
    parser.add_argument("--required-qids", type=Path, help="Decision CSV used to extract required QIDs.")
    parser.add_argument(
        "--required-scope",
        choices=("decisions", "graph"),
        default="decisions",
        help="Use decision CSV QIDs or every QID present in the parsed graph for required-QID output.",
    )
    parser.add_argument("--required-qids-output", type=Path, help="Optional newline QID output path.")
    args = parser.parse_args()

    edges, labels = parse_ttl(args.ttl)
    if args.format == "analogical-pruning":
        records = build_analogical_pruning_records(edges, labels)
    else:
        records = build_kgprune_records(edges, labels)

    write_lmdb(records, args.output, args.map_size)

    if args.required_qids_output:
        if args.required_scope == "graph":
            required_qids = sorted(records)
        elif args.required_qids:
            required_qids = read_required_qids(args.required_qids)
        else:
            raise SystemExit("--required-qids is required unless --required-scope graph is used")
        write_qids(args.required_qids_output, required_qids)

    print(f"Wrote {len(records)} entities from {len(edges)} edges to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
