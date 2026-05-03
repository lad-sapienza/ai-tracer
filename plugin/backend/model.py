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
CHECKPOINT_NAME = "sam2.1_hiera_tiny.pt"
CONFIG_NAME = "configs/sam2.1/sam2.1_hiera_t.yaml"
SESSION_TTL = 300  # seconds before idle session is evicted


class SegmentationModel:
    def __init__(self):
        checkpoint = CHECKPOINT_DIR / CHECKPOINT_NAME
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

        print(f"Loading SAM2-tiny on {device}…  "
              f"Python {sys.version}  |  torch {torch.__version__}  "
              f"| checkpoint: {checkpoint}",
              flush=True)

        try:
            sam2 = build_sam2(CONFIG_NAME, str(checkpoint), device=device)
        except Exception:
            # Print the full traceback to the log so we can diagnose
            # platform-specific failures (e.g. Hydra config on Windows).
            print("build_sam2 failed — full traceback:", flush=True)
            traceback.print_exc()
            raise

        self._predictor = SAM2ImagePredictor(sam2)
        self._sessions: dict[str, dict] = {}  # session_id → {embedding, last_access}
        print(f"SAM2-tiny loaded on {device}.", flush=True)

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

        with torch.inference_mode():
            masks, scores, _ = self._predictor.predict(
                point_coords=point_coords,
                point_labels=point_labels,
                multimask_output=True,
            )

        # Pick the mask with the highest score
        best_idx = int(np.argmax(scores))
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


def _build_prompt(positive: list, negative: list):
    coords, labels = [], []
    for p in positive:
        coords.append(p)
        labels.append(1)
    for p in negative:
        coords.append(p)
        labels.append(0)
    return np.array(coords, dtype=np.float32), np.array(labels, dtype=np.int32)
