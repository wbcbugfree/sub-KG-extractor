"""Shared fixed-population NALT evaluation, independent of model and gold gating."""
from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Mapping

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = ROOT / "tmp" / "nalt-baselines"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def peak_rss_mb():
    """Peak memory of this process; excludes independent child processes."""
    import sys
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ["PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage"]]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_handle = ctypes.windll.kernel32.GetCurrentProcess
        get_handle.restype = wintypes.HANDLE
        read = ctypes.windll.psapi.GetProcessMemoryInfo
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not read(get_handle(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.PeakWorkingSetSize / 1e6
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1e6 if sys.platform == "darwin" else 1000)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_protocol():
    return read_json(HERE / "protocol.json")


def fold_spec(protocol, test_branch: str, validation_branch: str):
    order = protocol["branch_order"]
    if test_branch not in order or validation_branch not in order or test_branch == validation_branch:
        raise ValueError("Test and validation must be distinct benchmark branches")
    return {"test": test_branch, "validation": validation_branch,
            "training": [branch for branch in order if branch not in (test_branch, validation_branch)]}


def all_folds(protocol):
    return [fold_spec(protocol, test, validation) for test in protocol["branch_order"]
            for validation in protocol["branch_order"] if test != validation]


def model_directory(runtime, algorithm, fold, configuration, seed):
    return (Path(runtime) / "models" / fold["test"] / fold["validation"] /
            algorithm / configuration / f"seed_{seed}")


def prediction_path(output, algorithm, fold, seed):
    return (Path(output) / "results" / "predictions" / fold["test"] /
            fold["validation"] / f"{algorithm}_seed_{seed}.csv")


def implementation_hashes():
    sources = [HERE / name for name in ("common.py", "model_runner.py", "run_experiments.py")]
    sources += [ROOT / "reproduction/src/models_on_labeled_decisions" / name
                for name in ("lstm_pruning.py", "sequence_analogy_pruning.py")]
    return {p.relative_to(ROOT).as_posix(): file_hash(p) for p in sources}


def input_hashes():
    return {"protocol_sha256": file_hash(HERE / "protocol.json"),
            "benchmark_sha256": file_hash(HERE / "data/benchmark.json"),
            "embedding_sha256": file_hash(HERE / "artifacts/embeddings.npy")}


def load_results(output=HERE):
    """Load complete, fingerprinted final records without search caches."""
    output = Path(output)
    metadata = read_json(output / "results/experiment.json")
    if metadata.get("status") != "complete":
        raise ValueError("Experiment has not completed all selected-model evaluations")
    if metadata["inputs"] != input_hashes() or metadata["implementation"] != implementation_hashes():
        raise ValueError("Inputs or implementation changed; use a separate output directory for a new experiment")
    path = output / "results/selected_models.json"
    if metadata["selected_models_sha256"] != file_hash(path):
        raise ValueError("Selected model metadata changed")
    models = read_json(path)["models"]
    protocol = load_protocol()
    expected = {(algorithm, fold["test"], fold["validation"])
                for algorithm in ("lstm", "pa") for fold in all_folds(protocol)}
    actual = {(m["algorithm"], m["fold"]["test"], m["fold"]["validation"]) for m in models}
    if len(models) != len(expected) or actual != expected:
        raise ValueError("Expected both algorithms for all 20 branch splits")
    for model in models:
        if model["fold"] != fold_spec(protocol, model["fold"]["test"], model["fold"]["validation"]):
            raise ValueError("Training branches differ from the protocol")
        runs = model["runs"]
        if len(runs) != len(protocol["reporting_seeds"]) or {r["seed"] for r in runs} != set(protocol["reporting_seeds"]):
            raise ValueError("Missing or duplicate reporting seeds")
        for run in runs:
            path = prediction_path(output, model["algorithm"], model["fold"], run["seed"])
            if file_hash(path) != run["prediction_sha256"]:
                raise ValueError(f"Prediction changed: {path}")
    return models, metadata


def canonical_paths(seeds, adjacency: Mapping[int, list[int]], maximum_depth=20):
    """First BFS path under fixed seed/neighbor ordering; no labels are read."""
    paths = {int(seed): [int(seed)] for seed in seeds}
    queue = deque(int(seed) for seed in seeds)
    while queue:
        node = queue.popleft()
        path = paths[node]
        if len(path) - 1 >= maximum_depth:
            continue
        for neighbor in adjacency.get(node, []):
            if neighbor not in paths:
                paths[neighbor] = path + [neighbor]
                queue.append(neighbor)
    return paths


def gated_traversal(branch, scores: Mapping[int, float], threshold: float, maximum_iterations=20):
    """Every encountered non-seed needs a score; no automatic bridge traversal."""
    adjacency = branch["adjacency"]
    seeds = set(branch["seeds"])
    visited, included = set(seeds), set(seeds)
    frontier = {v for seed in seeds for v in adjacency.get(seed, [])} - visited
    iterations = []
    for iteration in range(1, maximum_iterations + 1):
        candidates = frontier - visited
        if not candidates:
            break
        missing = candidates - scores.keys()
        if missing:
            raise ValueError(f"Missing decision scores for {len(missing)} reached candidates")
        visited.update(candidates)
        kept = {node for node in candidates if scores[node] >= threshold}
        included.update(kept)
        iterations.append({"iteration": iteration, "evaluated": len(candidates),
                           "included": len(kept), "pruned": len(candidates - kept)})
        if not kept:
            break
        frontier = {v for node in kept for v in adjacency.get(node, [])} - visited
    return included, visited, iterations


def fixed_metrics(branch, included):
    """All scored decisions contribute, including never-reached negatives."""
    decisions = [row for row in branch["decisions"] if row["scored"]]
    tp = sum(row["label"] == 1 and row["node"] in included for row in decisions)
    fp = sum(row["label"] == 0 and row["node"] in included for row in decisions)
    fn = sum(row["label"] == 1 and row["node"] not in included for row in decisions)
    tn = sum(row["label"] == 0 and row["node"] not in included for row in decisions)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": len(decisions),
            "precision": precision, "recall": recall,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
            "accuracy": (tp + tn) / len(decisions) if decisions else 0.0}


def evaluate_scores(branch, scores, threshold, maximum_iterations=20):
    included, visited, iterations = gated_traversal(branch, scores, threshold, maximum_iterations)
    return fixed_metrics(branch, included), included, visited, iterations


def select_threshold(branch, scores, maximum_iterations=20):
    trials = []
    for value in range(101):
        threshold = value / 100
        metrics, _, _, _ = evaluate_scores(branch, scores, threshold, maximum_iterations)
        trials.append({"threshold": threshold, **metrics})
    best = max(trials, key=lambda row: (row["f1"], row["precision"], row["recall"],
                                        -abs(row["threshold"] - 0.5), -row["threshold"]))
    return best, trials


def load_benchmark(path: Path | None = None):
    data = read_json(path or HERE / "data" / "benchmark.json")
    for branch in data["branches"].values():
        branch["adjacency"] = {int(k): v for k, v in branch["adjacency"].items()}
    return data
