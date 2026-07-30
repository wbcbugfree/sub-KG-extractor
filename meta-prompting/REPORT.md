# Meta-Prompting Experiment Report

## Scope

This report summarizes the Wikidata evaluations of human-authored and
meta-generated prompts for Gemini 3.5 Flash, GPT-5.4 mini, and
DeepSeek-V4-Pro across Datasets 1, 2, and 3.

Gemini and GPT evaluations use `full_dataset_batch_runner.py`; DeepSeek
evaluations use `subkg_extractor_wikidata.py`. Under BFS-gated scoring, a
gold-labeled decision instance that is not reached after an upstream exclusion
is assigned `EXCLUDE` rather than omitted.

Each evaluation row is the serialized record of one candidate decision
instance. We use **decision instance** for the semantic unit and **row** only
for CSV records and artifact fields. `KEEP` and `PRUNE` are the original
Analogical Pruning labels and correspond to `INCLUDE` and `EXCLUDE`,
respectively.

All labeled demonstrations and candidate inputs use the path-aware text
structure:

```text
- Seed: <seed label>
  Candidate: candidate=<candidate label> | path=<seed --predicate--> ... --> <candidate> | decision=<INCLUDE/EXCLUDE>
```

The candidate prompt omits `decision`. Dataset 3 uses bridge-aware traversal:
unlabelled bridge nodes support traversal and appear inside the path evidence
for the final labelled candidate node.

## Provenance

Gemini and GPT batch `manifest.json` files record prompt and
labeled-demonstration files and hashes, context level, and run directory.
DeepSeek run provenance is stored in the direct-run JSON files. The
corresponding BFS-gated prediction CSVs are the serialized sources for the
full-common and aligned metrics.

## Inputs

Full-common evaluation excludes decision instances whose seed or candidate
node is unavailable in the local graph, the union of labeled demonstrations
used by the compared strategies, local-path failures, and strict
gold-`PRUNE`-ancestor/gold-`KEEP`-descendant conflicts. The resulting counts
are 5,050 decision instances for Dataset 1, 935 for Dataset 2, and 2,014 for
Dataset 3. The Dataset 3 aligned runtime set contains 1,384 decision instances:
530 `KEEP` and 854 `PRUNE` decisions across 20 seeds.

Datasets 2 and 3 use content-identical human-authored and meta-prompting
labeled demonstration files; Dataset 1 uses distinct sets.

| Dataset | Human-Authored Demonstration File | Human-Authored Demonstrations | INCLUDE | EXCLUDE | Meta-Prompting Demonstration File | Meta-Prompting Demonstrations | INCLUDE | EXCLUDE |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 1 | `dataset1_path_simple_manual_examples.json` | 18 | 12 | 6 | `dataset1_path_simple_meta_examples.json` | 32 | 16 | 16 |
| 2 | `dataset2_path_simple_manual_examples.json` | 18 | 12 | 6 | `dataset2_path_simple_meta_examples.json` | 18 | 12 | 6 |
| 3 | `dataset3_path_simple_manual_examples.json` | 21 | 12 | 9 | `dataset3_path_simple_meta_examples.json` | 21 | 12 | 9 |

## Common Aligned Metric Views

The aligned comparison reports two estimators for every baseline and Sub-KG
Extractor configuration. Weighted metrics are means across the five folds,
using the number of scored decision instances in each fold as the weight, and
include weighted population standard deviations. Pooled metrics are calculated
jointly from all serialized predictions. Full-precision values, per-fold
scores, and pooled confusion counts are stored in
`results/wikidata_aligned_metric_views.csv` and
`results/wikidata_aligned_metric_views.json`.

### Dataset 1

