"""
Copyright (C) 2023 Orange
Authors: Lucas Jarnac, Miguel Couceiro, and Pierre Monnin

This software is distributed under the terms and conditions of the 'MIT'
license which can be found in the file 'LICENSE.txt' in this package distribution 
or at 'https://opensource.org/license/mit/'.
"""
import random
import pickle
import argparse
import time
import sys
from pathlib import Path

import lmdb
import numpy
import pandas
import tensorflow as tf

sys.path.append(str(Path(__file__).resolve().parents[1] / "utils"))
sys.path.append(str(Path(__file__).resolve().parents[2]))
from bridge_traversal import embedding_qids_from_transaction, reachable_decision_paths_for_mode
from reproducibility import configure_random_seed
import expansion_properties
import AnalogyStatistics as AnalogyStatistics
import utils
from TqdmLoggingHandler import *


def sequenced_analogy_model(seq_len, nb_filters1, nb_filters2, dropout):
    model = tf.keras.Sequential([
            tf.keras.layers.Conv2D(filters=nb_filters1, 
                                   kernel_size=(1, seq_len), 
                                   strides=(1, seq_len), 
                                   activation="relu", 
                                   input_shape=(200, seq_len*2, 1), 
                                   kernel_initializer="he_normal"),
            tf.keras.layers.Dropout(dropout),
            tf.keras.layers.Conv2D(filters=nb_filters2, 
                                   kernel_size=(2, 2), 
                                   strides=(2, 2), 
                                   activation="relu", 
                                   input_shape=(200, 2, nb_filters1), 
                                   kernel_initializer="he_normal"),
            tf.keras.layers.Dropout(dropout),
            tf.keras.layers.Flatten(),
            tf.keras.layers.Dense(units=1, activation='sigmoid')
        ])
    return model


def transpose(embedding):
    return numpy.transpose(numpy.array([embedding]), (1, 0))


