"""
Copyright (C) 2023 Orange
Authors: Lucas Jarnac, Miguel Couceiro, and Pierre Monnin

This software is distributed under the terms and conditions of the 'MIT'
license which can be found in the file 'LICENSE.txt' in this package distribution 
or at 'https://opensource.org/license/mit/'.
"""
import argparse
import logging
import pickle
import time
import sys
from pathlib import Path

import numpy
import pandas
import lmdb
import tqdm
import tensorflow as tf

sys.path.append(str(Path(__file__).resolve().parents[1] / "utils"))
sys.path.append(str(Path(__file__).resolve().parents[2]))
from bridge_traversal import embedding_qids_from_transaction, reachable_decision_paths_for_mode
import expansion_properties
import utils
from TqdmLoggingHandler import *


def pad_lstm_sequence(seq, max_length, pad_mode="after"):
    """ zero padding after QID and classes by default """
    sequence = seq
    pad = max_length - len(seq)
    if pad >= 0:
        if pad_mode == "after":
            sequence = sequence + pad * [numpy.zeros(200)]
        elif pad_mode == "before":
            sequence = pad * [numpy.zeros(200)] + sequence
        else:
            sequence = [sequence[0]] + pad * [numpy.zeros(200)] + sequence[1:]
    else:
        sequence = [sequence[0]] + sequence[-pad+1:]
        
    return sequence


def prediction_score(prediction):
    return float(numpy.asarray(prediction).mean())


def main():

    parser = argparse.ArgumentParser(prog="lstm_pruning", description="Long-Short-Term-Memory Classifier")
    parser.add_argument("--folds", dest="folds_path", help="File containing folds", required=True)
    parser.add_argument("--decisions", dest="decisions", help="CSV decision file", required=True)
    parser.add_argument("--wikidata", dest="wikidata_hashmap", help="Folder containing the LMDB Wikidata hashmap",
                        required=True)
    parser.add_argument("--predictions", help="File containing predictions", required=True)
    parser.add_argument("--embeddings", dest="embeddings_hashmap", help="Folder containing the LMDB embeddings hashmap",
                        required=True)
    parser.add_argument("--output", dest="output", help="Output pickle hashmap", required=True)
    parser.add_argument("--voting-threshold", dest="voting_threshold", help="Float value representing the threshold for deciding whether prediction analogies are 1 or 0",
                            required=False, default=0.5, type=float)
    parser.add_argument("--expansion-properties", dest="expansion_properties", nargs="+")
    parser.add_argument("--allow-bridge-nodes", action="store_true")
    args = parser.parse_args()

    start = time.time()

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

    # Loading decision file
    decisions = pandas.read_csv(args.decisions)
    output_decisions = dict()
    initial_properties = expansion_properties.initial_properties(args.expansion_properties)
    next_properties = expansion_properties.next_properties(args.expansion_properties)

    # Loading predictions hashmap
    predictions = pickle.load(open(args.predictions, "rb"))

    for fold in tqdm.tqdm(folds, desc="fold"):
        # Test model
        logger.info("Testing LSTM")
        test_set = folds[fold]["test"]
        output_decisions[fold] = dict()

        for qid in tqdm.tqdm(test_set, desc="test"):
            output_decisions[fold][qid] = dict()
            qid_emb = utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap)

            if qid_emb is None:
                continue

            seed_predictions = predictions.get(fold, {}).get(qid, {})
            predicted_targets = {
                cl: "1" if prediction_score(prediction) >= args.voting_threshold else "0"
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
    logger.info(f"Execution time = {utils.convert(time.time() - start)}")


if __name__ == '__main__':
    main()
