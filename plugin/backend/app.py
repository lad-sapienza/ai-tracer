from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from model import SegmentationModel

# Must match PLUGIN_VERSION in plugin/main.py — used to detect stale
# uvicorn processes left over from a previous plugin version.
APP_VERSION = "0.1.26"

_model: SegmentationModel | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    _model = SegmentationModel()
    yield
    _model = None


app = FastAPI(title="AITracer SAM2 backend", lifespan=lifespan)


# ------------------------------------------------------------------ #
# Request / response schemas                                          #
# ------------------------------------------------------------------ #

class SegmentRequest(BaseModel):
    image: str | None = None          # base64 PNG, required on first call
    positive_points: list[list[int]] = []
    negative_points: list[list[int]] = []
    session_id: str | None = None     # omit on first call


class SegmentResponse(BaseModel):
    session_id: str
    polygon: list[list[int]]          # canvas pixel coords, outer ring
    confidence: float


class ClearRequest(BaseModel):
    session_id: str


# ------------------------------------------------------------------ #
# Routes                                                              #
# ------------------------------------------------------------------ #

@app.get("/health")
def health():
    return {"status": "ok", "model": "sam2-tiny", "version": APP_VERSION}


@app.post("/segment", response_model=SegmentResponse)
def segment(req: SegmentRequest):
    if _model is None:
        raise HTTPException(503, detail="Model not loaded.")

    if not req.session_id and not req.image:
        raise HTTPException(422, detail="Provide image on first call or session_id for refinement.")

    if not req.positive_points and not req.negative_points:
        raise HTTPException(422, detail="At least one prompt point required.")

    try:
        result = _model.segment(
            image_b64=req.image,
            positive_points=req.positive_points,
            negative_points=req.negative_points,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
    except Exception as e:
        raise HTTPException(500, detail=f"Inference failed: {e}")

    # polygon may be [] when SAM2 finds nothing — return it as-is.
    # The client already handles an empty polygon list gracefully.
    return SegmentResponse(
        session_id=result["session_id"],
        polygon=result["polygon"],
        confidence=result["confidence"],
    )


@app.post("/clear")
def clear(req: ClearRequest):
    if _model:
        _model.clear_session(req.session_id)
    return {"status": "cleared"}
