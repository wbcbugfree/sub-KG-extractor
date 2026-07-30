# Path Analogy and LSTM Reproduction Report

## Scope

This report records the aligned-runtime evaluation of LSTM and Path Analogy.
The models use the Wikidata Sub-KG Extractor comparison sets: 4,400 decision
instances for Dataset 1, 868 for Dataset 2, and 1,384 for Dataset 3. Datasets 1
and 2 use decision-only traversal. Dataset 3 uses bridge-aware traversal over
the local 2022 subgraph; embedded intermediate nodes are traversed, and labeled
candidate decision instances are classified and scored.

Each evaluation row is the serialized record of one candidate decision
instance. We use **decision instance** for the semantic evaluation unit and
**row** only for CSV records or artifact fields. `KEEP` and `PRUNE` are the
original Analogical Pruning labels and correspond to `INCLUDE` and `EXCLUDE`,
respectively.

All neural subprocesses use random seed `41`. The runners set
`PYTHONHASHSEED`, enable deterministic TensorFlow operations, and seed Python,
NumPy, and TensorFlow before training. Complete prediction sets are reported
both as five-fold metrics weighted by scored decision-instance count (with
weighted population standard deviations) and as metrics calculated from pooled
predictions.

## Runtime Inputs

The builder starts from source decision instances, applies the graph,
English-label, and labeled-demonstration filters, then requires PBG-covered
seeds and PBG-traversable local paths and removes strict gold
`PRUNE`-ancestor/`KEEP`-descendant conflicts. The intermediate counts in the
second numeric column precede the local-path and strict-conflict filters and
are therefore not the full-common-set sizes defined in the paper supplement.

| Dataset | Source decision instances | After graph/label/demonstration filters | PBG-traversable instances | Strict conflicts removed | Aligned runtime | Seeds | KEEP | PRUNE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Dataset 1 | 5,233 | 5,072 | 4,400 | 0 | 4,400 | 436 | 1,601 | 2,799 |
| Dataset 2 | 982 | 938 | 868 | 0 | 868 | 103 | 574 | 294 |
| Dataset 3 | 2,111 | 2,067 | 1,437 | 53 | 1,384 | 20 | 530 | 854 |

Datasets 1 and 2 use their checked-in five-fold splits. Dataset 3 uses
`folds/dataset3_overlap_reduced_5_folds.pkl`.

## Parameter Settings

Dataset 1 and Dataset 2 use the architecture and training settings reported for
the analogical-pruning paper together with the reference pipeline's fixed
voting settings. The next fold is used for validation, with early-stopping
patience 5 for Path Analogy and 20 for LSTM.

| Algorithm | Dataset | Sequence | Architecture/padding | Learning rate | Epoch cap | Analogy counts | Fixed threshold |
| --- | --- | ---: | --- | ---: | ---: | --- | ---: |
| Path Analogy | Dataset 1 | 4 | 16/8 filters, dropout 0.0, between | 0.001 | 50 | train 5, test 5 | 0.58 |
| Path Analogy | Dataset 2 | 3 | 4/2 filters, dropout 0.3, between | 0.001 | 50 | train 20, test 20 | 0.30 |
| LSTM | Dataset 1 | 5 | 150 units, before | 0.01 | 200 | -- | 0.40 |
| LSTM | Dataset 2 | 3 | 150 units, before | 0.001 | 200 | -- | 0.20 |

Dataset 3 evaluates the two published configurations as separate fixed
candidates: a candidate's architecture and training settings remain unchanged
across all five outer folds. For each configuration, one universal voting
threshold is swept from `0.00` to `1.00` in `0.01` increments. Each threshold
is evaluated on all five validation folds, and their F1 values are averaged
using the number of aligned decision instances in each fold as weights. The
configuration--threshold pair with the highest weighted validation F1 is then
applied unchanged to all five outer test folds. Test folds 1--5 use validation
folds 2, 3, 4, 5, and 1, respectively.

| Algorithm | Fixed config | Universal threshold | Weighted validation F1 | Weighted test F1 | Selected |
| --- | --- | ---: | ---: | ---: | --- |
| Path Analogy | D1 | 0.52 | 82.54 | 82.98 |  |
| Path Analogy | D2 | 0.31 | **83.60** | 72.01 | Yes |
| LSTM | D1 | 0.39 | **83.99** | 84.50 | Yes |
| LSTM | D2 | 0.69 | 82.58 | 70.62 |  |

Here D1 and D2 denote the Dataset 1 and Dataset 2 paper configurations in the
preceding table. Both the configuration and universal threshold are selected
from validation predictions; outer-test predictions are used only for the
reported final metrics.

## Overall Results

Five-fold mean weighted by scored decision-instance count +/- weighted
population standard deviation:

| Algorithm | Dataset/configuration | Precision | Recall | F1 | Accuracy |
| --- | --- | ---: | ---: | ---: | ---: |
| Path Analogy | Dataset 1 paper config | 80.84 +/- 7.10 | 67.98 +/- 6.19 | 73.44 +/- 4.13 | 81.95 +/- 5.15 |
| LSTM | Dataset 1 paper config | 75.53 +/- 5.85 | 75.19 +/- 9.49 | 74.59 +/- 1.72 | 81.52 +/- 2.84 |
| Path Analogy | Dataset 2 paper config | 80.04 +/- 8.03 | 91.14 +/- 6.93 | 84.64 +/- 3.49 | 78.46 +/- 6.57 |
| LSTM | Dataset 2 paper config | 77.14 +/- 8.75 | 90.83 +/- 5.99 | 82.82 +/- 3.89 | 75.58 +/- 7.11 |
| Path Analogy | Dataset 3, D2 config, threshold 0.31 | 62.02 +/- 16.82 | 92.31 +/- 8.80 | 72.01 +/- 10.09 | 72.04 +/- 11.18 |
| LSTM | Dataset 3, D1 config, threshold 0.39 | 78.45 +/- 6.95 | 92.30 +/- 3.34 | 84.50 +/- 2.62 | 87.14 +/- 2.62 |

