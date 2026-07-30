from __future__ import annotations

import random

import numpy
import tensorflow as tf


def configure_random_seed(random_seed: int) -> None:
    random.seed(random_seed)
    numpy.random.seed(random_seed)
    tf.keras.utils.set_random_seed(random_seed)
    enable_determinism = getattr(tf.config.experimental, "enable_op_determinism", None)
    if enable_determinism is not None:
        enable_determinism()
