"""Extract needed Wikidata embeddings from PBG names and vectors artifacts."""

from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

import numpy
from numpy.lib import format as npy_format

from extract_pbg_embeddings import embedding_key

try:
    from isal import igzip as accelerated_gzip
except ImportError:  # Optional acceleration for the multi-gigabyte PBG archive.
    accelerated_gzip = None

try:
    import rapidgzip
except ImportError:  # Optional parallel reader for local gzip archives.
    rapidgzip = None


def _import_lmdb():
    try:
        import lmdb
    except ImportError as exc:
        raise SystemExit(
            "The 'lmdb' package is required to write LMDB artifacts. "
            "Install it in the active environment with: python -m pip install lmdb"
        ) from exc
    return lmdb


def positions_from_names_manifest(manifest: dict[str, object]) -> dict[str, int]:
    hits = manifest.get("hits", {})
    if not isinstance(hits, dict):
        raise ValueError("Names manifest must contain a 'hits' object")

    positions: dict[str, int] = {}
    for qid, hit in hits.items():
        if not isinstance(hit, dict) or "index" not in hit:
            raise ValueError(f"Names manifest hit for {qid} is missing an index")
        positions[qid] = int(hit["index"])
    return positions


def missing_positions_from_lmdb(output: Path, qid_positions: dict[str, int], key_format: str) -> dict[str, int]:
    if not (output / "data.mdb").exists():
        return dict(qid_positions)

    lmdb = _import_lmdb()
    env = lmdb.open(str(output), readonly=True, readahead=False, lock=False)
    try:
        with env.begin() as txn:
            return {
                qid: position
                for qid, position in qid_positions.items()
                if txn.get(embedding_key(qid, key_format).encode("ascii")) is None
            }
    finally:
        env.close()


def read_npy_header(handle: BinaryIO) -> tuple[tuple[int, ...], numpy.dtype]:
    version = npy_format.read_magic(handle)
    if version == (1, 0):
        shape, fortran_order, dtype = npy_format.read_array_header_1_0(handle)
    elif version == (2, 0):
        shape, fortran_order, dtype = npy_format.read_array_header_2_0(handle)
    else:
        raise ValueError(f"Unsupported NPY version: {version}")
    if fortran_order:
        raise ValueError("Fortran-ordered PBG vector arrays are not supported")
    if len(shape) != 2:
        raise ValueError(f"Expected a 2D vector array, got shape {shape}")
    return shape, numpy.dtype(dtype)


def format_progress(scanned: int, found: int, required: int) -> str:
    return f"vector_rows_scanned={scanned} found={found}/{required} missing={required - found}"


def iter_selected_vectors(
    handle: BinaryIO,
    qid_positions: dict[str, int],
    progress_every: int = 0,
    scan_chunk_rows: int = 100_000,
) -> Iterator[tuple[str, numpy.ndarray]]:
    if not qid_positions:
        return

    shape, dtype = read_npy_header(handle)
    rows, dimensions = shape
    positions_to_qids: dict[int, list[str]] = {}
    for qid, position in qid_positions.items():
        if position < 0 or position >= rows:
            continue
        positions_to_qids.setdefault(position, []).append(qid)

    row_size = dimensions * dtype.itemsize
    max_position = max(positions_to_qids) if positions_to_qids else -1
    found = 0

    scan_chunk_rows = max(1, int(scan_chunk_rows))
    selected_positions = sorted(positions_to_qids)
    selected_index = 0
    previous_progress_bucket = 0

    for chunk_start in range(0, max_position + 1, scan_chunk_rows):
        chunk_row_count = min(scan_chunk_rows, max_position + 1 - chunk_start)
        expected_bytes = chunk_row_count * row_size
        blocks: list[bytes] = []
        received = 0
        while received < expected_bytes:
            block = handle.read(expected_bytes - received)
            if not block:
                raise EOFError(
                    f"Unexpected end of vector array at row "
                    f"{chunk_start + received // row_size}"
                )
            blocks.append(block)
            received += len(block)
        chunk_bytes = blocks[0] if len(blocks) == 1 else b"".join(blocks)
        chunk = numpy.frombuffer(chunk_bytes, dtype=dtype).reshape(chunk_row_count, dimensions)
        chunk_end = chunk_start + chunk_row_count

        while (
            selected_index < len(selected_positions)
            and selected_positions[selected_index] < chunk_end
        ):
            row_index = selected_positions[selected_index]
            vector = chunk[row_index - chunk_start].astype(float)
            for qid in sorted(positions_to_qids[row_index]):
                found += 1
                yield qid, vector.copy()
            selected_index += 1

        if progress_every:
            progress_bucket = chunk_end // progress_every
            if progress_bucket > previous_progress_bucket:
                print(
                    format_progress(chunk_end, found, len(qid_positions)),
                    file=sys.stderr,
                    flush=True,
                )
                previous_progress_bucket = progress_bucket

    if progress_every:
        print(format_progress(max_position + 1, found, len(qid_positions)), file=sys.stderr, flush=True)


