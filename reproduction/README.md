# Bridge-Aware Path Analogy and LSTM Reproduction

This package evaluates Path Analogy and LSTM on the aligned runtime sets for
Dataset 1, Dataset 2, and Dataset 3. Dataset 3 uses bridge-inclusive traversal over
`wikidata/dataset3/dataset3_subgraph_2022.ttl`; bridge nodes serve as traversal
intermediates, and labeled candidate nodes form the decision outputs.

Each evaluation row is the serialized record of one candidate decision
instance. We use **decision instance** for the semantic evaluation unit and
**row** only for CSV records or artifact fields. `KEEP` and `PRUNE` are the
original Analogical Pruning labels and correspond to `INCLUDE` and `EXCLUDE`,
respectively.

## Contents

- `src/`: adapted KGPrune/analogical-pruning model and evaluation code.
- `bridge_traversal.py`: decision-only traversal for Datasets 1 and 2 and
  bridge-aware BFS for Dataset 3.
- `folds/`: Dataset 1/2 seed folds and the overlap-reduced Dataset 3 seed split.
  The JSON summary records the Dataset 3 search objective and per-fold overlap.
- `results/`: final metrics, Dataset 3 tuning summaries, and serialized
  decision records.
- `REPORT.md`: preprocessing behavior, edge-case analysis, and results.

Dataset 3 uses `folds/dataset3_overlap_reduced_5_folds.pkl`, which defines
disjoint train/test seed partitions.

## Environment

TensorFlow 2.12 requires Python 3.10 or 3.11:

```powershell
conda create -p tmp\kgprune_bridge_path_analogy\kgb310 python=3.10 -y
conda run -p tmp\kgprune_bridge_path_analogy\kgb310 python -m pip install -r reproduction\requirements.txt
```

## Runtime Data

Store runtime LMDBs, filtered CSVs, predictions, and PBG vectors under
`tmp/kgprune_bridge_path_analogy/`. Build graph LMDBs from the three checked-in
TTL files with `build_local_lmdb_artifacts.py`, inspect the 2019 PBG names with
`inspect_pbg_names.py`, extract required vectors with
`extract_pbg_vectors.py`, then run:

```powershell
tmp\kgprune_bridge_path_analogy\kgb310\python.exe reproduction\prepare_runtime_inputs.py `
  --names-manifest tmp\kgprune_bridge_path_analogy\e1_combined_names_manifest.json `
  --dataset3-wikidata-lmdb tmp\kgprune_bridge_path_analogy\dataset3\wikidata_lmdb_ap

tmp\kgprune_bridge_path_analogy\kgb310\python.exe reproduction\run_full_path_analogy_repro.py `
  --embeddings-lmdb tmp\kgprune_bridge_path_analogy\e1_embeddings_lmdb `
  --python tmp\kgprune_bridge_path_analogy\kgb310\python.exe --run
```

Prepared aligned runtime inputs contain 4,400 Dataset 1 decision instances,
868 Dataset 2 decision instances, and 1,384 Dataset 3 decision instances. The
runner stores training artifacts and threshold trials under `tmp/`, metrics and
tuning summaries under `results/metrics/` and `results/tuning/`, and complete
serialized decision records under `results/decisions/`.

## Metric aggregation

Each overall and seen/unseen metric CSV reports two estimators over the same
complete prediction set:

- `weighted <metric>` is the mean of the five fold-level metric values weighted
  by the number of evaluated decision instances in each fold; `weighted std
  <metric>` is the matching weighted population standard deviation.
- `pooled <metric>` is calculated once from the combined predictions across all
  five folds. The CSV also stores the pooled confusion counts.

Dataset 3 evaluates the Dataset 1 and Dataset 2 paper configurations as two
separate fixed candidates for each algorithm. Within a candidate, the same
architecture and training settings are used for all five outer folds. One
universal voting threshold is swept from `0.00`--`1.00` in increments of
`0.01`. For every configuration--threshold pair, F1 is calculated on each of
the five validation folds and averaged using the aligned decision-instance
counts as weights. The pair with the highest weighted validation F1 is then
applied unchanged to all five test folds. The selected settings are the
Dataset 2 configuration with threshold `0.31` for Path Analogy and the Dataset
1 configuration with threshold `0.39` for LSTM. The complete validation curves
and selected test metrics are recorded under `results/tuning/`.

Path Analogy and LSTM subprocesses use random seed `41` by default. The runners
set `PYTHONHASHSEED`, enable deterministic TensorFlow operations, and seed
Python, NumPy, and TensorFlow before training. `--random-seed` selects another
seed.
