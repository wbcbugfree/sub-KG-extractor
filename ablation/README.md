# Wikidata and NALT ablation studies

This directory collects path-information, topic-prompt and demonstration-count
experiments. The twelve additional full-setting runs are stored with their
benchmark results. The five path/prompt ablations are stored here.

## Retained studies

All five path/prompt ablations used GPT-5.4 mini, with served model snapshot
`gpt-5.4-mini-2026-03-17`, and one run per condition.

| Benchmark | Condition | Run ID | Results |
|---|---|---|---|
| Wikidata D2 | Detailed prompt, paths removed | `07_W_NOPATH_openai_r1` | [Saved run](wikidata/dataset2/no_path/gpt-5.4-mini/20261028/) |
| Wikidata D2 | Generic prompt, paths retained | `09_W_GENERIC_openai_r1` | [Saved run](wikidata/dataset2/generic_prompt/gpt-5.4-mini/20261028/) |
| NALT Soil | Detailed prompt, paths added | `08_N_PATH_openai_r1` | [Saved run](nalt/soil/with_paths/gpt-5.4-mini/20261028/) |
| NALT Soil | Generic prompt, labels only | `10_N_GENERIC_openai_r1` | [Saved run](nalt/soil/generic_prompt/gpt-5.4-mini/20261028/) |
| Wikidata D3 | Detailed prompt, paths removed | `D3_NOPATH_openai_r1` | [Saved run](wikidata/dataset3/no_path/gpt-5.4-mini/20261029/) |

The existing Cell Biology demonstration-count study is under
[nalt/demonstration_count](nalt/demonstration_count/REPORT.md). It preserves the
4/8/12/16-example conditions, generated prompts, example packs, results and
dedicated runner.

The D2/Soil study used live gated BFS with five workers and up to 20 candidates
per request. The two new full-setting runs per model used the same modified
detailed prompts as their corresponding ablations. Earlier results used their
original prompts and execution settings. The new Gemini runs used Gemini 3.6
Flash, whereas the earlier benchmark used Gemini 3.5 Flash.

The D3 study classified the original 2,014 fixed candidate rows and applied
BFS gating afterward. Its aligned evaluation contains 1,384 rows. The control
uses existing full-path answers through the same scorer. The
[historical scoring audit](wikidata/dataset3/no_path/gpt-5.4-mini/20261029/result.json)
under `validation.historical_scoring_audit`
records 15 differences from the original saved gated CSV. D3 timing and cost
therefore describe fixed-candidate classification. The D2 path ablation remains
available alongside the later D3 experiment.

## Repeated full-setting runs

Each directory contains `20261028_r1/` and `20261028_r2/`.

| Benchmark | Model | Results |
|---|---|---|
| Wikidata D2 | GPT-5.4 mini | [Repeated runs](../meta-prompting/results/gpt-5.4-mini/dataset2/zero_shot_manual/repeats/) |
| Wikidata D2 | DeepSeek V4 Pro | [Repeated runs](../meta-prompting/results/deepseek-v4-pro/dataset2/zero_shot_manual/repeats/) |
| Wikidata D2 | Gemini 3.6 Flash | [Repeated runs](../meta-prompting/results/gemini-3.6-flash/dataset2/zero_shot_manual/repeats/) |
| NALT Soil | GPT-5.4 mini | [Repeated runs](../usecase/results/gpt-5.4-mini/soil/zero_shot_manual/repeats/) |
| NALT Soil | DeepSeek V4 Pro | [Repeated runs](../usecase/results/deepseek-v4-pro/soil/zero_shot_manual/repeats/) |
| NALT Soil | Gemini 3.6 Flash | [Repeated runs](../usecase/results/gemini-3.6-flash/soil/zero_shot_manual/repeats/) |

## Artifact contents

- `result.json` preserves metrics and execution times. D2 also includes per-seed
  outputs and timing. NALT includes every visited, included and pruned node,
  gold nodes, evaluation exclusions and iteration records.
- `predictions.csv` stores Wikidata decisions. D3 retains all original columns,
  including raw and BFS-gated decisions and confidence values.
- `status.json` preserves model identity, completion state, UTC timestamps,
  total wall time and aggregate token/cost accounting.
- `attempts.jsonl` preserves individual API attempts, candidate identities,
  input/cached/reasoning/output tokens, per-request tariffs, estimated costs,
  latency and request identifiers.
- `process_time.json` records process wall time. D2/Soil retain their external
  measurements. D3's `wall_seconds` copies the final internal timer from
  `status.json:process_wall_seconds`; `exit_code` is null because an external
  process exit code was not retained. Its original timing fields in `result.json`
  and `status.json` remain unchanged. New runs identify their internal CLI timer
  in the `measurement` and `source` fields.
- [experiments.json](experiments.json) maps all 17 runs to their locations,
  prompts and protocols, and records file sizes and SHA-256 hashes.

All five path/prompt ablations use [run_experiment.py](run_experiment.py), with
experiment-specific logic in [path_prompt.py](path_prompt.py) and API accounting
in [recorded_api.py](recorded_api.py). Their prompts are together in
[prompts/](prompts/); the fixed scoring identities are in [data/](data/).
The D3 prompt diff is beside its prompt. Its complete scoring audit and original
validation record are inside `result.json` under `validation`; their source
hashes remain in the registry. D2 and D3 use the same five-file layout:
`predictions.csv`, `result.json`, `status.json`, `attempts.jsonl` and
`process_time.json`. NALT stores its node predictions inside `result.json`.

The runner directly imports `meta-prompting/subkg_extractor_wikidata.py`,
`meta-prompting/full_dataset_batch_runner.py` and
`usecase/subkg_extractor_nalt.py`. It uses their classifier callbacks and scoring
functions; no copies of these modules are maintained here. Original protocol
manifests and their historical code hashes are preserved in `experiments.json`.
The protocol field distinguishes live BFS from fixed-candidate classification.

The archived tariffs are dated October 2026. Reasoning tokens are included in
billable output. The per-request records support recalculation using other
prices without repeating inference.

## Validation and reproduction

Run commands from the repository root with Python 3.10+ and the extraction
dependencies. The read-only integrity check uses the standard library.

```powershell
python ablation/run_experiment.py verify
```

Input validation and fresh inference require a new output directory under
`tmp/`. The `validate` command constructs the original inputs without provider
calls. The `run` command performs paid inference with the recorded model and
protocol. Run IDs are listed above and in `experiments.json`.

```powershell
python ablation/run_experiment.py validate 07_W_NOPATH_openai_r1 --output-dir tmp/check-d2-no-path
python ablation/run_experiment.py validate D3_NOPATH_openai_r1 --output-dir tmp/check-d3-no-path
python ablation/run_experiment.py run 08_N_PATH_openai_r1 --output-dir tmp/new-soil-with-paths
```

The integrity check verifies saved results, prompts and data. It reports changes
to the project code separately, so the main codebase can evolve normally. Each
new run records the code hashes it actually uses. Future runs retain the
October 2026 tariff assumptions for their cost estimates.

Reproduction also uses the existing Wikidata graphs, evaluation folds,
demonstrations and baseline results. NALT uses the pinned RDF/XML input at
`tmp/nalt-baseline-source/nalt-full_dwn_20240716.rdf`, with SHA-256
`71a3e2e5ca84e5f680e1ebbf3fa7a5892415c58de716334b5add27b54af6d168`.
The NALT path cache is regenerated from that graph and checked against its
original fingerprint before the path condition runs.

The demonstration-count study retains its own entry point.

```powershell
python ablation/nalt/demonstration_count/run_cell_biology_ablation.py --summarize-only
```
