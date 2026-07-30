#!/usr/bin/env python3
"""
Wikidata Subgraph Sampler — 2022 Historical Snapshot
=====================================================
Same output format as sample_subgraphs.py, but fetches each entity's
state as of 2022-06-01 using the Wikidata Revision History API instead
of querying current Wikidata via SPARQL.  No 100 GB dump download needed.

Why this matters
----------------
The KGPrune datasets were built from a 2022 Wikidata snapshot.  Running
sample_subgraphs.py against the live 2026 endpoint produces many "bridge
node" gaps caused by Wikidata drift (P279 edges added/removed/redirected
since 2022).  This script reproduces the 2022 graph faithfully for every
QID already enumerated in the gold-decision CSVs.

Strategy
--------
For each QID in the expected set (seeds ∪ gold-decision QIDs), we call:

  GET https://www.wikidata.org/w/api.php
      ?action=query
      &titles=<single QID>       ← ONE entity per request (API restriction)
      &prop=revisions
      &rvlimit=1
      &rvdir=older
      &rvstart=2022-06-01T00:00:00Z   ← last revision ≤ this timestamp
      &rvprop=content
      &rvslots=main
      &format=json

IMPORTANT: The Wikidata API forbids rvstart/rvdir/rvlimit when querying
multiple titles (error code: invalidparammix).  We MUST query one QID per
call.  To compensate, ThreadPoolExecutor runs CONCURRENT parallel workers.
With 3 workers and 1s sleep each → ~3 req/s ≈ 180 req/min (well within
Wikidata's ~200 req/min limit for unauthenticated users).

Response path to entity JSON:
  data["query"]["pages"][page_id]["revisions"][0]["slots"]["main"]["*"]
  → a JSON string that is json.loads()'d to get the full entity dict.

Entity dict structure (relevant fields):
  entity["labels"]["en"]["value"]          → English label
  entity["claims"]["P31"][i]["mainsnak"]   → claim snak
    ["snaktype"]          == "value"
    ["datavalue"]["type"] == "wikibase-entityid"
    ["datavalue"]["value"]["id"]           → e.g. "Q5"
  entity["claims"][...][i]["rank"]         → "normal"|"preferred"|"deprecated"

Edges
-----
  Forward  (P31, P279, …):  entity has claim P → value; both in expected set.
  Inverse  ((-P279), …):    entity X has claim P → V; V is in expected set.
                             Derived edge: (V, (-)P, X)  (no extra API call).

Labels are extracted from the same entity JSON — no SPARQL label queries.

Bridge analysis
---------------
If a gold node is unreachable through expected-set nodes only, we inspect
its own 2022 entity JSON: any claim target outside the expected set is a
candidate bridge node.  All analysis uses 2022 data.

Outputs (per dataset)
---------------------
  datasetN_subgraph_2022.ttl          Standard RDF Turtle (wdt: triples + rdfs:label)
  datasetN_subgraph_2022_report.json  Stats + reachability + bridge analysis

Requirements
------------
  pip install requests
  Python 3.8+
"""

import csv
import argparse
import json
import os
import time
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ─── Configuration ────────────────────────────────────────────────────────────

DATA_DIR      = str(Path(__file__).resolve().parent)
SNAPSHOT_DATE = "2022-06-01T00:00:00Z"   # fetch last revision ≤ this timestamp
REVISION_URL  = "https://www.wikidata.org/w/api.php"
USER_AGENT    = (
    "KGPruneResearch/1.0 "
    "(2022 historical snapshot; academic benchmark; "
    "contact: researcher@example.com)"
)

CONCURRENT     = 10    # parallel API requests (Wikidata allows ~200 req/min)
SLEEP_SEC      = 1    # per-worker delay between requests
MAX_RETRIES    = 4    # retries with exponential back-off on transient failures
FETCH_BRIDGE_LABELS = True

DATASETS = {
    1: {
        "dataset_dir":    "dataset1",
        "seeds_file":     "seeds_dataset1.csv",
        "props_file":     "properties_dataset1.csv",
        "decisions_file": "dataset1_gold_decisions.csv",
        "schema":         "full",      # columns: from, starting label, QID, label, depth, target
        "output_ttl":     "dataset1_subgraph_2022.ttl",
        "output_json":    "dataset1_subgraph_2022_report.json",
        "output_perseed_json": "dataset1_subgraph_2022_perseed.json",
    },
    2: {
        "dataset_dir":    "dataset2",
        "seeds_file":     "seeds_dataset2.csv",
        "props_file":     "properties_dataset2.csv",
        "decisions_file": "dataset2_gold_decisions.csv",
        "schema":         "full",
        "output_ttl":     "dataset2_subgraph_2022.ttl",
        "output_json":    "dataset2_subgraph_2022_report.json",
        "output_perseed_json": "dataset2_subgraph_2022_perseed.json",
    },
    3: {
        "dataset_dir":    "dataset3",
        "seeds_file":     "seeds_dataset3.csv",
        "props_file":     "properties_dataset3.csv",
        "decisions_file": "dataset3_gold_decisions.csv",
        "schema":         "full",
        "output_ttl":     "dataset3_subgraph_2022.ttl",
        "output_json":    "dataset3_subgraph_2022_report.json",
        "output_perseed_json": "dataset3_subgraph_2022_perseed.json",
    },
}


