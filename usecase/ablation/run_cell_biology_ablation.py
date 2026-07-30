import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
USECASE_DIR = SCRIPT_DIR.parent
REPO_ROOT = USECASE_DIR.parent
EXAMPLES_DIR = SCRIPT_DIR / "examples"
PROMPTS_DIR = SCRIPT_DIR / "prompts" / "deepseek-v4-pro"
RESULTS_DIR = SCRIPT_DIR / "results"
SUMMARY_JSON = SCRIPT_DIR / "RESULTS.json"
REPORT_MD = SCRIPT_DIR / "REPORT.md"

MODEL = "deepseek-v4-pro"
BENCHMARK = "cell_biology"
ALL_LEVELS = (4, 8, 12, 16)
FRESH_LEVELS = (4, 8, 16)
META_PROMPT = USECASE_DIR / "prompts" / "meta_prompt_label_only.md"
EVALUATION_EXAMPLES = EXAMPLES_DIR / "cell_biology_16_nodes.json"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command):
    print("Running:", subprocess.list2cmdline([str(item) for item in command]))
    subprocess.run(
        [str(item) for item in command],
        cwd=REPO_ROOT,
        check=True,
    )


def pack_path(level):
    return EXAMPLES_DIR / f"cell_biology_{level}_nodes.json"


def prompt_path(level):
    return PROMPTS_DIR / f"cell_biology_{level}_nodes.md"


def result_root(level):
    return RESULTS_DIR / f"{level}_nodes"


def metrics_dir(level):
    return result_root(level) / MODEL / "cell" / "zero_shot_meta"


def find_single_metrics(directory):
    matches = sorted(Path(directory).glob("*_METRICS.json"))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one metrics file in {directory}, found {len(matches)}"
        )
    return matches[0]


def all_conditions_available():
    return all(
        len(list(metrics_dir(level).glob("*_METRICS.json"))) == 1
        for level in ALL_LEVELS
    )


def validate_pack(path, expected_count):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    examples = payload.get("examples", [])
    if payload.get("benchmark_name") != BENCHMARK:
        raise ValueError(f"Unexpected benchmark in {path}")
    if len(examples) != expected_count:
        raise ValueError(
            f"Expected {expected_count} examples in {path}, found {len(examples)}"
        )
    include_count = sum(
        item.get("decision") == "INCLUDE" for item in examples
    )
    exclude_count = sum(
        item.get("decision") == "EXCLUDE" for item in examples
    )
    if include_count != exclude_count or include_count * 2 != expected_count:
        raise ValueError(f"Pack is not balanced: {path}")
    return payload


def example_signature(payload):
    return {
        (
            item["seed_label"],
            item["candidate_label"],
            item["decision"],
        )
        for item in payload["examples"]
    }


def validate_design():
    packs = {
        level: validate_pack(pack_path(level), level)
        for level in ALL_LEVELS
    }
    signatures = {
        level: example_signature(payload)
        for level, payload in packs.items()
    }
    for smaller, larger in ((4, 8), (8, 12), (12, 16)):
        if not signatures[smaller] < signatures[larger]:
            raise ValueError(
                f"The {smaller}-node pack must be a strict subset of the "
                f"{larger}-node pack"
            )


def run_level(level, overwrite=False):
    examples = pack_path(level)
    prompt = prompt_path(level)
    output_directory = metrics_dir(level)
    validate_pack(examples, level)

    if prompt.exists() and not overwrite:
        raise FileExistsError(
            f"Prompt already exists: {prompt}. Use --overwrite to replace it."
        )
    if output_directory.exists() and any(output_directory.iterdir()) and not overwrite:
        raise FileExistsError(
            f"Results already exist: {output_directory}. Use --overwrite to replace them."
        )

    run_command([
        sys.executable,
        USECASE_DIR / "subkg_extractor_nalt.py",
        "generate",
        "--benchmark", BENCHMARK,
        "--examples-file", examples,
        "--meta-prompt", META_PROMPT,
        "--provider", "deepseek",
        "--model", MODEL,
        "--output", prompt,
    ])

    extract_command = [
        sys.executable,
        USECASE_DIR / "subkg_extractor_nalt.py",
        "extract",
        "--benchmark", BENCHMARK,
        "--output-dir", result_root(level),
        "--llm-provider", "deepseek",
        "--llm-model", MODEL,
        "--prompt-file", prompt,
        "--result-mode", "zero_shot_meta",
        "--exclude-examples-file", EVALUATION_EXAMPLES,
    ]
    if overwrite:
        extract_command.append("--overwrite")
    run_command(extract_command)


def result_record(level, metrics_path, reused):
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    examples = pack_path(level)
    prompt = prompt_path(level)
    record = {
        "example_nodes": level,
        "contrastive_pairs": level // 2,
        "reused_canonical_run": reused,
        "examples_file": str(examples.relative_to(REPO_ROOT)),
        "examples_sha256": sha256(examples),
        "prompt_file": str(prompt.relative_to(REPO_ROOT)),
        "prompt_sha256": sha256(prompt),
        "metrics_file": str(metrics_path.relative_to(REPO_ROOT)),
    }
    for key in (
        "precision", "recall", "f1", "accuracy", "true_positive",
        "false_positive", "false_negative", "true_negative",
        "gold_positive_count", "included_count", "visited_count",
        "raw_visited_count", "excluded_evaluation_count", "iterations",
    ):
        record[key] = metrics[key]
    return record


