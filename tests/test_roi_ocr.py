import math
from pathlib import Path
import re
from types import SimpleNamespace

from PIL import Image
import pytest

from drawing_roi_ocr import roi_ocr


def page(width=100, height=100):
    return {"number": 3, "width_px": width, "height_px": height,
            "width_mm": width / 10, "height_mm": height / 10, "rotation": 0}


def rect(x=1, y=2, width=4, height=3):
    return {"x": x, "y": y, "width": width, "height": height}


def region(text, x, y, width=10, height=10):
    return {"text": text, "bbox_px": {"x": x, "y": y, "width": width, "height": height}}


def payload(*rows):
    return {"res": {"rec_texts": [row[0] for row in rows],
                    "rec_scores": [row[1] for row in rows],
                    "rec_polys": [row[2] for row in rows]}}


def quad(x=0, y=0, width=10, height=10):
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


class FakeSession:
    def __init__(self, results):
        self.results = iter(results)
        self.paths = []
        self.cache_roots = []
        self.closed = False
        self.signature = "fixture-model-signature"

    def recognize(self, image_path, cache_root):
        assert Path(image_path).is_file()
        self.paths.append(image_path)
        self.cache_roots.append(Path(cache_root))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def image_file(tmp_path, size=(100, 100), mode="RGB"):
    path = tmp_path / "日本語 page.png"
    Image.new(mode, size, "white").save(path)
    return path


def test_crop_bounds_enclose_requested_mm_using_actual_raster_size():
    info = {"width_mm": 210, "height_mm": 297, "width_px": 2481, "height_px": 3508}
    value = rect(10.123, 20.456, 30.789, 15.432)
    assert roi_ocr.crop_bounds(value, info) == (
        math.floor(10.123 * 2481 / 210), math.floor(20.456 * 3508 / 297),
        math.ceil((10.123 + 30.789) * 2481 / 210), math.ceil((20.456 + 15.432) * 3508 / 297))


def test_only_microscopic_boundary_rounding_is_clamped():
    assert roi_ocr.crop_bounds(rect(-1e-8, 0, 10 + 2e-8, 10), page()) == (0, 0, 100, 100)
    for value in (rect(-0.01, 0), rect(8, 0), rect(0, 9)):
        with pytest.raises(ValueError, match="outside"):
            roi_ocr.crop_bounds(value, page())


@pytest.mark.parametrize("value", [rect(width=0), rect(height=-1), rect(x=float("nan")), rect(y=True)])
def test_invalid_rectangles_are_rejected(value):
    with pytest.raises(ValueError):
        roi_ocr.crop_bounds(value, page())


def test_reading_order_uses_rows_and_preserves_strings():
    values = [region("右", 30, 10), region(" 001-020 ", 0, 12), region("3:1", 0, 30)]
    result = roi_ocr.ordered_regions(values)
    assert result == [values[1], values[0], values[2]]
    assert result[0] is values[1]
    assert roi_ocr.join_regions(values) == " 001-020 \n右\n3:1"
    assert roi_ocr.join_regions([]) == ""


def test_overlap_chain_does_not_merge_distinct_lines():
    values = [region("first", 30, 0), region("middle", 20, 5), region("last", 10, 10)]
    assert [r["text"] for r in roi_ocr.ordered_regions(values)] == ["middle", "first", "last"]


def test_crop_ocr_preserves_text_score_zero_and_global_and_local_coordinates(tmp_path):
    image = image_file(tmp_path)
    raw_payload = payload((" 0007 ", 0.0, quad(0, 0, 40, 20)))
    session = FakeSession([raw_payload])
    regions, raw, geometry = roi_ocr.recognize_crop(
        image, page(), rect(), tmp_path / "models", tmp_path / "切出し", session=session, crop_id="Scale")
    assert len(regions) == 1
    value = regions[0]
    assert value["text"] == " 0007 " and value["confidence"] == 0.0
    assert value["id"] == "p3:Scale:r000001"
    assert value["bbox_px"] == {"x": 10, "y": 20, "width": 40, "height": 20}
    assert value["bbox_mm"] == {"x": 1, "y": 2, "width": 4, "height": 2}
    assert value["local_bbox_px"]["x"] == 0
    assert value["polygon_px"][0] == [10, 20]
    assert value["polygon_mm"][0] == [1, 2]
    assert raw["tiles"][0]["result"] is raw_payload
    assert raw["observations_px"][0]["edge_clipped"] is False
    assert geometry["box_px"] == [10, 20, 50, 50]
    assert geometry["crop_bbox_mm"] == rect()
    assert Path(geometry["crop_path"]).is_file()
    assert not session.closed
    assert not any((tmp_path / "切出し").glob("t-*"))


