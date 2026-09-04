"""Is this the same car?

Grouping used to answer that with a difference hash of the crop's shape and a
coarse hue histogram. Both are cheap, and both were measured on a real burst
and found wanting: shape ran 2..32 bits for the same car and 9..40 for
different ones, which overlaps almost entirely, and the histogram scored
about 1.00 for every pair including a green ute against a blue hatchback,
because it is computed over the whole crop and grass, track and sky swamp it.

So the class name and the vision model's guess at the make ended up doing the
gatekeeping -- which made grouping depend on the thing grouping exists to
correct. One blue Falcon came back as a Fiesta, an Astra, a Commodore and a
Mustang, and the make gate then refused to merge the frames that disagreed.

This replaces the resemblance signals with an embedding: a small vision
transformer trained so that two views of the same object land near each other
and two different objects do not. DINOv2 is trained for exactly that -- it is
a self-supervised feature extractor rather than a classifier, so it separates
*instances* rather than categories, which is the difference between "both of
these are hatchbacks" and "both of these are that hatchback".

Deliberately not a classifier's penultimate layer, which is what an ImageNet
model would give: those are trained to collapse instances of a category
together, which is the wrong direction entirely for this question.

Runs on onnxruntime, which is already here for the plate reader and the plate
detector, so this adds a model file and no new Python package.
"""

from __future__ import annotations

import hashlib
import threading

import numpy as np
from PIL import Image

from .config import MODEL_DIR

# Xenova's ONNX export of facebook/dinov2-small, dynamically quantised to
# uint8. The full-precision export is 88 MB and the quantised one is 24 MB
# for no meaningful loss on this task -- the question here is which of two
# crops is nearer, not an absolute score, and quantisation moves both.
MODEL_NAME = "dinov2-small-quantized.onnx"
MODEL_URL = ("https://huggingface.co/Xenova/dinov2-small/resolve/main/"
             "onnx/model_quantized.onnx")
MODEL_SHA256 = "3afdc8bc63b50558d6e5770f5b799bb82455c2311183a2de43803f343a29d917"
MODEL_BYTES = 24_451_943

# What the model was trained on: 224px square, ImageNet statistics.
INPUT_EDGE = 224
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)

_session = None
_session_lock = threading.Lock()


def model_path():
    return MODEL_DIR / MODEL_NAME


def is_ready() -> bool:
    """Whether the model is here and complete.

    Size only. Hashing 24 MB on every call to a question asked once per scan
    is waste; the hash is checked when the file is written, which is the
    moment it can be wrong.
    """
    path = model_path()
    try:
        return path.exists() and path.stat().st_size == MODEL_BYTES
    except OSError:
        return False


def download(on_progress=None, should_stop=None) -> bool:
    """Fetch the model, resuming a part-finished file rather than restarting.

    Written against what actually happened rather than the happy path: on a
    slow link this came down at about 18 KB/s and the connection dropped
    twice, which on a plain one-shot download leaves a truncated ONNX that
    onnxruntime accepts as a file and then fails to parse. So the transfer
    resumes from what is already on disk, and the result is verified against
    a known hash before it is allowed to count -- a wrong file is deleted
    rather than left to be discovered later by a scan.
    """
    import httpx

    path = model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    have = path.stat().st_size if path.exists() else 0
    if have > MODEL_BYTES:                    # a stale or corrupt part-file
        path.unlink(missing_ok=True)
        have = 0
    if have == MODEL_BYTES:
        return _verify(path, on_progress)

    while have < MODEL_BYTES:
        if should_stop and should_stop():
            return False
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", MODEL_URL, headers=headers,
                              follow_redirects=True, timeout=60.0) as response:
                # A server that ignores Range answers 200 and starts again;
                # appending to what we have would corrupt it silently.
                if have and response.status_code != 206:
                    have = 0
                    path.unlink(missing_ok=True)
                response.raise_for_status()
                mode = "ab" if have else "wb"
                with open(path, mode) as fh:
                    for chunk in response.iter_bytes(256 * 1024):
                        if should_stop and should_stop():
                            return False
                        fh.write(chunk)
                        have += len(chunk)
                        if on_progress:
                            on_progress(have, MODEL_BYTES)
        except Exception:
            # Dropped part way. What arrived is kept and the next attempt
            # picks up from it; the caller decides whether to try again.
            grew = path.stat().st_size if path.exists() else 0
            if grew <= have and grew < MODEL_BYTES:
                return False
            have = grew
    return _verify(path, on_progress)


def _verify(path, on_progress=None) -> bool:
    if on_progress:
        on_progress(MODEL_BYTES, MODEL_BYTES)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() == MODEL_SHA256:
        return True
    # Better to have nothing than a file that looks like a model.
    path.unlink(missing_ok=True)
    return False


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort

            ort.set_default_logger_severity(3)      # errors only
            options = ort.SessionOptions()
            # One thread per session. The analysis pool already runs several
            # workers, and letting each of them fan out again oversubscribes
            # the machine and makes the whole scan slower.
            options.intra_op_num_threads = 1
            _session = ort.InferenceSession(
                str(model_path()), options,
                providers=["CPUExecutionProvider"])
        return _session


def _prepare(image: Image.Image) -> np.ndarray:
    """Crop to square and scale, rather than squashing to 224x224.

    A vehicle crop is wide -- three to one is normal -- and stretching that
    into a square distorts exactly the proportions that tell one car from
    another. Taking the centre square keeps the shapes honest, and the centre
    is where the car is: the crop is built around the detector's box.
    """
    image = image.convert("RGB")
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    square = image.crop((left, top, left + side, top + side))
    square = square.resize((INPUT_EDGE, INPUT_EDGE), Image.BILINEAR)

    data = np.asarray(square, dtype=np.float32).transpose(2, 0, 1) / 255.0
    data = (data - MEAN) / STD
    return data[np.newaxis, ...]


def embed(image: Image.Image) -> np.ndarray | None:
    """One crop as a unit vector, or None if the model is not available.

    Returns the CLS token: DINOv2's summary of the whole image, which is what
    its own retrieval work uses. Normalised, so comparing two of them is a
    dot product and cosine distance needs no division later.
    """
    if not is_ready():
        return None
    try:
        session = _get_session()
        inputs = {session.get_inputs()[0].name: _prepare(image)}
        output = session.run(None, inputs)[0]
    except Exception:
        return None
    vector = np.asarray(output, dtype=np.float32)
    if vector.ndim == 3:            # (batch, tokens, features): take the CLS
        vector = vector[0, 0]
    else:
        vector = vector.reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        return None
    return vector / norm


def nearness(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two embeddings, 0..1 for anything realistic."""
    return float(np.dot(a, b))


def pack(vector: np.ndarray) -> str:
    """Store as text, because that is what the detections table holds."""
    return ",".join(f"{v:.5f}" for v in vector)


def unpack(text: str) -> np.ndarray | None:
    if not text:
        return None
    try:
        return np.array(text.split(","), dtype=np.float32)
    except ValueError:
        return None
