"""Freeze NALT datasets, masked graph edges, folds, and cached-LLM comparisons."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import pickle
import random
import sys

from common import (HERE, ROOT, RUNTIME, canonical_paths, evaluate_scores, file_hash,
                    fixed_metrics, all_folds, load_protocol, read_json, write_json)

sys.path.insert(0, str(ROOT))
from usecase import subkg_extractor_nalt as nalt


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_source_cache(source):
    source_sha = file_hash(source)
    builder_sha = file_hash(Path(__file__))
    adapter_sha = file_hash(ROOT / "usecase" / "subkg_extractor_nalt.py")
    cache = RUNTIME / "prepared" / "source-index.pkl"
    if cache.exists():
        with cache.open("rb") as handle:
            data = pickle.load(handle)
        if (data["source_sha256"] == source_sha
                and data.get("builder_sha256") == builder_sha
                and data.get("adapter_sha256") == adapter_sha):
            return data
    print("Loading the verified NALT source", flush=True)
    graph = nalt.rdflib.Graph().parse(source, format="xml" if source.suffix == ".rdf" else "turtle")
    index = nalt.NaltSkosIndex(graph)
    uris = sorted(str(uri) for uri in index.pref_labels)
    ids = {uri: i for i, uri in enumerate(uris)}
    labels = [index.pref_labels[nalt.URIRef(uri)] for uri in uris]
    adjacency = {}
    for uri in uris:
        adjacency[ids[uri]] = [ids[node["uri"]] for node in index.one_hop_neighbors(uri)]
    roots = {key: ids[preset["gold_root_uri"]] for key, preset in nalt.NALT_BENCHMARK_PRESETS.items()}
    relations = [nalt.SKOS.broader, nalt.SKOS.narrower, nalt.SKOS.related]
    edges = sorted({(ids[str(lhs)], str(rel).split("#")[-1], ids[str(rhs)])
                    for rel in relations for lhs, rhs in graph.subject_objects(rel)
                    if str(lhs) in ids and str(rhs) in ids})
    gold, seeds = {}, {}
    for key, preset in nalt.NALT_BENCHMARK_PRESETS.items():
        root = nalt.URIRef(preset["gold_root_uri"])
        index.ignored_uris = {root}
        seed_uris = [index.find_uri_by_english_pref_label(label) for label in preset["seed_labels"]]
        seeds[key] = [ids[str(uri)] for uri in seed_uris]
        raw = index.hierarchy_descendants(root, exclude={root})
        reachable = nalt.reachable_uris_from_seeds(index, seed_uris)
        gold[key] = sorted(ids[str(uri)] for uri in raw & reachable)
    data = {"source_sha256": source_sha, "builder_sha256": builder_sha,
            "adapter_sha256": adapter_sha, "triples": len(graph), "uris": uris,
            "labels": labels, "adjacency": adjacency, "edges": edges,
            "roots": roots, "gold": gold, "seeds": seeds}
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as handle:
        pickle.dump(data, handle, protocol=pickle.HIGHEST_PROTOCOL)
    return data


def split_edges(edges, fraction, seed):
    """Keep inverse/duplicate endpoint pairs together; preserve training coverage."""
    groups = defaultdict(list)
    for edge in edges:
        groups[tuple(sorted((edge[0], edge[2])))].append(edge)
    degrees = Counter(node for pair in groups for node in set(pair))
    pairs = sorted(groups)
    random.Random(seed).shuffle(pairs)
    validation_pairs = set()
    target = round(len(groups) * fraction)
    for pair in pairs:
        if len(validation_pairs) >= target:
            break
        if all(degrees[node] > 1 for node in set(pair)):
            validation_pairs.add(pair)
            for node in set(pair):
                degrees[node] -= 1
    train, validation = [], []
    for pair in sorted(groups):
        (validation if pair in validation_pairs else train).extend(groups[pair])
    train_nodes = {x for a, _, b in train for x in (a, b)}
    all_nodes = {x for a, _, b in edges for x in (a, b)}
    assert all_nodes == train_nodes
    assert not ({tuple(sorted((a, b))) for a, _, b in train}
                & {tuple(sorted((a, b))) for a, _, b in validation})
    return train, validation


def prepare(source):
    protocol = load_protocol()
    data = build_source_cache(source)
    ids = {uri: i for i, uri in enumerate(data["uris"])}
    root_ids = set(data["roots"].values())
    observations = defaultdict(list)
    input_hashes = {}
    for path in sorted((ROOT / "usecase" / "results").rglob("*_METRICS.json")):
        payload = read_json(path)
        branch = payload["run_config"]["benchmark_name"]
        prefix = path.name.removesuffix("_METRICS.json")
        paths = {kind: path.with_name(prefix + f"_{kind}.csv")
                 for kind in ("VISITED", "INCLUDED", "PRUNED")}
        records = {kind: csv_rows(csv_path) for kind, csv_path in paths.items()}
        for input_path in [path, *paths.values()]:
            input_hashes[str(input_path.relative_to(ROOT)).replace("\\", "/")] = file_hash(input_path)
        observations[branch].append({"path": path, "payload": payload, "records": records})
    benchmark = {"protocol_sha256": file_hash(HERE / "protocol.json"),
                 "source_sha256": data["source_sha256"], "concepts": data["uris"],
                 "masked_roots": {key: data["uris"][value] for key, value in data["roots"].items()},
                 "folds": all_folds(protocol),
                 "branches": {}}
    summaries, llm_replays = [], []
    for key in protocol["branch_order"]:
        runs = observations[key]
        if len(runs) != 6:
            raise ValueError(f"Expected six retained runs for {key}; found {len(runs)}")
        exclusion_sets = [{ids[uri] for uri in run["payload"]["exclude_evaluation_uris"]} for run in runs]
        assert all(excluded == exclusion_sets[0] for excluded in exclusion_sets)
        excluded = exclusion_sets[0]
        positive = set(data["gold"][key])
        observed = {ids[row["uri"]] for run in runs for row in run["records"]["VISITED"]}
        for run in runs:
            for row in run["records"]["VISITED"]:
                if (row["in_hierarchy_gold"].lower() == "true") != (ids[row["uri"]] in positive):
                    raise ValueError(f"Retained label disagrees with source: {key}, {row['uri']}")
        scored = (positive | observed) - excluded - root_ids
        # Demonstrations remain ordinary classified nodes, but are never scored or trained on.
        candidates = (scored | excluded | set(data["seeds"][key])) - root_ids
        adjacency = {node: [v for v in data["adjacency"][node] if v in candidates]
                     for node in sorted(candidates)}
        paths = canonical_paths(data["seeds"][key], adjacency, protocol["maximum_iterations"])
        rows = [{"node": node, "label": int(node in positive), "scored": node in scored,
                 "seed": node in data["seeds"][key], "path": paths.get(node, [])}
                for node in sorted(candidates)]
        branch = {"seeds": data["seeds"][key], "decisions": rows, "adjacency": adjacency}
        benchmark["branches"][key] = branch
        gold_scores = {node: float(node in positive) for node in candidates}
        oracle, _, _, _ = evaluate_scores(branch, gold_scores, 0.5, protocol["maximum_iterations"])
        lengths = Counter(len(row["path"]) for row in rows if row["path"] and not row["seed"])
        summary = {"branch": key, "positive": sum(row["label"] for row in rows if row["scored"]),
                   "negative": sum(1 - row["label"] for row in rows if row["scored"]),
                   "seeds": len(branch["seeds"]), "unscored_demonstrations": len(candidates - scored),
                   "removed_root_candidates": sorted(data["uris"][x] for x in observed & root_ids),
                   "scored_without_path": sum(row["scored"] and not row["path"] for row in rows),
                   "path_length_histogram": dict(sorted(lengths.items())), "gold_gated_oracle": oracle}
        summaries.append(summary)
        print(json.dumps(summary), flush=True)
        for run in runs:
            included_old = {ids[row["uri"]] for row in run["records"]["INCLUDED"]}
            pruned_old = {ids[row["uri"]] for row in run["records"]["PRUNED"]}
            scores = {node: 1.0 for node in included_old}
            scores.update({node: 0.0 for node in pruned_old})
            metrics, included, visited, iterations = evaluate_scores(
                branch, scores, 0.5, protocol["maximum_iterations"])
            record = {"branch": key, "model": run["payload"]["llm_model"],
                      "mode": run["payload"]["run_config"]["result_mode"],
                      "source": str(run["path"].relative_to(ROOT)).replace("\\", "/"),
                      "metrics": metrics, "original_predictions_rescored": fixed_metrics(branch, included_old),
                      "scored_inclusions_lost_after_mask": len((included_old - included) & scored),
                      "replayed_visited": len(visited), "iterations": iterations,
                      "interpretation": "cached LLM judgments replayed under common mask; no new provider calls"}
            llm_replays.append(record)
    edge_rows = [edge for edge in data["edges"] if edge[0] not in root_ids and edge[2] not in root_ids]
    train, validation = split_edges(edge_rows, protocol["pbg"]["edge_validation_fraction"],
                                    protocol["pbg"]["edge_split_seed"])
    for name, edges in (("all", edge_rows), ("train", train), ("validation", validation)):
        path = RUNTIME / "prepared" / f"edges_{name}.tsv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerows((data["uris"][a], rel, data["uris"][b]) for a, rel, b in edges)
    embedding_nodes = {x for a, _, b in edge_rows for x in (a, b)}
    required_nodes = {node for branch in benchmark["branches"].values()
                      for row in branch["decisions"] for node in row["path"]}
    missing = required_nodes - embedding_nodes
    if missing:
        raise ValueError(f"{len(missing)} required path nodes have no embedding edges")
    metadata = {"source_sha256": data["source_sha256"], "source_triples": data["triples"],
                "english_concepts": len(data["uris"]), "embedding_nodes": len(embedding_nodes),
                "embedding_edges": len(edge_rows), "edge_train": len(train), "edge_validation": len(validation),
                "edge_relations": dict(Counter(edge[1] for edge in edge_rows)),
                "required_path_nodes": len(required_nodes), "missing_embedding_nodes": len(missing),
                "branches": summaries, "retained_run_count": len(llm_replays),
                "input_hashes": input_hashes,
                "edge_hashes": {kind: file_hash(RUNTIME / "prepared" / f"edges_{kind}.tsv")
                                for kind in ("all", "train", "validation")}}
    write_json(HERE / "data" / "benchmark.json", benchmark)
    write_json(HERE / "data" / "manifest.json", metadata)
    write_json(HERE / "results" / "llm_replay.json", llm_replays)
    with (RUNTIME / "prepared" / "decisions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["branch", "uri", "pref_label", "label", "scored", "seed", "canonical_path_length"])
        for key, branch in benchmark["branches"].items():
            writer.writerows((key, data["uris"][row["node"]], data["labels"][row["node"]],
                              row["label"], row["scored"], row["seed"], len(row["path"]))
                             for row in branch["decisions"])
    print(json.dumps({key: value for key, value in metadata.items()
                      if key not in ("input_hashes", "branches")}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "tmp/nalt-baseline-source/nalt-full_dwn_20240716.rdf")
    prepare(parser.parse_args().source)
