"""Inspect PyTorch-BigGraph Wikidata names for required QID positions."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, TextIO

from extract_pbg_embeddings import normalize_qid, read_required_qids


WHITESPACE = " \t\r\n"


def iter_json_string_array(chunks: Iterable[str]) -> Iterator[str]:
    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    started = False
    finished = False

    for chunk in chunks:
        buffer += chunk
        while True:
            while position < len(buffer) and buffer[position] in WHITESPACE:
                position += 1

            if not started:
                if position >= len(buffer):
                    break
                if buffer[position] != "[":
                    raise ValueError("PBG names input must be a JSON array")
                started = True
                position += 1
                continue

            if position >= len(buffer):
                break
            if buffer[position] == ",":
                position += 1
                continue
            if buffer[position] == "]":
                finished = True
                position += 1
                break
            if buffer[position] != '"':
                raise ValueError("PBG names JSON array must contain strings")

            try:
                value, next_position = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                break
            if not isinstance(value, str):
                raise ValueError("PBG names JSON array must contain strings")

            yield value
            position = next_position

        if position:
            buffer = buffer[position:]
            position = 0
        if finished:
            break

    if not finished:
        raise ValueError("Incomplete PBG names JSON array")


def format_progress(scanned: int, found: int, required: int) -> str:
    return f"names_scanned={scanned} found={found}/{required} missing={required - found}"


def find_required_name_positions(
    names: Iterable[str],
    required_qids: set[str],
    progress_every: int = 0,
) -> dict[str, dict[str, object]]:
    remaining_qids = set(required_qids)
    hits: dict[str, dict[str, object]] = {}
    for index, key in enumerate(names):
        if progress_every and index and index % progress_every == 0:
            print(format_progress(index, len(hits), len(required_qids)), file=sys.stderr, flush=True)

        qid = normalize_qid(key)
        if qid not in remaining_qids:
            continue

        hits[qid] = {"index": index, "key": key}
        remaining_qids.remove(qid)
        if not remaining_qids:
            break

    if progress_every:
        print(format_progress(index + 1, len(hits), len(required_qids)), file=sys.stderr, flush=True)
    return hits


@contextmanager
def open_names_text(source: str, chunk_size: int = 1024 * 1024) -> Iterator[Iterator[str]]:
    if source.startswith(("http://", "https://")):
        response = urllib.request.urlopen(source)
        try:
            binary = gzip.GzipFile(fileobj=response) if source.endswith(".gz") else response
            yield (chunk.decode("utf-8") for chunk in iter(lambda: binary.read(chunk_size), b""))
        finally:
            response.close()
        return

    path = Path(source)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            yield iter(lambda: handle.read(chunk_size), "")
    else:
        with path.open(encoding="utf-8") as handle:
            yield iter(lambda: handle.read(chunk_size), "")


def write_manifest(path: Path, required_qids: set[str], hits: dict[str, dict[str, object]]) -> None:
    missing_qids = sorted(required_qids - set(hits))
    found_indices = [int(hit["index"]) for hit in hits.values()]
    manifest = {
        "required": len(required_qids),
        "found": len(hits),
        "missing": len(missing_qids),
        "missing_qids": missing_qids,
        "max_found_index": max(found_indices) if found_indices else None,
        "hits": {qid: hits[qid] for qid in sorted(hits)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names-source", required=True, help="Local names JSON/JSON.GZ path or URL.")
    parser.add_argument("--required-qids", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    args = parser.parse_args()

    required_qids = read_required_qids(args.required_qids)
    with open_names_text(args.names_source) as chunks:
        hits = find_required_name_positions(
            iter_json_string_array(chunks),
            required_qids,
            progress_every=args.progress_every,
        )

    write_manifest(args.output, required_qids, hits)
    missing = len(required_qids) - len(hits)
    print(f"Found {len(hits)} of {len(required_qids)} required QIDs in {args.names_source}; missing {missing}.")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
