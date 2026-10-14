"""CPU tests for native boundaries, immutability and atomic publication."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from docuworks_ctypes import AnnotationType, AuthMode, OpenMode
from docuworks_ctypes._raw import types as T
from docuworks_ctypes.encoding import decode_wchar_buffer

from drawing_roi_ocr import templates as subject


def rectangle(x=2, y=3, width=6, height=4, **extra):
    return dict(kind=int(AnnotationType.RECTANGLE), x=x, y=y, width=width, height=height,
                children=[], **extra)


def write_xdw(path, *, rectangles=None, width=20, height=15, degree=0, pages=1, extra=None):
    annotations = list(rectangles if rectangles is not None else [rectangle()]) + list(extra or [])
    path.write_text(json.dumps(dict(width=width, height=height, degree=degree,
                                  pages=pages, annotations=annotations)), encoding="utf-8")
    return path


class FakeAnnotation:
    def __init__(self, document, data):
        self.document, self.data = document, data

    @property
    def annotation_type(self):
        return self.data["kind"]

    @property
    def position(self):
        return SimpleNamespace(x=self.data.get("x"), y=self.data.get("y"))

    @property
    def size(self):
        if self.data.get("no_size"):
            return None
        return SimpleNamespace(width=self.data.get("width"), height=self.data.get("height"))

    def descendants(self):
        for data in self.data.get("children", []):
            child = FakeAnnotation(self.document, data)
            yield child
            yield from child.descendants()

    def remove(self):
        assert self.document.mode == OpenMode.UPDATE
        self.document.data["annotations"].remove(self.data)

    def get_standard_attribute(self, name):
        return self.data["styles"][name]


class FakePage:
    def __init__(self, document):
        self.document = document

    def annotations(self, *, recursive):
        assert recursive is False
        return tuple(FakeAnnotation(self.document, data)
                     for data in self.document.data["annotations"])

    def add_rectangle(self, box, **styles):
        assert self.document.mode == OpenMode.UPDATE
        data = rectangle(x=box.x, y=box.y, width=box.width, height=box.height)
        data["styles"] = {"%BorderColor": styles["border_color"],
                          "%BorderWidth": styles["border_width"],
                          "%BorderStyle": styles["border_visible"],
                          "%FillStyle": styles["fill_visible"]}
        self.document.data["annotations"].append(data)
        return FakeAnnotation(self.document, data)


class FakeRaw:
    def __init__(self, api):
        self.api = api

    def XDW_GetPageInformation(self, document, number, pointer):
        assert number == 1
        info = ctypes.cast(pointer, ctypes.POINTER(T.XDW_PAGE_INFO_EX)).contents
        info.nWidth = round(document.data["width"] * 100)
        info.nHeight = round(document.data["height"] * 100)
        info.nDegree = document.data["degree"]
        return 0

    def XDW_CreateXdwFromImageFileW(self, image, output, pointer):
        image, output = Path(decode_wchar_buffer(image)), Path(decode_wchar_buffer(output))
        options = ctypes.cast(pointer, ctypes.POINTER(T.XDW_CREATE_OPTION)).contents
        self.api.create_options = {name: getattr(options, name) for name, _ in T.XDW_CREATE_OPTION._fields_}
        with Image.open(image) as bmp:
            self.api.bmp_size, self.api.bmp_dpi = bmp.size, bmp.info["dpi"]
        write_xdw(output, width=options.nWidth / 100, height=options.nHeight / 100,
                  rectangles=self.api.background_annotations)
        return 0


class FakeDocument:
    def __init__(self, api, path, mode):
        self.api, self.path, self.mode = api, Path(path), mode
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.raw, self.handle = api.raw, self

    @property
    def page_count(self):
        return self.data["pages"]

    def page(self, number):
        assert number == 1
        return FakePage(self)

    def save(self):
        assert self.mode == OpenMode.UPDATE
        self.api.saves += 1
        if self.api.fail_save_at == self.api.saves:
            raise RuntimeError("injected save failure")
        if self.api.save_hook:
            self.api.save_hook(self.data)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.api.closed += 1


class FakeApi:
    def __init__(self):
        self.raw = FakeRaw(self)
        self.opens, self.closed, self.saves = [], 0, 0
        self.fail_save_at, self.save_hook = None, None
        self.background_annotations = []

    def open_document(self, path, *, mode, auth):
        self.opens.append((Path(path), mode, auth))
        return FakeDocument(self, path, mode)


@pytest.fixture
def api(monkeypatch):
    instance = FakeApi()
    monkeypatch.setattr(subject, "_load_api", lambda factory=None: factory() if factory else instance)
    return instance


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "背景.png"
    with Image.new("RGB", (237, 178), "white") as original:
        original.save(path)
    return path


PAGE = dict(width_mm=20, height_mm=15, width_px=237, height_px=178, rotation=0)


def test_inspect_preserves_name_source_and_uses_short_readonly_copy(tmp_path, api):
    path = write_xdw(tmp_path / "図番 01.XDW")
    before = path.read_bytes()
    result = subject.inspect_template(path, api_factory=lambda: api)
    assert result == dict(name="図番 01", path=str(path), width_mm=20, height_mm=15,
                         rotation=0, rect_mm=dict(x=2, y=3, width=6, height=4),
                         sha256=subject.sha(path))
    assert before == path.read_bytes()
    assert api.opens and all(p != path and mode == OpenMode.READONLY and auth == AuthMode.NO_DIALOG
                             for p, mode, auth in api.opens)
    assert api.closed == len(api.opens)


@pytest.mark.parametrize("changes", [
    dict(rectangles=[]), dict(rectangles=[rectangle(), rectangle(x=10)]),
    dict(pages=2), dict(degree=90), dict(width=0),
    dict(rectangles=[rectangle(x=-0.01)]), dict(rectangles=[rectangle(x=19)]),
    dict(rectangles=[rectangle(width=0)]), dict(rectangles=[rectangle(y=float("nan"))]),
    dict(rectangles=[rectangle(width=float("inf"))]),
    dict(rectangles=[rectangle(no_size=True)]),
])
def test_inspect_rejects_invalid_template(tmp_path, api, changes):
    path = write_xdw(tmp_path / "field.xdw", **changes)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        subject.inspect_template(path)
    assert path.read_bytes() == before


def test_nested_rectangle_is_not_misread_as_page_geometry(tmp_path, api):
    group = dict(kind=int(AnnotationType.GROUP), children=[rectangle()])
    path = write_xdw(tmp_path / "field.xdw", extra=[group])
    with pytest.raises(ValueError, match="グループ"):
        subject.inspect_template(path)


def test_nonrectangle_annotation_is_not_an_extraction_range(tmp_path, api):
    text = dict(kind=int(AnnotationType.TEXT), children=[])
    path = write_xdw(tmp_path / "field.xdw", extra=[text])
    assert subject.inspect_template(path)["rect_mm"]["x"] == 2


def test_load_direct_xdws_in_filename_order_and_accept_point_one_mm(tmp_path, api):
    write_xdw(tmp_path / "b.xdw", width=20.1)
    write_xdw(tmp_path / "A.XDW")
    (tmp_path / "ignore.txt").write_text("not a template")
    subfolder = tmp_path / "ignored"
    subfolder.mkdir()
    write_xdw(subfolder / "not-included.xdw", width=99)
    assert [r["name"] for r in subject.load_template_set(tmp_path)] == ["A", "b"]


@pytest.mark.parametrize("dimensions", [(20, 20.11), (19.9, 20, 20.1)])
def test_template_dimensions_must_agree_across_the_entire_set(tmp_path, api, dimensions):
    for number, width in enumerate(dimensions):
        write_xdw(tmp_path / f"{number}.xdw", width=width)
    with pytest.raises(ValueError, match="0.1mm"):
        subject.load_template_set(tmp_path)


def test_empty_template_folder_rejected(tmp_path, api):
    with pytest.raises(ValueError, match="ありません"):
        subject.load_template_set(tmp_path)


def test_create_background_and_clamp_minimum_rectangles_without_source_write(tmp_path, api, image):
    before = image.read_bytes()
    target = tmp_path / "editable.xdw"
    regions = [dict(bbox_mm=dict(x=0, y=0, width=.2, height=.4)),
               dict(bbox_mm=dict(x=19, y=14, width=1, height=1))]
    result = subject.create_editable_xdw(image, PAGE, regions, target)
    assert result["rectangle_count"] == 2
    assert result["sha256"] == subject.sha(target)
    assert result["rotation"] == 0
    payload = json.loads(target.read_text())
    assert [(a["x"], a["y"], a["width"], a["height"]) for a in payload["annotations"]] == [
        (0, 0, 3, 3), (17, 12, 3, 3)]
    assert image.read_bytes() == before
    assert api.create_options["nWidth"] == 2000
    assert api.create_options["nHeight"] == 1500
    assert api.create_options["nZoom"] == 100
    assert api.bmp_size == (237, 178)
    assert abs(api.bmp_dpi[0] - 300) < .01
    assert api.closed == len(api.opens)


def test_create_can_start_with_an_empty_editable_page(tmp_path, api, image):
    result = subject.create_editable_xdw(image, PAGE, [], tmp_path / "editable.xdw")
    assert result["rectangle_count"] == 0
    assert api.saves == 0


@pytest.mark.parametrize("page,regions", [
    ({**PAGE, "width_px": 238}, []), ({**PAGE, "width_mm": 30}, []),
    ({**PAGE, "rotation": 90}, []),
    (PAGE, [dict(bbox_mm=dict(x=30, y=1, width=1, height=1))]),
    (PAGE, [dict(bbox_mm=dict(x=0, y=1, width=float("nan"), height=1))]),
])
def test_create_rejects_bad_geometry_without_publishing(tmp_path, api, image, page, regions):
    target = tmp_path / "editable.xdw"
    with pytest.raises(ValueError):
        subject.create_editable_xdw(image, page, regions, target)
    assert not target.exists()


def test_create_rejects_background_annotations(tmp_path, api, image):
    api.background_annotations = [rectangle()]
    target = tmp_path / "editable.xdw"
    with pytest.raises(RuntimeError, match="予期しない注釈"):
        subject.create_editable_xdw(image, PAGE, [], target)
    assert not target.exists()


def test_create_checks_saved_geometry_before_publication(tmp_path, api, image):
    api.save_hook = lambda data: data["annotations"][0].update(x=4)
    target = tmp_path / "editable.xdw"
    with pytest.raises(RuntimeError, match="矩形が一致"):
        subject.create_editable_xdw(image, PAGE, [dict(bbox_mm=dict(x=1, y=1, width=4, height=4))], target)
    assert not target.exists()


def test_existing_output_file_is_never_overwritten(tmp_path, api, image):
    target = tmp_path / "editable.xdw"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        subject.create_editable_xdw(image, PAGE, [], target)
    assert target.read_bytes() == b"keep"
    assert not api.opens


def test_racing_output_file_is_preserved(tmp_path, api, image, monkeypatch):
    target = tmp_path / "editable.xdw"
    commit = subject._commit_new_file

    def raced(temporary, output):
        output.write_bytes(b"another writer")
        commit(temporary, output)

    monkeypatch.setattr(subject, "_commit_new_file", raced)
    with pytest.raises(FileExistsError):
        subject.create_editable_xdw(image, PAGE, [], target)
    assert target.read_bytes() == b"another writer"


def test_split_preserves_source_and_each_range_and_nonrectangle(tmp_path, api):
    text = dict(kind=int(AnnotationType.TEXT), children=[])
    path = write_xdw(tmp_path / "edited.xdw", rectangles=[rectangle(), rectangle(x=11)], extra=[text])
    before = path.read_bytes()
    output = tmp_path / "項目一式"
    result = subject.split_template(path, output)
    assert [p.name for p in sorted(output.iterdir())] == ["項目_001.xdw", "項目_002.xdw"]
    assert [r["rect_mm"]["x"] for r in result] == [2, 11]
    for record in result:
        inspected = subject.inspect_template(record["path"])
        assert inspected == record
        assert len(json.loads(Path(record["path"]).read_text())["annotations"]) == 2
    assert before == path.read_bytes()
    assert all(opened != path for opened, _, _ in api.opens)


def test_split_save_failure_publishes_no_partial_folder(tmp_path, api):
    path = write_xdw(tmp_path / "edited.xdw", rectangles=[rectangle(), rectangle(x=11)])
    before = path.read_bytes()
    output = tmp_path / "fields"
    api.fail_save_at = 2
    with pytest.raises(RuntimeError, match="injected"):
        subject.split_template(path, output)
    assert not output.exists()
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".s*"))


def test_split_rejects_empty_or_existing_folder(tmp_path, api):
    path = write_xdw(tmp_path / "edited.xdw", rectangles=[])
    output = tmp_path / "fields"
    with pytest.raises(ValueError, match="ありません"):
        subject.split_template(path, output)
    assert not output.exists()
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        subject.split_template(path, output)
    assert (output / "keep.txt").read_text() == "keep"


def test_sdk_path_limit_counts_utf16_units():
    with pytest.raises(ValueError, match="255"):
        subject._sdk_path(Path("x" + "\U0001f600" * 128))


def test_overlong_temp_rejected_before_loading_sdk_or_creating_output(tmp_path, image, monkeypatch):
    monkeypatch.setattr(subject.tempfile, "gettempdir", lambda: str(tmp_path / ("t" * 230)))
    monkeypatch.setattr(subject, "_load_api", lambda *_: pytest.fail("SDK loaded before path preflight"))
    target = tmp_path / "new" / "editable.xdw"
    with pytest.raises(ValueError, match="255"):
        subject.create_editable_xdw(image, PAGE, [], target)
    assert not target.parent.exists()
    source = write_xdw(tmp_path / "source.xdw")
    with pytest.raises(ValueError, match="255"):
        subject.inspect_template(source)
    with pytest.raises(ValueError, match="255"):
        subject.split_template(source, tmp_path / "new" / "fields")
    assert not target.parent.exists()


def test_new_output_length_rejected_before_loading_sdk(tmp_path, image, monkeypatch):
    monkeypatch.setattr(subject, "_load_api", lambda *_: pytest.fail("SDK loaded before output preflight"))
    target = tmp_path / ("d" * 150) / ("n" * 120 + ".xdw")
    with pytest.raises(ValueError, match="240"):
        subject.create_editable_xdw(image, PAGE, [], target)
    assert not target.parent.exists()


def test_new_intermediate_names_stay_short(tmp_path, api, image):
    editable = tmp_path / "edited.xdw"
    subject.create_editable_xdw(image, PAGE,
        [dict(bbox_mm=dict(x=1, y=1, width=5, height=5)),
         dict(bbox_mm=dict(x=10, y=8, width=5, height=5))], editable)
    subject.split_template(editable, tmp_path / "fields")
    assert {p.name for p, _, _ in api.opens} == {"s.xdw", "e.xdw", "i1.xdw", "i2.xdw"}
    assert all(len(p.parent.name) == 9 and p.parent.name.startswith("x") for p, _, _ in api.opens)
    assert all(len(str(p).encode("utf-16-le")) // 2 <= 255 for p, _, _ in api.opens)


def test_import_does_not_load_core_or_models():
    source = str(Path(__file__).resolve().parents[1] / "source")
    result = subprocess.run([sys.executable, "-B", "-c",
        f"import sys; sys.path.insert(0, {source!r}); import drawing_roi_ocr.templates; "
        "assert 'docuworks_ctypes' not in sys.modules; "
        "assert 'paddle' not in sys.modules; assert 'paddleocr' not in sys.modules"],
        capture_output=True, text=True, env=os.environ.copy(), timeout=30)
    assert result.returncode == 0, result.stderr
