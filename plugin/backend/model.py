import base64
import sys
import time
import traceback
import uuid
from io import BytesIO
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor

from utils import mask_to_polygon_at_point

CHECKPOINT_DIR = Path.home() / ".aitracer" / "weights"
DEFAULT_MODEL = "tiny"
MODELS = {
    # key: (checkpoint file, sam2 config)
    "tiny":  ("sam2.1_hiera_tiny.pt",      "configs/sam2.1/sam2.1_hiera_t.yaml"),
    "small": ("sam2.1_hiera_small.pt",     "configs/sam2.1/sam2.1_hiera_s.yaml"),
    "base+": ("sam2.1_hiera_base_plus.pt", "configs/sam2.1/sam2.1_hiera_b+.yaml"),
    "large": ("sam2.1_hiera_large.pt",     "configs/sam2.1/sam2.1_hiera_l.yaml"),
}
# TEMPORARY switch for model comparison: put one of the keys above in this
# file (e.g. `echo "base+" > ~/.aitracer/model`) and restart the backend.
MODEL_CHOICE_FILE = Path.home() / ".aitracer" / "model"
SESSION_TTL = 300  # seconds before idle session is evicted

# Feed the previous mask logits back as `mask_input` when a refinement only
# adds points to the prompt that produced them (SAM2's recommended interactive
# loop). Keeps successive masks stable instead of re-deriving them from the
# points alone. Module-level so it can be switched off for A/B testing.
REFINE_WITH_MASK_INPUT = True


def _select_model() -> str:
    """Model key from MODEL_CHOICE_FILE, falling back to the default."""
    try:
        choice = MODEL_CHOICE_FILE.read_text().strip().lower()
    except OSError:
        return DEFAULT_MODEL
    if choice not in MODELS:
        print(f"Unknown model '{choice}' in {MODEL_CHOICE_FILE}; "
              f"using {DEFAULT_MODEL}.", flush=True)
        return DEFAULT_MODEL
    if not (CHECKPOINT_DIR / MODELS[choice][0]).exists():
        print(f"Checkpoint for '{choice}' not found in {CHECKPOINT_DIR}; "
              f"using {DEFAULT_MODEL}.", flush=True)
        return DEFAULT_MODEL
    return choice


class SegmentationModel:
    def __init__(self):
        self.name = _select_model()
        checkpoint_name, config_name = MODELS[self.name]
        checkpoint = CHECKPOINT_DIR / checkpoint_name
        if not checkpoint.exists():
            raise FileNotFoundError(
                f"Model checkpoint not found: {checkpoint}\n"
                f"Run the setup to download it first."
            )

        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"  # Apple Silicon GPU — 3-5× faster than CPU
        else:
            device = "cpu"

        print(f"Loading SAM2.1 {self.name} on {device}…  "
              f"Python {sys.version}  |  torch {torch.__version__}  "
              f"| checkpoint: {checkpoint}",
              flush=True)

        try:
            sam2 = build_sam2(config_name, str(checkpoint), device=device)
        except Exception:
            # Print the full traceback to the log so we can diagnose
            # platform-specific failures (e.g. Hydra config on Windows).
            print("build_sam2 failed — full traceback:", flush=True)
            traceback.print_exc()
            raise

        self._predictor = SAM2ImagePredictor(sam2)
        self._sessions: dict[str, dict] = {}  # session_id → {embedding, last_access}
        print(f"SAM2.1 {self.name} loaded on {device}.", flush=True)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def segment(self,
                image_b64: str | None,
                positive_points: list,
                negative_points: list,
                session_id: str | None) -> dict:
        """Run segmentation and return polygon + session_id.

        On first call provide image_b64; on refinement calls provide
        session_id and omit image_b64 (or pass None).
        """
        self._evict_stale_sessions()

        if session_id and session_id in self._sessions:
            # Refinement: restore cached embedding
            entry = self._sessions[session_id]
            self._predictor.model.eval()
            # The predictor stores features internally; we restore them
            self._predictor._features = entry["features"]
            self._predictor._orig_hw = entry["orig_hw"]
            self._predictor._is_image_set = True
        else:
            # First call: decode image and set it on the predictor
            if not image_b64:
                raise ValueError("image_b64 required for first call (no session_id).")
            image = _decode_image(image_b64)
            self._predictor.set_image(image)
            session_id = str(uuid.uuid4())
            self._sessions[session_id] = {
                "features": self._predictor._features,
                "orig_hw": self._predictor._orig_hw,
                "last_access": time.time(),
            }

        self._sessions[session_id]["last_access"] = time.time()

        point_coords, point_labels = _build_prompt(positive_points, negative_points)

        # Reuse the previous mask only when this prompt extends the one that
        # produced it. After an undo/removal the cached logits no longer match
        # the prompt, so fall back to a fresh prediction.
        entry = self._sessions[session_id]
        prompt = (_point_set(positive_points), _point_set(negative_points))
        mask_input = None
        if (
            REFINE_WITH_MASK_INPUT
            and entry.get("logits") is not None
            and entry.get("prompt") is not None
            and prompt[0] >= entry["prompt"][0]
            and prompt[1] >= entry["prompt"][1]
        ):
            mask_input = entry["logits"]

        with torch.inference_mode():
            masks, scores, logits = self._predictor.predict(
                point_coords=point_coords,
                point_labels=point_labels,
                mask_input=mask_input,
                # Several candidates on a fresh prompt (ambiguous single
                # click); a single, stable mask when refining.
                multimask_output=mask_input is None,
            )

        # Pick the mask with the highest score
        best_idx = int(np.argmax(scores))
        entry["logits"] = logits[best_idx][None, :, :]
        entry["prompt"] = prompt
        mask = masks[best_idx].astype(np.uint8)
        confidence = float(scores[best_idx])

        # Use the first positive point to select the correct contour
        click_xy = positive_points[0] if positive_points else [mask.shape[1] // 2, mask.shape[0] // 2]
        polygon = mask_to_polygon_at_point(mask, click_xy)

        return {
            "session_id": session_id,
            "polygon": polygon,
            "confidence": confidence,
        }

    def clear_session(self, session_id: str):
        self._sessions.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    def _evict_stale_sessions(self):
        now = time.time()
        stale = [sid for sid, e in self._sessions.items()
                 if now - e["last_access"] > SESSION_TTL]
        for sid in stale:
            del self._sessions[sid]


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _decode_image(image_b64: str) -> np.ndarray:
    raw = base64.b64decode(image_b64)
    image = Image.open(BytesIO(raw)).convert("RGB")
    return np.array(image)


def _point_set(points: list) -> frozenset:
    return frozenset((int(p[0]), int(p[1])) for p in points)


def _build_prompt(positive: list, negative: list):
    coords, labels = [], []
    for p in positive:
        coords.append(p)
        labels.append(1)
    for p in negative:
        coords.append(p)
        labels.append(0)
    return np.array(coords, dtype=np.float32), np.array(labels, dtype=np.int32)
