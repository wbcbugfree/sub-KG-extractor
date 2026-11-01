"""Path/prompt experiment policies using the project's extraction and scoring code."""
import csv
import difflib
import hashlib
import json
import re
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "meta-prompting"), str(ROOT / "usecase")]
import subkg_extractor_wikidata as w
import full_dataset_batch_runner as batch_runner
import subkg_extractor_nalt as n
from rdflib import Graph, URIRef
from recorded_api import RecordedAPI, save_json, utc_now

SOURCE = ROOT / "tmp/nalt-baseline-source/nalt-full_dwn_20240716.rdf"
W_ROWS = ROOT / "meta-prompting/results/gpt-5.4-mini/dataset2/zero_shot_manual/rows.csv"
W_BASELINE = ROOT / "reproduction/results/decisions/dataset2_lstm_dataset2_config.csv"
W_EXAMPLES = ROOT / "meta-prompting/examples/dataset2_path_simple_manual_examples.json"
N_EXAMPLES = ROOT / "usecase/prompts/examples/soil_science_label_meta_examples.json"
D3_BASELINE = ROOT / "meta-prompting/results/gpt-5.4-mini/dataset3/zero_shot_manual"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def chunks(rows, size=20):
    return [rows[i:i+size] for i in range(0, len(rows), size)]


def wikidata_data():
    graph = w.load_graph_context(2)
    all_rows = w.augment_rows_with_graph_context(2, w.load_decision_rows(2))
    traversal, _ = w.skip_rows_absent_from_local_graph(all_rows, graph)
    lookup = {r["row_id"]: r for r in traversal}
    with W_ROWS.open(encoding="utf-8") as handle:
        frozen = list(csv.DictReader(handle))
    score = []
    for saved in frozen:
        row = lookup[saved["row_id"]]
        assert (row["seed_qid"], row["qid"], row["target"]) == (saved["seed_qid"], saved["qid"], int(saved["target"]))
        score.append(row)
    with W_BASELINE.open(encoding="utf-8") as handle:
        pairs = {(r["from"], r["QID"]) for r in csv.DictReader(handle)}
    assert len(score) == 935 and len({r["seed_qid"] for r in score}) == 104
    assert len(w.rows_in_pairs(score, pairs)) == 868
    excluded = {r["row_id"] for r in w.load_examples(W_EXAMPLES)["examples"]}
    assert not excluded.intersection(r["row_id"] for r in score)
    identities = [{key: row[key] for key in ("row_id", "seed_qid", "qid", "target")} for row in score]
    assert identities == read_json(HERE / "data/wikidata_d2_score_identities.json")
    return graph, traversal, score, pairs


def nalt_index():
    graph = Graph()
    graph.parse(SOURCE, format="xml")
    return n.NaltSkosIndex(graph, ignored_uris={n.NALT_SOIL_SCIENCE_URI})


