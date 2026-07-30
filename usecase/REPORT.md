# NALT Prompting Benchmark Report

## Scope

This report records the retained USDA NALT benchmark runs for five topic-rooted
benchmarks. Each run performs real LLM-gated BFS over the local NALT Full
vocabulary using `skos:broader`, `skos:narrower`, and `skos:related`. The
benchmark topic root is removed from traversal, so the extractor must recover
the topic-specific subgraph from internal seed concepts.

Two prompting settings are compared for each topic:

- **Human-authored**: a directly written system prompt used for zero-shot
  extraction.
- **Meta-generated**: a system prompt generated from
  `prompts/meta_prompt_label_only.md` and the topic-specific labeled
  demonstration set, then used for zero-shot extraction.

For each topic, the complete labeled demonstration set is held out from
evaluation for every model and both prompting settings. Applying the same
topic-specific exclusions gives all models and prompt sources the same held-out
positive set and the same excluded demonstration set. The evaluated negative
set remains traversal-dependent because it contains only visited
non-descendants. The exclusions affect scoring only and do not alter the
retained BFS traversals.

Model outputs follow the same hierarchy as the Wikidata experiments:
`results/<model>/<topic>/<mode>/`. Generated prompts
are stored under `prompts/<model>/`; human-authored prompts and labeled
demonstration sets remain shared across models.

The descendants of each hidden benchmark topic root define its raw structural
gold set; scoring uses the reachable descendants after holding out the labeled
demonstrations.

For NALT, accuracy is computed after removing the held-out demonstration
concepts: `(TP + TN) / (TP + FP + FN + TN)`, where `TN` is a visited
non-descendant classified as `EXCLUDE`. False negatives include reachable gold
concepts that were excluded or never reached. This differs from accuracy in the
Wikidata experiments: Wikidata scores every decision instance in a fixed
evaluation set and assigns `EXCLUDE` to candidates made unreachable by an
upstream exclusion, whereas NALT counts only visited non-descendants as
negative instances and does not score unvisited non-descendants. Consequently,
the NALT accuracy denominator can differ across traversals, is not generally
equal to the visited count, and does not represent the same type of evaluation
universe as Wikidata accuracy.

## Run Settings

All retained extraction runs used `usecase/subkg_extractor_nalt.py` with:

- LLM provider/models: `openai` / `gpt-5.4-mini`, `gemini` /
  `gemini-3.5-flash`, and `deepseek` / `deepseek-v4-pro`
- Extraction prompt mode: zero-shot
- `batch_size=20`, `llm_max_workers=5`, `max_iterations=20`
- Evaluation exclusions: the topic's complete labeled demonstration set for
  both human-authored and meta-generated runs
- NALT TTL filename: `nalt-full_dwn_20240716.ttl`; the script resolves or
  downloads this external input, so the large TTL is not stored under `usecase/`

OpenAI prompt generation uses `xhigh` reasoning, Gemini prompt generation uses
`high` thinking, and DeepSeek prompt generation uses `max` reasoning. Extraction
uses provider-default settings with schema-constrained output for OpenAI and
Gemini and strict structured tool output for DeepSeek.

The `Eval. Gold` columns below show reachable gold concepts after holding out
positive demonstrations; `Unreachable Gold` remains the pre-exclusion
structural reachability count.

## Inputs

| Topic | Hidden Root | Seed Labels | Human-Authored Prompt | Labeled Demonstration / Held-Out Set | Demonstrations |
|---|---|---|---|---|---:|
| Soil science | `Soil science` | `soil` | `prompts/manual/soil_science.md` | `prompts/examples/soil_science_label_meta_examples.json` | 8 |
| Pest management | `Pest management` | `pest control`; `pesticides` | `prompts/manual/pest_management.md` | `prompts/examples/pest_management_label_meta_examples.json` | 12 |
| Immunology | `Immunology` | `immunity` | `prompts/manual/immunology.md` | `prompts/examples/immunology_label_meta_examples.json` | 18 |
| Cell biology | `Cell biology` | `cells` | `prompts/manual/cell_biology.md` | `prompts/examples/cell_biology_label_meta_examples.json` | 12 |
| Plant health | `Plant health` | `plant diseases and disorders`; `plant protection` | `prompts/manual/plant_health.md` | `prompts/examples/plant_health_label_meta_examples.json` | 15 |

## GPT-5.4 mini Results

