"""Train actual PyTorch-BigGraph and freeze one shared NALT embedding space."""
from __future__ import annotations

import argparse
from functools import partial
import logging
import os
from pathlib import Path
import random
import shutil
import sys
import time

from common import HERE, RUNTIME, file_hash, load_benchmark, load_protocol, read_json, write_json

VENDOR = RUNTIME / "vendor" / ("PyTorch-BigGraph-" + load_protocol()["pbg"]["upstream_commit"])
sys.path.insert(0, str(VENDOR))


def pbg_path(path):
    # PBG interprets a Windows drive letter as a URL scheme. Relative paths
    # work with its file backend and remain portable inside this checkout.
    return Path(os.path.relpath(path)).as_posix()


def seed_process(seed):
    import numpy as np
    import torch
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(4)


def windows_snapshot_copies():
    """Snapshots use copies on Windows; no numerical training code is changed."""
    if os.name != "nt":
        return
    from torchbiggraph.checkpoint_storage import FileCheckpointStorage

    def copy_entity(self, version, entity_name, partition, epoch_idx):
        shutil.copy2(self.get_entity_partition_file(version, entity_name, partition),
                     self.get_entity_partition_file(version, entity_name, partition,
                                                    path=self.get_snapshot_path(epoch_idx)))

    def copy_model(self, version, epoch_idx):
        shutil.copy2(self.get_model_file(version),
                     self.get_model_file(version, path=self.get_snapshot_path(epoch_idx)))

    FileCheckpointStorage.copy_entity_partition_to_snapshot = copy_entity
    FileCheckpointStorage.copy_model_to_snapshot = copy_model


def config_dict(lr, epochs, checkpoint, edge_set="train", preserve=True):
    p = load_protocol()["pbg"]
    return {
        "entities": {"concept": {"num_partitions": 1}},
        "relations": [{"name": "skos", "lhs": "concept", "rhs": "concept", "operator": p["operator"]}],
        "entity_path": pbg_path(RUNTIME / "pbg" / "entities"),
        "edge_paths": [pbg_path(RUNTIME / "pbg" / ("edges_" + edge_set))],
        "checkpoint_path": pbg_path(checkpoint),
        "dimension": p["dimension"], "global_emb": p["global_emb"],
        "comparator": p["comparator"], "loss_fn": p["loss_fn"],
        "dynamic_relations": p["dynamic_relations"], "lr": float(lr),
        "relation_lr": p["relation_lr"], "num_epochs": epochs,
        "batch_size": p["batch_size"], "num_batch_negs": p["num_batch_negs"],
        "num_uniform_negs": p["num_uniform_negs"],
        "eval_num_batch_negs": p["num_batch_negs"],
        "eval_num_uniform_negs": p["num_uniform_negs"],
        "workers": p["workers"], "num_edge_chunks": 1, "eval_fraction": 0.0,
        "checkpoint_preservation_interval": 1 if preserve else None,
    }


def prepare():
    from torchbiggraph.config import ConfigSchema
    from torchbiggraph.converters.importers import convert_input_data, TSVEdgelistReader
    seed_process(load_protocol()["pbg"]["training_seed"])
    config = ConfigSchema.from_dict(config_dict(0.1, 1, RUNTIME / "pbg" / "unused"))
    names = ["train", "validation", "all"]
    manifest_path = RUNTIME / "pbg" / "import_manifest.json"
    expected = {"upstream_commit": load_protocol()["pbg"]["upstream_commit"],
                "import_seed": load_protocol()["pbg"]["training_seed"],
                "input_edge_sha256": {name: file_hash(RUNTIME / "prepared" / ("edges_" + name + ".tsv"))
                                      for name in names}}
    if manifest_path.exists():
        if read_json(manifest_path) != expected:
            raise ValueError("PBG imported graph fingerprints differ; preserve old artifacts and use a fresh runtime directory")
    elif (RUNTIME / "pbg" / "entities").exists():
        raise ValueError("PBG imported graph exists without its provenance manifest; refusing unchecked cache reuse")
    convert_input_data(config.entities, config.relations, config.entity_path,
                       [pbg_path(RUNTIME / "pbg" / ("edges_" + n)) for n in names],
                       [RUNTIME / "prepared" / ("edges_" + n + ".tsv") for n in names],
                       TSVEdgelistReader(lhs_col=0, rhs_col=2, rel_col=1),
                       entity_min_count=1, relation_type_min_count=1, dynamic_relations=True)
    write_json(manifest_path, expected)


def train_one(lr, epochs, checkpoint, edge_set="train", preserve=True):
    from torchbiggraph.config import ConfigSchema
    from torchbiggraph.train import train
    expected = config_dict(lr, epochs, checkpoint, edge_set, preserve)
    done = checkpoint / "completed.json"
    if done.exists():
        previous = read_json(done)
        if previous["config"] != expected:
            raise ValueError(f"Existing PBG configuration differs: {checkpoint}")
        return previous
    if (checkpoint / "checkpoint_version.txt").exists():
        raise RuntimeError(f"Incomplete PBG run at {checkpoint}; use a fresh run directory for deterministic restart")
    checkpoint.mkdir(parents=True, exist_ok=True)
    seed = load_protocol()["pbg"]["training_seed"]
    seed_process(seed)
    windows_snapshot_copies()
    started = time.perf_counter()
    train(ConfigSchema.from_dict(expected), subprocess_init=partial(seed_process, seed))
    result = {"config": expected, "seconds": time.perf_counter() - started, "seed": seed}
    write_json(done, result)
    return result