def write_summary():
    records = []
    for level in ALL_LEVELS:
        metrics_path = find_single_metrics(metrics_dir(level))
        records.append(result_record(level, metrics_path, level == 12))
    previous_f1 = None
    for record in records:
        record["f1_delta_from_previous"] = (
            None if previous_f1 is None else record["f1"] - previous_f1
        )
        previous_f1 = record["f1"]

    summary = {
        "study": "NALT Cell Biology labeled demonstration set size ablation",
        "benchmark": BENCHMARK,
        "provider": "deepseek",
        "model": MODEL,
        "prompt_generation_reasoning_effort": "max",
        "extraction_settings": "provider defaults",
        "replications_per_condition": 1,
        "evaluation_exclusion_file": str(
            EVALUATION_EXAMPLES.relative_to(REPO_ROOT)
        ),
        "evaluation_exclusion_nodes": 16,
        "conditions": records,
    }
    SUMMARY_JSON.write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Cell Biology Labeled Demonstration Set Ablation",
        "",
        "## Experimental Setup",
        "",
        "This experiment measures how the size of the labeled demonstration set affects meta-prompting on the NALT Cell Biology benchmark. All other settings remain fixed:",
        "",
        "- meta-prompt: `../prompts/meta_prompt_label_only.md`",
        "- prompt generator: `deepseek-v4-pro` with maximum reasoning effort",
        "- extractor: `deepseek-v4-pro` with provider-default inference settings",
        "- traversal: real LLM-gated BFS from the single seed `cells`",
        "- evaluation: the same 16-concept superset is held out from every condition",
        "- replication: one generated prompt and one extraction run per condition",
        "",
        "## Labeled Demonstration Set Design",
        "",
        "The sets are nested and contrastive:",
        "",
        "| Condition | Included pairs |",
        "|---:|---|",
        "| 4 demonstrations | A, C |",
        "| 8 demonstrations | A, B, C, D |",
        "| 12 demonstrations | A, B, C, D, E, F |",
        "| 16 demonstrations | A, B, C, D, E, F, G, H |",
        "",
        "The 12-demonstration condition reuses the canonical traversal in `../results/deepseek-v4-pro/cell/zero_shot_meta/`; it was not rerun. Byte-identical copies of its generated prompt and labeled demonstration set are stored as `prompts/deepseek-v4-pro/cell_biology_12_nodes.md` and `examples/cell_biology_12_nodes.json`. Its unchanged traversal artifacts are also copied into this directory and rescored separately with the common 16-concept exclusion set.",
        "",
        "## Reproduction",
        "",
        "```powershell",
        "python usecase\\ablation\\run_cell_biology_ablation.py",
        "```",
        "",
        "Use `--levels 4 8` to run selected fresh conditions. Existing prompts or result directories are never replaced unless `--overwrite` is passed.",
        "",
        "## Results",
        "",
        "DeepSeek-v4-pro generated one system prompt per fresh condition with maximum reasoning effort, followed by one provider-default full BFS extraction. The 12-demonstration condition is the retained canonical traversal; all other conditions are fresh single attempts. All four scoring views exclude the largest 16-concept demonstration set. Fourteen of those concepts occur in each condition's scoring universe; the other two are non-gold concepts that no retained traversal visited.",
        "",
        "| Demonstrations | Pairs | Eval. gold | Precision | Recall | F1 | Accuracy | TP | FP | FN | TN | Eval. included | Eval. visited | Source |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for record in records:
        source = "retained canonical" if record["reused_canonical_run"] else "fresh single run"
        lines.append(
            "| {example_nodes} | {contrastive_pairs} | {gold_positive_count:,} | {precision:.3f} | "
            "{recall:.3f} | {f1:.3f} | {accuracy:.3f} | {true_positive} | "
            "{false_positive} | {false_negative} | {true_negative} | "
            "{included_count:,} | {visited_count:,} | {source} |".format(
                source=source,
                **record,
            )
        )
    deltas = [record["f1_delta_from_previous"] for record in records[1:]]
    lines.extend([
        "",
        "## Observed Trend",
        "",
        "F1 increased by {:.3f} from 4 to 8 demonstrations and by a further {:.3f} from 8 to 12. The 16-demonstration condition decreased F1 by {:.3f} relative to 12 and performed similarly to the 8-demonstration condition. In these single attempts, four demonstrations were insufficient for recall, the 12-demonstration set gave the strongest precision/F1 balance, and adding four more demonstrations did not improve extraction quality.".format(deltas[0], deltas[1], abs(deltas[2])),
        "",
        "Because each condition has one run, these values describe the observed ablation trajectory and do not estimate run-to-run variance or statistical significance.",
        "",
    ])
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report saved: {REPORT_MD}")
    print(f"Machine-readable summary saved: {SUMMARY_JSON}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the NALT Cell Biology example-count ablation."
    )
    parser.add_argument(
        "--levels",
        nargs="+",
        type=int,
        choices=FRESH_LEVELS,
        default=list(FRESH_LEVELS),
        help="Fresh example-node conditions to run (default: 4 8 16).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace prompts and result artifacts for selected conditions.",
    )
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="Rebuild REPORT.md and RESULTS.json without API calls.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    validate_design()
    if not args.summarize_only:
        for level in args.levels:
            print(f"\n=== Cell Biology ablation: {level} example nodes ===")
            run_level(level, overwrite=args.overwrite)
    if all_conditions_available():
        write_summary()
    elif args.summarize_only:
        raise RuntimeError(
            "Cannot summarize until the 4-, 8-, and 16-node results exist"
        )
    else:
        print("Summary deferred until all fresh conditions are complete.")


if __name__ == "__main__":
    main()
