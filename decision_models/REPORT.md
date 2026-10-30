# Decision Model Experiment Report

This report covers the five NALT branches and Wikidata Datasets 1, 2, and 3
with Jev and OpenAI Decisions, using unchanged human-authored system prompts
and zero-shot or few-shot prompting on Wikidata.

## NALT scope and protocol

This report consolidates the recorded TypeSafe Jev 1.14.0 and OpenAI Decisions
`gpt-6-luna` experiments from 27–28 October 2026 across five NALT branches.
GPT-5.4 mini, Gemini 3.5 Flash, and DeepSeek-V4-Pro comparisons use retained
human-authored-prompt results, verified by offline replay; they are not new
provider calls.

The experiments use the fingerprinted 16 July 2024 NALT RDF, the original branch
seeds and hidden-root masks, unchanged human-authored prompts, and the same
held-out demonstration exclusions. Inputs contain English preferred and
alternative labels. Each reached non-seed candidate receives an independent
binary INCLUDE/EXCLUDE question. Requests contain up to 20 candidates, with five
workers and a maximum of 20 BFS layers.

Traversal starts from included seeds and expands only INCLUDE nodes. EXCLUDE
stops expansion through that node, although another included path can still reach
its descendants. Confidence does not control traversal. Seeds and unreached
nodes have no invented model probabilities.

## NALT OpenAI Soil exception

The original strict OpenAI study completed four branches. Soil stopped when the
API returned a refusal for desalination. The completed Soil rerun applies the
explicitly authorized rule: if that exact concept receives a refusal, assign
EXCLUDE and continue. It completed nine layers with 1,304 model choices and one
override. The raw refusal is retained; the override is marked
`user_refusal_override`, has no provider probabilities or confidence, and is
excluded from probability scoring.

The combined OpenAI result below therefore uses four retained strict runs and
this Soil rerun with one manual rule. It must not be presented as five entirely
autonomous model runs. Normal model answers and refusals for other concepts are
not covered by the exception.

## NALT extraction results

All entries are F1 percentages. Macro F1 weights the five branches equally.
Scoring uses the existing hierarchy gold and held-out-example exclusions.

| Branch | Jev 1.14.0 | OpenAI Decisions¹ | GPT-5.4 mini | Gemini 3.5 Flash | DeepSeek-V4-Pro |
| --- | ---: | ---: | ---: | ---: | ---: |
| Soil science | 89.06 | 76.70 | 86.55 | 88.97 | 89.19 |
| Pest management | 92.45 | 87.09 | 84.55 | 94.56 | 93.66 |
| Immunology | 82.14 | 77.51 | 79.19 | 83.27 | 83.56 |
| Cell biology | 70.60 | 67.14 | 74.03 | 76.13 | 77.46 |
| Plant health | 89.58 | 88.33 | 86.25 | 96.02 | 96.33 |
| **Five-branch macro** | **84.77** | **79.35** | **82.11** | **87.79** | **88.04** |

¹ Includes the authorized Soil refusal override described above.

The table is sourced from the retained
[branch comparison](results/nalt/summary/comparison.csv)
and [macro comparison](results/nalt/summary/macro_comparison.csv).
Jev has higher F1 than OpenAI Decisions on all five branches. OpenAI Soil recall
is 66.18%, compared with Jev's 83.24%; their precision is 91.19% and 95.76%,
respectively. These runs do not estimate repeated-run variance. Traversal-dependent
accuracy is not used to rank systems; per-run comparisons also retain accuracy
on a common pooled evaluation population.

## NALT recorded API usage and time

These totals cover only the ten final runs selected above. Earlier failed or
superseded attempts and their intermediate summaries have been removed.

| Completed-run scope | HTTP calls | Input tokens | Output tokens | Sum of BFS wall seconds | Estimated USD |
| --- | ---: | ---: | ---: | ---: | ---: |
| Jev, all five branches | 239 | 4,507,286 | 161,782 | 28.614 | $0.189306 |
| OpenAI, four strict branches | 177 | 3,549,301 | 0 | 35.354 | $0.355062 |
| OpenAI, Soil with override | 71 | 1,072,799 | 0 | 12.025 | $0.107280 |
| OpenAI, combined five branches | 248 | 4,622,100 | 0 | 47.379 | $0.462342 |

