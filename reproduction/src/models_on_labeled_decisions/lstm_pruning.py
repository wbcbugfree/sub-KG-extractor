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
from reproducibility import configure_random_seed
import expansion_properties
import utils
from TqdmLoggingHandler import *


def lstm_model(units=50, input_shape=8):
    """ Build LSTM model based on paths in the graph (sequences of the model). """
    model = tf.keras.Sequential([
        tf.keras.layers.LSTM(units=units, input_shape=(input_shape, 200), return_sequences=False,
                             kernel_regularizer=tf.keras.regularizers.L1L2(l1=1e-5, l2=1e-4),
                             bias_regularizer=tf.keras.regularizers.L2(1e-4),
                             activity_regularizer=tf.keras.regularizers.L2(1e-5)),
        tf.keras.layers.Dense(units=1, activation='sigmoid')
    ])
    return model


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


def path_to_lstm_sequence(path, embedding_hashmap):
    sequence = []
    for qid in path:
        embedding = utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap)
        if embedding is None:
            return None
        sequence.append(embedding)
    return sequence


def bridge_decision_paths_for_seed(
    qid,
    decisions,
    wikidata_hashmap,
    initial_properties,
    next_properties,
    allow_bridge_nodes,
    available_embedding_qids,
):
    reached_q_w_decisions = set(decisions[(decisions["from"] == qid)]["QID"])
    return reachable_decision_paths_for_mode(
        get_record=lambda node: utils.get_hashmap_content(node, wikidata_hashmap),
        seed_qid=qid,
        decision_qids=reached_q_w_decisions,
        initial_properties=initial_properties,
        next_properties=next_properties,
        allow_bridge_nodes=allow_bridge_nodes,
        available_embedding_qids=available_embedding_qids,
    )


def add_lstm_examples_for_seed(
    qid,
    decisions,
    wikidata_hashmap,
    embedding_hashmap,
    initial_properties,
    next_properties,
    sequence_len,
    padding,
    allow_bridge_nodes,
    available_embedding_qids,
    features,
    labels,
):
    if utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap) is None:
        return

    for decision_path in bridge_decision_paths_for_seed(
        qid,
        decisions,
        wikidata_hashmap,
        initial_properties,
        next_properties,
        allow_bridge_nodes,
        available_embedding_qids,
    ):
        sequence = path_to_lstm_sequence(decision_path.path, embedding_hashmap)
        if sequence is None:
            continue
        target = decisions[(decisions["from"] == qid) & (decisions["QID"] == decision_path.qid)]["target"].iloc[0]
        features.append(pad_lstm_sequence(sequence, sequence_len, padding))
        labels.append(target)


def predict_lstm_for_seeds(
    seed_qids,
    model,
    decisions,
    wikidata_hashmap,
    embedding_hashmap,
    initial_properties,
    next_properties,
    sequence_len,
    padding,
    allow_bridge_nodes,
    available_embedding_qids,
    description,
):
    predictions = {}
    for qid in tqdm.tqdm(seed_qids, desc=description):
        if utils.get_hashmap_content(utils.WIKIDATA_PREFIX + qid + ">", embedding_hashmap) is None:
            continue

        predictions[qid] = {}
        for decision_path in bridge_decision_paths_for_seed(
            qid,
            decisions,
            wikidata_hashmap,
            initial_properties,
            next_properties,
            allow_bridge_nodes,
            available_embedding_qids,
        ):
            sequence = path_to_lstm_sequence(decision_path.path, embedding_hashmap)
            if sequence is None:
                continue
            predictions[qid][decision_path.qid] = model.predict(
                numpy.array([pad_lstm_sequence(sequence, sequence_len, padding)]),
                verbose=0,
            )[0]
    return predictions