| Method | Weighted P (mean +/- SD) | Weighted R (mean +/- SD) | Weighted F1 (mean +/- SD) | Weighted Acc (mean +/- SD) | Pooled P | Pooled R | Pooled F1 | Pooled Acc |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LSTM | 0.755 +/- 0.058 | 0.752 +/- 0.095 | 0.746 +/- 0.017 | 0.815 +/- 0.028 | 0.743 | 0.753 | 0.748 | 0.815 |
| Path Analogy | 0.808 +/- 0.071 | 0.680 +/- 0.062 | 0.734 +/- 0.041 | 0.820 +/- 0.051 | 0.792 | 0.684 | 0.734 | 0.820 |
| Gemini 3.5 Flash / zero-shot human-authored | 0.858 +/- 0.009 | 0.757 +/- 0.043 | 0.804 +/- 0.026 | 0.866 +/- 0.025 | 0.857 | 0.759 | 0.805 | 0.866 |
| Gemini 3.5 Flash / few-shot human-authored | 0.855 +/- 0.023 | 0.771 +/- 0.029 | 0.810 +/- 0.024 | 0.870 +/- 0.022 | 0.856 | 0.771 | 0.811 | 0.870 |
| Gemini 3.5 Flash / zero-shot meta-prompting | 0.770 +/- 0.045 | 0.651 +/- 0.042 | 0.705 +/- 0.042 | 0.805 +/- 0.019 | 0.775 | 0.656 | 0.710 | 0.805 |
| Gemini 3.5 Flash / few-shot meta-prompting | 0.802 +/- 0.035 | 0.648 +/- 0.045 | 0.716 +/- 0.037 | 0.815 +/- 0.023 | 0.805 | 0.651 | 0.720 | 0.815 |
| GPT-5.4 mini / zero-shot human-authored | 0.805 +/- 0.031 | 0.647 +/- 0.050 | 0.716 +/- 0.030 | 0.812 +/- 0.041 | 0.804 | 0.640 | 0.712 | 0.812 |
| GPT-5.4 mini / few-shot human-authored | 0.813 +/- 0.028 | 0.687 +/- 0.027 | 0.744 +/- 0.013 | 0.828 +/- 0.026 | 0.813 | 0.685 | 0.743 | 0.828 |
| GPT-5.4 mini / zero-shot meta-prompting | 0.644 +/- 0.045 | 0.760 +/- 0.049 | 0.694 +/- 0.015 | 0.758 +/- 0.030 | 0.643 | 0.753 | 0.694 | 0.758 |
| GPT-5.4 mini / few-shot meta-prompting | 0.661 +/- 0.025 | 0.733 +/- 0.029 | 0.694 +/- 0.013 | 0.766 +/- 0.029 | 0.662 | 0.729 | 0.694 | 0.766 |
| DeepSeek-V4-Pro / zero-shot human-authored | 0.945 +/- 0.024 | 0.449 +/- 0.024 | 0.608 +/- 0.025 | 0.790 +/- 0.032 | 0.945 | 0.449 | 0.609 | 0.790 |
| DeepSeek-V4-Pro / few-shot human-authored | 0.946 +/- 0.019 | 0.473 +/- 0.014 | 0.631 +/- 0.015 | 0.799 +/- 0.030 | 0.947 | 0.473 | 0.631 | 0.799 |
| DeepSeek-V4-Pro / zero-shot meta-prompting | 0.877 +/- 0.014 | 0.600 +/- 0.022 | 0.712 +/- 0.014 | 0.825 +/- 0.021 | 0.876 | 0.603 | 0.714 | 0.825 |
| DeepSeek-V4-Pro / few-shot meta-prompting | 0.878 +/- 0.028 | 0.657 +/- 0.026 | 0.751 +/- 0.022 | 0.843 +/- 0.019 | 0.878 | 0.660 | 0.753 | 0.843 |

### Dataset 2