Usage comes from the selected run records, consolidated in the
[per-run accounting table](results/nalt/summary/api_usage.csv) and
[final summary](results/nalt/summary/summary.json).
Costs use each run's dated recorded tariff and are estimates, not invoices. The
API-reported zero output-token counts are preserved. The combined OpenAI runs
report 5,282 cache-write tokens and no cache-read tokens; cache counts are subsets
of input usage.

BFS wall time includes live traversal and concurrent requests. Summing per-request
latencies would double-count overlapping work. Preparation, baseline replay,
exports, and subprocess overhead are recorded separately; the completed Soil
rerun took 81.07 seconds overall. The aggregate BFS times above are sums across
separate runs, not one continuous end-to-end execution.

## NALT result artifacts

Run directories retain raw requests and responses, request identifiers, usage,
cost estimates, timestamps, per-node decisions and probabilities, layer traces,
metrics, and prompt/source/code fingerprints. Exactly one completed
run is retained per branch and decision model; the final Soil selection includes
the authorized override. The ordinary-LLM baselines remain in `usecase/results/`.

- [Selected runs and provenance](results/nalt/summary/runs.json)
- [Final comparison and usage summary](results/nalt/summary/summary.json)
- [Verification of the ten retained runs](results/nalt/summary/verification.json)

The retained audits establish exact reachable frontiers, Include-only expansion,
one visit per node, exhausted completed traversals, unchanged prompts and baseline
artifacts, agreement with raw API answers, and reconciliation of usage and costs.
Probability metrics cover scored, visited model decisions; they exclude seeds,
unreached nodes, and the Soil override.

## Wikidata: zero-shot human-authored

All three datasets have one completed Jev 1.14.0 run and one completed OpenAI
Decisions `gpt-6-luna` run from 28 October 2026. The OpenAI runs are new;
Jev and the three ordinary LLM comparisons use retained results. Each uses
its unchanged `meta-prompting/prompts/manual_dataset<N>.md`. Effective prompt
hashes match the retained zero-shot human-authored runs. Candidate evidence
uses `path_simple`, with no demonstrations or gold labels sent to the API.
Held-out demonstration exclusions and scoring populations are unchanged.

Both decision models use the original Wikidata BFS: seeds start included,
INCLUDE expands a node, and EXCLUDE closes that path. Existing unlabelled
bridge traversal is preserved. Requests contain at most 20 independent binary
choices, with five parallel seed workers and a ten-iteration limit. OpenAI's
request schema differs, but the human prompt, candidate text, question
selectors, and option descriptions match the Jev adapter. Confidence does
not control expansion. No fallback model or refusal override was used.

| Dataset | Seeds | Full-common instances | Aligned instances | Full-common positives |
| --- | ---: | ---: | ---: | ---: |
| 1 | 451 | 5,050 | 4,400 | 1,734 |
| 2 | 104 | 935 | 868 | 579 |
| 3 | 27 | 2,014 | 1,384 | 866 |

### Extraction quality

Full-common pooled F1 percentages use identical scoring identities within
each dataset. Macro F1 weights the three datasets equally.

| Model, zero-shot human-authored | Dataset 1 | Dataset 2 | Dataset 3 | Macro |
| --- | ---: | ---: | ---: | ---: |
| OpenAI Decisions gpt-6-luna, new live BFS | 73.45 | 90.02 | 66.67 | 76.71 |
| Jev 1.14.0, retained live BFS | 64.02 | 89.86 | 73.30 | 75.73 |
| Gemini 3.5 Flash, retained | 79.43 | 92.13 | 84.62 | 85.39 |
| DeepSeek-V4-Pro, retained | 60.58 | 89.48 | 81.78 | 77.28 |
| GPT-5.4 mini, retained | 70.12 | 85.63 | 75.85 | 77.20 |

OpenAI exceeds Jev by 9.43 percentage points on Dataset 1 and 0.15 on Dataset 2,
but trails by 6.63 on Dataset 3. Its macro advantage is 0.98 points. Gemini
has the highest F1 on every dataset. OpenAI exceeds retained GPT and DeepSeek
on Datasets 1 and 2, but trails all three ordinary LLMs on Dataset 3.