| Topic | Prompt Source | Run Timestamp | Eval. Gold | Unreachable Gold | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy | Eval. Included | Eval. Visited | Iterations |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Soil science | human-authored | `20260711_192210` | 1,032 | 0 | 862 | 98 | 170 | 465 | 0.898 | 0.835 | 0.865 | 0.832 | 960 | 1,498 | 9 |
| Soil science | meta-generated | `20260706_122939` | 1,032 | 0 | 816 | 121 | 216 | 429 | 0.871 | 0.791 | 0.829 | 0.787 | 937 | 1,445 | 10 |
| Pest management | human-authored | `20260712_010918` | 852 | 1 | 717 | 127 | 135 | 402 | 0.850 | 0.842 | 0.846 | 0.810 | 844 | 1,373 | 9 |
| Pest management | meta-generated | `20260706_123151` | 852 | 1 | 760 | 382 | 92 | 808 | 0.665 | 0.892 | 0.762 | 0.768 | 1,142 | 2,037 | 10 |
| Immunology | human-authored | `20260712_011051` | 239 | 0 | 215 | 89 | 24 | 255 | 0.707 | 0.900 | 0.792 | 0.806 | 304 | 575 | 9 |
| Immunology | meta-generated | `20260706_155045` | 239 | 0 | 207 | 146 | 32 | 282 | 0.586 | 0.866 | 0.699 | 0.733 | 353 | 656 | 9 |
| Cell biology | human-authored | `20260711_192658` | 415 | 3 | 295 | 87 | 120 | 483 | 0.772 | 0.711 | 0.740 | 0.790 | 382 | 923 | 12 |
| Cell biology | meta-generated | `20260702_223335` | 415 | 3 | 275 | 86 | 140 | 477 | 0.762 | 0.663 | 0.709 | 0.769 | 361 | 903 | 11 |
| Plant health | human-authored | `20260711_192825` | 219 | 0 | 207 | 54 | 12 | 382 | 0.793 | 0.945 | 0.862 | 0.899 | 261 | 651 | 6 |
| Plant health | meta-generated | `20260709_150853` | 219 | 0 | 214 | 210 | 5 | 894 | 0.505 | 0.977 | 0.666 | 0.837 | 424 | 1,321 | 9 |

## Gemini 3.5 Flash Human-Authored Results

| Topic | Run Timestamp | Eval. Gold | Unreachable Gold | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy | Eval. Included | Eval. Visited | Iterations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Soil science | `20260713_153913` | 1,032 | 0 | 851 | 30 | 181 | 288 | 0.966 | 0.825 | 0.890 | 0.844 | 881 | 1,246 | 9 |
| Pest management | `20260713_154256` | 852 | 1 | 809 | 50 | 43 | 271 | 0.942 | 0.950 | 0.946 | 0.921 | 859 | 1,171 | 6 |
| Immunology | `20260713_154555` | 239 | 0 | 214 | 61 | 25 | 188 | 0.778 | 0.895 | 0.833 | 0.824 | 275 | 485 | 7 |
| Cell biology | `20260713_154930` | 415 | 3 | 287 | 52 | 128 | 393 | 0.847 | 0.692 | 0.761 | 0.791 | 339 | 762 | 9 |
| Plant health | `20260713_155226` | 219 | 0 | 205 | 3 | 14 | 239 | 0.986 | 0.936 | 0.960 | 0.963 | 208 | 457 | 5 |

## Gemini 3.5 Flash Meta-Generated Results

| Topic | Run Timestamp | Eval. Gold | Unreachable Gold | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy | Eval. Included | Eval. Visited | Iterations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Soil science | `20260713_160613` | 1,032 | 0 | 758 | 32 | 274 | 207 | 0.959 | 0.734 | 0.832 | 0.759 | 790 | 1,084 | 8 |
| Pest management | `20260713_161109` | 852 | 1 | 832 | 188 | 20 | 410 | 0.816 | 0.977 | 0.889 | 0.857 | 1,020 | 1,450 | 7 |
| Immunology | `20260713_173150` | 239 | 0 | 197 | 50 | 42 | 179 | 0.798 | 0.824 | 0.811 | 0.803 | 247 | 455 | 8 |
| Cell biology | `20260714_182323` | 415 | 3 | 286 | 29 | 129 | 348 | 0.908 | 0.689 | 0.784 | 0.801 | 315 | 738 | 12 |
| Plant health | `20260713_213331` | 219 | 0 | 211 | 65 | 8 | 423 | 0.764 | 0.963 | 0.853 | 0.897 | 276 | 705 | 5 |