def main():

    parser = argparse.ArgumentParser(prog="lstm_pruning", description="Long-Short-Term-Memory Classifier")
    parser.add_argument("--padding", help="Zero padding method for lstm and sequenced analogy",
                        choices=['before', 'between', 'after'], required=False, default="after")
    parser.add_argument("--sequence-len", dest="sequence_len", help="Length of the sequences", default=8, type=int)
    parser.add_argument("--folds", dest="folds_path", help="File containing folds", required=True)
    parser.add_argument("--decisions", dest="decisions", help="CSV decision file", required=True)
    parser.add_argument("--wikidata", dest="wikidata_hashmap", help="Folder containing the LMDB Wikidata hashmap",
                        required=True)
    parser.add_argument("--embeddings", dest="embeddings_hashmap", help="Folder containing the LMDB embeddings hashmap",
                        required=True)
    parser.add_argument("--predictions-output", dest="predictions_output", help="Output predictions pickle hashmap", required=True)
    parser.add_argument(
        "--validation-predictions-output",
        dest="validation_predictions_output",
        help="Optional output pickle containing decision scores for each outer fold's validation seeds",
    )
    parser.add_argument("--nb-units", dest="nb_units", help="Number of units in LSTM model", required=False, type=int)
    parser.add_argument("--learning-rate", dest="learning_rate", help="Learning rate", required=True, type=float)
    parser.add_argument("--epochs", help="Number of epochs to train models", required=False, default=200, type=int)
    parser.add_argument("--expansion-properties", dest="expansion_properties", nargs="+")
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

    # Loading decision file
    decisions = pandas.read_csv(args.decisions)
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
        for qid in tqdm.tqdm(train_set):
            add_lstm_examples_for_seed(
                qid,
                decisions,
                wikidata_hashmap,
                embedding_hashmap,
                initial_properties,
                next_properties,
                args.sequence_len,
                args.padding,
                args.allow_bridge_nodes,
                available_embedding_qids,
                train_features,
                train_labels,
            )

        logger.info("Building validation data")
        for qid in tqdm.tqdm(val_set):
            add_lstm_examples_for_seed(
                qid,
                decisions,
                wikidata_hashmap,
                embedding_hashmap,
                initial_properties,
                next_properties,
                args.sequence_len,
                args.padding,
                args.allow_bridge_nodes,
                available_embedding_qids,
                val_features,
                val_labels,
            )

        train_features = numpy.array(train_features)
        train_labels = numpy.array(train_labels)

        val_features = numpy.array(val_features)
        val_labels = numpy.array(val_labels)

        weight_for_0 = 1 / numpy.count_nonzero(train_labels == 0) * (len(train_labels) / 2.0)
        weight_for_1 = 1 / numpy.count_nonzero(train_labels == 1) * (len(train_labels) / 2.0)

        early_stopping_cb = tf.keras.callbacks.EarlyStopping(patience=20, restore_best_weights=True, monitor="val_loss")

        lstm = lstm_model(units=args.nb_units, input_shape=args.sequence_len)
        lstm.summary()
        lstm.compile(loss=tf.keras.losses.BinaryCrossentropy(),
                                optimizer=tf.keras.optimizers.Adam(learning_rate=args.learning_rate),
                                metrics=[tf.keras.metrics.Precision(),
                                         tf.keras.metrics.Recall(),
                                         tf.keras.metrics.BinaryAccuracy()])
        lstm.fit(train_features,
                 train_labels,
                 validation_data=(val_features, val_labels),
                 epochs=args.epochs,
                 shuffle=True,
                 class_weight={0: weight_for_0, 1: weight_for_1},
                 callbacks=[early_stopping_cb])

        if args.validation_predictions_output:
            logger.info("Predicting validation decisions")
            validation_predictions[fold] = predict_lstm_for_seeds(
                val_set,
                lstm,
                decisions,
                wikidata_hashmap,
                embedding_hashmap,
                initial_properties,
                next_properties,
                args.sequence_len,
                args.padding,
                args.allow_bridge_nodes,
                available_embedding_qids,
                "validation",
            )

        logger.info("Predicting outer-test decisions")
        test_set = folds[fold]["test"]
        predictions[fold] = predict_lstm_for_seeds(
            test_set,
            lstm,
            decisions,
            wikidata_hashmap,
            embedding_hashmap,
            initial_properties,
            next_properties,
            args.sequence_len,
            args.padding,
            args.allow_bridge_nodes,
            available_embedding_qids,
            "test",
        )

    pickle.dump(predictions, open(args.predictions_output, "wb"))
    if args.validation_predictions_output:
        pickle.dump(validation_predictions, open(args.validation_predictions_output, "wb"))
    logger.info(f"Execution time = {utils.convert(time.time() - start)}")


if __name__ == '__main__':
    main()
