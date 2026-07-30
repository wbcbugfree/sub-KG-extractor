"""Extract needed Wikidata embeddings from a PyTorch-BigGraph TSV stream."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import pickle
import re
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, TextIO

import numpy


WIKIDATA_PREFIX = "<http://www.wikidata.org/entity/"
QID_RE = re.compile(r"^Q\d+$")
QID_TOKEN_RE = re.compile(r"\bQ\d+\b")


def normalize_qid(identifier: str) -> str | None:
    value = identifier.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if "/entity/" in value:
        value = value.rsplit("/entity/", 1)[1]
    return value if QID_RE.match(value) else None


def embedding_key(qid: str, key_format: str) -> str:
    if key_format == "uri":
        return f"{WIKIDATA_PREFIX}{qid}>"
    if key_format == "qid":
        return qid
    raise ValueError(f"Unsupported key format: {key_format}")


def format_progress(scanned: int, found: int, required: int) -> str:
    return f"scanned={scanned} found={found}/{required} missing={required - found}"


def iter_matching_embeddings(
    lines: Iterable[str],
    required_qids: set[str],
    progress_every: int = 0,
) -> Iterator[tuple[str, numpy.ndarray]]:
    remaining_qids = set(required_qids)
    found = 0
    for line_number, line in enumerate(lines):
        if progress_every and line_number and line_number % progress_every == 0:
            print(format_progress(line_number, found, len(required_qids)), file=sys.stderr, flush=True)

        fields = line.rstrip("\n").split("\t")
        if line_number == 0 and len(fields) >= 3 and all(field.isdigit() for field in fields[:3]):
            continue
        if len(fields) < 2:
            continue

        qid = normalize_qid(fields[0])
        if qid not in remaining_qids:
            continue

        remaining_qids.remove(qid)
        found += 1
        yield qid, numpy.array([float(value) for value in fields[1:]], dtype=float)
        if not remaining_qids:
            break

    if progress_every:
        print(format_progress(line_number + 1, found, len(required_qids)), file=sys.stderr, flush=True)


def read_required_qids(paths: Iterable[Path]) -> set[str]:
    qids: set[str] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            if "," in sample:
                reader = csv.DictReader(handle)
                if reader.fieldnames and {"from", "QID"} & set(reader.fieldnames):
                    for row in reader:
                        for column in ("from", "QID"):
                            qid = normalize_qid(row.get(column, ""))
                            if qid:
                                qids.add(qid)
                    continue

            for line in handle:
                for qid in QID_TOKEN_RE.findall(line):
                    qids.add(qid)
    return qids


@contextmanager
def open_embedding_text(source: str) -> Iterator[TextIO]:
    if source.startswith(("http://", "https://")):
        response = urllib.request.urlopen(source)
        try:
            binary = gzip.GzipFile(fileobj=response) if source.endswith(".gz") else response
            text = (line.decode("utf-8") for line in binary)
            yield text
        finally:
            response.close()
        return

    path = Path(source)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            yield handle
    else:
        with path.open(encoding="utf-8") as handle:
            yield handle


def write_lmdb(
    embeddings: Iterable[tuple[str, numpy.ndarray]],
    output: Path,
    key_format: str,
    map_size: int,
) -> set[str]:
    try:
        import lmdb
    except ImportError as exc:
        raise SystemExit(
            "The 'lmdb' package is required to write LMDB artifacts. "
            "Install it in the active environment with: python -m pip install lmdb"
        ) from exc

    output.mkdir(parents=True, exist_ok=True)
    found_qids: set[str] = set()
    env = lmdb.open(str(output), map_size=map_size)
    with env.begin(write=True) as txn:
        for qid, vector in embeddings:
            txn.put(embedding_key(qid, key_format).encode("ascii"), pickle.dumps(vector))
            found_qids.add(qid)
    env.close()
    return found_qids


def write_manifest(path: Path, required_qids: set[str], found_qids: set[str], key_format: str) -> None:
    missing_qids = sorted(required_qids - found_qids)
    manifest = {
        "required": len(required_qids),
        "found": len(found_qids),
        "missing": len(missing_qids),
        "key_format": key_format,
        "found_qids": sorted(found_qids),
        "missing_qids": missing_qids,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Local TSV/TSV.GZ path or URL.")
    parser.add_argument("--required-qids", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-format", choices=("uri", "qid"), default="uri")
    parser.add_argument("--map-size", type=int, default=2_000_000_000)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    parser.add_argument("--manifest-output", type=Path)
    args = parser.parse_args()

    required_qids = read_required_qids(args.required_qids)
    with open_embedding_text(args.source) as lines:
        found_qids = write_lmdb(
            iter_matching_embeddings(lines, required_qids, progress_every=args.progress_every),
            args.output,
            args.key_format,
            args.map_size,
        )

    if args.manifest_output:
        write_manifest(args.manifest_output, required_qids, found_qids, args.key_format)

    missing = len(required_qids) - len(found_qids)
    print(f"Wrote {len(found_qids)} embeddings to {args.output}; missing {missing} of {len(required_qids)} required QIDs.")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