The aligned weighted F1 is the mean across the original five test folds,
weighted by scored instance counts; SD is the corresponding population
standard deviation. It is not repeated-run uncertainty. All entries below
are percentages; full confusion matrices and fold results are in each run's
`metrics.json` and `comparison.csv`.

| Dataset / model | Full precision | Full recall | Full accuracy | Aligned pooled F1 | Aligned weighted F1 +/- SD |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 / Jev | 91.43 | 49.25 | 80.99 | 64.94 | 65.00 +/- 2.87 |
| 1 / OpenAI | 72.97 | 73.93 | 81.64 | 74.23 | 74.11 +/- 1.38 |
| 2 / Jev | 95.35 | 84.97 | 88.13 | 90.16 | 89.88 +/- 1.50 |
| 2 / OpenAI | 91.30 | 88.77 | 87.81 | 90.28 | 90.06 +/- 1.70 |
| 3 / Jev | 72.15 | 74.48 | 76.66 | 67.57 | 67.27 +/- 8.42 |
| 3 / OpenAI | 72.88 | 61.43 | 73.58 | 65.26 | 65.28 +/- 3.75 |

On Dataset 1, OpenAI recovers 1,282 true positives versus Jev's 854, while false
positives rise from 80 to 475. Recall improves from 49.25% to 73.93%, but
precision falls from 91.43% to 72.97%. Dataset 2 is nearly tied in F1, again
with higher OpenAI recall and lower precision. On Dataset 3, OpenAI has
532 true positives, 198 false positives, 334 false negatives, and 950 true
negatives. Its recall falls to 61.43% versus Jev's 74.48%.

These single runs do not establish statistical superiority or repeated-run
variability. Gemini/GPT classified candidates through Batch before applying
BFS gating; DeepSeek used online BFS. Online traversal can also produce
different paths from static retained candidate evidence. Prompt and scoring
populations match, but request grouping and execution differ, so historical
LLM runs are not matched latency or API-call comparisons.

### Traversal and probabilities

Unreached scored candidates receive EXCLUDE for evaluation, with missing
model probabilities. Actual decisions also cover reached, unscored traversal
candidates, including held-out demonstration candidates; their gold labels
do not control expansion. Bridge counts are visits summed across seeds,
not globally unique entities.

| Dataset / model | Actual decisions | Visited scored | Unreached scored | Unreached positives | Bridge visits | Max decision layers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 / Jev | 3,676 | 3,652 | 1,398 | 208 | 0 | 4 |
| 1 / OpenAI | 4,867 | 4,836 | 214 | 57 | 0 | 4 |
| 2 / Jev | 911 | 893 | 42 | 8 | 0 | 3 |
| 2 / OpenAI | 933 | 915 | 20 | 2 | 0 | 3 |
| 3 / Jev | 1,756 | 1,698 | 316 | 128 | 524 | 5 |
| 3 / OpenAI | 1,650 | 1,592 | 422 | 150 | 551 | 5 |

All traversals exhausted their frontiers before the iteration limit. Offline
replay of saved API answers through the original BFS reproduces frontiers,
final predictions, and scores. OpenAI leaves fewer positives unreached on
Datasets 1 and 2, but more on Dataset 3; these are extraction losses from
gating, distinct from direct model EXCLUDE answers.

Probability diagnostics use only visited, scored model decisions. The two
models visit different populations, so these values are conditional
diagnostics rather than a controlled calibration ranking.

| Dataset / model | Scored probabilities | Brier score | Log loss |
| --- | ---: | ---: | ---: |
| 1 / Jev | 3,652 | 0.14048 | 0.43763 |
| 1 / OpenAI | 4,836 | 0.12756 | 0.43019 |
| 2 / Jev | 893 | 0.08657 | 0.27985 |
| 2 / OpenAI | 915 | 0.08308 | 0.26630 |
| 3 / Jev | 1,698 | 0.12878 | 0.39947 |
| 3 / OpenAI | 1,592 | 0.16332 | 0.56499 |

### API usage and timing

All six final runs succeeded on their first attempts, with complete reported
token usage. The three OpenAI runs had zero API errors, retries, refusals,
or overrides. HTTP calls below count actual request attempts, not individual
candidate decisions. Total rows sum separate executions.

