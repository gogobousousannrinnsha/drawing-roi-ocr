"""OCR rectangular page crops with original strings and page-space evidence."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import math
import tempfile
import uuid

from PIL import Image

from .pdf_ocr import MAX_IMAGE_PIXELS, OCR_PROFILE, OCRSession, _deduplicate, _tile_rows, tile_boxes
from .storage import require_short_path


BOUNDARY_TOLERANCE_MM = 1e-6


class OcrInitializationError(RuntimeError):
    """The shared OCR model failed to initialize; retrying other fields is futile."""


class OcrResourceError(OcrInitializationError):
    """The OCR engine exhausted GPU resources after or during initialization."""


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def _page_geometry(page):
    width_mm = _number(page["width_mm"], "Page width_mm", positive=True)
    height_mm = _number(page["height_mm"], "Page height_mm", positive=True)
    width_px, height_px = page["width_px"], page["height_px"]
    if any(type(value) is not int or value <= 0 for value in (width_px, height_px)):
        raise ValueError("Page pixel dimensions must be positive integers")
    if width_px * height_px > MAX_IMAGE_PIXELS:
        raise ValueError("Page image exceeds the supported pixel limit")
    if page.get("rotation", 0) != 0:
        raise ValueError("Crop OCR requires the displayed page with rotation zero")
    return width_mm, height_mm, width_px, height_px


def crop_bounds(rect_mm, page):
    """Convert a requested page-mm rectangle to an enclosing pixel rectangle."""
    width_mm, height_mm, width_px, height_px = _page_geometry(page)
    x = _number(rect_mm["x"], "Rectangle x")
    y = _number(rect_mm["y"], "Rectangle y")
    width = _number(rect_mm["width"], "Rectangle width", positive=True)
    height = _number(rect_mm["height"], "Rectangle height", positive=True)
    right, bottom = x + width, y + height
    if (x < -BOUNDARY_TOLERANCE_MM or y < -BOUNDARY_TOLERANCE_MM
            or right > width_mm + BOUNDARY_TOLERANCE_MM
            or bottom > height_mm + BOUNDARY_TOLERANCE_MM):
        raise ValueError("The requested rectangle is outside the displayed page")
    x, y = max(0.0, x), max(0.0, y)
    right, bottom = min(width_mm, right), min(height_mm, bottom)
    if right <= x or bottom <= y:
        raise ValueError("The requested rectangle has no area inside the page")
    bounds = (max(0, math.floor(x * width_px / width_mm)),
              max(0, math.floor(y * height_px / height_mm)),
              min(width_px, math.ceil(right * width_px / width_mm)),
              min(height_px, math.ceil(bottom * height_px / height_mm)))
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise ValueError("The requested rectangle has no pixel area")
    return bounds


def ordered_regions(regions):
    """Read horizontal rows from top to bottom, then left to right within a row.

    A row requires at least half-height vertical overlap. Its common overlap
    band is narrowed for every member, preventing a chain of overlapping boxes
    from joining successive lines. Original region objects and text are kept.
    """
    entries = []
    for ordinal, region in enumerate(regions):
        box = region["bbox_px"]
        x = _number(box["x"], "Region x")
        y = _number(box["y"], "Region y")
        _number(box["width"], "Region width", positive=True)
        height = _number(box["height"], "Region height", positive=True)
        entries.append((y, x, ordinal, y + height, region))
    lines = []
    for top, left, ordinal, bottom, region in sorted(entries, key=lambda entry: entry[:3]):
        candidates = []
        for index, line in enumerate(lines):
            overlap = min(bottom, line["bottom"]) - max(top, line["top"])
            shorter = min(bottom - top, line["bottom"] - line["top"])
            if overlap > 0 and overlap >= shorter * 0.5:
                candidates.append((overlap / shorter, -index, line))
        if candidates:
            line = max(candidates, key=lambda item: item[:2])[2]
            line["top"], line["bottom"] = max(top, line["top"]), min(bottom, line["bottom"])
        else:
            line = {"top": top, "bottom": bottom, "first_top": top, "members": []}
            lines.append(line)
        line["members"].append((left, top, ordinal, region))
    return [entry[3] for line in sorted(lines, key=lambda item: item["first_top"])
            for entry in sorted(line["members"], key=lambda item: item[:3])]


def join_regions(regions):
    """Join positional OCR strings with newlines, without normalization."""
    texts = [region["text"] for region in ordered_regions(regions)]
    if any(not isinstance(text, str) for text in texts):
        raise ValueError("OCR region text must be a string")
    return "\n".join(texts)


@contextmanager
def _page_image(image_path, page_image):
    if page_image is not None:
        yield page_image
        return
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(image_path) as image:
            yield image
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit


def _preflight_paths(scratch, tile_count):
    require_short_path(scratch / ("c" + "0" * 12 + ".png"), label="切り出し画像")
    require_short_path(scratch / ("t-" + "0" * 8) / f"t{tile_count - 1:04d}.png", label="OCR一時画像")
    for child in ("profile", "paddlex", "paddle"):
        require_short_path(scratch / "c" / child, label="OCR一時保存先")


def _save_new_crop(image, scratch):
    """Reserve a short new filename exclusively; a collision never overwrites."""
    for _ in range(16):
        token = uuid.uuid4().hex[:12]
        path = scratch / f"c{token}.png"
        require_short_path(path, label="切り出し画像")
        try:
            stream = path.open("xb")
        except FileExistsError:
            continue
        try:
            with stream:
                image.save(stream, format="PNG")
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return path, token
    raise FileExistsError("切り出し画像に重複しない新しい名前を確保できませんでした。")


def recognize_crop(image_path, page, rect_mm, model_root, scratch, *, session=None,
                   crop_id="", page_image=None):
    """Save one crop, recognize local tiles, and return page-space evidence.

    ``page_image`` may be shared by all fields on a page and remains open.
    The caller owns a supplied OCRSession; only a locally created one is closed.
    """
    width_mm, height_mm, width_px, height_px = _page_geometry(page)
    bounds = crop_bounds(rect_mm, page)
    if not isinstance(crop_id, str):
        raise ValueError("crop_id must be a string")
    if session is not None and hasattr(session, "model_root"):
        if Path(model_root).resolve() != session.model_root:
            raise ValueError("OCR session belongs to another model directory")
    scratch = Path(scratch).resolve()
    boxes = tile_boxes(bounds[2] - bounds[0], bounds[3] - bounds[1])
    _preflight_paths(scratch, len(boxes))
    scratch.mkdir(parents=True, exist_ok=True)
    sx, sy = width_mm / width_px, height_mm / height_px
    geometry = {
        "box_px": list(bounds),
        "crop_bbox_mm": {"x": bounds[0] * sx, "y": bounds[1] * sy,
                         "width": (bounds[2] - bounds[0]) * sx,
                         "height": (bounds[3] - bounds[1]) * sy},
        "rect_mm": {key: rect_mm[key] for key in ("x", "y", "width", "height")},
        "width_px": bounds[2] - bounds[0], "height_px": bounds[3] - bounds[1],
    }
    own_session = session is None
    raw_tiles, observations = [], []
    try:
        with _page_image(image_path, page_image) as source:
            if source.size != (width_px, height_px):
                raise ValueError("Page image dimensions disagree with page metadata")
            with source.crop(bounds) as cropped:
                image = cropped if cropped.mode == "RGB" else cropped.convert("RGB")
                try:
                    crop_path, token = _save_new_crop(image, scratch)
                    geometry["crop_path"] = str(crop_path)
                    identifier = crop_id or token
                    if own_session:
                        try:
                            session = OCRSession(model_root)
                        except Exception as error:
                            raise OcrInitializationError(str(error)) from error
                    with tempfile.TemporaryDirectory(prefix="t-", dir=scratch) as temporary:
                        for ordinal, box in enumerate(boxes):
                            tile_path = Path(temporary) / f"t{ordinal:04d}.png"
                            require_short_path(tile_path, label="OCR一時画像")
                            with image.crop(box) as tile:
                                tile.save(tile_path, format="PNG")
                            try:
                                payload = session.recognize(tile_path, scratch / "c")
                            except Exception as error:
                                message = str(error).casefold()
                                resource_error = type(error).__name__ in ("ResourceExhaustedError", "OutOfMemoryError")
                                gpu_memory_error = (any(name in message for name in ("gpu", "cuda"))
                                                    and any(reason in message for reason in ("out of memory", "resource exhausted")))
                                if resource_error or gpu_memory_error:
                                    raise OcrResourceError(str(error)) from error
                                if hasattr(session, "engine") and session.engine is None:
                                    raise OcrInitializationError(str(error)) from error
                                raise
                            raw_tiles.append({"box_px": list(box),
                                              "page_box_px": [box[0] + bounds[0], box[1] + bounds[1],
                                                              box[2] + bounds[0], box[3] + bounds[1]],
                                              "result": payload})
                            # ROI-local dimensions make only internal tile seams
                            # clipped: the requested ROI boundary is a real edge.
                            observations.extend(_tile_rows(payload, box, *image.size))
                            tile_path.unlink()
                finally:
                    if image is not cropped:
                        image.close()
        regions = []
        for row in _deduplicate(observations):
            local_box = dict(row["bbox_px"])
            local_polygon = [list(point) for point in row["polygon_px"]]
            box = {**local_box, "x": local_box["x"] + bounds[0], "y": local_box["y"] + bounds[1]}
            polygon = [[x + bounds[0], y + bounds[1]] for x, y in local_polygon]
            regions.append({"text": row["text"], "confidence": row["confidence"],
                            "bbox_px": box, "polygon_px": polygon,
                            "bbox_mm": {key: value * (sx if key in ("x", "width") else sy)
                                        for key, value in box.items()},
                            "polygon_mm": [[x * sx, y * sy] for x, y in polygon],
                            "local_bbox_px": local_box, "local_polygon_px": local_polygon})
        regions = ordered_regions(regions)
        for ordinal, region in enumerate(regions, 1):
            region["id"] = f"p{page.get('number', 1)}:{identifier}:r{ordinal:06d}"
        raw = {"schema": "roi-tiled-ocr-v1", "profile": dict(OCR_PROFILE),
               "producer_signature": getattr(session, "signature", "injected-test-session"),
               "crop_id": identifier, "coordinates": "crop-local-top-left-x-right-y-down",
               "tiles": raw_tiles, "observations_px": observations}
        return regions, raw, geometry
    finally:
        if own_session and session is not None:
            session.close()