| Method | Weighted P (mean +/- SD) | Weighted R (mean +/- SD) | Weighted F1 (mean +/- SD) | Weighted Acc (mean +/- SD) | Pooled P | Pooled R | Pooled F1 | Pooled Acc |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LSTM | 0.771 +/- 0.087 | 0.908 +/- 0.060 | 0.828 +/- 0.039 | 0.756 +/- 0.071 | 0.769 | 0.902 | 0.830 | 0.756 |
| Path Analogy | 0.800 +/- 0.080 | 0.911 +/- 0.069 | 0.846 +/- 0.035 | 0.785 +/- 0.066 | 0.796 | 0.906 | 0.848 | 0.785 |
| Gemini 3.5 Flash / zero-shot human-authored | 0.952 +/- 0.029 | 0.898 +/- 0.031 | 0.923 +/- 0.019 | 0.903 +/- 0.029 | 0.957 | 0.894 | 0.924 | 0.903 |
| Gemini 3.5 Flash / few-shot human-authored | 0.940 +/- 0.034 | 0.934 +/- 0.034 | 0.936 +/- 0.027 | 0.919 +/- 0.037 | 0.945 | 0.932 | 0.939 | 0.919 |
| Gemini 3.5 Flash / zero-shot meta-prompting | 0.984 +/- 0.012 | 0.777 +/- 0.043 | 0.867 +/- 0.025 | 0.840 +/- 0.058 | 0.987 | 0.768 | 0.864 | 0.840 |
| Gemini 3.5 Flash / few-shot meta-prompting | 0.980 +/- 0.013 | 0.810 +/- 0.042 | 0.887 +/- 0.025 | 0.862 +/- 0.051 | 0.983 | 0.805 | 0.885 | 0.862 |
| GPT-5.4 mini / zero-shot human-authored | 0.962 +/- 0.024 | 0.783 +/- 0.047 | 0.862 +/- 0.023 | 0.834 +/- 0.050 | 0.965 | 0.777 | 0.861 | 0.834 |
| GPT-5.4 mini / few-shot human-authored | 0.949 +/- 0.029 | 0.810 +/- 0.049 | 0.873 +/- 0.030 | 0.842 +/- 0.057 | 0.951 | 0.803 | 0.871 | 0.842 |
| GPT-5.4 mini / zero-shot meta-prompting | 0.951 +/- 0.019 | 0.710 +/- 0.054 | 0.812 +/- 0.034 | 0.781 +/- 0.074 | 0.955 | 0.702 | 0.809 | 0.781 |
| GPT-5.4 mini / few-shot meta-prompting | 0.943 +/- 0.023 | 0.762 +/- 0.047 | 0.841 +/- 0.022 | 0.809 +/- 0.060 | 0.945 | 0.754 | 0.839 | 0.809 |
| DeepSeek-V4-Pro / zero-shot human-authored | 0.946 +/- 0.035 | 0.850 +/- 0.039 | 0.895 +/- 0.023 | 0.872 +/- 0.030 | 0.953 | 0.848 | 0.898 | 0.872 |
| DeepSeek-V4-Pro / few-shot human-authored | 0.950 +/- 0.019 | 0.891 +/- 0.020 | 0.919 +/- 0.016 | 0.900 +/- 0.021 | 0.953 | 0.892 | 0.922 | 0.900 |
| DeepSeek-V4-Pro / zero-shot meta-prompting | 0.955 +/- 0.035 | 0.840 +/- 0.030 | 0.893 +/- 0.023 | 0.871 +/- 0.031 | 0.960 | 0.840 | 0.896 | 0.871 |
| DeepSeek-V4-Pro / few-shot meta-prompting | 0.958 +/- 0.034 | 0.866 +/- 0.027 | 0.909 +/- 0.022 | 0.888 +/- 0.034 | 0.963 | 0.864 | 0.911 | 0.888 |

### Dataset 3