| Dataset / model | HTTP calls | Input tokens | Output tokens | BFS wall seconds | Process wall seconds | Estimated USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 / Jev | 870 | 5,549,929 | 135,640 | 58.088 | 88.630 | $0.233097018 |
| 1 / OpenAI | 962 | 6,984,336 | 0 | 79.918 | 111.494 | $0.698524500 |
| 2 / Jev | 182 | 922,396 | 33,486 | 12.443 | 21.646 | $0.038740632 |
| 2 / OpenAI | 188 | 907,739 | 0 | 15.523 | 24.097 | $0.090773900 |
| 3 / Jev | 131 | 2,622,742 | 64,391 | 11.544 | 18.823 | $0.110155164 |
| 3 / OpenAI | 123 | 2,377,361 | 0 | 14.691 | 22.070 | $0.239764125 |
| All three / Jev | 1,183 | 9,095,067 | 233,517 | 82.075 | 129.098 | $0.381992814 |
| All three / OpenAI | 1,273 | 10,269,436 | 0 | 110.132 | 157.662 | $1.029062525 |

Jev consumed 9,328,584 total tokens; OpenAI consumed
10,269,436, with the API reporting zero output tokens.
OpenAI reported no cache reads and 84,757 cache-write tokens:
3,636 on Dataset 1, zero on Dataset 2, and 81,121 on Dataset 3. These writes
are subsets of input usage, not additional tokens. Default implicit caching
was used; no read savings were observed.

