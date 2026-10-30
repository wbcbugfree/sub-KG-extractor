# Decision Model Experiments

TypeSafe Jev and OpenAI Decisions adapters for NALT and Wikidata.
The runners use human-authored prompts and real BFS gating: INCLUDE
expands a node; EXCLUDE closes that path. Start with [REPORT.md](REPORT.md) for
results and comparison limitations.

## Layout

```text
decision_models/
  jev_nalt.py                 # Jev NALT runner and shared API utilities
  jev_wikidata.py             # Jev Wikidata runner
  openai_decisions_nalt.py     # OpenAI Decisions NALT runner and API utilities
  openai_decisions_wikidata.py # OpenAI Decisions Wikidata runner
  README.md                  # Setup, commands, artifact guide
  REPORT.md                  # Consolidated NALT and Wikidata results
  results/nalt/               # One final run per branch and decision model
  results/wikidata/           # Final runs by dataset, model, and prompt setting
```

Branch runs use `results/nalt/<branch>/<model>/<timestamp>/`, with exactly one
retained run for each of five branches and two decision models. The selection is
explicit in [summary/runs.json](results/nalt/summary/runs.json); `summary/` also
contains the final comparison and accounting tables. Failed runs, superseded
attempts, and intermediate study folders have been removed. The runners
produce structured results and metadata; this README and the central REPORT
are the documentation maintained for the experiment.

## Setup

Run commands from the repository root using Python 3.10+ and the root
`requirements.txt` dependencies. Windows runs use `curl.exe` with certificate
verification; Jev also supports `--transport urllib`.

The recorded runs used local classifier hooks in
`usecase/subkg_extractor_nalt.py::iterative_nalt_extraction` and
`meta-prompting/subkg_extractor_wikidata.py::run_bfs_for_seed`. Both functions
must accept a `classifier` callback. This folder-only publication excludes
those shared-file changes, so live inference from a fresh clone requires the
hooks to be published or applied separately. Retained results remain readable.

For NALT, the frozen RDF/XML input is external to this folder:
`tmp/nalt-baseline-source/nalt-full_dwn_20240716.rdf`. Its expected SHA-256 is
`71a3e2e5ca84e5f680e1ebbf3fa7a5892415c58de716334b5add27b54af6d168`.
The NALT runners verify this input and replay retained baselines before inference.
Prompts and held-out demonstration packs come from `usecase/prompts/`; the core
traversal comes from `usecase/subkg_extractor_nalt.py`.

Set `TYPESAFE_API_KEY` or `OPENAI_API_KEY`, or use `typesafe_api_key` and
`openai_api_key` in the ignored root `config` JSON file. Credentials are never
stored in result artifacts. OpenAI runs also validate the retained Jev runs.

## Run NALT experiments

Inspect options or validate inputs without provider calls:

```powershell
python decision_models/jev_nalt.py --help
python decision_models/openai_decisions_nalt.py --help
python decision_models/jev_nalt.py --benchmark plant_health --prepare-only --output-dir tmp/jev-nalt-prepare
python decision_models/openai_decisions_nalt.py --benchmark plant_health --prepare-only --output-dir tmp/openai-nalt-prepare
```

Run one branch with live, paid inference:

```powershell
python decision_models/jev_nalt.py --benchmark plant_health --model jev-1.14.0
python decision_models/openai_decisions_nalt.py --benchmark plant_health
```

Available branches are `plant_health`, `immunology`, `cell_biology`,
`pest_management`, and `soil_science`. OpenAI uses `gpt-6-luna`; omitting
`--benchmark` runs its five-branch study. Both runners accept `--output-dir` and
require a new directory. Published price constants are dated estimates; review
them before using future runs for cost comparisons.

Refusals normally stop OpenAI traversal. To reproduce the explicitly modified
Soil protocol:

```powershell
python decision_models/openai_decisions_nalt.py --benchmark soil_science --exclude-desalination-on-refusal
```

This flag assigns EXCLUDE only when the exact desalination URI receives a
provider refusal. The row is marked `user_refusal_override`; probabilities and
confidence remain missing. Normal answers and other refusals are unchanged.

## Run Wikidata experiments

Datasets 1, 2, and 3 use the checked-in graphs under `wikidata/dataset<N>/`,
the unchanged `meta-prompting/prompts/manual_dataset<N>.md`, and `path_simple`
candidate evidence. The default `--prompt-setting zero_shot_manual` sends no
demonstrations. `--prompt-setting few_shot_manual` appends the original manual
example pack using the existing formatter: 18 examples for Dataset 1, 18 for
Dataset 2, and 21 for Dataset 3. Effective prompt hashes must match all three
retained LLM baselines. Both settings keep the same held-out demonstration
exclusions and evaluation populations.

