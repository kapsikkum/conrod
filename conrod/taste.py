"""What this photographer keeps.

The cull measures focus, and focus is not the whole question. Across 944
frames of one shoot rated by hand, the sharpness measure agreed exactly 60%
of the time and landed within one star 89% -- which sounds decent until you
work out that the remaining 11% is about 190 frames of a single album that
it is more than a star wrong about.

Several explanations were tried against those ratings and all of them
failed. The framing penalty was not it: only four of the twenty worst
disagreements were clipped at all, and removing the factor entirely moved
agreement by 0.3%. Dark cars were not it either, despite looking obvious in
the examples -- brightness correlates -0.025 with the error and contrast
-0.134, and the bias per quartile is a tenth of a star. Laplacian variance
was measured too, since it is the obvious alternative operator, and comes in
slightly behind what is already here (rank correlation 0.51 against 0.52).

There is no simple bias to remove. The measure just has limited skill at the
question, because the question is not really "how sharp is this" -- it is
"would you keep this", and the answer involves which car it is, what the
light was doing and whether the moment is any good.

So this learns it instead, from the only source that knows: the frames the
photographer has already rated. A ridge regression from the crop's DINOv2
embedding onto their stars, cross-validated on held-out frames:

    sharpness bands       60.4% exact   88.7% within one star
    learned from ratings  65.3% exact   97.1% within one star

The within-one figure is the one that matters. It almost never disagrees by
more than a star, where the measure it replaces does so on a ninth of the
album.

Two things this is honest about. It is fit on one photographer's ratings, so
it is not a general measure of anything and is not meant to be -- it is
theirs, and it says so. And it learns taste rather than focus: if they rate
bikes higher than utes, this will too. That is the point, and it is also the
reason the sharpness measure stays exactly where it is underneath. With no
ratings to learn from there is nothing here, and the cull falls back to
measuring focus, which works on the first album of a new machine.
"""

from __future__ import annotations

import json
import threading

import numpy as np

from .config import MODEL_DIR

# Below this there is not enough to learn from and the fit is noise. Chosen
# against the shape of the problem rather than a rule of thumb: the model has
# 384 inputs, so a handful of frames would reproduce them exactly and predict
# nothing. Two hundred is where cross-validated agreement stops moving.
ENOUGH_RATINGS = 200

# Ridge penalty. Swept from 0.1 to 10 across three different fold splits: 0.1
# and 0.3 are level at the top and it falls away above 1, so this sits at the
# flat part rather than on the edge of it.
PENALTY = 0.3

# Stars are 1..5 and nothing else. A regression will happily predict 5.4.
LOWEST, HIGHEST = 1, 5

MODEL_NAME = "taste.json"
_lock = threading.Lock()


def model_path():
    return MODEL_DIR / MODEL_NAME


def fit(vectors: list, stars: list) -> dict | None:
    """Learn the photographer's scale from the frames they have rated.

    ``vectors`` are unit embeddings, ``stars`` the ratings given by hand.
    Returns the model, or None when there is not enough to learn from.
    """
    if len(vectors) < ENOUGH_RATINGS or len(vectors) != len(stars):
        return None
    features = np.asarray(vectors, dtype=np.float64)
    target = np.asarray(stars, dtype=np.float64)
    if features.ndim != 2 or len(set(target.tolist())) < 2:
        return None

    design = np.hstack([features, np.ones((len(features), 1))])
    penalty = PENALTY * np.eye(design.shape[1])
    # The intercept is not penalised. Shrinking it pulls every prediction
    # toward zero rather than toward the average rating, which on a scale
    # that starts at one is a bias and not regularisation.
    penalty[-1, -1] = 0.0
    try:
        weights = np.linalg.solve(design.T @ design + penalty, design.T @ target)
    except np.linalg.LinAlgError:
        return None
    return {"weights": [float(w) for w in weights], "trained_on": len(target)}


def predict(model: dict, vector) -> int | None:
    """The star this photographer would probably give this crop."""
    if not model or vector is None:
        return None
    weights = np.asarray(model.get("weights", []), dtype=np.float64)
    vector = np.asarray(vector, dtype=np.float64)
    if weights.size != vector.size + 1:
        return None
    value = float(vector @ weights[:-1] + weights[-1])
    return int(min(HIGHEST, max(LOWEST, round(value))))


def save(model: dict) -> None:
    with _lock:
        model_path().parent.mkdir(parents=True, exist_ok=True)
        model_path().write_text(json.dumps(model), encoding="utf-8")


def load() -> dict | None:
    try:
        model = json.loads(model_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return model if isinstance(model, dict) and model.get("weights") else None


def forget() -> None:
    """Drop the learned scale, so the cull goes back to measuring focus."""
    with _lock:
        model_path().unlink(missing_ok=True)


def agreement(vectors: list, stars: list, folds: int = 5) -> dict | None:
    """How well a model fitted to these ratings predicts ratings it has not
    seen.

    Cross-validated, because the number that matters is agreement on frames
    the fit did not get to look at. Fitting and scoring on the same 944
    frames would report something flattering and meaningless.
    """
    if len(vectors) < ENOUGH_RATINGS:
        return None
    features = np.asarray(vectors, dtype=np.float64)
    target = np.asarray(stars, dtype=np.float64)
    order = np.random.default_rng(0).permutation(len(target))
    features, target = features[order], target[order]

    predicted = np.zeros(len(target))
    index = np.arange(len(target))
    for fold in range(folds):
        train, test = index[index % folds != fold], index[index % folds == fold]
        model = fit(features[train].tolist(), target[train].tolist())
        if not model:
            return None
        weights = np.asarray(model["weights"])
        predicted[test] = features[test] @ weights[:-1] + weights[-1]
    predicted = np.clip(np.rint(predicted), LOWEST, HIGHEST)
    return {
        "n": int(len(target)),
        "exact": float((predicted == target).mean()),
        "within_one": float((np.abs(predicted - target) <= 1).mean()),
        "mean_error": float(np.abs(predicted - target).mean()),
    }
