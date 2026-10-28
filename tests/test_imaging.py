"""Pixel, geometry and de-identification tools against synthetic series with known geometry."""

import io
import shutil

import numpy as np
import pytest
from PIL import Image
from pydicom import dcmread
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, ImplicitVRLittleEndian, generate_uid

from dicom_agent_mcp.config import Settings
from dicom_agent_mcp.errors import DicomMcpError
from dicom_agent_mcp.service import DicomService

ROWS, COLS = 32, 40
SPACING = (0.8, 0.6)  # between rows, between columns
SLICE_SPACING = 2.0
SLICES = 20
STUDY = "1.2.826.0.1.3680043.8.498.77"
SERIES = "1.2.826.0.1.3680043.8.498.77.1"
FOR = "1.2.826.0.1.3680043.8.498.77.9"


def make_slice(k, *, iop=(1, 0, 0, 0, 1, 0), position=None, series=SERIES, sop=None, instance=None):
    """Axial CT slice k: HU = 10*k everywhere, plus a 5x10 block of 500 HU at rows 10-14, columns 20-29."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop or generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = STUDY, series, FOR
    ds.Modality = "CT"
    ds.PatientName, ds.PatientID, ds.PatientBirthDate, ds.PatientSex = "Doe^Jane", "MRN123", "19700101", "F"
    ds.StudyDate, ds.SeriesDate, ds.AccessionNumber = "20240315", "20240315", "ACC42"
    ds.InstitutionName, ds.ReferringPhysicianName = "General Hospital", "House^Gregory"
    ds.SeriesDescription = "Jane Doe chest"
    ds.InstanceNumber = instance if instance is not None else k + 1
    ds.ImagePositionPatient = list(position) if position is not None else [-12.0, -12.8, -100.0 + SLICE_SPACING * k]
    ds.ImageOrientationPatient = list(iop)
    ds.PixelSpacing = list(SPACING)
    ds.SliceThickness = SLICE_SPACING
    ds.Rows, ds.Columns = ROWS, COLS
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.RescaleIntercept, ds.RescaleSlope, ds.RescaleType = -1024, 1, "HU"
    pixels = np.full((ROWS, COLS), 1024 + 10 * k, dtype=np.uint16)
    pixels[10:15, 20:30] = 1024 + 500
    ds.PixelData = pixels.tobytes()
    ds.add_new(0x00090010, "LO", "ACME")
    ds.add_new(0x00091001, "LO", "secret private value")
    return ds


def write_series(folder, slices):
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, ds in enumerate(slices):
        path = folder / f"slice{i:03d}.dcm"
        ds.save_as(path, enforce_file_format=True)
        paths.append(path)
    return paths


@pytest.fixture
def local(tmp_path):
    inbox = tmp_path / "inbox"
    write_series(inbox / "axial", [make_slice(k) for k in range(SLICES)])
    settings = Settings.model_validate({"storage_dir": str(tmp_path / "cache"), "allowed_paths": [str(inbox)], "nodes": {}})
    return DicomService(settings), inbox


def decode(png):
    return np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=float)


# ---- render_image ----------------------------------------------------------------------------------


def test_render_image_corrects_aspect_and_labels_edges(local):
    service, inbox = local
    png, info = service.render_image(str(inbox / "axial" / "slice005.dcm"), window="soft_tissue")
    # 40 columns x 0.6 mm = 24 mm wide; 32 rows x 0.8 mm = 25.6 mm high -> 40 x 43 display pixels.
    assert Image.open(io.BytesIO(png)).size == (40, 43) == tuple(info["png_size"])
    assert info["edges"] == {"left": "R", "right": "L", "top": "A", "bottom": "P"}
    assert info["window"] == {"center": 50, "width": 350, "source": "preset:soft_tissue", "units": "HU"}
    assert info["geometry"]["orientation"] == "axial"
    assert info["input_fingerprint"].startswith("sha256:")
    assert (service.settings.storage_dir / "renders").exists() and info["png_path"].endswith(".png")


def test_render_image_real_ct_uses_file_window(settings_ct):
    service, path = settings_ct
    png, info = service.render_image(str(path))
    assert info["window"]["source"] == "percentile"  # CT_small.dcm has no WindowCenter/Width
    assert Image.open(io.BytesIO(png)).size == (128, 128)

    ds = dcmread(path)
    ds.WindowCenter = [40, 400]
    ds.WindowWidth = [400, 2000]
    ds.save_as(path)
    _, info = service.render_image(str(path))
    assert info["window"]["source"] == "file"
    assert info["window"]["center"] == 40 and info["window"]["width"] == 400


@pytest.fixture
def settings_ct(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(get_testdata_file("CT_small.dcm"), inbox / "ct.dcm")
    settings = Settings.model_validate({"storage_dir": str(tmp_path / "cache"), "allowed_paths": [str(inbox)], "nodes": {}})
    return DicomService(settings), inbox / "ct.dcm"


def test_fingerprint_survives_transfer_syntax_change(local):
    service, inbox = local
    original = inbox / "axial" / "slice003.dcm"
    ds = dcmread(original)
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    copy = inbox / "implicit.dcm"
    ds.save_as(copy, enforce_file_format=True)
    a = service.pixel_statistics(str(original))["input_fingerprint"]
    b = service.pixel_statistics(str(copy))["input_fingerprint"]
    c = service.pixel_statistics(str(inbox / "axial" / "slice004.dcm"))["input_fingerprint"]
    assert a == b != c


def test_render_rejects_paths_outside_sandbox(local, tmp_path):
    service, _ = local
    outside = tmp_path / "elsewhere.dcm"
    make_slice(0).save_as(outside, enforce_file_format=True)
    with pytest.raises(DicomMcpError, match="outside the allowed"):
        service.render_image(str(outside))


def test_frame_out_of_range(local):
    service, inbox = local
    with pytest.raises(DicomMcpError, match="out of range"):
        service.render_image(str(inbox / "axial" / "slice000.dcm"), frame=3)


# ---- statistics and measurement --------------------------------------------------------------------


def test_pixel_statistics_whole_frame_and_roi(local):
    service, inbox = local
    path = str(inbox / "axial" / "slice007.dcm")
    whole = service.pixel_statistics(path)
    assert whole["units"] == "HU" and whole["pixel_count"] == ROWS * COLS
    assert whole["min"] == 70 and whole["max"] == 500

    roi = service.pixel_statistics(path, roi={"shape": "rectangle", "row": 10, "column": 20, "height": 5, "width": 10})
    assert roi["pixel_count"] == 50 and roi["mean"] == 500 and roi["std"] == 0
    assert roi["area_mm2"] == pytest.approx(50 * 0.8 * 0.6)

    # The same box in mm: rows 10-14 -> 8-12 mm, columns 20-29 -> 12-18 mm.
    mm = service.pixel_statistics(path, roi={"shape": "rectangle", "row": 8, "column": 12, "height": 4, "width": 6, "units": "mm"})
    assert mm["pixel_count"] == 50 and mm["mean"] == 500

    ellipse = service.pixel_statistics(path, roi={"shape": "ellipse", "row": 10, "column": 20, "height": 5, "width": 10})
    assert 0 < ellipse["pixel_count"] < 50 and ellipse["mean"] == 500


def test_roi_outside_image(local):
    service, inbox = local
    with pytest.raises(DicomMcpError, match="does not cover"):
        service.pixel_statistics(str(inbox / "axial" / "slice000.dcm"), roi={"shape": "rectangle", "row": 100, "column": 100, "height": 5, "width": 5})


def test_measure_distance_uses_pixel_spacing(local):
    service, inbox = local
    path = str(inbox / "axial" / "slice000.dcm")
    vertical = service.measure_distance(path, [0, 0], [10, 0])
    assert vertical["distance_px"] == 10 and vertical["distance_mm"] == pytest.approx(8.0)
    horizontal = service.measure_distance(path, [0, 0], [0, 10])
    assert horizontal["distance_mm"] == pytest.approx(6.0)
    assert horizontal["start"]["patient_mm"] == [-12.0, -12.8, -100.0]
    assert horizontal["end"]["patient_mm"] == [-6.0, -12.8, -100.0]


# ---- check_geometry --------------------------------------------------------------------------------


def codes(result):
    return {issue["code"] for series in result["series"] for issue in series["issues"]}


def test_check_geometry_clean_series(local):
    service, inbox = local
    result = service.check_geometry([str(inbox / "axial")])
    assert result["ok"]
    series = result["series"][0]
    assert series["orientation"] == "axial" and series["volume_ready"]
    assert series["slice_spacing_mm"]["median"] == pytest.approx(SLICE_SPACING)
    assert series["extent_mm"] == pytest.approx(SLICE_SPACING * (SLICES - 1))
    assert series["frames"] == SLICES


def test_check_geometry_detects_missing_and_duplicate_slices(local, tmp_path):
    service, inbox = local
    slices = [make_slice(k) for k in range(SLICES) if k not in (8, 9)] + [make_slice(3)]
    write_series(inbox / "broken", slices)
    result = service.check_geometry([str(inbox / "broken")])
    assert not result["ok"]
    assert {"MISSING_SLICES", "DUPLICATE_POSITIONS"} <= codes(result)
    message = next(i["message"] for i in result["series"][0]["issues"] if i["code"] == "MISSING_SLICES")
    assert "About 2 slice(s)" in message


def test_check_geometry_detects_localizer_and_tilt(local):
    service, inbox = local
    mixed = [make_slice(k) for k in range(5)] + [make_slice(0, iop=(1, 0, 0, 0, 0, -1), position=(0, 0, 0))]
    write_series(inbox / "mixed", mixed)
    assert "MIXED_ORIENTATION" in codes(service.check_geometry([str(inbox / "mixed")]))

    tilted = [make_slice(k, position=(-12.0, -12.8 + 0.5 * k, -100.0 + SLICE_SPACING * k)) for k in range(10)]
    write_series(inbox / "tilted", tilted)
    result = service.check_geometry([str(inbox / "tilted")])
    assert "SHEARED_STACK" in codes(result) and result["ok"]  # a warning, not an error


def test_check_geometry_reports_instance_order(local):
    service, inbox = local
    write_series(inbox / "shuffled", [make_slice(k, instance=(k * 7) % 10 + 1) for k in range(10)])
    assert "INSTANCE_ORDER_MISMATCH" in codes(service.check_geometry([str(inbox / "shuffled")]))


# ---- render_mpr ------------------------------------------------------------------------------------


def test_render_mpr_coronal_orientation_and_aspect(local):
    service, inbox = local
    png, info = service.render_mpr([str(inbox / "axial")], plane="coronal", window_center=95, window_width=200)
    # 20 slices x 2 mm high, 40 columns x 0.6 mm wide -> 40 x 67 display pixels.
    assert Image.open(io.BytesIO(png)).size == (40, 67)
    assert info["edges"] == {"left": "R", "right": "L", "top": "S", "bottom": "I"}
    assert info["count"] == ROWS and info["index"] == ROWS // 2
    image = decode(png)
    assert image[1].mean() > image[-2].mean()  # superior slices (higher HU here) are at the top


def test_render_mpr_sagittal_and_axial(local):
    service, inbox = local
    _, sagittal = service.render_mpr([str(inbox / "axial")], plane="sagittal", index=25)
    assert sagittal["edges"] == {"left": "A", "right": "P", "top": "S", "bottom": "I"}
    assert sagittal["position_mm"] == {"x": pytest.approx(-12.0 + 25 * 0.6)}
    _, axial = service.render_mpr([str(inbox / "axial")], plane="axial", index=0)
    assert axial["position_mm"] == {"z": -100.0}


def test_render_mpr_refuses_broken_series(local):
    service, inbox = local
    write_series(inbox / "gap", [make_slice(k) for k in range(SLICES) if k != 10])
    with pytest.raises(DicomMcpError, match="MISSING_SLICES"):
        service.render_mpr([str(inbox / "gap")])


def test_render_mpr_needs_series_choice(local):
    service, inbox = local
    write_series(inbox / "other", [make_slice(k, series="1.2.826.0.1.3680043.8.498.77.2") for k in range(3)])
    with pytest.raises(DicomMcpError, match="pass series_uid"):
        service.render_mpr([str(inbox)])
    _, info = service.render_mpr([str(inbox)], series_uid=SERIES)
    assert info["series_uid"] == SERIES


# ---- deidentify ------------------------------------------------------------------------------------


def test_deidentify_series(local, monkeypatch):
    service, inbox = local
    monkeypatch.setenv("DICOM_AGENT_MCP_DEID_KEY", "test-secret")
    result = service.deidentify([str(inbox / "axial")])
    assert result["count"] == SLICES and result["linkable"] and result["date_mode"] == "shift"
    outputs = [dcmread(f["output"]) for f in result["files"]]
    originals = [dcmread(f["source"]) for f in result["files"]]
    first = outputs[0]

    assert str(first.PatientName).startswith("ANON^") and first.PatientID.startswith("ANON")
    assert "MRN123" not in str(first) and "Doe" not in str(first) and "General Hospital" not in str(first)
    assert first.AccessionNumber == "" and first.ReferringPhysicianName == "" and first.SeriesDescription == ""
    assert "InstitutionName" not in first and (0x0009, 0x1001) not in first
    assert first.PatientSex == "F" and first.PatientIdentityRemoved == "YES"
    assert {c.CodeValue for c in first.DeidentificationMethodCodeSequence} == {"113100", "113108", "113107"}

    assert len({o.StudyInstanceUID for o in outputs}) == 1 and outputs[0].StudyInstanceUID != STUDY
    assert len({o.FrameOfReferenceUID for o in outputs}) == 1 and outputs[0].FrameOfReferenceUID != FOR
    assert len({o.SOPInstanceUID for o in outputs}) == SLICES
    assert first.SOPClassUID == CTImageStorage and first.file_meta.MediaStorageSOPInstanceUID == first.SOPInstanceUID
    assert first.StudyDate != "20240315" and len({o.StudyDate for o in outputs}) == 1
    for original, output in zip(originals, outputs):
        assert original.PixelData == output.PixelData

    again = service.deidentify([str(inbox / "axial" / "slice000.dcm")])
    assert dcmread(again["files"][0]["output"]).StudyInstanceUID == first.StudyInstanceUID


def test_deidentify_without_key_is_not_linkable(local, monkeypatch):
    service, inbox = local
    monkeypatch.delenv("DICOM_AGENT_MCP_DEID_KEY", raising=False)
    a = service.deidentify([str(inbox / "axial" / "slice000.dcm")], date_mode="keep", keep_descriptions=True)
    b = service.deidentify([str(inbox / "axial" / "slice000.dcm")], date_mode="keep")
    da, db = dcmread(a["files"][0]["output"]), dcmread(b["files"][0]["output"])
    assert not a["linkable"] and da.StudyInstanceUID != db.StudyInstanceUID
    assert da.StudyDate == "20240315" and da.SeriesDescription == "Jane Doe chest"
    assert any("descriptions" in w for w in a["warnings"])