@contextmanager
def open_vector_binary(source: str) -> Iterator[BinaryIO]:
    gzip_module = accelerated_gzip or gzip
    if source.startswith(("http://", "https://")):
        response = urllib.request.urlopen(source)
        try:
            if source.endswith(".gz"):
                with gzip_module.GzipFile(fileobj=response) as handle:
                    yield handle
            else:
                yield response
        finally:
            response.close()
        return

    path = Path(source)
    if path.suffix == ".gz":
        if rapidgzip is not None:
            with rapidgzip.open(str(path), parallelization=0) as handle:
                yield handle
            return
        with gzip_module.open(path, "rb") as handle:
            yield handle
    else:
        with path.open("rb") as handle:
            yield handle


def write_lmdb(
    vectors: Iterator[tuple[str, numpy.ndarray]],
    output: Path,
    key_format: str,
    map_size: int,
    commit_every: int = 100,
) -> set[str]:
    lmdb = _import_lmdb()

    output.mkdir(parents=True, exist_ok=True)
    found_qids: set[str] = set()
    env = lmdb.open(str(output), map_size=map_size)
    txn = env.begin(write=True)
    pending = 0
    try:
        for qid, vector in vectors:
            txn.put(embedding_key(qid, key_format).encode("ascii"), pickle.dumps(vector))
            found_qids.add(qid)
            pending += 1
            if pending >= commit_every:
                txn.commit()
                txn = env.begin(write=True)
                pending = 0
        txn.commit()
    except Exception:
        txn.abort()
        raise
    finally:
        env.close()
    return found_qids


def write_vector_manifest(path: Path, requested_qids: set[str], found_qids: set[str], key_format: str) -> None:
    missing_qids = sorted(requested_qids - found_qids)
    manifest = {
        "required": len(requested_qids),
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
    parser.add_argument("--vectors-source", required=True, help="Local vectors NPY/NPY.GZ path or URL.")
    parser.add_argument("--names-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-format", choices=("uri", "qid"), default="uri")
    parser.add_argument("--map-size", type=int, default=2_000_000_000)
    parser.add_argument("--commit-every", type=int, default=100)
    parser.add_argument("--progress-every", type=int, default=1_000_000)
    parser.add_argument("--scan-chunk-rows", type=int, default=100_000)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--no-resume", action="store_true", help="Do not skip vectors already present in the output LMDB.")
    args = parser.parse_args()

    names_manifest = json.loads(args.names_manifest.read_text(encoding="utf-8"))
    qid_positions = positions_from_names_manifest(names_manifest)
    requested_qids = set(qid_positions)
    if not args.no_resume:
        qid_positions = missing_positions_from_lmdb(args.output, qid_positions, args.key_format)
        skipped = len(requested_qids) - len(qid_positions)
        if skipped:
            print(f"Skipping {skipped} QIDs already present in {args.output}.")
    if qid_positions:
        with open_vector_binary(args.vectors_source) as handle:
            newly_found_qids = write_lmdb(
                iter_selected_vectors(
                    handle,
                    qid_positions,
                    progress_every=args.progress_every,
                    scan_chunk_rows=args.scan_chunk_rows,
                ),
                args.output,
                args.key_format,
                args.map_size,
                commit_every=args.commit_every,
            )
    else:
        newly_found_qids = set()
    found_qids = requested_qids - set(qid_positions) | newly_found_qids

    if args.manifest_output:
        write_vector_manifest(args.manifest_output, requested_qids, found_qids, args.key_format)

    missing = len(requested_qids) - len(found_qids)
    print(f"Wrote {len(newly_found_qids)} new vectors to {args.output}; missing {missing} of {len(requested_qids)} requested QIDs.")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