# ─── I/O helpers (identical to sample_subgraphs.py) ──────────────────────────

def load_seeds(path: str) -> list:
    """Return list of QID strings from a one-per-line seeds file."""
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def load_properties(path: str):
    """
    Parse the properties file and return (forward_props, inverse_props).

    Lines WITHOUT a prefix  →  forward  e.g.  P279
    Lines starting with (-) →  inverse  e.g.  (-)P279  →  stored as "P279"
    """
    forward, inverse = [], []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip()
            if not p:
                continue
            if p.startswith("(-)"):
                inverse.append(p[3:])
            else:
                forward.append(p)
    return forward, inverse


def load_gold_decisions(path: str, schema: str):
    """
    Load gold decisions CSV, normalised to a common dict structure.

    Returns
    -------
    decisions : list of dicts  (keys: from, starting_label, QID, label, depth, target)
    gold_qids : set of every QID mentioned in either the 'from' or 'QID' columns
    """
    decisions = []
    gold_qids = set()

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            from_qid = row["from"].strip()
            qid      = row["QID"].strip()

            if schema == "full":
                rec = {
                    "from":           from_qid,
                    "starting_label": row.get("starting label", ""),
                    "QID":            qid,
                    "label":          row.get("label", ""),
                    "depth":          int(row["depth"]) if row.get("depth") else None,
                    "target":         int(row["target"]),
                }
            else:   # "minimal" (dataset 3)
                rec = {
                    "from":           from_qid,
                    "starting_label": "",
                    "QID":            qid,
                    "label":          "",
                    "depth":          None,
                    "target":         int(row["decision"]),
                }

            decisions.append(rec)
            gold_qids.add(from_qid)
            gold_qids.add(qid)

    return decisions, gold_qids


# ─── Revision History API ─────────────────────────────────────────────────────
#
# IMPORTANT: The MediaWiki API forbids rvstart / rvdir / rvlimit when multiple
# titles are specified (error code: invalidparammix).
# Therefore we MUST fetch ONE entity per API call.
# To compensate, we use ThreadPoolExecutor for concurrent requests.
#

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

# Thread-safe progress counter
_progress_lock = threading.Lock()


