"""Marker correctness and all-or-nothing publication without a native DLL."""
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from docuworks_ctypes import AnnotationType, AuthMode, OpenMode, Color
from docuworks_ctypes._raw import types as T

from drawing_roi_ocr import markers_xdw as subject


BOX = dict(x=2, y=3, width=10, height=6)


def save_document(path, *, pages=2, degree=0):
    data = {"pages": [dict(width_mm=40, height_mm=30, source_rotation=degree,
        annotations=[dict(kind=int(AnnotationType.TEXT), x=1, y=1, width=5, height=3)])
        for _ in range(pages)]}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class Annotation:
    def __init__(self, data):
        self.data = data

    @property
    def annotation_type(self):
        return self.data["kind"]

    @property
    def position(self):
        return SimpleNamespace(x=self.data["x"], y=self.data["y"])

    @property
    def size(self):
        return SimpleNamespace(width=self.data["width"], height=self.data["height"])

    def get_standard_attribute(self, key):
        if key == "%Points":
            return tuple(SimpleNamespace(x=p[0], y=p[1]) for p in self.data["points"])
        return self.data["attributes"][key]


class Page:
    def __init__(self, document, number):
        self.document = document
        self.data = document.data["pages"][number-1]

    def annotations(self, *, recursive):
        assert recursive is False
        return tuple(Annotation(data) for data in self.data["annotations"])

    def add_marker(self, points, *, border_color, border_width, border_transparent):
        assert self.document.mode == OpenMode.UPDATE
        data = dict(kind=int(AnnotationType.MARKER), x=min(p.x for p in points),
                    y=min(p.y for p in points), width=abs(points[1].x-points[0].x), height=1,
                    points=[(p.x, p.y) for p in points], attributes={"%BorderColor": border_color,
                    "%BorderWidth": border_width, "%BorderTransparent": border_transparent})
        self.data["annotations"].append(data)


class Raw:
    def XDW_GetPageInformation(self, document, number, pointer):
        data = document.data["pages"][number-1]
        info = ctypes.cast(pointer, ctypes.POINTER(T.XDW_PAGE_INFO_EX)).contents
        info.nWidth, info.nHeight = round(data["width_mm"]*100), round(data["height_mm"]*100)
        info.nDegree = data["source_rotation"]
        return 0


class Document:
    def __init__(self, api, path, mode):
        self.api, self.path, self.mode = api, Path(path), mode
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.raw, self.handle = Raw(), self

    @property
    def page_count(self):
        return len(self.data["pages"])

    def page(self, number):
        return Page(self, number)

    def save(self):
        assert self.mode == OpenMode.UPDATE
        if self.api.save_hook:
            self.api.save_hook(self)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.api.closes += 1


class Api:
    def __init__(self):
        self.opens, self.closes = [], 0
        self.save_hook = None

    def open_document(self, path, *, mode, auth):
        assert auth == AuthMode.NO_DIALOG
        self.opens.append((Path(path), mode))
        return Document(self, path, mode)


@pytest.fixture
def case(tmp_path, monkeypatch):
    api = Api()
    monkeypatch.setattr(subject, "_load_api", lambda: api)
    source = save_document(tmp_path / "original.xdw")
    return tmp_path, source, tmp_path / "new.xdw", api


def silent(*a, **k):
    pass


@pytest.mark.parametrize("degree", [0, 90, 180, 270])
def test_display_coordinates_are_not_rotated_twice_and_all_pages_preserved(case, degree):
    root, source, output, api = case
    save_document(source, pages=3, degree=degree)
    before = source.read_bytes()
    result = subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert result["rectangle_count"] == 1 and result["marker_count"] == 2
    assert result["page_count"] == 3
    assert all(page["output_rotation"] == degree for page in result["pages"].values())
    assert all(page["source_rotation"] == degree for page in result["source_pages"].values())
    assert result["source_sha256"] == subject.sha(source)
    assert result["output_sha256"] == subject.sha(output)
    assert source.read_bytes() == before
    after = json.loads(output.read_text())
    assert len(after["pages"][0]["annotations"]) == 3
    assert len(after["pages"][1]["annotations"]) == len(after["pages"][2]["annotations"]) == 1
    for annotation, stroke in zip(after["pages"][0]["annotations"][1:], result["verified_markers"]):
        assert annotation["kind"] == int(AnnotationType.MARKER)
        assert annotation["attributes"]["%BorderColor"] == int(Color.YELLOW)
        assert annotation["attributes"]["%BorderTransparent"] is True
        assert annotation["points"] == [list(p) for p in stroke["points"]]
    assert all(path != source for path, _ in api.opens)
    assert api.closes == len(api.opens)


