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
import sys
import time
from pathlib import Path

import lmdb
import numpy
import pandas
import tqdm

import expansion_properties
import utils
from TqdmLoggingHandler import *

sys.path.append(str(Path(__file__).resolve().parents[2]))
from bridge_traversal import embedding_qids_from_transaction, reachable_decision_paths_for_mode


def path_to_sequence(path, embedding_hashmap):
    sequence = []
    for qid in path:
        embedding = utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap)
        if embedding is None:
            return None
        sequence.append(numpy.transpose(numpy.array([embedding])))
    return sequence


def main():

    parser = argparse.ArgumentParser(prog="generate_sequenced_decisions", 
                                     description="Generate sequenced decisions with path in the graph for sequenced analogies training")
    parser.add_argument("--decisions", dest="decisions", help="CSV decision file", required=True)
    parser.add_argument("--wikidata", dest="wikidata_hashmap", help="Folder containing the LMDB Wikidata hashmap",
                        required=True)
    parser.add_argument("--embeddings", dest="embeddings_hashmap", help="Folder containing the LMDB embeddings hashmap",
                        required=True)
    parser.add_argument("--output", dest="output", help="Output pickle hashmap", required=True)
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
    initial_properties = expansion_properties.initial_properties(args.expansion_properties)
    next_properties = expansion_properties.next_properties(args.expansion_properties)

    starting_qids = set(decisions["from"])

    sequences = {}

    for qid in tqdm.tqdm(starting_qids):

        qid_emb = utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap)

        if qid_emb is not None:

            reached_q_w_decisions = set(decisions[(decisions["from"] == qid)]["QID"])
            qid_sequences = {}
            decision_paths = reachable_decision_paths_for_mode(
                get_record=lambda node: utils.get_hashmap_content(node, wikidata_hashmap),
                seed_qid=qid,
                decision_qids=reached_q_w_decisions,
                initial_properties=initial_properties,
                next_properties=next_properties,
                allow_bridge_nodes=args.allow_bridge_nodes,
                available_embedding_qids=available_embedding_qids,
            )
            for decision_path in decision_paths:
                sequence = path_to_sequence(decision_path.path, embedding_hashmap)
                if sequence is not None:
                    qid_sequences[decision_path.qid] = sequence

        sequences[qid] = qid_sequences

    pickle.dump(sequences, open(args.output, "wb"))
    logger.info(f"Execution time = {utils.convert(time.time() - start)}")


if __name__ == '__main__':
    main()