def canonical_paths(index):
    """Root-masked graph BFS; stable URI and predicate/direction tie-breaking."""
    start = index.find_uri_by_english_pref_label("soil")
    parent = {str(start): None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in sorted(index.adjacency.get(current, {}), key=str):
            if not index.is_traversable(neighbor) or str(neighbor) in parent:
                continue
            edges = []
            for predicate in sorted(n.TRAVERSAL_RELATIONS, key=str):
                if (current, predicate, neighbor) in index.graph:
                    edges.append((str(predicate), "forward"))
                if (neighbor, predicate, current) in index.graph:
                    edges.append((str(predicate), "inverse"))
            predicate, direction = sorted(edges)[0]
            parent[str(neighbor)] = [str(current), predicate, direction]
            queue.append(neighbor)
    assert n.NALT_SOIL_SCIENCE_URI not in parent
    return parent


def path_text(index, parents, uri):
    cur, pieces = uri, []
    while parents[cur] is not None:
        prev, predicate, direction = parents[cur]
        label = n.relation_name(URIRef(predicate))
        arrow = f" --{label}--> " if direction == "forward" else f" <--{label}-- "
        pieces.append(arrow + index.pref_labels[URIRef(cur)])
        cur = prev
    return index.pref_labels[URIRef(cur)] + "".join(reversed(pieces))


def prepare_nalt(cell, run_dir):
    index = nalt_index()
    excluded = n.resolve_example_candidate_uris(index, N_EXAMPLES)
    seed = index.find_uri_by_english_pref_label("soil")
    reachable = n.reachable_uris_from_seeds(index, [seed])
    root = URIRef(n.NALT_SOIL_SCIENCE_URI)
    gold = index.hierarchy_descendants(root, exclude={root}) & reachable
    scored_gold = {str(uri) for uri in gold} - {str(uri) for uri in excluded}
    assert scored_gold == set(read_json(HERE / "data/nalt_soil_gold.json"))
    parents = None
    if cell["condition"] == "PATH":
        parents = canonical_paths(index)
        cache = run_dir / "nalt_canonical_parents.json"
        save_json(cache, parents)
        expected = read_json(HERE / "experiments.json")["derived_inputs"]["nalt_paths"]["sha256"]
        if sha(cache) != expected:
            raise ValueError("Rebuilt NALT paths differ from the recorded experiment")
    return index, parents, excluded


def run_wikidata(cell, api, prompt, run_dir):
    began = time.perf_counter()
    graph, traversal, score, pairs = wikidata_data()
    score_by_seed, traversal_by_seed = defaultdict(list), defaultdict(list)
    for r in score:
        score_by_seed[r["seed_qid"]].append(r)
    for r in traversal:
        traversal_by_seed[r["seed_qid"]].append(r)
    args = SimpleNamespace(provider=cell["provider"], model=cell["model"], workers=5,
                           batch_size=20, max_iterations=10,
                           context_level="label" if cell["condition"] == "NOPATH" else "path_simple")

    def classify(system, rows, context_level, provider, model, batch_size, workers):
        results = []
        for part in chunks(rows, batch_size):
            decisions = api.infer(system, w.build_eval_user_message(part, context_level),
                                  [r["legacy_row_id"] for r in part],
                                  dict(seed=part[0]["seed_qid"], workload="W"))
            evaluations = w.BatchResponse.model_validate({"evaluations":decisions}).evaluations
            results.extend(w.match_evaluations_by_index(part, evaluations))
        return results

    def seed_runner(*pos, **kw):
        start, utc = time.perf_counter(), utc_now()
        result = w.run_bfs_for_seed(*pos, classifier=classify, **kw)
        result["wall_seconds"] = time.perf_counter()-start
        result["started_utc"] = utc
        save_json(run_dir/"seeds"/(result["seed_qid"]+".json"), result)
        return result

    prep_seconds = time.perf_counter()-began
    start = time.perf_counter()
    seeds, rows = w.run_bfs_for_seeds(score_by_seed, traversal_by_seed, graph, prompt, args, seed_runner=seed_runner)
    traversal_seconds = time.perf_counter()-start
    w.write_result_csv(run_dir/"predictions.csv", rows)
    aligned = w.rows_in_pairs(rows, pairs)
    return dict(metrics=w.compute_metrics(rows),
                metric_aggregations=dict(common=w.compute_fold_metric_aggregation(rows,2), aligned=w.compute_fold_metric_aggregation(aligned,2)),
                preparation_seconds=prep_seconds, traversal_seconds=traversal_seconds,
                output_included_nodes=sum(len(s["included_qids"]) for s in seeds),
                subgraph_count=len(seeds), scored_rows=len(rows), aligned_rows=len(aligned),
                seeds_at_iteration_limit=[s["seed_qid"] for s in seeds if s["iterations"] == 10],
                seed_results=seeds)


def run_nalt(cell, api, prompt, run_dir):
    began = time.perf_counter()
    index, parents, excluded = prepare_nalt(cell, run_dir)
    preparation_seconds = time.perf_counter()-began
    iteration = 0

    def classify(**kw):
        nonlocal iteration
        iteration += 1
        layer = iteration
        def one(part):
            labels = n.format_candidates_for_prompt(part)
            if parents is not None:
                labels = "\n".join(f"{i}. {node['displayName']} | path={path_text(index, parents, node['uri'])}"
                                   for i, node in enumerate(part,1))
            user = (f"Seed topic: {kw['seed_label']}\n\n"
                    f"Classify exactly {len(part)} NALT candidate concepts as INCLUDE or EXCLUDE.\n\n"
                    f"Candidate concepts:\n{labels}\n")
            decisions = api.infer(prompt, user, [r["uri"] for r in part],
                                  dict(seed=n.NALT_SOIL_URI, workload="N", iteration=layer))
            return [dict(node, classification=d["classification"], confidence=d["confidence"])
                    for node, d in zip(part, decisions)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            outputs = list(pool.map(one, chunks(kw["nodes"], 20)))
        result = [r for output in outputs for r in output]
        save_json(run_dir/"frontiers"/f"{layer:02d}.json", result)
        return result

    start = time.perf_counter()
    result = n.iterative_nalt_extraction(index, seed_labels=["soil"],
        gold_root_uri=n.NALT_SOIL_SCIENCE_URI, benchmark_name="soil_science",
        max_iterations=20, batch_size=20, llm_model=cell["model"], llm_provider=cell["provider"],
        llm_max_workers=5, system_prompt=prompt, result_mode="zero_shot_manual",
        exclude_evaluation_uris=excluded, exclude_examples_files=[N_EXAMPLES], classifier=classify)
    traversal_seconds = time.perf_counter()-start
    assert result["metrics"]["gold_positive_count"] == 1032
    expected_gold = set(read_json(HERE / "data/nalt_soil_gold.json"))
    assert result["gold_uris"] - result["exclude_evaluation_uris"] == expected_gold
    visited = {row["uri"] for row in result["all_visited"]}
    remaining = {neighbor["uri"] for node in result["all_included"]
                 for neighbor in index.one_hop_neighbors(URIRef(node["uri"])) if neighbor["uri"] not in visited}
    result.update(preparation_seconds=preparation_seconds, traversal_seconds=traversal_seconds,
                  subgraph_count=1, output_included_nodes=len(result["all_included"]),
                  incomplete_frontier_count=len(remaining))
    return result


def aggregate_fixed(rows, pairs):
    return dict(common_pooled=w.compute_metrics(rows),
                aligned_fold=w.compute_fold_metric_aggregation(w.rows_in_pairs(rows, pairs), 3))


def prepare_fixed_candidates(prompt, run_dir):
    manifest = read_json(D3_BASELINE / "manifest.json")
    historical = [json.loads(line) for line in (D3_BASELINE / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = batch_runner.read_rows_csv(D3_BASELINE / "rows.csv")
    by_id = {row["row_id"]: row for row in rows}
    prompt = prompt.strip()
    assert not re.search(r"\b(path|paths|bridge|bridges|chain|chains)\b|-->|reached_via|parent=", prompt, re.I)
    assert len(rows) == len(by_id) == 2014
    seed_count = len({row["seed_qid"] for row in rows})
    assert manifest["strategy"] == "zero_shot_manual" and manifest["examples_in_prompt"] is False
    assert len(historical) == len(manifest["requests"])
    jobs, seen = [], []
    for request, group in zip(historical, manifest["requests"]):
        assert request["custom_id"] == group["key"]
        ids = group["row_ids"]
        assert 1 <= len(ids) <= 20
        part = [by_id[row_id] for row_id in ids]
        old_body = request["body"]
        old_user = old_body["input"][1]["content"]
        user = w.build_eval_user_message(part, "labels")
        assert user == re.sub(r" \| path=[^\n]*", "", old_user)
        assert old_body["text"]["format"]["schema"] == batch_runner.batch_response_schema_for_openai()
        assert not re.search(r"path=|reached_via=|parent=|depth=|-->", user)
        jobs.append(dict(key=group["key"], row_ids=ids, user=user))
        seen.extend(ids)
    assert len(seen) == len(set(seen)) == len(rows) and set(seen) == set(by_id)
    graph = w.load_graph_context(3)
    old_predictions, errors = batch_runner.load_batch_predictions(D3_BASELINE / "batch_results.jsonl", manifest)
    assert not errors and len(old_predictions) == len(rows)
    old_gated = batch_runner.apply_bfs_gating(rows, old_predictions, dataset=3, graph_context=graph)
    saved = batch_runner.read_rows_csv(D3_BASELINE / "bfs_gated_predictions.csv")
    for row in saved:
        row["prediction"] = int(row["prediction"])
        row["raw_prediction"] = int(row["raw_prediction"])
    saved_by_id = {r["row_id"]: r for r in saved}
    assert all(old_predictions[r["row_id"]]["prediction"] == r["raw_prediction"] for r in saved)
    replay_changes = [dict(row_id=r["row_id"], seed_qid=r["seed_qid"], qid=r["qid"],
                           target=r["target"], published_prediction=saved_by_id[r["row_id"]]["prediction"],
                           replayed_prediction=r["prediction"]) for r in old_gated
                      if r["prediction"] != saved_by_id[r["row_id"]]["prediction"]]
    old_summary = read_json(D3_BASELINE / "summary.json")
    for key, value in w.compute_metrics(saved).items():
        if key in old_summary["metrics"]["bfs_gated"]:
            assert abs(value - old_summary["metrics"]["bfs_gated"][key]) < 1e-12
    baseline_files = [ROOT / "reproduction/results/decisions" / name for name in
                      ("dataset3_lstm_tuned.csv", "dataset3_path_analogy_tuned.csv")]
    baseline_pair_sets = []
    for baseline_file in baseline_files:
        with baseline_file.open(encoding="utf-8") as handle:
            baseline_pair_sets.append({(r["from"], r["QID"]) for r in csv.DictReader(handle)})
    assert baseline_pair_sets[0] == baseline_pair_sets[1]
    pairs = baseline_pair_sets[0]
    assert len(w.rows_in_pairs(rows, pairs)) == 1384
    assert len({r["seed_qid"] for r in w.rows_in_pairs(rows, pairs)}) == 20
    # Verify the original paper estimator before paid inference. All-common rows
    # contain 27 queries; only the 20 baseline-aligned queries have test folds.
    aggregate_fixed(old_gated, pairs)
    historical_audit = dict(
        raw_prediction_differences=0, gated_prediction_differences=len(replay_changes),
        changed_rows=replay_changes, original=aggregate_fixed(saved, pairs),
        matched_replay=aggregate_fixed(old_gated, pairs),
        interpretation="Retained raw answers are reused. Both conditions use the same scorer; the historical saved CSV is preserved separately. The source of the historical replay discrepancy is not established.")
    old_prompt = historical[0]["body"]["input"][0]["content"]
    assert all(item["body"]["input"][0]["content"] == old_prompt for item in historical)
    prompt_changes = "".join(difflib.unified_diff(
        old_prompt.splitlines(keepends=True), (prompt + "\n").splitlines(keepends=True),
        fromfile="existing_D3_effective_system_prompt", tofile="D3_no_path_system_prompt"))
    offline_validation = dict(passed=True, checked_utc=utc_now(),
        checks=[f"2014 fixed rows, {seed_count} queries, 1384 aligned rows over 20 queries", "exact original candidate groups and order",
                "user messages differ only by removed path fields", "no supplied-path instructions in system prompt",
                "same response schema", "historical raw predictions reproduced exactly",
                "saved CSV reproduces historical pooled metrics; replay difference recorded",
                "baseline-aligned five-fold estimator is available"], request_count=len(jobs))
    validation = dict(historical_scoring_audit=historical_audit,
                      offline_validation=offline_validation, prompt_changes=prompt_changes)
    print(f"Input validation passed: {len(rows)} rows, {len(jobs)} requests, {seed_count} seeds; no API calls.", flush=True)
    return prompt, rows, jobs, graph, pairs, old_gated, old_predictions, validation


class FixedCandidatesAPI(RecordedAPI):
    def request_parts(self, system, user):
        endpoint, headers, payload = super().request_parts(system, user)
        payload["prompt_cache_key"] = "subkg-d3-zero_shot_manual-no_path-gpt54mini"
        return endpoint, headers, payload

    def guard(self):
        super().guard()
        if sum(e.get("estimated_usd") or e.get("no_cache_usd") or 0 for e in self.events) >= 2:
            self.stopped.set()
            raise RuntimeError("USD 2 dispatch cap reached")


def run_fixed_candidates(cell, api, prompt, run_dir):
    began = time.perf_counter()
    prompt, rows, jobs, graph, pairs, old_gated, old_raw, validation = prepare_fixed_candidates(prompt, run_dir)
    preparation_seconds = time.perf_counter() - began
    started = time.perf_counter()
    predictions = {}

    def classify(job):
        decisions = api.infer(prompt, job["user"], job["row_ids"],
                              dict(dataset=3, original_request_key=job["key"], condition="NO_PATH"))
        result = {
            row_id: dict(prediction=int(decision["classification"] == "INCLUDE"),
                         classification=decision["classification"], confidence=decision["confidence"])
            for row_id, decision in zip(job["row_ids"], decisions)
        }
        save_json(run_dir / "groups" / (job["key"] + ".json"), result)
        return result

    predictions.update(classify(jobs[0]))
    assert api.summary()["served_models"] == [cell["model"]]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(classify, job) for job in jobs[1:]]
        for future in as_completed(futures):
            predictions.update(future.result())
    inference_seconds = time.perf_counter() - started
    assert len(predictions) == len(rows)
    scored = batch_runner.apply_bfs_gating(rows, predictions, dataset=3, graph_context=graph)
    batch_runner.write_scored_csv(run_dir / "predictions.csv", scored)
    return dict(
        baseline=aggregate_fixed(old_gated, pairs), ablation=aggregate_fixed(scored, pairs),
        raw_baseline=w.compute_metrics(batch_runner.raw_prediction_rows(rows, old_raw)),
        raw_ablation=w.compute_metrics(batch_runner.raw_prediction_rows(rows, predictions)),
        preparation_seconds=preparation_seconds, inference_wall_seconds=inference_seconds,
        execution_wall_seconds=time.perf_counter() - started,
        process_wall_seconds=time.perf_counter() - began,
        validation=validation,
    )


def validate_run(cell, prompt, run_dir):
    """Construct and check inputs without opening a provider client."""
    if cell["protocol"] == "fixed_candidates":
        _, rows, jobs, _, _, _, _, validation = prepare_fixed_candidates(prompt, run_dir)
        return dict(scored_rows=len(rows), request_groups=len(jobs), validation=validation)
    if cell["benchmark"] == "wikidata_dataset2":
        _, _, rows, pairs = wikidata_data()
        return dict(scored_rows=len(rows), aligned_rows=len(w.rows_in_pairs(rows, pairs)))
    index, parents, _ = prepare_nalt(cell, run_dir)
    return dict(labelled_nodes=len(index.pref_labels), scored_gold=1032,
                path_nodes=len(parents) if parents is not None else None)