def test_large_roi_deduplicates_internal_seams_and_keeps_every_raw_observation(tmp_path):
    info = page(4000, 100)
    image = image_file(tmp_path, (4000, 100))
    # ROI 2100px: tile origins 0 and 52. They observe the same page word.
    session = FakeSession([payload(("same", .9, quad(100, 10))),
                           payload(("same", .8, quad(48, 10)))])
    regions, raw, geometry = roi_ocr.recognize_crop(
        image, info, rect(10, 0, 210, 10), tmp_path / "models", tmp_path / "scratch", session=session)
    assert len(raw["tiles"]) == 2 and len(raw["observations_px"]) == 2
    assert len(regions) == 1
    assert regions[0]["bbox_px"]["x"] == 200
    assert raw["tiles"][1]["box_px"] == [52, 0, 2100, 100]
    assert raw["tiles"][1]["page_box_px"] == [152, 0, 2200, 100]
    assert geometry["width_px"] == 2100


def test_shared_page_is_not_reopened_or_closed_and_paths_never_collide(tmp_path, monkeypatch):
    session = FakeSession([payload(), payload()])
    with Image.new("L", (100, 100), 255) as shared:
        monkeypatch.setattr(roi_ocr.Image, "open", lambda *_: pytest.fail("shared page reopened"))
        _, _, first = roi_ocr.recognize_crop("absent.png", page(), rect(), "models", tmp_path,
                                            session=session, page_image=shared)
        _, _, second = roi_ocr.recognize_crop("absent.png", page(), rect(), "models", tmp_path,
                                             session=session, page_image=shared)
        assert shared.getpixel((0, 0)) == 255
    assert first["crop_path"] != second["crop_path"]
    assert not session.closed


def test_missing_or_mismatched_page_and_limits_leave_model_unloaded(tmp_path):
    original_limit = Image.MAX_IMAGE_PIXELS
    session = FakeSession([])
    with pytest.raises(FileNotFoundError):
        roi_ocr.recognize_crop(tmp_path / "missing.png", page(), rect(), "models", tmp_path, session=session)
    assert Image.MAX_IMAGE_PIXELS == original_limit
    with pytest.raises(ValueError, match="dimensions"):
        roi_ocr.recognize_crop(image_file(tmp_path, (50, 50)), page(), rect(), "models", tmp_path, session=session)
    assert session.paths == []


def test_own_session_is_closed_and_initialization_failure_is_distinct(tmp_path, monkeypatch):
    image = image_file(tmp_path)
    session = FakeSession([payload()])
    monkeypatch.setattr(roi_ocr, "OCRSession", lambda *_: session)
    roi_ocr.recognize_crop(image, page(), rect(), "models", tmp_path)
    assert session.closed
    failed = FakeSession([RuntimeError("GPU unavailable")])
    failed.engine = None
    with pytest.raises(roi_ocr.OcrInitializationError, match="GPU unavailable"):
        roi_ocr.recognize_crop(image, page(), rect(), "models", tmp_path, session=failed)
    assert not failed.closed


def test_initialized_engine_inference_failure_is_not_initialization_failure(tmp_path):
    session = FakeSession([RuntimeError("bad inference")])
    session.engine = object()
    with pytest.raises(RuntimeError, match="bad inference") as error:
        roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path, session=session)
    assert not isinstance(error.value, roi_ocr.OcrInitializationError)


@pytest.mark.parametrize("error", [
    RuntimeError("CUDA out of memory"),
    RuntimeError("Resource exhausted on GPU 0"),
    type("ResourceExhaustedError", (RuntimeError,), {})("device allocation failed"),
    type("OutOfMemoryError", (RuntimeError,), {})("allocation failed"),
])
def test_initialized_engine_resource_failure_stops_shared_processing(tmp_path, error):
    session = FakeSession([error])
    session.engine = object()
    with pytest.raises(roi_ocr.OcrResourceError) as raised:
        roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path, session=session)
    assert isinstance(raised.value, roi_ocr.OcrInitializationError)
    assert raised.value.__cause__ is error
    assert not session.closed