## DeepSeek-V4-Pro Human-Authored Results

| Topic | Run Timestamp | Eval. Gold | Unreachable Gold | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy | Eval. Included | Eval. Visited | Iterations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Soil science | `20260712_163933` | 1,032 | 0 | 883 | 65 | 149 | 345 | 0.931 | 0.856 | 0.892 | 0.852 | 948 | 1,361 | 9 |
| Pest management | `20260712_221707` | 852 | 1 | 813 | 71 | 39 | 295 | 0.920 | 0.954 | 0.937 | 0.910 | 884 | 1,216 | 6 |
| Immunology | `20260712_222138` | 239 | 0 | 216 | 62 | 23 | 202 | 0.777 | 0.904 | 0.836 | 0.831 | 278 | 501 | 8 |
| Cell biology | `20260712_222829` | 415 | 3 | 311 | 77 | 104 | 433 | 0.802 | 0.749 | 0.775 | 0.804 | 388 | 882 | 11 |
| Plant health | `20260712_223206` | 219 | 0 | 210 | 7 | 9 | 264 | 0.968 | 0.959 | 0.963 | 0.967 | 217 | 488 | 5 |

## DeepSeek-V4-Pro Meta-Generated Results

| Topic | Run Timestamp | Eval. Gold | Unreachable Gold | TP | FP | FN | TN | Precision | Recall | F1 | Accuracy | Eval. Included | Eval. Visited | Iterations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Soil science | `20260713_000413` | 1,032 | 0 | 818 | 116 | 214 | 508 | 0.876 | 0.793 | 0.832 | 0.801 | 934 | 1,500 | 11 |
| Pest management | `20260713_001600` | 852 | 1 | 829 | 217 | 23 | 383 | 0.793 | 0.973 | 0.874 | 0.835 | 1,046 | 1,452 | 8 |
| Immunology | `20260713_002159` | 239 | 0 | 165 | 104 | 74 | 167 | 0.613 | 0.690 | 0.650 | 0.651 | 269 | 472 | 9 |
| Cell biology | `20260714_144851` | 415 | 3 | 301 | 39 | 114 | 356 | 0.885 | 0.725 | 0.797 | 0.811 | 340 | 770 | 10 |
| Plant health | `20260713_122326` | 219 | 0 | 210 | 22 | 9 | 322 | 0.905 | 0.959 | 0.931 | 0.945 | 232 | 562 | 6 |

The CSV files remain the unmodified traversal records. Each metrics JSON now
stores the resolved held-out URI set, the evaluated counts shown above, and
separate `raw_*` counts for the original traversal.

## Result Artifacts

| Topic | Human-Authored Metrics JSON | Meta-Generated Metrics JSON | Generated Prompt |
|---|---|---|---|
| Soil science | `results/gpt-5.4-mini/soil/zero_shot_manual/subkg_soil_science_NALT_20260711_192210_METRICS.json` | `results/gpt-5.4-mini/soil/zero_shot_meta/subkg_soil_science_NALT_20260706_122939_METRICS.json` | `prompts/gpt-5.4-mini/soil_science.md` |
| Pest management | `results/gpt-5.4-mini/pest/zero_shot_manual/subkg_pest_management_NALT_20260712_010918_METRICS.json` | `results/gpt-5.4-mini/pest/zero_shot_meta/subkg_pest_management_NALT_20260706_123151_METRICS.json` | `prompts/gpt-5.4-mini/pest_management.md` |
| Immunology | `results/gpt-5.4-mini/immunology/zero_shot_manual/subkg_immunology_NALT_20260712_011051_METRICS.json` | `results/gpt-5.4-mini/immunology/zero_shot_meta/subkg_immunology_NALT_20260706_155045_METRICS.json` | `prompts/gpt-5.4-mini/immunology.md` |
| Cell biology | `results/gpt-5.4-mini/cell/zero_shot_manual/subkg_cell_biology_NALT_20260711_192658_METRICS.json` | `results/gpt-5.4-mini/cell/zero_shot_meta/subkg_cell_biology_NALT_20260702_223335_METRICS.json` | `prompts/gpt-5.4-mini/cell_biology.md` |
| Plant health | `results/gpt-5.4-mini/plant/zero_shot_manual/subkg_plant_health_NALT_20260711_192825_METRICS.json` | `results/gpt-5.4-mini/plant/zero_shot_meta/subkg_plant_health_NALT_20260709_150853_METRICS.json` | `prompts/gpt-5.4-mini/plant_health.md` |