def get_analogies(train_features, 
                  train_labels, 
                  keeping_train, 
                  pruning_train, 
                  nb_training_analogies_per_decision,
                  sequenced_decisions, 
                  sequence_length, 
                  padding,
                  properties,
                  valid_analogies_pattern,
                  invalid_analogies_pattern,
                  distances_hashmap,
                  knn):

    keeping_decisions = keeping_train.values.tolist()
    pruning_decisions = pruning_train.values.tolist()
    keeping_to_shuffle = keeping_decisions.copy()
    pruning_to_shuffle = pruning_decisions.copy()

    # Keeping decisions :: Keeping decisions
    for i in range(len(keeping_decisions)):
        starting_qid_A = keeping_decisions[i][0]
        reached_class_B = keeping_decisions[i][1]
        if starting_qid_A in sequenced_decisions and reached_class_B in sequenced_decisions[starting_qid_A]:
            vec_kept_AB = utils.pad_sequence(sequenced_decisions[starting_qid_A][reached_class_B], sequence_length, padding)
            if "kk" in valid_analogies_pattern + invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                keeping = []
                if "train" not in knn:
                    random.shuffle(keeping_to_shuffle)
                    keeping = keeping_to_shuffle
                else:
                    # K nearest neighbors
                    keeping = utils.get_hashmap_distances(keeping_decisions[i][0],
                                                          distances_hashmap,
                                                          keeping_train)

                while nb_used_analogies < nb_training_analogies_per_decision:
                    if training_analogy_index == len(keeping):
                        break
                    starting_qid_C = keeping[training_analogy_index][0]
                    reached_class_D = keeping[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C] and \
                        keeping[training_analogy_index][0] != keeping_decisions[i][0]:
                        vec_kept_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        utils.add_sequence_analogy("train", vec_kept_CD, vec_kept_AB, properties, valid_analogies_pattern, "kk", train_features, train_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
            
            if "pk" in valid_analogies_pattern + invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                pruning = []
                if "train" not in knn:
                    random.shuffle(pruning_to_shuffle)
                    pruning = pruning_to_shuffle
                else:
                    # K nearest neighbors
                    pruning = utils.get_hashmap_distances(keeping_decisions[i][0],
                                                          distances_hashmap,
                                                          pruning_train)

                # Invalid analogies
                while nb_used_analogies < nb_training_analogies_per_decision:
                    if training_analogy_index == len(pruning):
                        break
                    starting_qid_C = pruning[training_analogy_index][0]
                    reached_class_D = pruning[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C] and \
                        pruning[training_analogy_index][0] != keeping_decisions[i][0]:
                        vec_pruned_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        # Pruning decisions :: Keeping decisions
                        utils.add_sequence_analogy("train", vec_pruned_CD, vec_kept_AB, properties, valid_analogies_pattern, "pk", train_features, train_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
            
    # Pruning decisions :: Pruning decisions
    for i in range(len(pruning_decisions)):
        starting_qid_A = pruning_decisions[i][0]
        reached_class_B = pruning_decisions[i][1]
        if starting_qid_A in sequenced_decisions and reached_class_B in sequenced_decisions[starting_qid_A]:
            vec_pruned_AB = utils.pad_sequence(sequenced_decisions[starting_qid_A][reached_class_B], sequence_length, padding)
            if "pp" in valid_analogies_pattern + invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                pruning = []
                if "train" not in knn:
                    random.shuffle(pruning_to_shuffle)
                    pruning = pruning_to_shuffle
                else:
                    # K nearest neighbors
                    pruning = utils.get_hashmap_distances(pruning_decisions[i][0],
                                                          distances_hashmap,
                                                          pruning_train)

                while nb_used_analogies < nb_training_analogies_per_decision: 
                    if training_analogy_index == len(pruning):
                        break
                    starting_qid_C = pruning[training_analogy_index][0]
                    reached_class_D = pruning[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C] and \
                        pruning[training_analogy_index][0] != pruning_decisions[i][0]:
                        vec_pruned_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        utils.add_sequence_analogy("train", vec_pruned_CD, vec_pruned_AB, properties, valid_analogies_pattern, "pp", train_features, train_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
            
            if "kp" in valid_analogies_pattern + invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                keeping = []
                if "train" not in knn:
                    random.shuffle(keeping_to_shuffle)
                    keeping = keeping_to_shuffle
                else:
                    # K nearest neighbors
                    keeping = utils.get_hashmap_distances(pruning_decisions[i][0],
                                                          distances_hashmap,
                                                          keeping_train)

                # Invalid analogies
                while nb_used_analogies < nb_training_analogies_per_decision:
                    if training_analogy_index == len(keeping):
                        break
                    starting_qid_C = keeping[training_analogy_index][0]
                    reached_class_D = keeping[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C] and \
                        keeping[training_analogy_index][0] != pruning_decisions[i][0]:
                        vec_kept_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        # Keeping decisions :: Pruning decisions
                        utils.add_sequence_analogy("train", vec_kept_CD, vec_pruned_AB, properties, valid_analogies_pattern, "kp", train_features, train_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
            
                
def get_testing_analogies(test_features, 
                          test_labels, 
                          keeping_decisions_test, 
                          pruning_decisions_test,
                          keeping_train,
                          pruning_train, 
                          nb_test_analogies, 
                          sequenced_decisions,
                          sequence_length,
                          padding,
                          properties,
                          valid_analogies_pattern,
                          invalid_analogies_pattern,
                          distances_hashmap,
                          knn):
    
    keeping_decisions = keeping_train.values.tolist()
    pruning_decisions = pruning_train.values.tolist()
    keeping_to_shuffle = keeping_decisions.copy()
    pruning_to_shuffle = pruning_decisions.copy()
    
    # Keeping decisions :: Keeping decisions
    for i in range(len(keeping_decisions_test)):
        starting_qid_A = keeping_decisions_test[i][0]
        reached_class_B = keeping_decisions_test[i][1]
        if starting_qid_A in sequenced_decisions and reached_class_B in sequenced_decisions[starting_qid_A]:
            vec_kept_AB = utils.pad_sequence(sequenced_decisions[starting_qid_A][reached_class_B], sequence_length, padding)
            
            if "kk" in valid_analogies_pattern + invalid_analogies_pattern and "kp" in invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                keeping = []
                if "test" not in knn:
                    random.shuffle(keeping_to_shuffle)
                    keeping = keeping_to_shuffle
                else:
                    # K nearest neighbors
                    keeping = utils.get_hashmap_distances(keeping_decisions_test[i][0],
                                                          distances_hashmap,
                                                          keeping_train)

                while nb_used_analogies < nb_test_analogies:
                    if training_analogy_index == len(keeping):
                        break
                    starting_qid_C = keeping[training_analogy_index][0]
                    reached_class_D = keeping[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C]:
                        vec_kept_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        utils.add_sequence_analogy("test", vec_kept_CD, vec_kept_AB, properties, valid_analogies_pattern, "kk", test_features, test_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
            
            if "pk" in valid_analogies_pattern + invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                pruning = []
                if "test" not in knn:
                    random.shuffle(pruning_to_shuffle)
                    pruning = pruning_to_shuffle
                else:
                    # K nearest neighbors
                    pruning = utils.get_hashmap_distances(keeping_decisions_test[i][0],
                                                          distances_hashmap,
                                                          pruning_train)

                # Invalid analogies
                while nb_used_analogies < nb_test_analogies:
                    if training_analogy_index == len(pruning):
                        break
                    starting_qid_C = pruning[training_analogy_index][0]
                    reached_class_D = pruning[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C]:
                        vec_pruned_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        # Pruning decisions :: Keeping decisions
                        utils.add_sequence_analogy("test", vec_pruned_CD, vec_kept_AB, properties, valid_analogies_pattern, "pk", test_features, test_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
    
    # Pruning decisions :: Pruning decisions
    for i in range(len(pruning_decisions_test)):
        starting_qid_A = pruning_decisions_test[i][0]
        reached_class_B = pruning_decisions_test[i][1]
        if starting_qid_A in sequenced_decisions and reached_class_B in sequenced_decisions[starting_qid_A]:
            vec_pruned_AB = utils.pad_sequence(sequenced_decisions[starting_qid_A][reached_class_B], sequence_length, padding)
            
            if "pp" in valid_analogies_pattern + invalid_analogies_pattern and "pk" in invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                pruning = []
                if "test" not in knn:
                    random.shuffle(pruning_to_shuffle)
                    pruning = pruning_to_shuffle
                else:
                    # K nearest neighbors
                    pruning = utils.get_hashmap_distances(pruning_decisions_test[i][0],
                                                          distances_hashmap,
                                                          pruning_train)

                while nb_used_analogies < nb_test_analogies:
                    if training_analogy_index == len(pruning):
                        break
                    starting_qid_C = pruning[training_analogy_index][0]
                    reached_class_D = pruning[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C]:
                        vec_pruned_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        utils.add_sequence_analogy("test", vec_pruned_CD, vec_pruned_AB, properties, valid_analogies_pattern, "pp", test_features, test_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1
            
            if "kp" in valid_analogies_pattern + invalid_analogies_pattern:
                nb_used_analogies = 0
                training_analogy_index = 0

                keeping = []
                if "test" not in knn:
                    random.shuffle(keeping_to_shuffle)
                    keeping = keeping_to_shuffle
                else:
                    # K nearest neighbors
                    keeping = utils.get_hashmap_distances(pruning_decisions_test[i][0],
                                                          distances_hashmap,
                                                          keeping_train)

                # Invalid analogies
                while nb_used_analogies < nb_test_analogies:
                    if training_analogy_index == len(keeping):
                        break
                    starting_qid_C = keeping[training_analogy_index][0]
                    reached_class_D = keeping[training_analogy_index][1]
                    if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C]:
                        vec_kept_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                        # Keeping decisions :: Pruning decisions
                        utils.add_sequence_analogy("test", vec_kept_CD, vec_pruned_AB, properties, valid_analogies_pattern, "kp", test_features, test_labels)
                        nb_used_analogies += 1
                    training_analogy_index += 1


def get_evaluation_analogies(decision_to_test,
                             keeping_train,
                             pruning_train, 
                             nb_keeping_in_test,
                             nb_pruning_in_test,
                             sequenced_decisions,
                             sequence_length,
                             padding,
                             properties,
                             valid_analogies_pattern,
                             invalid_analogies_pattern,
                             analogies_stats,
                             distances_hashmap,
                             knn):
    
    keeping_test = []
    pruning_test = []

    keeping_decisions = keeping_train.values.tolist()
    pruning_decisions = pruning_train.values.tolist()
    keeping_to_shuffle = keeping_decisions.copy()
    pruning_to_shuffle = pruning_decisions.copy()

    # Decision to test :: Keeping decisions
    starting_qid_A = decision_to_test[0]
    reached_class_B = decision_to_test[1]
    if starting_qid_A in sequenced_decisions and reached_class_B in sequenced_decisions[starting_qid_A]:
        vec_kept_AB = utils.pad_sequence(sequenced_decisions[starting_qid_A][reached_class_B], sequence_length, padding)
        
        if "kk" in valid_analogies_pattern + invalid_analogies_pattern and "kp" in invalid_analogies_pattern:
            nb_used_analogies = 0
            training_analogy_index = 0

            keeping = []
            if "test" not in knn:
                random.shuffle(keeping_to_shuffle)
                keeping = keeping_to_shuffle
            else:
                # K nearest neighbors
                keeping = utils.get_hashmap_distances(decision_to_test[0],
                                                      distances_hashmap,
                                                      keeping_train)

            while nb_used_analogies < nb_keeping_in_test:
                if training_analogy_index == len(keeping):
                    break
                starting_qid_C = keeping[training_analogy_index][0]
                reached_class_D = keeping[training_analogy_index][1]
                if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C]:
                    vec_kept_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                    
                    utils.add_evaluation_sequence_analogy(keeping_test,
                                            keeping[training_analogy_index],
                                            decision_to_test,
                                            vec_kept_CD,
                                            vec_kept_AB,
                                            analogies_stats,
                                            "keep",
                                            properties)
                    nb_used_analogies += 1
                training_analogy_index += 1
        
    # Decision to test :: Pruning decisions
    starting_qid_A = decision_to_test[0]
    reached_class_B = decision_to_test[1]
    if starting_qid_A in sequenced_decisions and reached_class_B in sequenced_decisions[starting_qid_A]:
        vec_pruned_AB = utils.pad_sequence(sequenced_decisions[starting_qid_A][reached_class_B], sequence_length, padding)
        if "pp" in valid_analogies_pattern + invalid_analogies_pattern and "pk" in invalid_analogies_pattern:
            nb_used_analogies = 0
            training_analogy_index = 0

            pruning = []
            if "test" not in knn:
                random.shuffle(pruning_to_shuffle)
                pruning = pruning_to_shuffle
            else:
                # K nearest neighbors
                pruning = utils.get_hashmap_distances(decision_to_test[0],
                                                      distances_hashmap,
                                                      pruning_train)

            while nb_used_analogies < nb_pruning_in_test:
                if training_analogy_index == len(pruning):
                    break
                starting_qid_C = pruning[training_analogy_index][0]
                reached_class_D = pruning[training_analogy_index][1]
                if starting_qid_C in sequenced_decisions and reached_class_D in sequenced_decisions[starting_qid_C]:
                    vec_pruned_CD = utils.pad_sequence(sequenced_decisions[starting_qid_C][reached_class_D], sequence_length, padding)
                    utils.add_evaluation_sequence_analogy(pruning_test,
                                           pruning[training_analogy_index],
                                           decision_to_test,
                                           vec_pruned_CD,
                                           vec_pruned_AB,
                                           analogies_stats,
                                           "prune",
                                           properties)
                    nb_used_analogies += 1
                training_analogy_index += 1
        
    return keeping_test, pruning_test


def predict_path_analogy_for_seeds(
    seed_qids,
    classifier,
    decisions,
    keeping_decisions_train,
    pruning_decisions_train,
    wikidata_hashmap,
    embedding_hashmap,
    available_embedding_qids,
    initial_properties,
    next_properties,
    allow_bridge_nodes,
    sequenced_decisions,
    sequence_length,
    padding,
    analogical_properties,
    valid_analogies_pattern,
    invalid_analogies_pattern,
    nb_keeping_in_test,
    nb_pruning_in_test,
    distances_hashmap,
    knn,
    dropout,
    description,
):
    predictions = {}
    analogies_stats = AnalogyStatistics.AnalogyStats()
    keeping_train_columns = keeping_decisions_train[["from", "QID", "depth", "starting label", "label"]]
    pruning_train_columns = pruning_decisions_train[["from", "QID", "depth", "starting label", "label"]]

    for qid in tqdm.tqdm(seed_qids, desc=description):
        qid_emb = utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap)
        if qid_emb is None:
            continue

        predictions[qid] = {}
        reached_q_w_decisions = set(decisions[(decisions["from"] == qid)]["QID"])
        decision_paths = reachable_decision_paths_for_mode(
            get_record=lambda node: utils.get_hashmap_content(node, wikidata_hashmap),
            seed_qid=qid,
            decision_qids=reached_q_w_decisions,
            initial_properties=initial_properties,
            next_properties=next_properties,
            allow_bridge_nodes=allow_bridge_nodes,
            available_embedding_qids=available_embedding_qids,
        )

        for decision_path in decision_paths:
            cl = decision_path.qid
            if qid not in sequenced_decisions or cl not in sequenced_decisions[qid]:
                continue

            predictions[qid][cl] = {}
            cl_decision = decisions[(decisions["from"] == qid) & (decisions["QID"] == cl)]
            keeping_analogies, pruning_analogies = get_evaluation_analogies(
                [
                    qid,
                    cl,
                    decision_path.depth,
                    cl_decision["starting label"].iloc[0],
                    cl_decision["label"].iloc[0],
                ],
                keeping_train_columns,
                pruning_train_columns,
                nb_keeping_in_test,
                nb_pruning_in_test,
                sequenced_decisions,
                sequence_length,
                padding,
                analogical_properties,
                valid_analogies_pattern,
                invalid_analogies_pattern,
                analogies_stats,
                distances_hashmap,
                knn,
            )

            if len(keeping_analogies) > 0:
                keeping_analogies = numpy.transpose(keeping_analogies, (0, 2, 1, 3))
                if dropout > 0:
                    probabilities = numpy.stack(
                        [classifier(keeping_analogies, training=True) for _ in range(10)]
                    )
                    keeping_predictions = probabilities.mean(axis=0)
                else:
                    keeping_predictions = classifier.predict(keeping_analogies, verbose=0)
                predictions[qid][cl]["keeping"] = keeping_predictions

            if len(pruning_analogies) > 0:
                pruning_analogies = numpy.transpose(pruning_analogies, (0, 2, 1, 3))
                if dropout > 0:
                    probabilities = numpy.stack(
                        [classifier(pruning_analogies, training=True) for _ in range(10)]
                    )
                    pruning_predictions = probabilities.mean(axis=0)
                else:
                    pruning_predictions = classifier.predict(pruning_analogies, verbose=0)
                predictions[qid][cl]["pruning"] = pruning_predictions

    return predictions


def main():

    parser = argparse.ArgumentParser(prog="analogy_pruning", description="Analogy-based classifier")
    parser.add_argument("--folds", dest="folds_path", help="File containing folds", required=True)
    parser.add_argument("--decisions", dest="decisions", help="CSV decision file", required=True)
    parser.add_argument("--wikidata", dest="wikidata_hashmap", help="Folder containing the LMDB Wikidata hashmap",
                        required=True)
    parser.add_argument("--distances-hashmap", dest="distances_hashmap", help="Hashmap containing Euclidean distances between all pairs of Wikidata QIDS", required=True)
    parser.add_argument("--knn", nargs="+")
    parser.add_argument("--embeddings", dest="embeddings_hashmap", help="Folder containing the LMDB embeddings hashmap",
                        required=True)
    parser.add_argument("--nb-training-analogies-per-decision", dest="nb_training_analogies_per_decision", 
                        required=False, default=20, type=int, help="Number of training analogies per decision")
    parser.add_argument("--nb-test-analogies", dest="nb_test_analogies", type=int, help="Number of test analogies")
    parser.add_argument("--nb-keeping-in-test", dest="nb_keeping_in_test", type=int, help="Number of keeping test analogies", default=20)
    parser.add_argument("--nb-pruning-in-test", dest="nb_pruning_in_test", type=int, help="Number of pruning test analogies", default=20)
    parser.add_argument("--sequenced-decisions", dest="sequenced_decisions", required=True, 
                        help="Pickle file containing the sequenced decisions from generate_sequenced_decisions script")
    parser.add_argument("--sequence-length", dest="sequence_length", help="Length of sequences of the decisions", default=4, type=int)
    parser.add_argument("--padding", help="Zero padding mode", default="after", choices=["before", "between", "after"])
    parser.add_argument("--analogical-properties", dest="analogical_properties", nargs='+')
    parser.add_argument("--expansion-properties", dest="expansion_properties", nargs="+")
    parser.add_argument("--valid-analogies-pattern", dest="valid_analogies_pattern", nargs='+')
    parser.add_argument("--invalid-analogies-pattern", dest="invalid_analogies_pattern", nargs='+')
    parser.add_argument("--nb-filters1", dest="nb_filters1", type=int)
    parser.add_argument("--nb-filters2", dest="nb_filters2", type=int)
    parser.add_argument("--learning-rate", dest="learning_rate", help="Learning rate", required=True, type=float)
    parser.add_argument("--predictions-output", dest="predictions_output", help="Output predictions pickle hashmap", required=True)
    parser.add_argument(
        "--validation-predictions-output",
        dest="validation_predictions_output",
        help="Optional output pickle containing decision scores for each outer fold's validation seeds",
    )
    parser.add_argument("--stats-file", dest="stats_file", help="Output file containing statitics on analogies", required=True)
    parser.add_argument("--epochs", help="Number of epochs to train models", required=False, default=1, type=int)
    parser.add_argument("--dropout", required=True, type=float)
    parser.add_argument("--allow-bridge-nodes", action="store_true")
    parser.add_argument("--random-seed", type=int, default=42)
    args = parser.parse_args()

    configure_random_seed(args.random_seed)

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

    # Load distances hashmap
    distances = lmdb.open(args.distances_hashmap, readonly=True, readahead=False)
    distances_hashmap = distances.begin()

    # Loading decision file
    decisions = pandas.read_csv(args.decisions)

    # Load sequenced decisions
    sequenced_decisions = pickle.load(open(args.sequenced_decisions, "rb"))
    initial_properties = expansion_properties.initial_properties(args.expansion_properties)
    next_properties = expansion_properties.next_properties(args.expansion_properties)

    # Predictions for voting thresholds
    predictions = dict()
    validation_predictions = dict()

    for fold in tqdm.tqdm(folds, desc="fold"):

        predictions[fold] = dict()
        validation_predictions[fold] = dict()

        val_set = set(folds[(fold+1)%5]["test"])
        val_features = []
        val_labels = []

        # Train model
        train_set = set(folds[fold]["train"]) - val_set
        train_features = []
        train_labels = []

        logger.info("Building training data")
        keeping_decisions_train = decisions[(decisions["from"].isin(train_set)) & (decisions["target"] == 1)]
        pruning_decisions_train = decisions[(decisions["from"].isin(train_set)) & (decisions["target"] == 0)]

        # Generate analogies from keeping and pruning decisions
        get_analogies(train_features,
                      train_labels, 
                      keeping_decisions_train[["from", "QID", "depth", "starting label", "label"]], 
                      pruning_decisions_train[["from", "QID", "depth", "starting label", "label"]], 
                      args.nb_training_analogies_per_decision,
                      sequenced_decisions,
                      args.sequence_length,
                      args.padding,
                      args.analogical_properties,
                      args.valid_analogies_pattern,
                      args.invalid_analogies_pattern,
                      distances_hashmap,
                      args.knn)
        
        logger.info("Building validation analogies")
        keeping_decisions_val = decisions[(decisions["from"].isin(val_set)) & (decisions["target"] == 1)]
        pruning_decisions_val = decisions[(decisions["from"].isin(val_set)) & (decisions["target"] == 0)]

        # Generate analogies from keeping and pruning decisions
        get_testing_analogies(val_features,
                              val_labels,
                              keeping_decisions_val[["from", "QID", "depth", "starting label", "label"]].values.tolist(), 
                              pruning_decisions_val[["from", "QID", "depth", "starting label", "label"]].values.tolist(),
                              keeping_decisions_train[["from", "QID", "depth", "starting label", "label"]],
                              pruning_decisions_train[["from", "QID", "depth", "starting label", "label"]],
                              args.nb_test_analogies,
                              sequenced_decisions,
                              args.sequence_length,
                              args.padding,
                              args.analogical_properties,
                              args.valid_analogies_pattern,
                              args.invalid_analogies_pattern,
                              distances_hashmap,
                              args.knn)
        
        val_features = numpy.transpose(val_features, (0, 2, 1, 3))
        val_labels = numpy.array(val_labels)
        
        # Tranform to the input format of analogy-based convolutional model
        train_features = numpy.transpose(train_features, (0, 2, 1, 3))
        train_labels = numpy.array(train_labels)

        # Training
        sequenced_analogy_classifier = sequenced_analogy_model(args.sequence_length, args.nb_filters1, args.nb_filters2, args.dropout)
        sequenced_analogy_classifier.summary()
        sequenced_analogy_classifier.compile(loss=tf.keras.losses.BinaryCrossentropy(),
                                optimizer=tf.keras.optimizers.Adam(learning_rate=args.learning_rate),
                                metrics=[tf.keras.metrics.Precision(), 
                                         tf.keras.metrics.Recall(), 
                                         tf.keras.metrics.BinaryAccuracy()])
        
        weight_for_0 = 1 / numpy.count_nonzero(train_labels == 0) * (len(train_labels) / 2.0)
        weight_for_1 = 1 / numpy.count_nonzero(train_labels == 1) * (len(train_labels) / 2.0)

        early_stopping_cb = tf.keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True, monitor="val_loss")

        history = sequenced_analogy_classifier.fit(train_features,
                               train_labels,
                               validation_data=(val_features, val_labels),
                               callbacks=[early_stopping_cb],
                               class_weight={0: weight_for_0, 1: weight_for_1},
                               shuffle=True,
                               epochs=args.epochs)
        
        # Produce outer-test decision scores without reading outer-test labels.
        test_set = folds[fold]["test"]

        if args.validation_predictions_output:
            logger.info("Predicting validation decisions")
            validation_predictions[fold] = predict_path_analogy_for_seeds(
                val_set,
                sequenced_analogy_classifier,
                decisions,
                keeping_decisions_train,
                pruning_decisions_train,
                wikidata_hashmap,
                embedding_hashmap,
                available_embedding_qids,
                initial_properties,
                next_properties,
                args.allow_bridge_nodes,
                sequenced_decisions,
                args.sequence_length,
                args.padding,
                args.analogical_properties,
                args.valid_analogies_pattern,
                args.invalid_analogies_pattern,
                args.nb_keeping_in_test,
                args.nb_pruning_in_test,
                distances_hashmap,
                args.knn,
                args.dropout,
                "validation",
            )

        logger.info("Predicting outer-test decisions")
        predictions[fold] = predict_path_analogy_for_seeds(
            test_set,
            sequenced_analogy_classifier,
            decisions,
            keeping_decisions_train,
            pruning_decisions_train,
            wikidata_hashmap,
            embedding_hashmap,
            available_embedding_qids,
            initial_properties,
            next_properties,
            args.allow_bridge_nodes,
            sequenced_decisions,
            args.sequence_length,
            args.padding,
            args.analogical_properties,
            args.valid_analogies_pattern,
            args.invalid_analogies_pattern,
            args.nb_keeping_in_test,
            args.nb_pruning_in_test,
            distances_hashmap,
            args.knn,
            args.dropout,
            "test",
        )

    pickle.dump(predictions, open(args.predictions_output, "wb"))
    if args.validation_predictions_output:
        pickle.dump(validation_predictions, open(args.validation_predictions_output, "wb"))
    logger.info(f"Execution time = {utils.convert(time.time() - start)}")

if __name__ == '__main__':
    main()