| Method | Weighted P (mean +/- SD) | Weighted R (mean +/- SD) | Weighted F1 (mean +/- SD) | Weighted Acc (mean +/- SD) | Pooled P | Pooled R | Pooled F1 | Pooled Acc |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LSTM | 0.785 +/- 0.069 | 0.923 +/- 0.033 | 0.845 +/- 0.026 | 0.871 +/- 0.026 | 0.783 | 0.919 | 0.845 | 0.871 |
| Path Analogy | 0.620 +/- 0.168 | 0.923 +/- 0.088 | 0.720 +/- 0.101 | 0.720 +/- 0.112 | 0.587 | 0.908 | 0.713 | 0.720 |
| Gemini 3.5 Flash / zero-shot human-authored | 0.797 +/- 0.088 | 0.851 +/- 0.061 | 0.817 +/- 0.029 | 0.857 +/- 0.020 | 0.795 | 0.843 | 0.819 | 0.857 |
| Gemini 3.5 Flash / few-shot human-authored | 0.849 +/- 0.037 | 0.911 +/- 0.042 | 0.877 +/- 0.017 | 0.905 +/- 0.008 | 0.852 | 0.909 | 0.880 | 0.905 |
| Gemini 3.5 Flash / zero-shot meta-prompting | 0.793 +/- 0.086 | 0.871 +/- 0.032 | 0.827 +/- 0.047 | 0.865 +/- 0.027 | 0.795 | 0.872 | 0.832 | 0.865 |
| Gemini 3.5 Flash / few-shot meta-prompting | 0.804 +/- 0.078 | 0.910 +/- 0.040 | 0.851 +/- 0.044 | 0.883 +/- 0.022 | 0.808 | 0.911 | 0.856 | 0.883 |
| GPT-5.4 mini / zero-shot human-authored | 0.701 +/- 0.125 | 0.761 +/- 0.068 | 0.724 +/- 0.086 | 0.787 +/- 0.049 | 0.702 | 0.770 | 0.734 | 0.787 |
| GPT-5.4 mini / few-shot human-authored | 0.741 +/- 0.096 | 0.690 +/- 0.110 | 0.711 +/- 0.092 | 0.799 +/- 0.033 | 0.753 | 0.708 | 0.730 | 0.799 |
| GPT-5.4 mini / zero-shot meta-prompting | 0.663 +/- 0.122 | 0.577 +/- 0.060 | 0.609 +/- 0.054 | 0.721 +/- 0.034 | 0.655 | 0.574 | 0.612 | 0.721 |
| GPT-5.4 mini / few-shot meta-prompting | 0.727 +/- 0.078 | 0.612 +/- 0.086 | 0.664 +/- 0.082 | 0.773 +/- 0.021 | 0.742 | 0.625 | 0.678 | 0.773 |
| DeepSeek-V4-Pro / zero-shot human-authored | 0.821 +/- 0.076 | 0.786 +/- 0.049 | 0.799 +/- 0.030 | 0.850 +/- 0.027 | 0.821 | 0.779 | 0.800 | 0.850 |
| DeepSeek-V4-Pro / few-shot human-authored | 0.852 +/- 0.037 | 0.799 +/- 0.089 | 0.821 +/- 0.044 | 0.866 +/- 0.047 | 0.852 | 0.785 | 0.817 | 0.866 |
| DeepSeek-V4-Pro / zero-shot meta-prompting | 0.740 +/- 0.084 | 0.887 +/- 0.059 | 0.802 +/- 0.038 | 0.837 +/- 0.019 | 0.740 | 0.885 | 0.806 | 0.837 |
| DeepSeek-V4-Pro / few-shot meta-prompting | 0.770 +/- 0.061 | 0.904 +/- 0.044 | 0.829 +/- 0.028 | 0.860 +/- 0.019 | 0.771 | 0.902 | 0.831 | 0.860 |

## Full-Common-Set Gemini Human-Authored Results

Gemini human-authored prompting uses Batch API evaluations over every eligible
full-common decision instance. The aligned columns score the common runtime
instances defined above.