### Gemini Human-Authored Artifacts

| Topic | Metrics JSON |
|---|---|
| Soil science | `results/gemini-3.5-flash/soil/zero_shot_manual/subkg_soil_science_NALT_20260713_153913_METRICS.json` |
| Pest management | `results/gemini-3.5-flash/pest/zero_shot_manual/subkg_pest_management_NALT_20260713_154256_METRICS.json` |
| Immunology | `results/gemini-3.5-flash/immunology/zero_shot_manual/subkg_immunology_NALT_20260713_154555_METRICS.json` |
| Cell biology | `results/gemini-3.5-flash/cell/zero_shot_manual/subkg_cell_biology_NALT_20260713_154930_METRICS.json` |
| Plant health | `results/gemini-3.5-flash/plant/zero_shot_manual/subkg_plant_health_NALT_20260713_155226_METRICS.json` |

### Gemini Meta-Generated Artifacts

| Topic | Metrics JSON | Generated Prompt |
|---|---|---|
| Soil science | `results/gemini-3.5-flash/soil/zero_shot_meta/subkg_soil_science_NALT_20260713_160613_METRICS.json` | `prompts/gemini-3.5-flash/soil_science.md` |
| Pest management | `results/gemini-3.5-flash/pest/zero_shot_meta/subkg_pest_management_NALT_20260713_161109_METRICS.json` | `prompts/gemini-3.5-flash/pest_management.md` |
| Immunology | `results/gemini-3.5-flash/immunology/zero_shot_meta/subkg_immunology_NALT_20260713_173150_METRICS.json` | `prompts/gemini-3.5-flash/immunology.md` |
| Cell biology | `results/gemini-3.5-flash/cell/zero_shot_meta/subkg_cell_biology_NALT_20260714_182323_METRICS.json` | `prompts/gemini-3.5-flash/cell_biology.md` |
| Plant health | `results/gemini-3.5-flash/plant/zero_shot_meta/subkg_plant_health_NALT_20260713_213331_METRICS.json` | `prompts/gemini-3.5-flash/plant_health.md` |

### DeepSeek Human-Authored Artifacts

| Topic | Metrics JSON |
|---|---|
| Soil science | `results/deepseek-v4-pro/soil/zero_shot_manual/subkg_soil_science_NALT_20260712_163933_METRICS.json` |
| Pest management | `results/deepseek-v4-pro/pest/zero_shot_manual/subkg_pest_management_NALT_20260712_221707_METRICS.json` |
| Immunology | `results/deepseek-v4-pro/immunology/zero_shot_manual/subkg_immunology_NALT_20260712_222138_METRICS.json` |
| Cell biology | `results/deepseek-v4-pro/cell/zero_shot_manual/subkg_cell_biology_NALT_20260712_222829_METRICS.json` |
| Plant health | `results/deepseek-v4-pro/plant/zero_shot_manual/subkg_plant_health_NALT_20260712_223206_METRICS.json` |

### DeepSeek Meta-Generated Artifacts

| Topic | Metrics JSON | Generated Prompt |
|---|---|---|
| Soil science | `results/deepseek-v4-pro/soil/zero_shot_meta/subkg_soil_science_NALT_20260713_000413_METRICS.json` | `prompts/deepseek-v4-pro/soil_science.md` |
| Pest management | `results/deepseek-v4-pro/pest/zero_shot_meta/subkg_pest_management_NALT_20260713_001600_METRICS.json` | `prompts/deepseek-v4-pro/pest_management.md` |
| Immunology | `results/deepseek-v4-pro/immunology/zero_shot_meta/subkg_immunology_NALT_20260713_002159_METRICS.json` | `prompts/deepseek-v4-pro/immunology.md` |
| Cell biology | `results/deepseek-v4-pro/cell/zero_shot_meta/subkg_cell_biology_NALT_20260714_144851_METRICS.json` | `prompts/deepseek-v4-pro/cell_biology.md` |
| Plant health | `results/deepseek-v4-pro/plant/zero_shot_meta/subkg_plant_health_NALT_20260713_122326_METRICS.json` | `prompts/deepseek-v4-pro/plant_health.md` |
