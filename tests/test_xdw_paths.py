"""Short native/staging names and preflight without Windows setting changes."""
import ctypes
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest
from docuworks_ctypes._raw import types as T
from docuworks_ctypes.encoding import decode_wchar_buffer

from drawing_roi_ocr import xdw_input
from drawing_roi_ocr.storage import path_units, require_short_path


def test_maximum_supported_root_leaves_room_for_sdk_and_staging(monkeypatch):
    # No E-drive directory is created: these are path-budget calculations.
    root = Path("E:/" + "r" * 37)
    assert path_units(root) == 40
    monkeypatch.setattr(xdw_input.tempfile, "gettempdir", lambda: str(root / "temp"))
    for name in ("p.bmp", "s.xdw", "b.bmp", "e.xdw", "i16382.xdw"):
        sdk_path = xdw_input._check_sdk_temp(name)
        assert path_units(sdk_path) <= 66
        assert path_units(sdk_path) < 255
    work = root / "data" / "template-work" / ("t" * 32)
    for path in (work / ".p00000000" / "f.xdw",
                 root / "templates" / ".s00000000" / "d" / "項目_16382.xdw"):
        assert require_short_path(path) == path.resolve()
        assert path_units(path) < 150


def test_non_bmp_characters_count_as_two_utf16_units(monkeypatch):
    base = Path("E:/" + "r" * 195 + "\U0001f600" * 20)
    monkeypatch.setattr(xdw_input.tempfile, "gettempdir", lambda: str(base))
    assert len(str(base)) < path_units(base)
    with pytest.raises(ValueError, match="255"):
        xdw_input._check_sdk_temp("i16382.xdw")


def test_overlong_native_input_rejected_before_sdk_load(monkeypatch):
    source = Path("E:/" + "p" * 125 + "/" + "n" * 125 + ".xdw")
    monkeypatch.setattr(xdw_input, "_load_api", lambda *_: pytest.fail("SDK loaded before input preflight"))
    with pytest.raises(ValueError, match="255"):
        with xdw_input.XdwInput(source):
            pass


def open_fake(tmp_path):
    source = tmp_path / "source.xdw"
    source.write_bytes(b"synthetic placeholder")
    calls = []

    class Raw:
        def XDW_GetPageInformation(self, handle, number, pointer):
            calls.append("inspect")
            info = ctypes.cast(pointer, ctypes.POINTER(T.XDW_PAGE_INFO_EX)).contents
            info.nWidth, info.nHeight, info.nDegree = 2540, 1270, 0
            return 0

        def XDW_ConvertPageToImageFileW(self, handle, number, output, options):
            destination = Path(decode_wchar_buffer(output))
            calls.append(destination)
            with Image.new("RGB", (300, 150), "white") as bitmap:
                bitmap.save(destination, format="BMP", dpi=(300, 300))
            return 0

    document = SimpleNamespace(page_count=1, raw=Raw(), handle=object(), close=lambda: None)
    api = SimpleNamespace(open_document=lambda *a, **k: document)
    return xdw_input.XdwInput(source, api_factory=lambda: api), calls


def test_overlong_temp_rejected_before_page_native_call(tmp_path, monkeypatch):
    document, calls = open_fake(tmp_path)
    monkeypatch.setattr(xdw_input.tempfile, "gettempdir", lambda: str(tmp_path / ("t" * 230)))
    with document:
        with pytest.raises(ValueError, match="255"):
            document.render(1, tmp_path / "new" / "p.png")
    assert calls == []
    assert not (tmp_path / "new").exists()


def test_overlong_png_target_rejected_before_page_native_call(tmp_path):
    document, calls = open_fake(tmp_path)
    with document:
        with pytest.raises(ValueError, match="240"):
            document.render(1, tmp_path / ("t" * 200) / ("n" * 60 + ".png"))
    assert calls == []


def test_native_render_uses_short_paths_and_preserves_source(tmp_path, monkeypatch):
    document, calls = open_fake(tmp_path)
    short_temp = tmp_path / "temp"
    short_temp.mkdir()
    monkeypatch.setattr(xdw_input.tempfile, "gettempdir", lambda: str(short_temp))
    before = document.path.read_bytes()
    output = tmp_path / "images" / "p.png"
    with document:
        result = document.render(1, output)
    assert result["width_px"] == 300 and result["height_px"] == 150
    native_paths = [p for p in calls if isinstance(p, Path)]
    assert len(native_paths) == 1
    assert native_paths[0].name == "p.bmp"
    assert len(native_paths[0].parent.name) == 9
    assert native_paths[0].parent.name.startswith("x")
    assert path_units(native_paths[0]) <= 255
    assert output.is_file() and document.path.read_bytes() == before
    assert not list(output.parent.glob(".p*"))