def fetch_single_entity(qid: str, date: str = SNAPSHOT_DATE):
    """
    Fetch the 2022 revision of a SINGLE entity.

    Returns
    -------
    (qid, entity_dict | None)

    API response path
    -----------------
    data["query"]["pages"][page_id]["revisions"][0]["slots"]["main"]["*"]
      → JSON string → json.loads() → entity dict
    """
    params = {
        "action":  "query",
        "titles":  qid,             # ← SINGLE title only
        "prop":    "revisions",
        "rvlimit": "1",
        "rvdir":   "older",
        "rvstart": date,
        "rvprop":  "content",
        "rvslots": "main",
        "format":  "json",
    }

    data = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.get(REVISION_URL, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:
            wait = SLEEP_SEC * (2 ** attempt)
            if attempt == MAX_RETRIES - 1:
                print(f"\n    [ERROR] All retries exhausted for {qid}: {exc}")
            time.sleep(wait)

    if data is None:
        return qid, None

    # Check for API-level errors
    if "error" in data:
        print(f"\n    [ERROR] API error for {qid}: {data['error']}")
        return qid, None

    # Handle normalisation / redirects
    resolved_title = qid
    for norm in data.get("query", {}).get("normalized", []):
        if norm["from"] == qid:
            resolved_title = norm["to"]
    for redir in data.get("query", {}).get("redirects", []):
        if redir["from"] == resolved_title:
            resolved_title = redir["to"]

    # Find the page
    pages = data.get("query", {}).get("pages", {})
    for page_id, page in pages.items():
        if int(page_id) < 0:
            return qid, None   # entity does not exist

        revisions = page.get("revisions", [])
        if not revisions:
            return qid, None   # created after snapshot date

        content_str = (
            revisions[0]
            .get("slots", {})
            .get("main", {})
            .get("*", "")
        )
        if not content_str:
            return qid, None

        try:
            return qid, json.loads(content_str)
        except json.JSONDecodeError as exc:
            print(f"\n    [WARN] JSON parse error for {qid}: {exc}")
            return qid, None

    return qid, None


def _worker_fetch(qid: str, date: str) -> tuple:
    """Worker function: fetch one entity, then sleep for rate limiting."""
    result = fetch_single_entity(qid, date)
    time.sleep(SLEEP_SEC)
    return result


RETRY_ROUNDS   = 3    # how many retry rounds for 429-failed entities
RETRY_COOLDOWN = 30   # seconds to wait before each retry round
RETRY_WORKERS  = 2    # reduced concurrency for retries
RETRY_SLEEP    = 3    # longer per-worker sleep for retries


def _worker_fetch_slow(qid: str, date: str) -> tuple:
    """Retry worker: fetch one entity with longer sleep (for rate-limit recovery)."""
    result = fetch_single_entity(qid, date)
    time.sleep(RETRY_SLEEP)
    return result


def fetch_all_entities(qids, date: str = SNAPSHOT_DATE) -> dict:
    """
    Fetch 2022 revisions for all QIDs using concurrent requests.

    Uses CONCURRENT parallel workers, each sleeping SLEEP_SEC between requests.
    Effective rate: ~CONCURRENT requests per SLEEP_SEC seconds.

    After the initial pass, any QIDs that returned None (possibly due to 429
    rate-limit errors) are retried up to RETRY_ROUNDS times with reduced
    concurrency and longer cooldown.

    Returns dict {qid: entity_dict | None}.
    """
    entities = {}
    qid_list = sorted(set(qids))
    total    = len(qid_list)

    est_sec = total * SLEEP_SEC / CONCURRENT
    est_min = est_sec / 60
    print(f"  Fetching {total} entity revisions at {date}")
    print(f"  Concurrency: {CONCURRENT} workers  |  "
          f"Sleep: {SLEEP_SEC}s/worker  |  "
          f"Est. time: ~{est_min:.0f} min")

    done = 0
    n_ok = 0

    # ── Initial pass ──────────────────────────────────────────────────────
    with ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
        futures = {
            pool.submit(_worker_fetch, q, date): q
            for q in qid_list
        }

        for future in as_completed(futures):
            qid, entity = future.result()
            entities[qid] = entity
            done += 1
            if entity is not None:
                n_ok += 1
            if done % 50 == 0 or done == total:
                print(f"    Progress: {done}/{total}  "
                      f"({n_ok} ok, {done - n_ok} missing)",
                      end="\r", flush=True)

    n_missing = total - n_ok
    print(f"\n  Initial pass: {n_ok} ok  |  {n_missing} missing")

    # ── Retry rounds for failed QIDs ──────────────────────────────────────
    for retry_round in range(1, RETRY_ROUNDS + 1):
        missing_qids = sorted(q for q, v in entities.items() if v is None)
        if not missing_qids:
            break

        print(f"\n  Retry round {retry_round}/{RETRY_ROUNDS}: "
              f"{len(missing_qids)} QIDs to retry  "
              f"(cooldown {RETRY_COOLDOWN}s …)")
        time.sleep(RETRY_COOLDOWN)

        recovered = 0
        retry_done = 0

        with ThreadPoolExecutor(max_workers=RETRY_WORKERS) as pool:
            futures = {
                pool.submit(_worker_fetch_slow, q, date): q
                for q in missing_qids
            }
            for future in as_completed(futures):
                qid, entity = future.result()
                retry_done += 1
                if entity is not None:
                    entities[qid] = entity
                    recovered += 1
                if retry_done % 20 == 0 or retry_done == len(missing_qids):
                    print(f"    Retry {retry_round} progress: "
                          f"{retry_done}/{len(missing_qids)}  "
                          f"(recovered: {recovered})",
                          end="\r", flush=True)

        n_ok += recovered
        print(f"\n  Retry round {retry_round}: recovered {recovered} entities")

    # ── Final summary ─────────────────────────────────────────────────────
    final_missing = sum(1 for v in entities.values() if v is None)
    print(f"\n  Final result: {total - final_missing} ok  |  "
          f"{final_missing} still missing at {date}")

    if final_missing:
        missing_list = sorted(q for q, v in entities.items() if v is None)
        print(f"  QIDs with no revision at {date} "
              f"(created after date, deleted, or permanently failed):")
        for q in missing_list[:30]:
            print(f"    {q}")
        if len(missing_list) > 30:
            print(f"    … and {len(missing_list) - 30} more")

    return entities


# ─── Claims & label parsing ───────────────────────────────────────────────────

def parse_claims(qid: str, entity: dict, props: list) -> list:
    """
    Extract forward property edges from an entity's Wikidata claims JSON.

    Parameters
    ----------
    qid    : the entity being parsed (always the subject)
    entity : parsed Wikidata entity dict
    props  : property IDs to extract, e.g. ["P31", "P279"]

    Returns
    -------
    list of (qid, prop_id, value_qid)

    Rules applied
    -------------
    • Skips statements with rank == "deprecated"
    • Skips snaks with snaktype != "value"
    • Skips non-wikibase-entityid datavalues (dates, strings, …)
    • Skips values whose ID starts with "P" or "L" (only keeps Q-items)

    Entity claim structure (for reference):
      entity["claims"]["P31"][i] = {
          "rank": "normal" | "preferred" | "deprecated",
          "type": "statement",
          "mainsnak": {
              "snaktype": "value",
              "property": "P31",
              "datavalue": {
                  "type": "wikibase-entityid",
                  "value": {
                      "entity-type": "item",
                      "numeric-id": 5,
                      "id": "Q5"
                  }
              }
          }
      }
    """
    edges  = []
    claims = entity.get("claims", {})

    # Some old Wikidata revisions store empty claims as [] not {}
    if not isinstance(claims, dict):
        return edges

    for prop in props:
        for stmt in claims.get(prop, []):
            if stmt.get("rank") == "deprecated":
                continue

            mainsnak = stmt.get("mainsnak", {})
            if mainsnak.get("snaktype") != "value":
                continue

            dv = mainsnak.get("datavalue", {})
            if dv.get("type") != "wikibase-entityid":
                continue

            val_id = dv.get("value", {}).get("id", "")
            if val_id.startswith("Q"):
                edges.append((qid, prop, val_id))

    return edges


def extract_labels_from_revisions(entities_map: dict) -> dict:
    """
    Pull English labels directly from the fetched entity JSON.

    Much faster than a separate SPARQL label query because the label
    is already present in the revision content we already fetched.

    Entity label structure:
      entity["labels"]["en"] = {"language": "en", "value": "research institute"}

    Returns dict {qid: label_string}.
    """
    labels = {}
    for qid, entity in entities_map.items():
        if entity is None:
            continue
        label = entity.get("labels", {}).get("en", {}).get("value")
        if label:
            labels[qid] = label
    return labels


# ─── Graph building from 2022 revisions ──────────────────────────────────────

def build_edges_from_revisions(entities_map: dict,
                                forward_props: list,
                                inverse_props: list) -> list:
    """
    Build the induced subgraph from 2022 entity revisions — no SPARQL needed.

    Forward edges  (A, P,    B):
        A's 2022 claims include P → B, and B is in the expected set.

    Inverse edges  (B, (-)P, A):
        A's 2022 claims include P → B, and B is in the expected set.
        Logically: "from B, following the inverse of P, you reach A."
        Derived without extra API calls by iterating over ALL entities' claims.

    This correctly handles properties that appear in BOTH forward_props and
    inverse_props (e.g. P279 in Datasets 1 & 2):
        Claim (X, P279, V)  →  forward edge  (X, P279,    V)   [if V in set]
                            +  inverse edge  (V, (-)P279, X)   [if V in set]

    Both endpoints must be in entities_map (i.e. in the expected set).

    Returns
    -------
    Deduplicated list of (entity_qid, pred_label, neighbor_qid) logical edges.
    """
    fwd_set   = set(forward_props)
    inv_set   = set(inverse_props)
    all_props = list(fwd_set | inv_set)   # props that appear in either role
    in_set    = set(entities_map)         # expected-node QIDs

    edges = []

    for qid, entity in entities_map.items():
        if entity is None:
            continue

        for subj, pred, obj in parse_claims(qid, entity, all_props):

            # Forward edge: A -pred-> B  (requires B in expected set)
            if pred in fwd_set and obj in in_set:
                edges.append((subj, pred, obj))

            # Derived inverse edge: B -(-pred)-> A  (requires B in expected set)
            if pred in inv_set and obj in in_set:
                edges.append((obj, f"(-){pred}", subj))

    # Deduplicate while preserving discovery order
    seen    = set()
    deduped = []
    for e in edges:
        if e not in seen:
            seen.add(e)
            deduped.append(e)

    return deduped


# ─── Triple normalisation ─────────────────────────────────────────────────────

def to_wikidata_triples(subgraph_edges: list) -> list:
    """
    Convert logical edges (which may include (-)P notation) back to the
    canonical Wikidata RDF direction, and deduplicate.

    Logical edge         →  Canonical Wikidata triple
    ─────────────────────────────────────────────────
    (A, P279,    B)      →  (A, P279, B)           [forward: unchanged]
    (A, (-)P279, B)      →  (B, P279, A)           [inverse: subject/object swapped]

    Returns a sorted, deduplicated list of (subject, predicate, object) tuples.
    """
    triple_set = set()
    for entity, pred, neighbor in subgraph_edges:
        if pred.startswith("(-)"):
            actual_pred = pred[3:]
            triple_set.add((neighbor, actual_pred, entity))   # flip
        else:
            triple_set.add((entity, pred, neighbor))
    return sorted(triple_set)


# ─── Turtle writer ────────────────────────────────────────────────────────────

def _escape_turtle(s: str) -> str:
    """Escape characters that are special inside a Turtle double-quoted string."""
    return (
        s.replace("\\", "\\\\")
         .replace('"',  '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )


def write_turtle(path: str, triples: list, labels: dict,
                 seed_set: set, snapshot_date: str) -> tuple:
    """
    Write a standards-compliant Turtle file for the 2022 subgraph.

    Contents
    --------
    Prefixes       wd:, wdt:, rdfs:
    Header comment with snapshot date
    Edge section   one  wd:S wdt:P wd:O .  triple per line
    Label section  one  wd:S rdfs:label "…"@en .  per labelled node

    Returns (n_nodes, n_labeled, n_missing).
    """
    all_nodes = set()
    for s, _, o in triples:
        all_nodes.add(s)
        all_nodes.add(o)

    labeled_nodes  = [(q, labels[q]) for q in sorted(all_nodes) if q in labels]
    missing_labels = sorted(all_nodes - set(labels))

    with open(path, "w", encoding="utf-8") as f:

        # ── Prefixes ──────────────────────────────────────────────────
        f.write("@prefix wd:   <http://www.wikidata.org/entity/> .\n")
        f.write("@prefix wdt:  <http://www.wikidata.org/prop/direct/> .\n")
        f.write("@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n")
        f.write("\n")
        f.write(f"# Wikidata state as of: {snapshot_date}\n")
        f.write("# Source: Wikidata Revision History API\n")
        f.write("\n")

        # ── Edge triples ──────────────────────────────────────────────
        f.write(f"# ── Subgraph edges  ({len(triples):,} triples) "
                f"────────────────────────────────────────────\n\n")
        for subj, pred, obj in triples:
            f.write(f"wd:{subj} wdt:{pred} wd:{obj} .\n")

        f.write("\n")

        # ── Label triples ─────────────────────────────────────────────
        f.write(f"# ── Node labels  ({len(labeled_nodes):,} labeled  |  "
                f"{len(missing_labels):,} without English label) "
                f"───────────────\n\n")
        for qid, label in labeled_nodes:
            escaped = _escape_turtle(label)
            f.write(f'wd:{qid} rdfs:label "{escaped}"@en .\n')

        if missing_labels:
            f.write("\n")
            f.write("# Nodes with no English label in 2022 Wikidata:\n")
            for qid in missing_labels:
                f.write(f"#   wd:{qid}\n")

    return len(all_nodes), len(labeled_nodes), len(missing_labels)


# ─── Reachability ─────────────────────────────────────────────────────────────

def bfs_reachable(seeds: list, adjacency: dict) -> set:
    """BFS from seeds; returns the set of all reachable nodes."""
    visited = set(seeds)
    queue   = deque(seeds)
    while queue:
        node = queue.popleft()
        for nb in adjacency.get(node, set()):
            if nb not in visited:
                visited.add(nb)
                queue.append(nb)
    return visited


def build_per_seed_subgraphs(seeds, decisions, canonical_triples, labels):
    """
    Build isolated subgraphs for each seed, scoped to that seed's gold decisions.

    For seed S, the subgraph contains only canonical triples where:
      1. Both endpoints are in {S} ∪ {gold decision QIDs for S}, AND
      2. The two endpoints are at adjacent depths (|depth(s) - depth(o)| == 1).

    Rule 2 eliminates lateral edges (e.g. two depth-1 nodes linked by P279)
    that would cause unnecessary BFS expansion in prompt-based evaluation.

    When depth information is unavailable (dataset 3, schema="minimal"),
    only rule 1 is applied.

    Parameters
    ----------
    seeds             : list of seed QID strings
    decisions         : list of dicts with keys "from", "QID", and optionally "depth"
    canonical_triples : list of (subj, pred, obj) canonical Wikidata triples
    labels            : dict {qid: english_label_string}

    Returns
    -------
    dict  {seed_qid: {"label": str, "edges": [[s,p,o], ...],
                       "labels": {qid: label}, "stats": {"n_edges": int, "n_nodes": int}}}
    """
    # Group decision info by seed: {seed_qid: {QID: depth_or_None, ...}}
    decisions_by_seed = defaultdict(dict)
    for dec in decisions:
        decisions_by_seed[dec["from"]][dec["QID"]] = dec.get("depth")

    per_seed = {}
    for seed in seeds:
        seed_dec = decisions_by_seed.get(seed, {})

        # Node universe for this seed: the seed itself + its gold decision QIDs
        seed_nodes = {seed} | set(seed_dec.keys())

        # Build depth map: seed=0, decision nodes from gold CSV
        depth_map = {seed: 0}
        has_depth = True
        for qid, d in seed_dec.items():
            if d is not None:
                depth_map[qid] = d
            else:
                has_depth = False

        # Filter canonical triples to those where BOTH endpoints are in this seed's universe
        seed_edges = []
        for s, p, o in canonical_triples:
            if s not in seed_nodes or o not in seed_nodes:
                continue
            # If depth info available, only keep edges between adjacent depths
            if has_depth:
                ds = depth_map.get(s)
                do = depth_map.get(o)
                if ds is not None and do is not None and abs(ds - do) != 1:
                    continue
            seed_edges.append([s, p, o])

        # Collect labels for all nodes in the universe that have labels
        seed_labels = {q: labels[q] for q in seed_nodes if q in labels}

        per_seed[seed] = {
            "label": labels.get(seed, seed),
            "edges": seed_edges,
            "labels": seed_labels,
            "stats": {
                "n_edges": len(seed_edges),
                "n_nodes": len(seed_nodes),
            },
        }

    return per_seed


def write_per_seed_json(path, per_seed_data, ds_id, seeds, fwd_props, inv_props,
                        snapshot_date):
    """
    Write per-seed subgraphs to a single JSON file.

    Parameters
    ----------
    path           : output file path
    per_seed_data  : dict from build_per_seed_subgraphs()
    ds_id          : dataset ID (1, 2, or 3)
    seeds          : list of seed QID strings
    fwd_props      : list of forward property IDs
    inv_props      : list of inverse property IDs
    snapshot_date  : ISO timestamp of the Wikidata snapshot
    """
    import datetime

    output = {
        "metadata": {
            "dataset_id": ds_id,
            "snapshot_date": snapshot_date,
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "n_seeds": len(seeds),
            "forward_properties": fwd_props,
            "inverse_properties": inv_props,
        },
        "seeds": per_seed_data,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


# ─── Bridge-node analysis (uses 2022 entity data directly) ───────────────────

def find_bridge_nodes(unreachable_gold: set,
                      entities_map: dict,
                      expected_nodes: set,
                      forward_props: list,
                      inverse_props: list) -> list:
    """
    Find candidate bridge nodes for unreachable gold nodes using 2022 data.

    For each unreachable gold node G, we inspect G's own 2022 entity JSON:

    (a) Forward claims of G:  G --prop--> X where X ∉ expected_nodes
        Means: to follow `prop` from G (if G were a source), X would be needed.

    (b) Parent claims of G via inverse props:  G --inv_prop--> P where P ∉ expected_nodes
        E.g. G --P279--> P means G is a subclass of P.
        If P is not in the dataset, it is a missing bridge for any seed that
        would have reached G by following (-)P279 downward through P.

    Unlike sample_subgraphs.py which uses a live SPARQL query (current Wikidata),
    this analysis is fully based on the 2022 revision data — no extra API calls.

    Returns list of dicts: gold_node, bridge_node, relation, direction, source.
    """
    fwd_set   = set(forward_props)
    inv_set   = set(inverse_props)
    all_props = list(fwd_set | inv_set)
    report    = []

    for gold in sorted(unreachable_gold):
        entity = entities_map.get(gold)
        if entity is None:
            # entity didn't exist in 2022 — it IS the gap itself
            report.append({
                "gold_node":   gold,
                "bridge_node": None,
                "relation":    None,
                "direction":   "gold node had no revision at snapshot date",
                "source":      f"2022 revision fetch at {SNAPSHOT_DATE}",
            })
            continue

        for _, pred, target in parse_claims(gold, entity, all_props):
            if target in expected_nodes:
                continue   # already in the dataset — not a bridge

            if pred in fwd_set:
                # G --prop--> bridge  (outside dataset)
                report.append({
                    "gold_node":   gold,
                    "bridge_node": target,
                    "relation":    pred,
                    "direction":   (f"gold --{pred}--> bridge "
                                   f"(bridge is outside dataset, 2022 state)"),
                    "source":      f"2022 revision at {SNAPSHOT_DATE}",
                })

            if pred in inv_set:
                # G --inv_prop--> bridge  (gold's superclass not in dataset)
                # E.g. gold P279-> bridge means no seed can reach gold
                # via (-)P279 unless the bridge is included
                report.append({
                    "gold_node":   gold,
                    "bridge_node": target,
                    "relation":    f"(-){pred}",
                    "direction":   (f"bridge --{pred}--> gold "
                                   f"(gold's 2022 {pred} parent, outside dataset)"),
                    "source":      f"2022 revision at {SNAPSHOT_DATE}",
                })

    return report


# ─── Core per-dataset logic ───────────────────────────────────────────────────

def process_dataset(ds_id: int, cfg: dict):
    join = lambda f: os.path.join(DATA_DIR, cfg["dataset_dir"], f)

    print(f"\n{'=' * 65}")
    print(f"  DATASET {ds_id}")
    print(f"{'=' * 65}")

    # ── Load metadata ──────────────────────────────────────────────────────
    seeds                = load_seeds(join(cfg["seeds_file"]))
    fwd_props, inv_props = load_properties(join(cfg["props_file"]))
    decisions, gold_qids = load_gold_decisions(join(cfg["decisions_file"]),
                                               cfg["schema"])
    seed_set       = set(seeds)
    expected_nodes = seed_set | gold_qids

    print(f"  Seeds                     : {len(seeds)}")
    print(f"  Forward properties        : {fwd_props}")
    print(f"  Inverse properties        : {inv_props}")
    print(f"  Gold decisions (rows)     : {len(decisions)}")
    print(f"  Unique gold QIDs          : {len(gold_qids)}")
    print(f"  Total expected nodes      : {len(expected_nodes)}")

    # ── Step 1: Fetch 2022 revisions for all expected nodes ────────────────
    print(f"\n  [Step 1] Fetching 2022 entity revisions via Revision History API …")
    entities_map = fetch_all_entities(sorted(expected_nodes), SNAPSHOT_DATE)

    n_ok      = sum(1 for v in entities_map.values() if v is not None)
    n_missing = len(entities_map) - n_ok

    # ── Step 2: Extract labels from the same entity JSON (no SPARQL!) ──────
    print(f"\n  [Step 2] Extracting labels from entity JSON …")
    labels = extract_labels_from_revisions(entities_map)
    print(f"  Labels extracted          : {len(labels)} / {len(expected_nodes)}")

    # ── Step 3: Build induced subgraph from 2022 claims ────────────────────
    print(f"\n  [Step 3] Building induced subgraph from 2022 claims …")
    logical_edges = build_edges_from_revisions(entities_map, fwd_props, inv_props)

    adjacency: dict = defaultdict(set)
    for entity, pred, neighbor in logical_edges:
        adjacency[entity].add(neighbor)

    wikidata_triples = to_wikidata_triples(logical_edges)
    print(f"  Logical edges             : {len(logical_edges)}")
    print(f"  Canonical RDF triples     : {len(wikidata_triples)}")

    # ── Step 4: BFS reachability check ────────────────────────────────────
    print(f"\n  [Step 4] BFS reachability from {len(seeds)} seeds …")
    reachable        = bfs_reachable(seeds, dict(adjacency))
    gold_non_seed    = gold_qids - seed_set
    reachable_gold   = gold_non_seed & reachable
    unreachable_gold = gold_non_seed - reachable

    print(f"  Gold (non-seed) nodes     : {len(gold_non_seed)}")
    print(f"  Reachable via dataset     : {len(reachable_gold)}")
    print(f"  NOT reachable             : {len(unreachable_gold)}")

    if unreachable_gold:
        pct = len(unreachable_gold) / len(gold_non_seed) * 100
        print(f"  Unreachable rate          : {pct:.1f}%")
        if pct > 5:
            print(f"  [NOTE] High unreachable rate may indicate the snapshot date")
            print(f"         is misaligned with when the dataset was built.")
            print(f"         Consider adjusting SNAPSHOT_DATE.")

    # ── Filter triples to reachable nodes only ────────────────────────────
    # Unreachable gold nodes can only be reached via bridge nodes that are
    # OUTSIDE the dataset (expected_nodes).  We must not write them — or any
    # edge touching them — to the TTL.  The output file must contain only
    # nodes that are reachable from a seed through dataset-internal edges.
    reachable_triples = [
        (s, p, o) for s, p, o in wikidata_triples
        if s in reachable and o in reachable
    ]
    n_triples_excluded = len(wikidata_triples) - len(reachable_triples)
    if n_triples_excluded:
        print(f"  Triples excluded (unreachable): {n_triples_excluded}")

    # ── Step 5: Bridge-node analysis (2022 entity data) ───────────────────
    bridge_report = []
    if unreachable_gold:
        print(f"\n  [Step 5] Bridge analysis for {len(unreachable_gold)} "
              f"unreachable nodes …")
        bridge_report = find_bridge_nodes(
            unreachable_gold, entities_map, expected_nodes, fwd_props, inv_props
        )
        print(f"  Bridge cases found        : {len(bridge_report)}")

        # Fetch 2022 labels for any bridge nodes outside the expected set
        bridge_qids = {
            item["bridge_node"]
            for item in bridge_report
            if item["bridge_node"] is not None
        }
        unlabeled_bridges = bridge_qids - set(labels)
        if unlabeled_bridges and FETCH_BRIDGE_LABELS:
            print(f"  Fetching labels for {len(unlabeled_bridges)} bridge nodes …")
            bridge_entities = fetch_all_entities(sorted(unlabeled_bridges),
                                                 SNAPSHOT_DATE)
            bridge_labels   = extract_labels_from_revisions(bridge_entities)
            labels.update(bridge_labels)
        elif unlabeled_bridges:
            print(f"  Bridge labels skipped     : {len(unlabeled_bridges)}")
    else:
        print("  ✓  All gold nodes reachable via 2022 subgraph — no bridge nodes.")

    # ── Step 6: Write Turtle file ──────────────────────────────────────────
    # Use reachable_triples (not wikidata_triples) so every node in the TTL
    # is reachable from a seed via within-dataset edges only.
    # Unreachable gold nodes and any edge touching them are omitted.
    subgraph_nodes = set()
    for s, _, o in reachable_triples:
        subgraph_nodes.add(s)
        subgraph_nodes.add(o)

    # Restrict labels dict to nodes that appear in the TTL subgraph
    subgraph_labels = {q: labels[q] for q in subgraph_nodes if q in labels}

    ttl_path = join(cfg["output_ttl"])
    n_nodes, n_labeled, n_missing_lbl = write_turtle(
        ttl_path, reachable_triples, subgraph_labels, seed_set, SNAPSHOT_DATE
    )
    print(f"\n  Turtle file saved         : {ttl_path}")
    print(f"    Nodes in TTL            : {n_nodes}  (reachable from seeds only)")
    print(f"    Labeled                 : {n_labeled}")
    print(f"    Without English label   : {n_missing_lbl}")

    # ── Step 6b: Build & write per-seed subgraphs (JSON) ──────────────────
    # Each seed gets its own isolated subgraph so that BFS evaluation cannot
    # leak across seeds.
    print(f"\n  [Step 6b] Building per-seed subgraphs …")
    per_seed_data = build_per_seed_subgraphs(
        seeds, decisions, reachable_triples, labels
    )
    total_ps_edges = sum(d["stats"]["n_edges"] for d in per_seed_data.values())
    avg_ps_edges   = total_ps_edges / max(len(seeds), 1)
    print(f"  Per-seed subgraphs built  : {len(per_seed_data)}")
    print(f"  Avg edges/seed            : {avg_ps_edges:.1f}")

    perseed_path = join(cfg["output_perseed_json"])
    write_per_seed_json(
        perseed_path, per_seed_data, ds_id, seeds,
        fwd_props, inv_props, SNAPSHOT_DATE
    )
    print(f"  Per-seed JSON saved       : {perseed_path}")

    # ── Step 7: Annotate decisions + write JSON report ─────────────────────
    unreachable_set = set(unreachable_gold)
    for dec in decisions:
        dec["reachable_in_2022_subgraph"] = dec["QID"] not in unreachable_set

    stats = {
        "snapshot_date":              SNAPSHOT_DATE,
        "total_seeds":                len(seeds),
        "total_gold_decisions":       len(decisions),
        "unique_gold_qids":           len(gold_qids),
        "total_expected_nodes":       len(expected_nodes),
        "entities_fetched_ok":        n_ok,
        "entities_missing_at_date":   n_missing,
        "logical_edges":              len(logical_edges),
        "canonical_rdf_triples":      len(wikidata_triples),
        "triples_in_ttl":             len(reachable_triples),
        "triples_excluded_unreachable": n_triples_excluded,
        "subgraph_nodes":             n_nodes,
        "nodes_with_label":           n_labeled,
        "nodes_without_label":        n_missing_lbl,
        "gold_nodes_reachable":       len(reachable_gold),
        "gold_nodes_unreachable":     len(unreachable_gold),
        "bridge_cases_found":         len(bridge_report),
    }

    report = {
        "dataset_id":                  ds_id,
        "snapshot_date":               SNAPSHOT_DATE,
        "seeds":                       seeds,
        "forward_properties":          fwd_props,
        "inverse_properties":          inv_props,
        "stats":                       stats,
        "entities_missing_at_date":    [q for q, v in entities_map.items()
                                        if v is None],
        "unreachable_gold_nodes":      sorted(unreachable_gold),
        "bridge_report":               bridge_report,
        "decisions_with_reachability": decisions,
    }

    json_path = join(cfg["output_json"])
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  JSON report saved         : {json_path}")

    # ── Step 8: Bridge summary to console ─────────────────────────────────
    if bridge_report:
        print(f"\n  ⚠  BRIDGE NODE REPORT  (2022 Wikidata state)")
        print(f"  {'─' * 60}")
        seen_pairs = set()
        n_gold_missing = sum(
            1 for item in bridge_report if item["bridge_node"] is None
        )
        for item in bridge_report:
            if item["bridge_node"] is None:
                g_lbl = labels.get(item["gold_node"], item["gold_node"])
                print(f"  gold   = {item['gold_node']} ({g_lbl})")
                print(f"  issue  = {item['direction']}")
                print()
                continue
            key = (item["gold_node"], item["bridge_node"])
            if key not in seen_pairs:
                seen_pairs.add(key)
                g_lbl = labels.get(item["gold_node"],   item["gold_node"])
                b_lbl = labels.get(item["bridge_node"], item["bridge_node"])
                print(f"  gold   = {item['gold_node']} ({g_lbl})")
                print(f"  bridge = {item['bridge_node']} ({b_lbl})")
                print(f"  via    {item['direction']}")
                print()
        print(f"  {'─' * 60}")
        print(f"  Unique (gold, bridge) pairs      : {len(seen_pairs)}")
        print(f"  Gold nodes missing from 2022     : {n_gold_missing}")

    return report


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    global CONCURRENT, SLEEP_SEC, MAX_RETRIES, FETCH_BRIDGE_LABELS

    parser = argparse.ArgumentParser(
        description="Generate 2022 Wikidata subgraphs for configured datasets."
    )
    parser.add_argument(
        "--dataset",
        action="append",
        type=dataset_key,
        help=(
            "Dataset key to process. May be repeated. "
            f"Available: {', '.join(str(key) for key in DATASETS)}. "
            "Defaults to all datasets."
        ),
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=CONCURRENT,
        help=f"Number of parallel revision API workers. Default: {CONCURRENT}.",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=SLEEP_SEC,
        help=f"Delay after each revision API request per worker. Default: {SLEEP_SEC}.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_RETRIES,
        help=f"Retries per entity request. Default: {MAX_RETRIES}.",
    )
    parser.add_argument(
        "--skip-bridge-labels",
        action="store_true",
        help="Skip extra revision API calls used only to label bridge nodes in the report.",
    )
    args = parser.parse_args()

    CONCURRENT = args.concurrent
    SLEEP_SEC = args.sleep_sec
    MAX_RETRIES = args.max_retries
    FETCH_BRIDGE_LABELS = not args.skip_bridge_labels

    print("Wikidata Subgraph Sampler — 2022 Historical Snapshot")
    print("=" * 65)
    print(f"Data dir      : {DATA_DIR}")
    print(f"Snapshot date : {SNAPSHOT_DATE}")
    print(f"Revision API  : {REVISION_URL}")
    print(f"Concurrency   : {CONCURRENT} workers  "
          f"|  Sleep: {SLEEP_SEC}s/worker  |  Retries: {MAX_RETRIES}")
    print()
    print("NOTE: Only 'requests' is required (no SPARQLWrapper).")
    print("      pip install requests")

    dataset_keys = args.dataset or list(DATASETS)
    all_results = {}
    for ds_id in dataset_keys:
        all_results[ds_id] = process_dataset(ds_id, DATASETS[ds_id])

    print("\n" + "=" * 65)
    print("FINAL SUMMARY")
    print("=" * 65)
    for ds_id, res in all_results.items():
        s = res["stats"]
        print(f"\n  Dataset {ds_id}  (snapshot: {SNAPSHOT_DATE[:10]}):")
        print(f"    Expected nodes           : {s['total_expected_nodes']}")
        print(f"    Fetched OK at date       : {s['entities_fetched_ok']}")
        print(f"    Missing at date          : {s['entities_missing_at_date']}")
        print(f"    Canonical RDF triples    : {s['canonical_rdf_triples']}")
        print(f"    Nodes with label         : {s['nodes_with_label']}"
              f" / {s['subgraph_nodes']}")
        print(f"    Gold nodes reachable     : {s['gold_nodes_reachable']}")
        print(f"    Gold nodes NOT reachable : {s['gold_nodes_unreachable']}")
        print(f"    Bridge cases found       : {s['bridge_cases_found']}")
        print(f"    Output TTL               : {DATASETS[ds_id]['output_ttl']}")

    print("\nDone.")


def dataset_key(value: str):
    for key in DATASETS:
        if str(key) == value:
            return key
    valid = ", ".join(str(key) for key in DATASETS)
    raise argparse.ArgumentTypeError(
        f"unknown dataset {value!r}; expected one of: {valid}"
    )


if __name__ == "__main__":
    main()