| Dataset | Setting | Decision instances | BFS P | BFS R | BFS F1 | BFS Acc | Aligned instances | Aligned pooled P | Aligned pooled R | Aligned pooled F1 | Aligned pooled Acc | Parse Errors | Predictions |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | zero_shot_manual | 5050 | 0.851 | 0.745 | 0.794 | 0.868 | 4400 | 0.857 | 0.759 | 0.805 | 0.866 | 0 | `results/gemini-3.5-flash/dataset1/zero_shot_manual/bfs_gated_predictions.csv` |
| 1 | few_shot_manual | 5050 | 0.848 | 0.761 | 0.802 | 0.871 | 4400 | 0.856 | 0.771 | 0.811 | 0.870 | 0 | `results/gemini-3.5-flash/dataset1/few_shot_manual/bfs_gated_predictions.csv` |
| 2 | zero_shot_manual | 935 | 0.955 | 0.889 | 0.921 | 0.906 | 868 | 0.957 | 0.894 | 0.924 | 0.903 | 0 | `results/gemini-3.5-flash/dataset2/zero_shot_manual/bfs_gated_predictions.csv` |
| 2 | few_shot_manual | 935 | 0.946 | 0.929 | 0.937 | 0.923 | 868 | 0.945 | 0.932 | 0.939 | 0.919 | 0 | `results/gemini-3.5-flash/dataset2/few_shot_manual/bfs_gated_predictions.csv` |
| 3 | zero_shot_manual | 2014 | 0.841 | 0.851 | 0.846 | 0.867 | 1384 | 0.795 | 0.843 | 0.819 | 0.857 | 0 | `results/gemini-3.5-flash/dataset3/zero_shot_manual/bfs_gated_predictions.csv` |
| 3 | few_shot_manual | 2014 | 0.881 | 0.902 | 0.892 | 0.906 | 1384 | 0.852 | 0.909 | 0.880 | 0.905 | 0 | `results/gemini-3.5-flash/dataset3/few_shot_manual/bfs_gated_predictions.csv` |

## Full-Common-Set GPT-5.4 Mini Human-Authored Results

GPT-5.4 mini human-authored prompting uses the BFS-gated scorer.

| Dataset | Setting | Decision instances | BFS P | BFS R | BFS F1 | BFS Acc | Aligned instances | Aligned pooled P | Aligned pooled R | Aligned pooled F1 | Aligned pooled Acc | Parse Errors | Predictions |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | zero_shot_manual | 5050 | 0.792 | 0.629 | 0.701 | 0.816 | 4400 | 0.804 | 0.640 | 0.712 | 0.812 | 0 | `results/gpt-5.4-mini/dataset1/zero_shot_manual/bfs_gated_predictions.csv` |
| 1 | few_shot_manual | 5050 | 0.802 | 0.674 | 0.732 | 0.831 | 4400 | 0.813 | 0.685 | 0.743 | 0.828 | 0 | `results/gpt-5.4-mini/dataset1/few_shot_manual/bfs_gated_predictions.csv` |
| 2 | zero_shot_manual | 935 | 0.961 | 0.772 | 0.856 | 0.840 | 868 | 0.965 | 0.777 | 0.861 | 0.834 | 0 | `results/gpt-5.4-mini/dataset2/zero_shot_manual/bfs_gated_predictions.csv` |
| 2 | few_shot_manual | 935 | 0.951 | 0.800 | 0.869 | 0.850 | 868 | 0.951 | 0.803 | 0.871 | 0.842 | 0 | `results/gpt-5.4-mini/dataset2/few_shot_manual/bfs_gated_predictions.csv` |
| 3 | zero_shot_manual | 2014 | 0.748 | 0.769 | 0.759 | 0.789 | 1384 | 0.702 | 0.770 | 0.734 | 0.787 | 0 | `results/gpt-5.4-mini/dataset3/zero_shot_manual/bfs_gated_predictions.csv` |
| 3 | few_shot_manual | 2014 | 0.809 | 0.696 | 0.749 | 0.799 | 1384 | 0.753 | 0.708 | 0.730 | 0.799 | 0 | `results/gpt-5.4-mini/dataset3/few_shot_manual/bfs_gated_predictions.csv` |

## Full-Common-Set DeepSeek-V4-Pro Human-Authored Results

DeepSeek-V4-Pro human-authored prompting uses provider-default reasoning and
BFS-gated scoring.

