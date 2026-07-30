# Cell Biology Labeled Demonstration Set Ablation

## Experimental Setup

This experiment measures how the size of the labeled demonstration set affects meta-prompting on the NALT Cell Biology benchmark. All other settings remain fixed:

- meta-prompt: `../prompts/meta_prompt_label_only.md`
- prompt generator: `deepseek-v4-pro` with maximum reasoning effort
- extractor: `deepseek-v4-pro` with provider-default inference settings
- traversal: real LLM-gated BFS from the single seed `cells`
- evaluation: the same 16-concept superset is held out from every condition
- replication: one generated prompt and one extraction run per condition

## Labeled Demonstration Set Design

The sets are nested and contrastive:

| Condition | Included pairs |
|---:|---|
| 4 demonstrations | A, C |
| 8 demonstrations | A, B, C, D |
| 12 demonstrations | A, B, C, D, E, F |
| 16 demonstrations | A, B, C, D, E, F, G, H |

## Reproduction

```powershell
python usecase\ablation\run_cell_biology_ablation.py
```

Use `--levels 4 8` to run selected fresh conditions. Existing prompts or result directories are never replaced unless `--overwrite` is passed.

## Results

DeepSeek-V4-Pro generated one system prompt per fresh condition with maximum reasoning effort, followed by one provider-default full BFS extraction. The 12-demonstration condition is the retained canonical traversal; all other conditions are fresh single attempts. All four scoring views exclude the largest 16-concept demonstration set. Fourteen of those concepts occur in each condition's scoring universe; the other two are non-gold concepts that no retained traversal visited.

The held-out positive set is fixed across conditions. NALT accuracy also includes visited non-descendants as true negatives, so its negative evaluation universe is traversal-dependent.

| Demonstrations | Pairs | Eval. gold | Precision | Recall | F1 | Accuracy | TP | FP | FN | TN | Eval. included | Eval. visited | Source |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 4 | 2 | 413 | 0.874 | 0.535 | 0.664 | 0.713 | 221 | 32 | 192 | 335 | 253 | 639 | fresh single run |
| 8 | 4 | 413 | 0.769 | 0.758 | 0.763 | 0.824 | 313 | 94 | 100 | 595 | 407 | 1,076 | fresh single run |
| 12 | 6 | 413 | 0.888 | 0.726 | 0.799 | 0.813 | 300 | 38 | 113 | 355 | 338 | 766 | retained canonical |
| 16 | 8 | 413 | 0.760 | 0.746 | 0.753 | 0.817 | 308 | 97 | 105 | 595 | 405 | 1,058 | fresh single run |

## Observed Trend

In these single attempts, F1 increased from 0.664 with 4 demonstrations to
0.799 with 12, then fell to 0.753 with 16. This non-monotonic trajectory shows
that increasing the number of demonstrations did not consistently improve
performance; boundary coverage and demonstration composition mattered more
than raw set size in the retained runs.
