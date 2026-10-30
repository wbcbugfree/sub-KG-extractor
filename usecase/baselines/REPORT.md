# NALT LSTM and Path Analogy results

Five held-out test branches, four validation choices per test branch, and three training branches per split: 20 splits, 40 selected configurations and 120 final evaluations across seeds 41, 42 and 43. All use one frozen PBG embedding and the same 7,902 scored decisions.

## Results across all assignments

F1 values are percentages. Means weight branches and validation choices equally. The ± values describe sample standard deviations across three per-seed averages, with embeddings fixed. They do not measure uncertainty across topics or assignments.

| Method | Soil science | Pest management | Plant health | Immunology | Cell biology | Macro F1 |
|---|---:|---:|---:|---:|---:|---:|
| LSTM | 48.35 ± 6.55 | 70.44 ± 5.01 | 32.18 ± 3.13 | 42.62 ± 1.99 | 42.89 ± 2.55 | 47.29 ± 0.69 |
| PA | 51.61 ± 9.23 | 65.83 ± 4.57 | 37.93 ± 3.95 | 46.63 ± 2.92 | 36.56 ± 3.14 | 47.71 ± 4.14 |
| deepseek-v4-pro / manual (cached replay) | 89.19 | 93.66 | 96.33 | 83.56 | 77.46 | 88.04 |
| deepseek-v4-pro / meta (cached replay) | 83.21 | 87.36 | 93.13 | 64.96 | 79.74 | 81.68 |
| gemini-3.5-flash / manual (cached replay) | 88.97 | 94.56 | 96.02 | 83.27 | 76.13 | 87.79 |
| gemini-3.5-flash / meta (cached replay) | 83.21 | 88.89 | 85.25 | 81.07 | 78.36 | 83.35 |
| gpt-5.4-mini / manual (cached replay) | 86.55 | 84.55 | 86.25 | 79.19 | 74.03 | 82.11 |
| gpt-5.4-mini / meta (cached replay) | 82.88 | 76.23 | 66.56 | 69.93 | 70.88 | 73.30 |
| Always INCLUDE | 68.44 | 55.16 | 25.47 | 42.91 | 48.26 | 48.05 |

The LLM rows replay retained judgments under the common candidate population and five-root mask; they are not fresh provider runs.

| Model | Macro precision | Macro recall | Macro F1 | Macro accuracy |
|---|---:|---:|---:|---:|
| LSTM | 49.25 ± 1.81 | 62.91 ± 1.93 | 47.29 ± 0.69 | 62.77 ± 2.10 |
| PA | 48.79 ± 2.99 | 64.11 ± 1.27 | 47.71 ± 4.14 | 60.70 ± 6.21 |

## Assignment schedules

Schedules 1–4 choose the validation branch one, two, three or four positions after the test branch in the cyclic order Soil science, Pest management, Plant health, Immunology, Cell biology. Each schedule contains five splits. All four schedules are reported equally; the table gives their observed macro F1.

| Schedule | LSTM macro F1 | PA macro F1 |
|---|---:|---:|
| 1 | 56.51 ± 2.72 | 52.56 ± 3.15 |
| 2 | 44.16 ± 2.59 | 42.80 ± 5.60 |
| 3 | 54.26 ± 1.65 | 46.78 ± 4.87 |
| 4 | 34.25 ± 1.84 | 48.71 ± 5.21 |

## Every test/validation pair

Each cell is mean F1 ± sample SD over the three seeds. Rows identify the test branch; columns identify validation. The other three branches are used for training. No split is selected using its test score.

### LSTM

| Test / validation | Soil science | Pest management | Plant health | Immunology | Cell biology |
|---|---:|---:|---:|---:|---:|
| Soil science | — | 68.68 ± 4.29 | 33.78 ± 12.62 | 81.52 ± 0.67 | 9.41 ± 15.64 |
| Pest management | 58.66 ± 11.44 | — | 74.78 ± 6.71 | 80.85 ± 3.94 | 67.47 ± 3.88 |
| Plant health | 42.88 ± 8.68 | 21.03 ± 10.98 | — | 43.91 ± 2.52 | 20.91 ± 2.25 |
| Immunology | 43.22 ± 0.42 | 43.76 ± 5.50 | 38.83 ± 2.62 | — | 44.65 ± 1.63 |
| Cell biology | 50.54 ± 1.39 | 42.05 ± 4.03 | 35.66 ± 5.72 | 43.32 ± 1.41 | — |

### PA

| Test / validation | Soil science | Pest management | Plant health | Immunology | Cell biology |
|---|---:|---:|---:|---:|---:|
| Soil science | — | 48.85 ± 2.02 | 25.05 ± 13.10 | 67.22 ± 12.97 | 65.32 ± 17.25 |
| Pest management | 57.57 ± 1.94 | — | 74.85 ± 12.40 | 73.73 ± 5.97 | 57.17 ± 1.93 |
| Plant health | 40.03 ± 12.61 | 37.84 ± 6.48 | — | 41.38 ± 6.04 | 32.47 ± 5.24 |
| Immunology | 50.47 ± 6.84 | 42.13 ± 4.00 | 43.76 ± 3.82 | — | 50.15 ± 4.45 |
| Cell biology | 47.59 ± 4.22 | 32.25 ± 2.24 | 27.33 ± 4.88 | 39.08 ± 7.49 | — |

## Protocol and retained files

Configuration selection uses seed-41 validation F1; each seed selects its own cutoff using validation data. Early stopping uses validation loss. PA references are training positives only. Every encountered non-seed receives a decision; only INCLUDE decisions expand, and unreached scored nodes count as EXCLUDE. The embedding uses unlabeled graph structure, including held-out branch concepts, so this is transductive transfer.

The observed-negative pool inherits earlier LLM traversal coverage. There are only five topics, and overlapping splits are not independent samples. Macro F1 is averaged separately from macro precision and recall.

Recorded neural fits: 320. Summed training time: 195.6 minutes. Peak memory in one model process: 1.99 GB. Execution timing and environment details are retained in `results/experiment.json`.

- [Per-seed results](results/summary.csv) and [per-assignment results](results/assignment_summary.csv).
- [Selected models and per-seed metadata](results/selected_models.json); all detailed fit/search logs live under `tmp/`.
- [Aggregate metrics](results/summary.json), [experiment metadata](results/experiment.json), and [embedding selection](results/embedding.json).
- [Reproduction instructions](README.md). Frozen embeddings remain in `artifacts/`; retraining PBG is not bitwise deterministic.