| Dataset | Setting | Decision instances | BFS P | BFS R | BFS F1 | BFS Acc | Aligned instances | Aligned pooled P | Aligned pooled R | Aligned pooled F1 | Aligned pooled Acc | Unmatched | Predictions |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | zero_shot_manual | 5050 | 0.945 | 0.446 | 0.606 | 0.801 | 4400 | 0.945 | 0.449 | 0.609 | 0.790 | 0 | `results/deepseek-v4-pro/dataset1_zero_shot_manual_deepseek-v4-pro.csv` |
| 1 | few_shot_manual | 5050 | 0.949 | 0.477 | 0.635 | 0.812 | 4400 | 0.947 | 0.473 | 0.631 | 0.799 | 0 | `results/deepseek-v4-pro/dataset1_few_shot_manual_deepseek-v4-pro.csv` |
| 2 | zero_shot_manual | 935 | 0.951 | 0.845 | 0.895 | 0.877 | 868 | 0.953 | 0.848 | 0.898 | 0.872 | 0 | `results/deepseek-v4-pro/dataset2_zero_shot_manual_deepseek-v4-pro.csv` |
| 2 | few_shot_manual | 935 | 0.954 | 0.889 | 0.920 | 0.905 | 868 | 0.953 | 0.892 | 0.922 | 0.900 | 0 | `results/deepseek-v4-pro/dataset2_few_shot_manual_deepseek-v4-pro.csv` |
| 3 | zero_shot_manual | 2014 | 0.866 | 0.775 | 0.818 | 0.852 | 1384 | 0.821 | 0.779 | 0.800 | 0.850 | 0 | `results/deepseek-v4-pro/dataset3_zero_shot_manual_deepseek-v4-pro.csv` |
| 3 | few_shot_manual | 2014 | 0.895 | 0.798 | 0.844 | 0.873 | 1384 | 0.852 | 0.785 | 0.817 | 0.866 | 0 | `results/deepseek-v4-pro/dataset3_few_shot_manual_deepseek-v4-pro.csv` |

## Full-Common-Set Gemini Meta-Prompting Results

Gemini meta-prompting uses one generated prompt per dataset; few-shot
extraction additionally includes the corresponding labeled demonstrations.

| Dataset | Setting | Decision instances | BFS P | BFS R | BFS F1 | BFS Acc | Aligned instances | Aligned pooled P | Aligned pooled R | Aligned pooled F1 | Aligned pooled Acc | Parse Errors | Predictions |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | zero_shot_meta | 5050 | 0.763 | 0.645 | 0.699 | 0.809 | 4400 | 0.775 | 0.656 | 0.710 | 0.805 | 0 | `results/gemini-3.5-flash/dataset1/zero_shot_meta/bfs_gated_predictions.csv` |
| 1 | few_shot_meta | 5050 | 0.790 | 0.641 | 0.708 | 0.818 | 4400 | 0.805 | 0.651 | 0.720 | 0.815 | 0 | `results/gemini-3.5-flash/dataset1/few_shot_meta/bfs_gated_predictions.csv` |
| 2 | zero_shot_meta | 935 | 0.987 | 0.765 | 0.862 | 0.848 | 868 | 0.987 | 0.768 | 0.864 | 0.840 | 0 | `results/gemini-3.5-flash/dataset2/zero_shot_meta/bfs_gated_predictions.csv` |
| 2 | few_shot_meta | 935 | 0.983 | 0.803 | 0.884 | 0.870 | 868 | 0.983 | 0.805 | 0.885 | 0.862 | 0 | `results/gemini-3.5-flash/dataset2/few_shot_meta/bfs_gated_predictions.csv` |
| 3 | zero_shot_meta | 2014 | 0.840 | 0.866 | 0.853 | 0.871 | 1384 | 0.795 | 0.872 | 0.832 | 0.865 | 0 | `results/gemini-3.5-flash/dataset3/zero_shot_meta/bfs_gated_predictions.csv` |
| 3 | few_shot_meta | 2014 | 0.849 | 0.906 | 0.877 | 0.890 | 1384 | 0.808 | 0.911 | 0.856 | 0.883 | 0 | `results/gemini-3.5-flash/dataset3/few_shot_meta/bfs_gated_predictions.csv` |

## Full-Common-Set GPT-5.4 Mini Meta-Prompting Results

GPT-5.4 mini meta-prompting uses the generated prompts under
`prompts/gpt-5.4-mini/`; few-shot extraction additionally includes the
corresponding labeled demonstrations.