@pytest.mark.parametrize("rectangles", [
    {3: [BOX]}, {True: [BOX]}, {1: [dict(BOX, x=-.01)]},
    {1: [dict(BOX, width=0)]}, {1: [dict(BOX, y=float("nan"))]},
    {1: [dict(BOX, height=float("inf"))]}, {1: [dict(BOX, x=39)]},
    {1: [BOX], 2: [dict(BOX, y=29)]}, {}, {1: []}, {1: "wrong"},
    {1: [dict(BOX, width=.00001)]},
])
def test_every_range_validated_before_any_writable_open(case, rectangles):
    root, source, output, api = case
    before = source.read_bytes()
    with pytest.raises(ValueError):
        subject.annotate_document(root, source, output, rectangles, progress=silent)
    assert not output.exists() and source.read_bytes() == before
    assert all(mode == OpenMode.READONLY for _, mode in api.opens)


def test_invalid_rotation_rejected_without_writing(case):
    root, source, output, api = case
    save_document(source, degree=45)
    with pytest.raises(ValueError, match="回転"):
        subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert not output.exists()
    assert all(mode == OpenMode.READONLY for _, mode in api.opens)


@pytest.mark.parametrize("failure", ["save", "count", "type", "color", "points", "transparent", "existing", "page"])
def test_save_or_reopen_mismatch_never_publishes(case, failure):
    root, source, output, api = case
    before = source.read_bytes()

    def corrupt(document):
        if failure == "save":
            raise RuntimeError("injected native save failure")
        page = document.data["pages"][0]
        marker = page["annotations"][-1]
        if failure == "count":
            page["annotations"].pop()
        elif failure == "type":
            marker["kind"] = int(AnnotationType.RECTANGLE)
        elif failure == "color":
            marker["attributes"]["%BorderColor"] = int(Color.RED)
        elif failure == "points":
            marker["points"][0] = (9, 9)
        elif failure == "transparent":
            marker["attributes"]["%BorderTransparent"] = False
        elif failure == "existing":
            page["annotations"][0]["x"] = 2
        elif failure == "page":
            page["width_mm"] = 41

    api.save_hook = corrupt
    with pytest.raises(RuntimeError):
        subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert not output.exists() and source.read_bytes() == before
    assert not list((root / "temp").glob("x*"))


def test_existing_destination_is_preserved(case):
    root, source, output, api = case
    output.write_bytes(b"existing result")
    with pytest.raises(FileExistsError):
        subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert output.read_bytes() == b"existing result" and not api.opens


def test_destination_created_during_commit_is_preserved(case, monkeypatch):
    root, source, output, _ = case
    commit = subject._commit_new_file

    def raced(staged, target):
        target.write_bytes(b"another writer")
        commit(staged, target)

    monkeypatch.setattr(subject, "_commit_new_file", raced)
    with pytest.raises(FileExistsError):
        subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert output.read_bytes() == b"another writer"


def test_source_change_is_detected_before_publication(case):
    root, source, output, api = case
    api.save_hook = lambda _: source.write_bytes(b"changed externally")
    with pytest.raises(RuntimeError, match="原本文書"):
        subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert not output.exists()


def test_pdf_conversion_preserves_unmarked_pages_and_validates_before_conversion(case, monkeypatch):
    root, _, output, _ = case
    source = root / "original.pdf"
    source.write_bytes(b"synthetic PDF placeholder")
    dimensions = {number: dict(width_mm=40, height_mm=30, source_rotation=(number-1)*90)
                  for number in range(1, 5)}
    monkeypatch.setattr(subject, "_pdf_dimensions", lambda _: dimensions)
    converted = []

    def convert(api, source, folder, metadata, progress):
        converted.append(source)
        return save_document(folder / "d.xdw", pages=len(metadata))

    monkeypatch.setattr(subject, "_pdf_to_xdw", convert)
    with pytest.raises(ValueError):
        subject.annotate_document(root, source, output, {4: [dict(BOX, x=39)]}, progress=silent)
    assert not converted and not output.exists()
    result = subject.annotate_document(root, source, output, {4: [BOX]}, progress=silent)
    assert result["page_count"] == 4 and result["source_format"] == "pdf"
    assert result["markers_by_page"] == {4: 2}
    assert [page["source_rotation"] for page in result["source_pages"].values()] == [0, 90, 180, 270]
    assert [page["output_rotation"] for page in result["pages"].values()] == [0, 0, 0, 0]
    assert len(converted) == 1


def test_tall_ranges_use_multiple_viewer_editable_width_strokes():
    planned = subject._strokes({1: [dict(x=2, y=3, width=10, height=30)]})
    assert len(planned) == 7
    assert all(1 <= stroke["width_pt"] <= 13 for stroke in planned)
    assert all(stroke["points"][0][0] < stroke["points"][1][0] for stroke in planned)


def test_excessive_stroke_plan_rejected_before_update(case, monkeypatch):
    root, source, output, api = case
    monkeypatch.setattr(subject, "MAX_STROKES", 1)
    with pytest.raises(ValueError, match="1文書1個"):
        subject.annotate_document(root, source, output, {1: [BOX]}, progress=silent)
    assert not output.exists() and all(mode == OpenMode.READONLY for _, mode in api.opens)
