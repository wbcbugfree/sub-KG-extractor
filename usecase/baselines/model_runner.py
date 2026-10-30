"""NALT input adapters around the retained original LSTM and PA architectures."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("OMP_NUM_THREADS", "4")

import numpy as np

from common import (HERE, ROOT, RUNTIME, evaluate_scores, file_hash, fold_spec,
                    load_benchmark, load_protocol, model_directory, prediction_path, peak_rss_mb, read_json, select_threshold, write_json)

MODEL_SOURCE = ROOT / "reproduction" / "src" / "models_on_labeled_decisions"


def initialize_tensorflow(seed):
    import tensorflow as tf
    tf.config.threading.set_intra_op_parallelism_threads(4)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    tf.keras.utils.set_random_seed(seed)
    tf.config.experimental.enable_op_determinism()
    return tf


def original_model(algorithm, config):
    sys.path.insert(0, str(MODEL_SOURCE))
    if algorithm == "lstm":
        from lstm_pruning import lstm_model
        return lstm_model(units=config["units"], input_shape=config["length"])
    from sequence_analogy_pruning import sequenced_analogy_model
    return sequenced_analogy_model(config["length"], *config["filters"], config["dropout"])


def query_rows(benchmark, branch_names, supervised=False):
    return [{**row, "branch": name} for name in branch_names
            for row in benchmark["branches"][name]["decisions"]
            if row["path"] and not row["seed"] and (row["scored"] or not supervised)]


def pad_path(path, embeddings, length, padding):
    """Same seed-plus-tail truncation and zero placement as the original code."""
    if len(path) > length:
        path = [path[0]] + path[-(length - 1):]
    result = np.zeros((length, embeddings.shape[1]), dtype=np.float32)
    values = embeddings[path]
    if padding == "before":
        result[-len(path):] = values
    elif padding == "between":
        result[0] = values[0]
        if len(path) > 1:
            result[-(len(path) - 1):] = values[1:]
    else:
        raise ValueError(padding)
    return result


def reference_indices(queries, references, count, seed, exclude_same_seed=False):
    """Deterministic sampling depends on identity, never the query's gold label."""
    result = []
    for query in queries:
        candidates = [i for i, row in enumerate(references)
                      if not exclude_same_seed or row["path"][0] != query["path"][0]]
        if len(candidates) < count:
            raise ValueError(f"Only {len(candidates)} admissible training KEEP references for {query['branch']}")
        identity = f"{seed}:{query['branch']}:{query['node']}".encode()
        rng = random.Random(int.from_bytes(hashlib.sha256(identity).digest()[:8], "big"))
        result.append(rng.sample(candidates, count))
    return np.asarray(result, dtype=np.int32)


def path_features(rows, embeddings, config, padding):
    return np.asarray([pad_path(row["path"], embeddings, config["length"], padding) for row in rows], dtype=np.float32)


def analogy_features(query_vectors, reference_vectors, indices):
    n, count = indices.shape
    length, dimension = query_vectors.shape[1:]
    features = np.empty((n * count, dimension, 2 * length, 1), dtype=np.float32)
    # Keep reference first, query second: C1 valid kk and invalid kp.
    for i in range(n):
        section = features[i * count:(i + 1) * count, :, :, 0]
        section[:, :, :length] = reference_vectors[indices[i]].transpose(0, 2, 1)
        section[:, :, length:] = query_vectors[i].T
    return features


def predict(model, algorithm, rows, references, embeddings, config, settings, seed):
    vectors = path_features(rows, embeddings, config, settings["padding"])
    if algorithm == "lstm":
        values = model.predict(vectors, batch_size=256, verbose=0).reshape(-1)
    else:
        reference_vectors = path_features(references, embeddings, config, settings["padding"])
        count = settings["prediction_references"]
        indices = reference_indices(rows, references, count, seed)
        passes = settings["mc_dropout_passes"] if config["dropout"] > 0 else 1
        values = np.empty(len(rows), dtype=np.float64)
        # Bounded allocation, including repeated MC passes with active dropout.
        for start in range(0, len(rows), 64):
            stop = min(start + 64, len(rows))
            features = analogy_features(vectors[start:stop], reference_vectors, indices[start:stop])
            sums = np.zeros(len(features), dtype=np.float64)
            for _ in range(passes):
                for left in range(0, len(features), 256):
                    right = min(left + 256, len(features))
                    sums[left:right] += model(features[left:right], training=config["dropout"] > 0).numpy().reshape(-1)
            values[start:stop] = (sums / passes).reshape(stop - start, count).mean(axis=1)
    if not np.isfinite(values).all():
        raise ValueError("Non-finite decision probabilities")
    return {row["node"]: float(value) for row, value in zip(rows, values)}