| Dataset | Setting | Decision instances | BFS P | BFS R | BFS F1 | BFS Acc | Aligned instances | Aligned pooled P | Aligned pooled R | Aligned pooled F1 | Aligned pooled Acc | Parse Errors | Predictions |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | zero_shot_meta | 5050 | 0.626 | 0.745 | 0.680 | 0.759 | 4400 | 0.643 | 0.753 | 0.694 | 0.758 | 0 | `results/gpt-5.4-mini/dataset1/zero_shot_meta/bfs_gated_predictions.csv` |
| 1 | few_shot_meta | 5050 | 0.642 | 0.719 | 0.678 | 0.766 | 4400 | 0.662 | 0.729 | 0.694 | 0.766 | 0 | `results/gpt-5.4-mini/dataset1/few_shot_meta/bfs_gated_predictions.csv` |
| 2 | zero_shot_meta | 935 | 0.951 | 0.699 | 0.806 | 0.791 | 868 | 0.955 | 0.702 | 0.809 | 0.781 | 0 | `results/gpt-5.4-mini/dataset2/zero_shot_meta/bfs_gated_predictions.csv` |
| 2 | few_shot_meta | 935 | 0.939 | 0.750 | 0.834 | 0.815 | 868 | 0.945 | 0.754 | 0.839 | 0.809 | 0 | `results/gpt-5.4-mini/dataset2/few_shot_meta/bfs_gated_predictions.csv` |
| 3 | zero_shot_meta | 2014 | 0.695 | 0.588 | 0.637 | 0.712 | 1384 | 0.655 | 0.574 | 0.612 | 0.721 | 0 | `results/gpt-5.4-mini/dataset3/zero_shot_meta/bfs_gated_predictions.csv` |
| 3 | few_shot_meta | 2014 | 0.799 | 0.628 | 0.703 | 0.772 | 1384 | 0.742 | 0.625 | 0.678 | 0.773 | 0 | `results/gpt-5.4-mini/dataset3/few_shot_meta/bfs_gated_predictions.csv` |

## Full-Common-Set DeepSeek-V4-Pro Meta-Prompting Results

DeepSeek-V4-Pro meta-prompting uses maximum reasoning for prompt generation
and provider-default reasoning for extraction; few-shot extraction additionally
includes the corresponding labeled demonstrations.

| Dataset | Setting | Decision instances | BFS P | BFS R | BFS F1 | BFS Acc | Aligned instances | Aligned pooled P | Aligned pooled R | Aligned pooled F1 | Aligned pooled Acc | Unmatched | Predictions |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | zero_shot_meta | 5050 | 0.880 | 0.604 | 0.717 | 0.836 | 4400 | 0.876 | 0.603 | 0.714 | 0.825 | 0 | `results/deepseek-v4-pro/dataset1_zero_shot_meta_deepseek-v4-pro.csv` |
| 1 | few_shot_meta | 5050 | 0.875 | 0.661 | 0.753 | 0.851 | 4400 | 0.878 | 0.660 | 0.753 | 0.843 | 0 | `results/deepseek-v4-pro/dataset1_few_shot_meta_deepseek-v4-pro.csv` |
| 2 | zero_shot_meta | 935 | 0.960 | 0.836 | 0.894 | 0.877 | 868 | 0.960 | 0.840 | 0.896 | 0.871 | 0 | `results/deepseek-v4-pro/dataset2_zero_shot_meta_deepseek-v4-pro.csv` |
| 2 | few_shot_meta | 935 | 0.961 | 0.860 | 0.908 | 0.892 | 868 | 0.963 | 0.864 | 0.911 | 0.888 | 0 | `results/deepseek-v4-pro/dataset2_few_shot_meta_deepseek-v4-pro.csv` |
| 3 | zero_shot_meta | 2014 | 0.798 | 0.879 | 0.836 | 0.852 | 1384 | 0.740 | 0.885 | 0.806 | 0.837 | 0 | `results/deepseek-v4-pro/dataset3_zero_shot_meta_deepseek-v4-pro.csv` |
| 3 | few_shot_meta | 2014 | 0.816 | 0.900 | 0.856 | 0.869 | 1384 | 0.771 | 0.902 | 0.831 | 0.860 | 0 | `results/deepseek-v4-pro/dataset3_few_shot_meta_deepseek-v4-pro.csv` |