Pooled-prediction metrics over the same decision instances:

| Algorithm | Dataset/configuration | Precision | Recall | F1 | Accuracy |
| --- | --- | ---: | ---: | ---: | ---: |
| Path Analogy | Dataset 1 paper config | 79.18 | 68.39 | 73.39 | 81.95 |
| LSTM | Dataset 1 paper config | 74.26 | 75.33 | 74.79 | 81.52 |
| Path Analogy | Dataset 2 paper config | 79.63 | 90.59 | 84.76 | 78.46 |
| LSTM | Dataset 2 paper config | 76.85 | 90.24 | 83.01 | 75.58 |
| Path Analogy | Dataset 3, D2 config, threshold 0.31 | 58.73 | 90.75 | 71.31 | 72.04 |
| LSTM | Dataset 3, D1 config, threshold 0.39 | 78.30 | 91.89 | 84.55 | 87.14 |

## Seen and Unseen Candidate Nodes

Metric order is precision/recall/F1/accuracy. Weighted-fold entries are mean
+/- weighted population standard deviation; pooled entries are point estimates.

| Algorithm | Dataset | Split | Weighted-fold P/R/F1/Acc (mean +/- SD) | Pooled P/R/F1/Acc |
| --- | --- | --- | ---: | ---: |
| Path Analogy | Dataset 1 | Unseen | 75.77 +/- 9.33/60.27 +/- 8.98/66.26 +/- 6.50/77.77 +/- 7.45 | 72.99/61.53/66.77/77.77 |
| Path Analogy | Dataset 1 | Seen | 85.09 +/- 6.99/73.61 +/- 4.47/78.63 +/- 3.03/85.17 +/- 4.00 | 83.71/73.65/78.36/85.17 |
| Path Analogy | Dataset 2 | Unseen | 76.92 +/- 8.17/91.34 +/- 7.98/82.80 +/- 3.38/76.63 +/- 7.19 | 76.37/90.65/82.90/76.63 |
| Path Analogy | Dataset 2 | Seen | 96.37 +/- 1.48/90.19 +/- 6.05/93.05 +/- 2.95/88.64 +/- 4.83 | 96.26/90.35/93.21/88.64 |
| LSTM | Dataset 1 | Unseen | 69.59 +/- 5.45/69.57 +/- 11.93/68.54 +/- 3.58/77.35 +/- 4.34 | 68.20/70.46/69.31/77.35 |
| LSTM | Dataset 1 | Seen | 80.51 +/- 6.85/79.15 +/- 7.98/79.15 +/- 1.06/84.73 +/- 2.86 | 79.05/79.05/79.05/84.73 |
| LSTM | Dataset 2 | Unseen | 73.67 +/- 8.71/90.41 +/- 6.84/80.50 +/- 3.71/73.10 +/- 7.81 | 73.31/89.57/80.63/73.10 |
| LSTM | Dataset 2 | Seen | 94.41 +/- 4.15/92.80 +/- 5.72/93.51 +/- 4.18/89.39 +/- 6.59 | 94.64/92.98/93.81/89.39 |

Dataset 3 selected-configuration, universal-threshold seen/unseen results:

| Algorithm | Split | Weighted-fold P/R/F1/Acc (mean +/- SD) | Pooled P/R/F1/Acc |
| --- | --- | ---: | ---: |
| Path Analogy | Seen | 73.15 +/- 7.78/95.77 +/- 5.24/82.55 +/- 4.58/86.73 +/- 3.99 | 72.61/95.11/82.35/86.73 |
| Path Analogy | Unseen | 56.16 +/- 19.84/90.79 +/- 10.70/66.00 +/- 12.00/61.90 +/- 13.24 | 52.94/88.44/66.23/61.90 |
| LSTM | Seen | 93.89 +/- 6.93/96.81 +/- 1.63/95.14 +/- 3.57/97.17 +/- 1.31 | 94.68/96.74/95.70/97.17 |
| LSTM | Unseen | 70.76 +/- 7.05/90.20 +/- 4.52/78.90 +/- 2.47/80.22 +/- 4.42 | 71.20/89.31/79.23/80.22 |

## Reached Test Candidate Nodes Seen in Training

Percentages follow the paper Table 2 definition and exclude the validation fold
from training.

| Dataset | Fold 1 | Fold 2 | Fold 3 | Fold 4 | Fold 5 | Average |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dataset 1 | 56.22 | 60.62 | 51.19 | 55.70 | 59.82 | 56.71 |
| Dataset 2 | 20.62 | 21.93 | 12.45 | 17.04 | 9.28 | 16.26 |
| Dataset 3 | 43.85 | 32.37 | 57.89 | 75.00 | 47.37 | 51.30 |

## Outputs and Validation

Compact metrics are stored under `results/metrics/`, and the two Dataset 3
selection summaries are stored under `results/tuning/`.

`results/decisions/` contains one CSV record for every aligned decision
instance. Each record stores `row_id`, fold, seed, QID, gold target, prediction,
correctness, reachability, `decision_source`, and model depth.
`decision_source=model` denotes explicit classification;
`decision_source=bfs_auto_prune` denotes a descendant assigned `PRUNE` after an
upstream predicted `PRUNE`.

All six final result sets pass checks for aligned and unique `row_id` values,
five-fold coverage, binary targets and predictions, BFS provenance counts, and
stored metric agreement with serialized predictions at absolute tolerance
`1e-12`.
