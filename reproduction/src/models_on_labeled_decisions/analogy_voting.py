"""
Copyright (C) 2023 Orange
Authors: Lucas Jarnac, Miguel Couceiro, and Pierre Monnin

This software is distributed under the terms and conditions of the 'MIT'
license which can be found in the file 'LICENSE.txt' in this package distribution 
or at 'https://opensource.org/license/mit/'.
"""
import pickle
import argparse
import sys
from pathlib import Path

import numpy
import lmdb
import pandas

sys.path.append(str(Path(__file__).resolve().parents[1] / "utils"))
sys.path.append(str(Path(__file__).resolve().parents[2]))
from bridge_traversal import embedding_qids_from_transaction, reachable_decision_paths_for_mode
import expansion_properties
import utils
from TqdmLoggingHandler import *


def vote_prediction(prediction, args):
    decision = 0
    keep = 0
    prune = 0

    if "keeping" in prediction:
        y_keeping_pred = prediction["keeping"]
        if args.voting == "majority":
            keep_counter = (y_keeping_pred >= args.voting_threshold).astype('int32')
            keep += numpy.count_nonzero(keep_counter == 1)
            prune += numpy.count_nonzero(keep_counter == 0)
        if args.voting == "weighted":
            keep = y_keeping_pred

    if "pruning" in prediction:
        y_pruning_pred = prediction["pruning"]
        if args.voting == "majority":
            prune_counter = (y_pruning_pred < args.voting_threshold).astype('int32')
            prune += numpy.count_nonzero(prune_counter == 1)
            keep += numpy.count_nonzero(prune_counter == 0)
        if args.voting == "weighted":
            prune = y_pruning_pred

    if args.voting == "weighted":
        if "kk" in args.valid_analogies_pattern and "pp" not in args.valid_analogies_pattern:
            keep = numpy.mean(keep)
        elif "pp" in args.valid_analogies_pattern and "kk" not in args.valid_analogies_pattern:
            if "pruning" in prediction:
                keep = numpy.mean(1-prune)
        elif "pp" in args.valid_analogies_pattern and "kk" in args.valid_analogies_pattern:
            if "pruning" in prediction:
                keep = numpy.mean(numpy.concatenate((keep, 1 - prune)))
            else:
                keep = numpy.mean(keep)
        if keep >= args.voting_threshold:
            decision = 1
    elif args.voting == "majority":
        if keep >= args.voting_threshold:
            decision = 1
    return decision


def main():

    parser = argparse.ArgumentParser(prog="analogy_evaluation", description="Evaluation of analogy-based models")
    parser.add_argument("--voting", help="Voting method for expansion (evaluation of analogy model)", required=False, default="majority")
    parser.add_argument("--voting-threshold", dest="voting_threshold", help="Float value representing the threshold for deciding whether prediction analogies are 1 or 0",
                            required=False, default=0.5, type=float)
    parser.add_argument("--folds", dest="folds_path", help="File containing folds", required=True)
    parser.add_argument("--predictions", help="File containing folds", required=True)
    parser.add_argument("--wikidata", dest="wikidata_hashmap", help="Folder containing the LMDB Wikidata hashmap",
                        required=True)
    parser.add_argument("--embeddings", dest="embeddings_hashmap", help="Folder containing the LMDB embeddings hashmap",
                        required=True)
    parser.add_argument("--expansion-properties", dest="expansion_properties", nargs="+")
    parser.add_argument("--valid-analogies-pattern", dest="valid_analogies_pattern", nargs='+')
    parser.add_argument("--invalid-analogies-pattern", dest="invalid_analogies_pattern", nargs='+')
    parser.add_argument("--decisions", dest="decisions", help="CSV decision file", required=True)
    parser.add_argument("--output", dest="output", help="Output pickle hashmap", required=True)
    parser.add_argument("--allow-bridge-nodes", action="store_true")
    args = parser.parse_args()

    # Logging parameters
    logger = logging.getLogger()
    tqdm_logging_handler = TqdmLoggingHandler()
    tqdm_logging_handler.setFormatter(logging.Formatter(fmt="[%(asctime)s][%(levelname)s] %(message)s"))
    logger.addHandler(tqdm_logging_handler)
    logger.setLevel(logging.INFO)

    # Load folds
    folds = pickle.load(open(args.folds_path, "rb"))

    # Load Wikidata hashmap (QID -> properties)
    dump = lmdb.open(args.wikidata_hashmap, readonly=True, readahead=False)
    wikidata_hashmap = dump.begin()

    # Load embeddings (QID URL -> embedding)
    embeddings = lmdb.open(args.embeddings_hashmap, readonly=True, readahead=False)
    embedding_hashmap = embeddings.begin()
    available_embedding_qids = (
        embedding_qids_from_transaction(embedding_hashmap) if args.allow_bridge_nodes else None
    )

    # Loading predictions hashmap
    predictions = pickle.load(open(args.predictions, "rb"))

    # Loading decision file
    decisions = pandas.read_csv(args.decisions)
    output_decisions = dict()
    initial_properties = expansion_properties.initial_properties(args.expansion_properties)
    next_properties = expansion_properties.next_properties(args.expansion_properties)

    logger.info("Expansion --> evaluation of decisions")

    for fold in tqdm.tqdm(folds, desc="fold"):

        test_set = folds[fold]["test"]
        
        # Decisions evalution
        output_decisions[fold] = dict()
        # Start the expansion
        for qid in tqdm.tqdm(test_set):
            qid_emb = utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap)
            output_decisions[fold][qid] = dict()

            if qid_emb is None:
                continue

            seed_predictions = predictions.get(fold, {}).get(qid, {})
            predicted_targets = {
                cl: str(vote_prediction(prediction, args))
                for cl, prediction in seed_predictions.items()
            }
            reached_q_w_decisions = set(decisions[(decisions["from"] == qid)]["QID"])
            decision_paths = reachable_decision_paths_for_mode(
                get_record=lambda node: utils.get_hashmap_content(node, wikidata_hashmap),
                seed_qid=qid,
                decision_qids=reached_q_w_decisions,
                initial_properties=initial_properties,
                next_properties=next_properties,
                allow_bridge_nodes=args.allow_bridge_nodes,
                available_embedding_qids=available_embedding_qids,
                decision_targets=predicted_targets,
                keep_gated=True,
            )
            for decision_path in decision_paths:
                if decision_path.qid not in predicted_targets:
                    continue
                output_decisions[fold][qid][decision_path.qid] = {
                    "depth": decision_path.depth,
                    "decision": int(predicted_targets[decision_path.qid]),
                }

    pickle.dump(output_decisions, open(args.output, "wb"))


if __name__ == '__main__':
    main()