Estimates use the recorded tariffs checked on 28 October 2026, not invoices:
[Jev](https://docs.typesafe.ai/models) charges $0.042 per million input
tokens, with free output. [OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions)
has free output; the [recorded OpenAI rates](https://developers.openai.com/api/docs/pricing)
are $0.10/M uncached input, $0.01/M cache reads, and $0.125/M 30-minute cache
writes. Cache writes replace the uncached rate for those input tokens.
OpenAI cost 2.69 times as much as Jev across these
three runs. Its summed BFS wall time was 1.34 times Jev's;
the models made different decisions and reached different populations.

BFS wall time includes traversal and concurrent requests. Process wall time
also includes interpreter startup, preparation, and exports. Runner times
exclude interpreter startup; per-request latencies below are seconds.

| Dataset / model | Runner wall seconds | Median request latency | 95th-percentile latency |
| --- | ---: | ---: | ---: |
| 1 / Jev | 85.951 | 0.312 | 0.410 |
| 1 / OpenAI | 108.717 | 0.389 | 0.629 |
| 2 / Jev | 19.012 | 0.308 | 0.389 |
| 2 / OpenAI | 21.463 | 0.345 | 0.624 |
| 3 / Jev | 15.935 | 0.393 | 0.453 |
| 3 / OpenAI | 18.947 | 0.429 | 0.725 |

Summed request latencies overlap across workers and are not wall times.
Per-call transport timing, preparation/export times, start/end timestamps,
and the launch commands remain in the run artifacts.

### Retained artifacts

Exactly one completed run is retained per dataset, model, and prompt setting.

| Dataset / model | Final run | Metrics and comparison | API accounting |
| --- | ---: | ---: | ---: |
| 1 / Jev | [20261028T164131Z](results/wikidata/dataset1/jev-1.14.0/zero_shot_manual/20261028T164131Z/) | [Metrics](results/wikidata/dataset1/jev-1.14.0/zero_shot_manual/20261028T164131Z/metrics.json), [comparison](results/wikidata/dataset1/jev-1.14.0/zero_shot_manual/20261028T164131Z/comparison.csv) | [Usage](results/wikidata/dataset1/jev-1.14.0/zero_shot_manual/20261028T164131Z/usage.json), [calls](results/wikidata/dataset1/jev-1.14.0/zero_shot_manual/20261028T164131Z/api_calls.csv), [execution](results/wikidata/dataset1/jev-1.14.0/zero_shot_manual/20261028T164131Z/execution.json) |
| 1 / OpenAI | [20261028T172355Z](results/wikidata/dataset1/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172355Z/) | [Metrics](results/wikidata/dataset1/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172355Z/metrics.json), [comparison](results/wikidata/dataset1/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172355Z/comparison.csv) | [Usage](results/wikidata/dataset1/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172355Z/usage.json), [calls](results/wikidata/dataset1/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172355Z/api_calls.csv), [execution](results/wikidata/dataset1/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172355Z/execution.json) |
| 2 / Jev | [20261028T163014Z](results/wikidata/dataset2/jev-1.14.0/zero_shot_manual/20261028T163014Z/) | [Metrics](results/wikidata/dataset2/jev-1.14.0/zero_shot_manual/20261028T163014Z/metrics.json), [comparison](results/wikidata/dataset2/jev-1.14.0/zero_shot_manual/20261028T163014Z/comparison.csv) | [Usage](results/wikidata/dataset2/jev-1.14.0/zero_shot_manual/20261028T163014Z/usage.json), [calls](results/wikidata/dataset2/jev-1.14.0/zero_shot_manual/20261028T163014Z/api_calls.csv), [execution](results/wikidata/dataset2/jev-1.14.0/zero_shot_manual/20261028T163014Z/execution.json) |
| 2 / OpenAI | [20261028T172330Z](results/wikidata/dataset2/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172330Z/) | [Metrics](results/wikidata/dataset2/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172330Z/metrics.json), [comparison](results/wikidata/dataset2/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172330Z/comparison.csv) | [Usage](results/wikidata/dataset2/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172330Z/usage.json), [calls](results/wikidata/dataset2/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172330Z/api_calls.csv), [execution](results/wikidata/dataset2/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172330Z/execution.json) |
| 3 / Jev | [20261028T164259Z](results/wikidata/dataset3/jev-1.14.0/zero_shot_manual/20261028T164259Z/) | [Metrics](results/wikidata/dataset3/jev-1.14.0/zero_shot_manual/20261028T164259Z/metrics.json), [comparison](results/wikidata/dataset3/jev-1.14.0/zero_shot_manual/20261028T164259Z/comparison.csv) | [Usage](results/wikidata/dataset3/jev-1.14.0/zero_shot_manual/20261028T164259Z/usage.json), [calls](results/wikidata/dataset3/jev-1.14.0/zero_shot_manual/20261028T164259Z/api_calls.csv), [execution](results/wikidata/dataset3/jev-1.14.0/zero_shot_manual/20261028T164259Z/execution.json) |
| 3 / OpenAI | [20261028T172546Z](results/wikidata/dataset3/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172546Z/) | [Metrics](results/wikidata/dataset3/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172546Z/metrics.json), [comparison](results/wikidata/dataset3/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172546Z/comparison.csv) | [Usage](results/wikidata/dataset3/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172546Z/usage.json), [calls](results/wikidata/dataset3/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172546Z/api_calls.csv), [execution](results/wikidata/dataset3/openai-decisions-gpt-6-luna/zero_shot_manual/20261028T172546Z/execution.json) |

Each run also retains `decisions.csv` with actual decisions and probabilities,
`bfs_gated_predictions.csv` for the complete scoring population, the prompt
and source/code hashes, exact request bodies, raw responses, sanitized HTTP
metadata and request IDs, and per-seed layer and traversal records. Existing
Jev and LLM artifacts were preserved. Preparation and verification utilities
remain in ignored `tmp/`; there are no per-run Markdown reports or source snapshots.

## Wikidata: few-shot human-authored

Both decision models were run live on all three datasets on 28 October 2026.
The unchanged human-authored system prompt is followed by the existing
`format_examples_for_extraction` block from the manual demonstration pack.
The complete effective prompt matches the SHA-256 recorded by all three
retained few-shot LLM baselines, including example text and ordering.
Example-file paths and whole-file hashes differ from some older manifests;
the effective prompt hashes confirm that the transmitted text matches exactly.

| Dataset | Demonstrations | INCLUDE examples | EXCLUDE examples | Held-out instances |
| --- | ---: | ---: | ---: | ---: |
| 1 | 18 | 12 | 6 | 32 |
| 2 | 18 | 12 | 6 | 18 |
| 3 | 21 | 12 | 9 | 21 |

The scored populations, aligned folds, seeds, graph inputs, 20-candidate
batches, five seed workers, ten-iteration limit, and original BFS code match
the zero-shot study above. Every reached decision candidate receives a model
choice; INCLUDE expands and EXCLUDE closes that path. Existing unlabelled
bridges are preserved. Demonstration instances remain excluded from scoring;
their labels are not used to force traversal decisions. No confidence gate,
fallback model, or refusal override was used.

### Few-shot extraction quality

Full-common pooled F1 percentages; macro weights the three datasets equally.
Only the Jev and OpenAI Decisions rows are new inference.

| Model | Dataset 1 | Dataset 2 | Dataset 3 | Macro |
| --- | ---: | ---: | ---: | ---: |
| Jev 1.14.0, new live BFS | 64.50 | 90.91 | 82.96 | 79.46 |
| OpenAI Decisions gpt-6-luna, new live BFS | 74.27 | 88.46 | 68.06 | 76.93 |
| Gemini 3.5 Flash, retained | 80.18 | 93.73 | 89.16 | 87.69 |
| GPT-5.4 mini, retained | 73.25 | 86.87 | 74.86 | 78.32 |
| DeepSeek-V4-Pro, retained | 63.49 | 92.05 | 84.37 | 79.97 |

Jev leads OpenAI on Datasets 2 and 3; OpenAI leads on Dataset 1. Jev's macro
F1 is 79.46%, versus OpenAI's 76.93%. Gemini remains strongest on every
dataset. Jev exceeds retained GPT on Datasets 2 and 3 and DeepSeek on
Dataset 1, but remains below Gemini and DeepSeek in macro F1.

The additional metrics below use the same populations as the zero-shot tables.
Aligned weighted F1 is weighted by test-fold instance counts; SD describes
the five folds, not repeated-run uncertainty. Confusion matrices and all fold
metrics are retained in each run's JSON and CSV outputs.

| Dataset / model | Full precision | Full recall | Full accuracy | Aligned pooled F1 | Aligned weighted F1 +/- SD |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 / Jev | 91.24 | 49.88 | 81.15 | 65.25 | 65.19 +/- 2.89 |
| 1 / OpenAI | 75.33 | 73.24 | 82.57 | 74.55 | 74.40 +/- 1.84 |
| 2 / Jev | 94.92 | 87.22 | 89.20 | 91.11 | 90.84 +/- 1.61 |
| 2 / OpenAI | 94.15 | 83.42 | 86.52 | 88.70 | 88.51 +/- 2.82 |
| 3 / Jev | 83.59 | 82.33 | 85.45 | 79.89 | 79.76 +/- 2.10 |
| 3 / OpenAI | 78.14 | 60.28 | 75.67 | 64.64 | 64.74 +/- 3.89 |

Changes from the retained zero-shot decision-model runs, in percentage points:

| Dataset / model | Zero-shot F1 | Few-shot F1 | Change |
| --- | ---: | ---: | ---: |
| 1 / Jev | 64.02 | 64.50 | +0.49 |
| 1 / OpenAI | 73.45 | 74.27 | +0.82 |
| 2 / Jev | 89.86 | 90.91 | +1.05 |
| 2 / OpenAI | 90.02 | 88.46 | -1.56 |
| 3 / Jev | 73.30 | 82.96 | +9.66 |
| 3 / OpenAI | 66.67 | 68.06 | +1.39 |

Jev improves on all three datasets in these runs. The main gain is Dataset 3:
true positives rise from 645 to 713 and false positives fall from 249 to 140,
improving both recall and precision. OpenAI becomes more conservative on all
three datasets: false positives and true positives both decrease. Its full
F1 rises slightly on Datasets 1 and 3 but falls on Dataset 2. Dataset 3's
OpenAI gain does not carry over to the aligned subset: pooled F1 decreases
from 65.26% to 64.64%.

These are single-run observations. As above, Gemini/GPT used Batch followed
by gating and DeepSeek used online BFS; the retained ordinary LLM timings
and request counts are not controlled comparisons with these live runs.

### Few-shot traversal and probabilities

All frontiers exhausted before the iteration limit. Replaying saved model
answers through the original BFS reproduced every frontier, prediction,
and score. Unreached scored nodes are EXCLUDE for evaluation and have no
invented probabilities. Actual decisions include reached unscored candidates;
bridge visits are summed across seeds.

| Dataset / model | Actual decisions | Visited scored | Unreached scored | Unreached positives | Bridge visits | Max layers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 / Jev | 3,699 | 3,673 | 1,377 | 202 | 0 | 4 |
| 1 / OpenAI | 4,723 | 4,693 | 357 | 73 | 0 | 4 |
| 2 / Jev | 913 | 895 | 40 | 7 | 0 | 3 |
| 2 / OpenAI | 932 | 914 | 21 | 5 | 0 | 3 |
| 3 / Jev | 1,663 | 1,643 | 371 | 50 | 491 | 5 |
| 3 / OpenAI | 1,534 | 1,476 | 538 | 149 | 494 | 5 |

Probability diagnostics cover only the visited scored rows shown above.
Because visit sets differ, these are not a controlled calibration ranking.

| Dataset / model | Brier score | Log loss |
| --- | ---: | ---: |
| 1 / Jev | 0.13838 | 0.41861 |
| 1 / OpenAI | 0.12408 | 0.40070 |
| 2 / Jev | 0.08063 | 0.26374 |
| 2 / OpenAI | 0.09890 | 0.32300 |
| 3 / Jev | 0.10676 | 0.34491 |
| 3 / OpenAI | 0.15559 | 0.51909 |

### Few-shot API accounting

HTTP calls count actual attempts. OpenAI Dataset 1 encountered one HTTP 503
(`server_error`: "Decision inference is unavailable."); its automatic retry
succeeded. This was a service error, not a refusal. The raw error response,
headers, timing, and retry are retained. No model refusals or overrides occurred.

Every successful response has usage. The 503 response supplied no usage or
cost, so that attempt remains unknown rather than being counted as free.
Token and cost totals below cover reported usage; HTTP-call totals include
the failed attempt. Costs are estimates, not invoices, and the total rows
sum separate executions.

| Dataset / model | HTTP calls | Reported input tokens | Reported output tokens | BFS wall seconds | Process wall seconds | Estimated known USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 / Jev | 874 | 8,661,900 | 136,481 | 61.633 | 92.250 | $0.363799800 |
| 1 / OpenAI | 946 | 10,415,437 | 0 | 97.851 | 131.126 | $1.041216460 |
| 2 / Jev | 183 | 1,607,535 | 33,561 | 12.956 | 23.501 | $0.067516470 |
| 2 / OpenAI | 187 | 1,554,482 | 0 | 19.844 | 29.113 | $0.155448200 |
| 3 / Jev | 126 | 5,246,023 | 60,986 | 14.513 | 23.140 | $0.220332966 |
| 3 / OpenAI | 117 | 4,599,308 | 0 | 16.103 | 23.842 | $0.457687100 |
| All three / Jev | 1,183 | 15,515,458 | 231,028 | 89.101 | 138.891 | $0.651649236 |
| All three / OpenAI | 1,250 | 16,569,227 | 0 | 133.799 | 184.080 | $1.654351760 |

Total reported token consumption is 15,746,486 for Jev and
16,569,227 for OpenAI. OpenAI reported
38,836 cache-read and 36,972
cache-write tokens, both subsets of input tokens. The same
[Jev](https://docs.typesafe.ai/models) and
[OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions)
tariffs recorded above were rechecked on 28 October 2026; OpenAI cache rates
follow its [standard pricing](https://developers.openai.com/api/docs/pricing).
Known few-shot cost for OpenAI is 2.54 times Jev's
across these datasets, with different visited populations.
Against each provider's retained zero-shot totals, Jev's estimated cost rises
from $0.3820 to $0.6516, and OpenAI's known estimate rises from $1.0291 to
$1.6544. The additional demonstrations increase input consumption; any
usage from the failed OpenAI attempt remains unknown.

Process time includes interpreter startup, preparation, live BFS, and exports.
Runner wall time excludes interpreter startup. Request latencies are seconds:

| Dataset / model | Runner wall seconds | Median request latency | 95th-percentile latency |
| --- | ---: | ---: | ---: |
| 1 / Jev | 89.359 | 0.322 | 0.461 |
| 1 / OpenAI | 127.512 | 0.451 | 0.855 |
| 2 / Jev | 19.278 | 0.318 | 0.426 |
| 2 / OpenAI | 26.214 | 0.450 | 0.867 |
| 3 / Jev | 19.855 | 0.507 | 0.605 |
| 3 / OpenAI | 20.784 | 0.545 | 0.865 |

### Few-shot retained artifacts

One final run is retained per dataset, decision model, and prompt setting.
The existing zero-shot and NALT results are unchanged.

| Dataset / model | Final run | Metrics and comparison | API accounting |
| --- | ---: | ---: | ---: |
| 1 / Jev | [20261028T174325Z](results/wikidata/dataset1/jev-1.14.0/few_shot_manual/20261028T174325Z/) | [Metrics](results/wikidata/dataset1/jev-1.14.0/few_shot_manual/20261028T174325Z/metrics.json), [comparison](results/wikidata/dataset1/jev-1.14.0/few_shot_manual/20261028T174325Z/comparison.csv) | [Usage](results/wikidata/dataset1/jev-1.14.0/few_shot_manual/20261028T174325Z/usage.json), [calls](results/wikidata/dataset1/jev-1.14.0/few_shot_manual/20261028T174325Z/api_calls.csv), [execution](results/wikidata/dataset1/jev-1.14.0/few_shot_manual/20261028T174325Z/execution.json) |
| 1 / OpenAI | [20261028T174457Z](results/wikidata/dataset1/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174457Z/) | [Metrics](results/wikidata/dataset1/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174457Z/metrics.json), [comparison](results/wikidata/dataset1/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174457Z/comparison.csv) | [Usage](results/wikidata/dataset1/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174457Z/usage.json), [calls](results/wikidata/dataset1/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174457Z/api_calls.csv), [execution](results/wikidata/dataset1/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174457Z/execution.json) |
| 2 / Jev | [20261028T174232Z](results/wikidata/dataset2/jev-1.14.0/few_shot_manual/20261028T174232Z/) | [Metrics](results/wikidata/dataset2/jev-1.14.0/few_shot_manual/20261028T174232Z/metrics.json), [comparison](results/wikidata/dataset2/jev-1.14.0/few_shot_manual/20261028T174232Z/comparison.csv) | [Usage](results/wikidata/dataset2/jev-1.14.0/few_shot_manual/20261028T174232Z/usage.json), [calls](results/wikidata/dataset2/jev-1.14.0/few_shot_manual/20261028T174232Z/api_calls.csv), [execution](results/wikidata/dataset2/jev-1.14.0/few_shot_manual/20261028T174232Z/execution.json) |
| 2 / OpenAI | [20261028T174255Z](results/wikidata/dataset2/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174255Z/) | [Metrics](results/wikidata/dataset2/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174255Z/metrics.json), [comparison](results/wikidata/dataset2/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174255Z/comparison.csv) | [Usage](results/wikidata/dataset2/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174255Z/usage.json), [calls](results/wikidata/dataset2/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174255Z/api_calls.csv), [execution](results/wikidata/dataset2/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174255Z/execution.json) |
| 3 / Jev | [20261028T174708Z](results/wikidata/dataset3/jev-1.14.0/few_shot_manual/20261028T174708Z/) | [Metrics](results/wikidata/dataset3/jev-1.14.0/few_shot_manual/20261028T174708Z/metrics.json), [comparison](results/wikidata/dataset3/jev-1.14.0/few_shot_manual/20261028T174708Z/comparison.csv) | [Usage](results/wikidata/dataset3/jev-1.14.0/few_shot_manual/20261028T174708Z/usage.json), [calls](results/wikidata/dataset3/jev-1.14.0/few_shot_manual/20261028T174708Z/api_calls.csv), [execution](results/wikidata/dataset3/jev-1.14.0/few_shot_manual/20261028T174708Z/execution.json) |
| 3 / OpenAI | [20261028T174731Z](results/wikidata/dataset3/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174731Z/) | [Metrics](results/wikidata/dataset3/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174731Z/metrics.json), [comparison](results/wikidata/dataset3/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174731Z/comparison.csv) | [Usage](results/wikidata/dataset3/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174731Z/usage.json), [calls](results/wikidata/dataset3/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174731Z/api_calls.csv), [execution](results/wikidata/dataset3/openai-decisions-gpt-6-luna/few_shot_manual/20261028T174731Z/execution.json) |

Each run retains the unchanged `system_prompt.md`, exact `effective_prompt.md`,
`examples.json`, prompt/example/source/code fingerprints, decisions and
probabilities, request/response payloads, sanitized headers and request IDs,
usage and cache accounting, timestamps and transport timing, per-seed layer
and traversal traces, execution command, and log. Verification and report
utilities remain in ignored `tmp/`; no per-run reports or source snapshots
were added to the public experiment folders.