```powershell
python decision_models/jev_wikidata.py --dataset 2 --prepare-only --output-dir tmp/jev-wikidata-prepare
python decision_models/openai_decisions_wikidata.py --dataset 2 --prepare-only --output-dir tmp/openai-wikidata-prepare
python decision_models/jev_wikidata.py --dataset 2
python decision_models/openai_decisions_wikidata.py --dataset 2
```

For few-shot inference, run Jev before OpenAI so the latter can validate its
matching Jev comparison:

```powershell
python decision_models/jev_wikidata.py --dataset 2 --prompt-setting few_shot_manual
python decision_models/openai_decisions_wikidata.py --dataset 2 --prompt-setting few_shot_manual
```

Use `--dataset 1`, `2`, or `3`. The models are Jev 1.14.0 and OpenAI Decisions
`gpt-6-luna`. Both default to 20 candidates per request, five parallel seed
workers, and the existing ten-iteration limit. Every seed uses the original Wikidata BFS;
each reached decision candidate receives one binary Choice, and EXCLUDE prevents
its expansion. Existing unlabelled bridge handling is preserved, including
Dataset 3's bridge nodes. Gold labels are used only for evaluation. Unreached
scored rows receive EXCLUDE without model probabilities.

The OpenAI adapter translates the Jev request containers while preserving the
human prompt, candidate evidence, question selectors, and option descriptions.
It checks the retained Jev run and all three LLM baselines before inference.
Refusals stop traversal without becoming classifications; the NALT Soil override
is not available in this runner. OpenAI uses default implicit prompt caching and
records cache reads and writes as subsets of input tokens.

| Dataset | Seeds | Full-common instances | Aligned instances |
| --- | ---: | ---: | ---: |
| 1 | 451 | 5,050 | 4,400 |
| 2 | 104 | 935 | 868 |
| 3 | 27 | 2,014 | 1,384 |

Request grouping and online versus Batch gating differ from the retained LLM
runs. The central report distinguishes full-common pooled scores from aligned
pooled and row-count-weighted fold scores.

Outputs use
`results/wikidata/dataset<N>/<model-directory>/<prompt-setting>/<timestamp>/`,
where the model directory is `jev-1.14.0` or `openai-decisions-gpt-6-luna`, and
the setting is `zero_shot_manual` or `few_shot_manual`.
`decisions.csv` contains every actual model decision; `bfs_gated_predictions.csv`
contains the fixed scoring population. `metrics.json` and `comparison.csv`
report pooled and aligned fold metrics. API payloads, layer decisions, timing,
and traversal records are under `seeds/<QID>/`; `api_calls.csv` and `usage.json`
aggregate all seeds. The reported runs also retain `execution.json` and
`execution.log` for their launch commands and full process timing. No per-run
report or source snapshots are generated. Few-shot runs additionally retain
`examples.json` and `effective_prompt.md`, alongside the unchanged base
`system_prompt.md`, to preserve exactly what the model received.

## NALT results and experiment metadata

Each completed branch records:

- `manifest.json`, `system_prompt.md`, and `preflight.json`: settings, versions,
  input and code hashes, and baseline provenance.
- `requests/`: exact request/response payloads, sanitized HTTP headers, request
  IDs, usage, costs, retries, and transport timing.
- `layers/` and `timing/`: reached frontiers, decisions, and layer timing.
- `decisions.csv`, `included.csv`, and `pruned.csv`: labels, probability
  distributions, confidence, and decision provenance.
- `metrics.json`, `comparison.csv`, `pooled_evaluation.csv`, and `errors.json`:
  extraction quality, retained-model comparisons, and errors.
- `usage.json`, `api_calls.csv`, and `audit.json`: accounting and traversal
  verification. OpenAI also saves `refusal_overrides.json` for current runs.

Selected launch commands, process timing, and logs are stored in each run's
`execution.json` and `execution.log` where originally captured. Missing pilot
metadata remains missing. Historical paths in existing metadata describe the
layout at execution time and are not runtime dependencies. New runs do not
create source snapshots or per-run Markdown reports.

The OpenAI comparison combines four strict runs with the Soil override run;
preserve this qualification when reporting its metrics. The three ordinary-LLM
comparison models remain in `usecase/results/` and are not duplicated here.