@pytest.mark.parametrize("message", ["CPU out of memory", "CUDA invalid argument", "ordinary field failure"])
def test_ordinary_runtime_failures_are_not_reclassified_as_resource_errors(tmp_path, message):
    original = RuntimeError(message)
    session = FakeSession([original])
    session.engine = object()
    with pytest.raises(RuntimeError) as raised:
        roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path, session=session)
    assert raised.value is original


def test_model_loader_oserror_is_initialization_failure_but_preserves_cause(tmp_path):
    session = FakeSession([OSError("CUDA library unavailable")])
    session.engine = None
    with pytest.raises(roi_ocr.OcrInitializationError) as error:
        roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path, session=session)
    assert isinstance(error.value.__cause__, OSError)


def test_output_save_errors_are_not_hidden(tmp_path, monkeypatch):
    image = image_file(tmp_path)
    session = FakeSession([])
    monkeypatch.setattr(Image.Image, "save", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        roi_ocr.recognize_crop(image, page(), rect(), "models", tmp_path, session=session)
    assert session.paths == []
    assert not list(tmp_path.glob("c????????????.png"))


def test_short_crop_tile_and_cache_names_keep_the_same_geometry_interface(tmp_path):
    session = FakeSession([payload()])
    _, _, geometry = roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path,
                                            session=session)
    crop = Path(geometry["crop_path"])
    assert re.fullmatch(r"c[0-9a-f]{12}\.png", crop.name)
    assert crop.is_absolute() and crop.is_file()
    assert session.paths[0].name == "t0000.png"
    assert session.paths[0].parent.name.startswith("t-")
    assert session.cache_roots == [tmp_path / "c"]
    assert not session.paths[0].parent.exists()


def test_crop_name_collision_never_overwrites_existing_file(tmp_path, monkeypatch):
    existing = tmp_path / ("c" + "a" * 12 + ".png")
    existing.write_bytes(b"existing saved crop")
    tokens = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(roi_ocr.uuid, "uuid4", lambda: SimpleNamespace(hex=next(tokens)))
    _, _, geometry = roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path,
                                            session=FakeSession([payload()]))
    assert existing.read_bytes() == b"existing saved crop"
    assert Path(geometry["crop_path"]).name == "c" + "b" * 12 + ".png"


def test_repeated_collisions_fail_without_overwriting_or_starting_ocr(tmp_path, monkeypatch):
    existing = tmp_path / ("c" + "a" * 12 + ".png")
    existing.write_bytes(b"retained")
    monkeypatch.setattr(roi_ocr.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    session = FakeSession([])
    with pytest.raises(FileExistsError):
        roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", tmp_path, session=session)
    assert existing.read_bytes() == b"retained"
    assert session.paths == []


def _scratch_with_length(tmp_path, units, *, emoji=False):
    parent_units = len(str(tmp_path.resolve()).encode("utf-16-le")) // 2
    prefix = "😀" * 4 if emoji else ""
    count = units - parent_units - 1 - len(prefix.encode("utf-16-le")) // 2
    assert count > 0
    return tmp_path / (prefix + "a" * count)


def test_exact_maximum_tile_path_length_is_accepted(tmp_path):
    suffix_units = len(str(Path("t-" + "0" * 8) / "t0000.png")) + 1
    scratch = _scratch_with_length(tmp_path, 240 - suffix_units)
    session = FakeSession([payload()])
    roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", scratch, session=session)
    assert len(str(session.paths[0]).encode("utf-16-le")) // 2 == 240


@pytest.mark.parametrize("emoji", [False, True])
def test_overlong_tile_path_is_rejected_before_outputs_or_ocr(tmp_path, emoji):
    suffix_units = len(str(Path("t-" + "0" * 8) / "t0000.png")) + 1
    scratch = _scratch_with_length(tmp_path, 241 - suffix_units, emoji=emoji)
    session = FakeSession([])
    with pytest.raises(ValueError):
        roi_ocr.recognize_crop(image_file(tmp_path), page(), rect(), "models", scratch, session=session)
    assert not scratch.exists()
    assert session.paths == []