def evaluate(lr, epoch, checkpoint):
    from torchbiggraph.config import ConfigSchema
    from torchbiggraph.eval import do_eval_and_report_stats
    seed = load_protocol()["pbg"]["evaluation_seed"]
    seed_process(seed)
    cfg = ConfigSchema.from_dict(config_dict(lr, epoch, checkpoint, "validation", False))
    final = None
    for edge_path_idx, bucket, stats in do_eval_and_report_stats(cfg, subprocess_init=partial(seed_process, seed)):
        if edge_path_idx is None and bucket is None:
            final = stats.to_dict()
    if final is None:
        raise RuntimeError("PBG did not produce aggregate evaluation statistics")
    return {"learning_rate": lr, "epochs": epoch, "statistics": final,
            "evaluation_seed": seed, "checkpoint": str(checkpoint.relative_to(RUNTIME))}


def export(checkpoint):
    import numpy as np
    from torchbiggraph.checkpoint_manager import CheckpointManager
    from torchbiggraph.graph_storages import ENTITY_STORAGES
    benchmark = load_benchmark()
    storage = ENTITY_STORAGES.make_instance(pbg_path(RUNTIME / "pbg" / "entities"))
    names = storage.load_names("concept", 0)
    tensor, _ = CheckpointManager(pbg_path(checkpoint)).read("concept", 0)
    array = np.zeros((len(benchmark["concepts"]), load_protocol()["pbg"]["dimension"]), dtype=np.float32)
    coverage = np.zeros(len(array), dtype=bool)
    indices = {uri: i for i, uri in enumerate(benchmark["concepts"])}
    for row, uri in enumerate(names):
        index = indices[uri]
        array[index] = tensor[row].numpy()
        coverage[index] = True
    for branch in benchmark["branches"].values():
        for decision in branch["decisions"]:
            if decision["path"] and not coverage[decision["path"]].all():
                raise ValueError("A model path contains a node without a trained embedding")
    target = RUNTIME / "embeddings.npy"
    artifacts = HERE / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    arrays = {"embeddings.npy": array, "embedding_coverage.npy": coverage}
    for name, values in arrays.items():
        source, retained = RUNTIME / ("export_" + name), artifacts / name
        np.save(source, values)
        if retained.exists() and file_hash(retained) != file_hash(source):
            raise ValueError("Retained embeddings differ; reproduce PBG in a separate checkout")
    for name in arrays:
        # Replacement also avoids writing through a runtime hard link.
        (RUNTIME / ("export_" + name)).replace(RUNTIME / name)
        if not (artifacts / name).exists():
            shutil.copy2(RUNTIME / name, artifacts / name)
    write_json(HERE / "results" / "artifacts.json", [
        {"path": (artifacts / name).relative_to(HERE.parents[1]).as_posix(),
         "sha256": file_hash(artifacts / name), "bytes": (artifacts / name).stat().st_size}
        for name in arrays
    ])
    return {"path": str(target.relative_to(HERE.parents[1])), "sha256": file_hash(target),
            "shape": list(array.shape), "trained_nodes": int(coverage.sum()),
            "unembedded_rows": int((~coverage).sum())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "run"])
    args = parser.parse_args()
    from torchbiggraph.util import setup_logging
    setup_logging()
    logging.getLogger().setLevel(logging.INFO)
    prepare()
    if args.stage == "prepare":
        return
    p = load_protocol()["pbg"]
    started = time.perf_counter()
    trials, training_runs = [], []
    for lr in p["learning_rates"]:
        cp = RUNTIME / "pbg" / ("lr_" + str(lr))
        training_runs.append(train_one(lr, max(p["epoch_checkpoints"]), cp))
        for epoch in p["epoch_checkpoints"]:
            result = evaluate(lr, epoch, cp / f"epoch_{epoch}")
            trials.append(result)
            write_json(RUNTIME / "pbg" / "search-progress.json", trials)
    selected = max(trials, key=lambda row: (row["statistics"]["metrics"]["mrr"], -row["epochs"], -row["learning_rate"]))
    cp = RUNTIME / "pbg" / "final"
    final_training = train_one(selected["learning_rate"], selected["epochs"], cp, "all", False)
    exported = export(cp)
    import torch
    write_json(HERE / "results" / "embedding.json", {
        "protocol_sha256": file_hash(HERE / "protocol.json"),
        "benchmark_sha256": file_hash(HERE / "data" / "benchmark.json"),
        "upstream_commit": p["upstream_commit"], "torch_version": torch.__version__,
        "selection_metric": "sampled link-prediction MRR on held-out unordered endpoint-pair groups",
        "validation_trials": trials, "selected": selected, "search_training": training_runs,
        "final_training": final_training, "embedding": exported,
        "wall_seconds": time.perf_counter() - started,
        "compatibility": "Windows checkpoint snapshot copies replace symbolic links; worker RNG is explicitly seeded",
    })
    print("Shared embeddings frozen", exported, flush=True)


if __name__ == "__main__":
    main()