def signature(algorithm, config, fold, seed, epoch_cap):
    embedding = read_json(HERE / "results" / "embedding.json")
    value = {"algorithm": algorithm, "config": config, "fold": fold, "seed": seed,
             "epoch_cap": epoch_cap, "protocol_sha256": file_hash(HERE / "protocol.json"),
             "benchmark_sha256": file_hash(HERE / "data" / "benchmark.json"),
             "embedding_sha256": embedding["embedding"]["sha256"],
             "adapter_sha256": file_hash(Path(__file__)),
             "evaluation_sha256": file_hash(HERE / "common.py"),
             "original_model_sha256": file_hash(MODEL_SOURCE / ("lstm_pruning.py" if algorithm == "lstm" else "sequence_analogy_pruning.py"))}
    return value


def run(args):
    protocol = load_protocol()
    settings = protocol[args.algorithm]
    config = next(row for row in settings["configurations"] if row["id"] == args.configuration)
    fold = fold_spec(protocol, args.test_branch, args.validation_branch)
    cap = settings["epoch_cap"]
    ident = f"{fold['test']}/{fold['validation']}/{args.algorithm}/{config['id']}/seed_{args.seed}"
    directory = model_directory(args.runtime_dir, args.algorithm, fold, config["id"], args.seed)
    result_path = directory / "fit.json"
    expected = signature(args.algorithm, config, fold, args.seed, cap)
    previous = read_json(result_path) if result_path.exists() else None
    if previous is not None and previous["signature"] != expected:
        raise ValueError(f"Fingerprint mismatch; will not silently reuse {result_path}")
    predictions_file = prediction_path(args.output_dir, args.algorithm, fold, args.seed)
    completed_evaluation = (previous is not None and "test" in previous and predictions_file.exists()
                            and file_hash(predictions_file) == previous["prediction_sha256"])
    if previous is not None and (args.stage == "train" or completed_evaluation):
        print(f"Already complete: {ident}", flush=True)
        return
    if args.stage == "evaluate" and previous is None:
        raise ValueError("Train and select the model before evaluating the held-out branch")
    embedding_path = HERE / "artifacts/embeddings.npy"
    if file_hash(embedding_path) != expected["embedding_sha256"]:
        raise ValueError("Embedding fingerprint mismatch")
    benchmark = load_benchmark()
    embeddings = np.load(embedding_path, mmap_mode="r")
    train_rows = query_rows(benchmark, fold["training"], supervised=True)
    val_rows = query_rows(benchmark, [fold["validation"]], supervised=True)
    references = [row for row in train_rows if row["label"] == 1]
    if set(fold["training"]) & {fold["validation"], fold["test"]}:
        raise ValueError("Overlapping supervised branches")
    directory.mkdir(parents=True, exist_ok=True)
    tf = initialize_tensorflow(args.seed)
    model = original_model(args.algorithm, config)
    model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=config["learning_rate"]),
                  loss="binary_crossentropy")
    weights = directory / "best.weights.h5"
    if args.stage == "train":
        started = time.perf_counter()
        y_train = np.asarray([row["label"] for row in train_rows], dtype=np.float32)
        y_val = np.asarray([row["label"] for row in val_rows], dtype=np.float32)
        x_train = path_features(train_rows, embeddings, config, settings["padding"])
        x_val = path_features(val_rows, embeddings, config, settings["padding"])
        if args.algorithm == "pa":
            reference_vectors = path_features(references, embeddings, config, settings["padding"])
            nt = settings["training_analogies_per_decision"]
            nv = settings["validation_analogies_per_decision"]
            train_refs = reference_indices(train_rows, references, nt, args.seed, exclude_same_seed=True)
            val_refs = reference_indices(val_rows, references, nv, args.seed)
            x_train = analogy_features(x_train, reference_vectors, train_refs)
            x_val = analogy_features(x_val, reference_vectors, val_refs)
            y_train, y_val = np.repeat(y_train, nt), np.repeat(y_val, nv)
        counts = np.bincount(y_train.astype(int), minlength=2)
        class_weight = {i: float(len(y_train) / (2 * counts[i])) for i in range(2)}

        class Progress(tf.keras.callbacks.Callback):
            def on_epoch_end(self, epoch, logs=None):
                data = {"run": ident, "epoch": epoch + 1, "cap": cap,
                        "seconds": time.perf_counter() - started,
                        **{k: float(v) for k, v in (logs or {}).items()}}
                write_json(directory / "progress.json", data)
                if epoch == 0 or (epoch + 1) % 5 == 0:
                    print(json.dumps(data), flush=True)

        history = model.fit(x_train, y_train, validation_data=(x_val, y_val),
                            batch_size=settings["batch_size"], epochs=cap, shuffle=True,
                            class_weight=class_weight, verbose=0,
                            callbacks=[tf.keras.callbacks.EarlyStopping(monitor="val_loss", patience=settings["patience"],
                                                                        restore_best_weights=True),
                                       tf.keras.callbacks.ModelCheckpoint(str(weights), monitor="val_loss",
                                                                          save_best_only=True, save_weights_only=True),
                                       Progress()])
        # Also restore the best epoch when the epoch cap is reached before
        # EarlyStopping fires (older Keras only restores when it stops early).
        model.load_weights(weights)
        training_seconds = time.perf_counter() - started
        # Reset the inference RNG; dropout scores are independent of how many
        # epochs early stopping took and reproducible when reloaded later.
        tf.keras.utils.set_random_seed(args.seed + 1000)
        queries = query_rows(benchmark, [fold["validation"]])
        scores = predict(model, args.algorithm, queries, references, embeddings, config, settings, args.seed)
        chosen, trials = select_threshold(benchmark["branches"][fold["validation"]], scores, protocol["maximum_iterations"])
        write_json(directory / "validation_scores.json", scores)
        previous = {"signature": expected, "tensorflow_version": tf.__version__,
                    "model_parameters": model.count_params(), "training_decisions": len(train_rows),
                    "validation_decisions": len(val_rows), "training_KEEP_reference_count": len(references),
                    "training_instances": len(y_train), "class_weight": class_weight,
                    "epochs_run": len(history.epoch),
                    "best_epoch": int(np.argmin(history.history["val_loss"]) + 1),
                    "history": {k: [float(v) for v in values] for k, values in history.history.items()},
                    "training_seconds": training_seconds, "total_seconds": time.perf_counter() - started,
                    "peak_process_rss_mb": peak_rss_mb(),
                    "validation": chosen, "threshold_trials": trials,
                    "weights_sha256": file_hash(weights)}
        write_json(result_path, previous)
        print(json.dumps({"completed": ident, "validation": chosen}), flush=True)
        return
    model.load_weights(weights)
    if file_hash(weights) != previous["weights_sha256"]:
        raise ValueError("Saved model weight fingerprint mismatch")
    tf.keras.utils.set_random_seed(args.seed + 1000)
    queries = query_rows(benchmark, [fold["test"]])
    started = time.perf_counter()
    scores = predict(model, args.algorithm, queries, references, embeddings, config, settings, args.seed)
    branch = benchmark["branches"][fold["test"]]
    metrics, included, visited, iterations = evaluate_scores(branch, scores, previous["validation"]["threshold"], protocol["maximum_iterations"])
    previous["test"] = {**metrics, "threshold": previous["validation"]["threshold"],
                        "prediction_seconds": time.perf_counter() - started, "iterations": iterations}
    predictions_file = prediction_path(args.output_dir, args.algorithm, fold, args.seed)
    predictions_file.parent.mkdir(parents=True, exist_ok=True)
    with predictions_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["uri", "label", "scored", "seed", "score", "visited", "included"])
        writer.writeheader()
        for row in branch["decisions"]:
            node = row["node"]
            writer.writerow({"uri": benchmark["concepts"][node], "label": row["label"],
                             "scored": int(row["scored"]), "seed": int(row["seed"]),
                             "score": 1.0 if row["seed"] else scores.get(node, ""),
                             "visited": int(node in visited), "included": int(node in included)})
    previous["prediction_sha256"] = file_hash(predictions_file)
    write_json(result_path, previous)
    print(json.dumps({"evaluated": ident, "test": metrics}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["train", "evaluate"])
    parser.add_argument("--algorithm", choices=["lstm", "pa"], required=True)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--test-branch", required=True)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--validation-branch", required=True)
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
