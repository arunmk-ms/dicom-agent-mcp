"""Pixel and geometry operations on local DICOM files.

Pixel data stays on the server. Tools return numbers, findings and small PNG previews, never raw arrays.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image
from pydicom import Dataset, dcmread
from pydicom.errors import InvalidDicomError
from pydicom.pixels import apply_color_lut, apply_modality_lut

from .errors import DicomMcpError

# (center, width) in Hounsfield units.
WINDOW_PRESETS: dict[str, tuple[float, float]] = {
    "lung": (-600, 1500),
    "mediastinum": (40, 400),
    "soft_tissue": (50, 350),
    "abdomen": (40, 400),
    "liver": (60, 160),
    "bone": (400, 1800),
    "brain": (40, 80),
    "subdural": (75, 215),
    "stroke": (40, 40),
}
PROJECTION_MODALITIES = {"CR", "DX", "MG", "IO", "PX", "XA", "RF", "US", "ES", "XC", "OP", "SC", "OT", "DOC"}
Plane = Literal["axial", "coronal", "sagittal"]


# ---- reading -------------------------------------------------------------------------------------


def read_dataset(path: Path, *, pixels: bool = True) -> Dataset:
    try:
        return dcmread(path, stop_before_pixels=not pixels)
    except (InvalidDicomError, OSError) as exc:
        raise DicomMcpError(f"Not a readable DICOM file: {path}") from exc


def frame_count(ds: Dataset) -> int:
    return int(ds.get("NumberOfFrames", 1) or 1)


def is_color(ds: Dataset) -> bool:
    return int(ds.get("SamplesPerPixel", 1)) > 1 or ds.get("PhotometricInterpretation") == "PALETTE COLOR"


def stored_frame(ds: Dataset, frame: int = 0) -> np.ndarray:
    """Stored pixel values of one frame (color images come back as RGB)."""
    if "PixelData" not in ds:
        raise DicomMcpError("This instance has no pixel data (e.g. SR, PR or KO objects).")
    n = frame_count(ds)
    if not 0 <= frame < n:
        raise DicomMcpError(f"Frame {frame} is out of range; the instance has {n} frame(s) (0-based).")
    try:
        arr = ds.pixel_array
    except Exception as exc:  # pydicom raises a variety of decoder errors.
        ts = ds.file_meta.get("TransferSyntaxUID", "unknown")
        name = getattr(ts, "name", ts)
        raise DicomMcpError(
            f"Cannot decode pixel data ({name}): {exc}. "
            "For compressed images install the codecs: pip install 'dicom-agent-mcp[codecs]'."
        ) from exc
    return arr[frame] if n > 1 else arr


# ---- functional groups and geometry --------------------------------------------------------------


def _item(ds: Dataset | None, keyword: str, index: int = 0) -> Dataset | None:
    if ds is None:
        return None
    seq = ds.get(keyword)
    return seq[index] if seq and len(seq) > index else None


def _fg_value(ds: Dataset, frame: int, sequence: str, attr: str) -> Any:
    """Look up an attribute in the per-frame, then shared functional groups (enhanced multi-frame objects)."""
    for container in (_item(ds, "PerFrameFunctionalGroupsSequence", frame), _item(ds, "SharedFunctionalGroupsSequence")):
        inner = _item(container, sequence)
        if inner is not None and attr in inner:
            return inner[attr].value
    return None


def _value(ds: Dataset, frame: int, attr: str, sequence: str) -> Any:
    value = ds.get(attr)
    return value if value not in (None, "") else _fg_value(ds, frame, sequence, attr)


@dataclass
class FrameGeometry:
    rows: int
    columns: int
    spacing: tuple[float, float] | None = None  # (between rows, between columns) in mm
    spacing_source: str | None = None
    position: np.ndarray | None = None  # ImagePositionPatient of the first transmitted pixel
    row_cosine: np.ndarray | None = None  # direction of increasing column index
    column_cosine: np.ndarray | None = None  # direction of increasing row index
    slice_thickness: float | None = None
    frame_of_reference: str | None = None

    @property
    def has_plane(self) -> bool:
        return self.position is not None and self.row_cosine is not None and self.column_cosine is not None

    @property
    def normal(self) -> np.ndarray | None:
        if self.row_cosine is None or self.column_cosine is None:
            return None
        return np.cross(self.row_cosine, self.column_cosine)

    def to_patient(self, row: float, column: float) -> list[float] | None:
        if not self.has_plane or self.spacing is None:
            return None
        p = self.position + column * self.spacing[1] * self.row_cosine + row * self.spacing[0] * self.column_cosine
        return [round(float(v), 3) for v in p]

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"rows": self.rows, "columns": self.columns}
        if self.spacing:
            out["pixel_spacing_mm"] = list(self.spacing)
            out["pixel_spacing_source"] = self.spacing_source
        if self.normal is not None:
            label, obliquity = orientation_label(self.normal)
            out["orientation"] = label
            out["obliquity_deg"] = round(obliquity, 2)
        if self.slice_thickness:
            out["slice_thickness_mm"] = self.slice_thickness
        return out


def geometry(ds: Dataset, frame: int = 0) -> FrameGeometry:
    geo = FrameGeometry(rows=int(ds.get("Rows", 0)), columns=int(ds.get("Columns", 0)))
    spacing = ds.get("PixelSpacing")
    source = "PixelSpacing"
    if not spacing:
        spacing = _fg_value(ds, frame, "PixelMeasuresSequence", "PixelSpacing")
        source = "PixelMeasuresSequence"
    if not spacing and ds.get("ImagerPixelSpacing"):
        spacing = ds.ImagerPixelSpacing
        source = "ImagerPixelSpacing (detector plane; not corrected for magnification)"
    if spacing and len(spacing) == 2:
        geo.spacing = (float(spacing[0]), float(spacing[1]))
        geo.spacing_source = source
    position = _value(ds, frame, "ImagePositionPatient", "PlanePositionSequence")
    orientation = _value(ds, frame, "ImageOrientationPatient", "PlaneOrientationSequence")
    if position and len(position) == 3:
        geo.position = np.array([float(v) for v in position])
    if orientation and len(orientation) == 6:
        values = np.array([float(v) for v in orientation])
        geo.row_cosine, geo.column_cosine = values[:3], values[3:]
    thickness = _value(ds, frame, "SliceThickness", "PixelMeasuresSequence")
    geo.slice_thickness = float(thickness) if thickness not in (None, "") else None
    geo.frame_of_reference = str(ds.get("FrameOfReferenceUID", "")) or None
    return geo


def orientation_label(normal: np.ndarray) -> tuple[str, float]:
    """Closest standard plane for a slice normal, and the angle (degrees) away from it."""
    axis = int(np.argmax(np.abs(normal)))
    cosine = float(min(1.0, abs(normal[axis]) / (np.linalg.norm(normal) or 1.0)))
    obliquity = math.degrees(math.acos(cosine))
    label = ("sagittal", "coronal", "axial")[axis]
    return (label if obliquity < 10 else f"oblique (closest to {label})"), obliquity


def direction_letters(vector: np.ndarray) -> str:
    """Patient direction (LPS) a vector points to, e.g. 'L', 'P', 'LS' for oblique."""
    letters = (("R", "L"), ("A", "P"), ("I", "S"))
    order = np.argsort(-np.abs(vector))
    out = [letters[i][1 if vector[i] > 0 else 0] for i in order if abs(vector[i]) > 0.3]
    return "".join(out) or "?"


def edge_labels(right: np.ndarray | None, down: np.ndarray | None) -> dict[str, str] | None:
    if right is None or down is None:
        return None
    return {
        "left": direction_letters(-right), "right": direction_letters(right),
        "top": direction_letters(-down), "bottom": direction_letters(down),
    }


# ---- values, units, windowing --------------------------------------------------------------------


def modality_values(ds: Dataset, arr: np.ndarray, frame: int = 0) -> np.ndarray:
    """Convert stored values to modality values (e.g. Hounsfield units for CT)."""
    if "ModalityLUTSequence" in ds:
        return np.asarray(apply_modality_lut(arr, ds), dtype=np.float64)
    slope = _value(ds, frame, "RescaleSlope", "PixelValueTransformationSequence")
    intercept = _value(ds, frame, "RescaleIntercept", "PixelValueTransformationSequence")
    values = arr.astype(np.float64)
    if slope not in (None, "") and float(slope) != 1.0:
        values *= float(slope)
    if intercept not in (None, "") and float(intercept) != 0.0:
        values += float(intercept)
    return values


def value_units(ds: Dataset, frame: int = 0) -> str:
    rescale_type = _value(ds, frame, "RescaleType", "PixelValueTransformationSequence")
    if rescale_type:
        return str(rescale_type)
    modality = ds.get("Modality")
    if modality == "CT":
        return "HU"
    if modality == "PT" and ds.get("Units"):
        return str(ds.Units)
    return "stored value"


def resolve_window(
    ds: Dataset,
    values: np.ndarray,
    window: str = "auto",
    center: float | None = None,
    width: float | None = None,
    frame: int = 0,
) -> tuple[float, float, str]:
    if center is not None and width is not None:
        return float(center), max(float(width), 1.0), "explicit"
    if window in WINDOW_PRESETS:
        c, w = WINDOW_PRESETS[window]
        return c, w, f"preset:{window}"
    if window == "auto":
        c = _value(ds, frame, "WindowCenter", "FrameVOILUTSequence")
        w = _value(ds, frame, "WindowWidth", "FrameVOILUTSequence")
        if c not in (None, "") and w not in (None, ""):
            return float(_first(c)), max(float(_first(w)), 1.0), "file"
    if window not in ("auto", "minmax"):
        raise DicomMcpError(f"Unknown window '{window}'. Use auto, minmax or one of: {', '.join(WINDOW_PRESETS)}.")
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0, 1.0, "minmax"
    lo, hi = (np.percentile(finite, [0.5, 99.5]) if window == "auto" else (finite.min(), finite.max()))
    return float((lo + hi) / 2), max(float(hi - lo), 1.0), ("percentile" if window == "auto" else "minmax")


def _first(value: Any) -> Any:
    """First item of a multi-valued attribute (WindowCenter may hold several windows)."""
    return value[0] if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else value


def apply_window(values: np.ndarray, center: float, width: float) -> np.ndarray:
    """DICOM linear VOI function (PS3.3 C.11.2.1.2.1) mapped to 8 bits."""
    scaled = ((values - (center - 0.5)) / max(width - 1, 1.0) + 0.5) * 255.0
    return np.clip(scaled, 0, 255).astype(np.uint8)


def to_png(image: np.ndarray, spacing: tuple[float, float] | None, max_size: int) -> tuple[bytes, tuple[int, int]]:
    """Encode 8-bit grey or RGB pixels, correcting for non-square pixels and fitting into max_size."""
    pil = Image.fromarray(image)
    width, height = pil.size
    if spacing and spacing[0] > 0 and spacing[1] > 0:
        height = height * spacing[0] / spacing[1]
    scale = min(1.0, max_size / max(width, height))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    if size != pil.size:
        pil = pil.resize(size, Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.BILINEAR)
    buffer = io.BytesIO()
    pil.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), size


def _color_to_uint8(ds: Dataset, arr: np.ndarray) -> np.ndarray:
    if ds.get("PhotometricInterpretation") == "PALETTE COLOR":
        arr = apply_color_lut(arr, ds)
        return (arr >> 8).astype(np.uint8) if arr.dtype.itemsize > 1 else arr.astype(np.uint8)
    bits = int(ds.get("BitsStored", 8))
    if bits > 8:
        arr = arr >> (bits - 8)
    return np.clip(arr, 0, 255).astype(np.uint8)


# ---- fingerprints --------------------------------------------------------------------------------


def _geometry_key(geo: FrameGeometry) -> list[Any]:
    def r(v: Any) -> Any:
        return None if v is None else [round(float(x), 4) for x in np.ravel(v)]
    return [geo.rows, geo.columns, r(geo.spacing), r(geo.position), r(geo.row_cosine), r(geo.column_cosine)]


def pixel_fingerprint(arr: np.ndarray, geo: FrameGeometry) -> str:
    """Content hash over decoded pixel values and geometry.

    It is independent of transfer syntax and file wrapping, so a tool can echo it back to prove which
    pixels it processed (the input-fingerprint step of the verification loop).
    """
    digest = hashlib.sha256()
    data = np.ascontiguousarray(arr)
    digest.update(f"{data.dtype.str}|{data.shape}|".encode())
    digest.update(data.tobytes())
    digest.update(json.dumps(_geometry_key(geo)).encode())
    return "sha256:" + digest.hexdigest()


# ---- single-image operations ---------------------------------------------------------------------


def _identity(ds: Dataset) -> dict[str, Any]:
    return {k: str(ds.get(k)) for k in ("SOPInstanceUID", "SeriesInstanceUID", "Modality") if ds.get(k)}


def render_frame(
    ds: Dataset,
    frame: int = 0,
    window: str = "auto",
    center: float | None = None,
    width: float | None = None,
    max_size: int = 512,
) -> tuple[bytes, dict[str, Any]]:
    arr = stored_frame(ds, frame)
    geo = geometry(ds, frame)
    info: dict[str, Any] = {**_identity(ds), "frame": frame, "frames": frame_count(ds)}
    if is_color(ds):
        image = _color_to_uint8(ds, arr)
        info["window"] = None
    else:
        values = modality_values(ds, arr, frame)
        c, w, source = resolve_window(ds, values, window, center, width, frame)
        image = apply_window(values, c, w)
        if ds.get("PhotometricInterpretation") == "MONOCHROME1":
            image = 255 - image
        units = value_units(ds, frame)
        info["window"] = {"center": c, "width": w, "source": source, "units": units}
        if source.startswith("preset") and units != "HU":
            info["warning"] = f"Window presets assume Hounsfield units, but this image is in '{units}'."
    png, size = to_png(image, geo.spacing, max_size)
    info.update(
        geometry=geo.summary(),
        edges=edge_labels(geo.row_cosine, geo.column_cosine),
        png_size=list(size),
        input_fingerprint=pixel_fingerprint(arr, geo),
    )
    return png, info


@dataclass
class Roi:
    shape: Literal["rectangle", "ellipse"]
    row: float
    column: float
    height: float
    width: float
    units: Literal["pixel", "mm"] = "pixel"


def roi_mask(shape: tuple[int, int], roi: Roi, spacing: tuple[float, float] | None) -> np.ndarray:
    row, column, height, width = roi.row, roi.column, roi.height, roi.width
    if roi.units == "mm":
        if spacing is None:
            raise DicomMcpError("The ROI is in mm but the image has no pixel spacing; use pixel units.")
        row, height = row / spacing[0], height / spacing[0]
        column, width = column / spacing[1], width / spacing[1]
    if height <= 0 or width <= 0:
        raise DicomMcpError("ROI height and width must be positive.")
    rr, cc = np.mgrid[0 : shape[0], 0 : shape[1]]
    centers_r, centers_c = rr + 0.5, cc + 0.5  # pixel centers
    if roi.shape == "rectangle":
        mask = (centers_r >= row) & (centers_r < row + height) & (centers_c >= column) & (centers_c < column + width)
    else:
        cr, cc0 = row + height / 2, column + width / 2
        mask = ((centers_r - cr) / (height / 2)) ** 2 + ((centers_c - cc0) / (width / 2)) ** 2 <= 1.0
    if not mask.any():
        raise DicomMcpError(f"The ROI does not cover any pixel of the {shape[0]}x{shape[1]} image.")
    return mask


def _describe(values: np.ndarray) -> dict[str, float]:
    p5, median, p95 = np.percentile(values, [5, 50, 95])
    return {
        "min": float(values.min()), "max": float(values.max()),
        "mean": round(float(values.mean()), 4), "std": round(float(values.std()), 4),
        "median": float(median), "p05": float(p5), "p95": float(p95),
    }


def pixel_statistics(ds: Dataset, frame: int = 0, roi: Roi | None = None) -> dict[str, Any]:
    if is_color(ds):
        raise DicomMcpError("Statistics are only defined for greyscale images.")
    arr = stored_frame(ds, frame)
    geo = geometry(ds, frame)
    values = modality_values(ds, arr, frame)
    mask = roi_mask(values.shape, roi, geo.spacing) if roi else np.ones(values.shape, dtype=bool)
    selected = values[mask]
    out: dict[str, Any] = {
        **_identity(ds), "frame": frame, "units": value_units(ds, frame),
        "pixel_count": int(selected.size), **_describe(selected),
    }
    if roi:
        out["roi"] = roi.__dict__
        rows, cols = np.nonzero(mask)
        out["roi_bounds_px"] = {"row_min": int(rows.min()), "row_max": int(rows.max()), "column_min": int(cols.min()), "column_max": int(cols.max())}
    if geo.spacing:
        out["area_mm2"] = round(selected.size * geo.spacing[0] * geo.spacing[1], 4)
        out["pixel_spacing_source"] = geo.spacing_source
    out["input_fingerprint"] = pixel_fingerprint(arr, geo)
    return out


def measure_distance(ds: Dataset, start: tuple[float, float], end: tuple[float, float], frame: int = 0) -> dict[str, Any]:
    geo = geometry(ds, frame)
    for name, (r, c) in (("start", start), ("end", end)):
        if not (0 <= r <= geo.rows and 0 <= c <= geo.columns):
            raise DicomMcpError(f"{name} point ({r}, {c}) is outside the {geo.rows}x{geo.columns} image.")
    d_row, d_col = end[0] - start[0], end[1] - start[1]
    out: dict[str, Any] = {
        **_identity(ds), "frame": frame,
        "start": {"row": start[0], "column": start[1], "patient_mm": geo.to_patient(*start)},
        "end": {"row": end[0], "column": end[1], "patient_mm": geo.to_patient(*end)},
        "distance_px": round(math.hypot(d_row, d_col), 4),
    }
    if geo.spacing:
        out["distance_mm"] = round(math.hypot(d_row * geo.spacing[0], d_col * geo.spacing[1]), 4)
        out["pixel_spacing_source"] = geo.spacing_source
        if not geo.spacing_source.startswith("PixelSpacing"):
            out["warning"] = "Distance uses detector-plane spacing; true anatomical size may differ (magnification)."
    else:
        out["warning"] = "The image has no pixel spacing; only the pixel distance is available."
    return out


# ---- series geometry -----------------------------------------------------------------------------


@dataclass
class _Slice:
    path: Path
    header: Dataset
    frame: int
    geo: FrameGeometry


@dataclass
class SeriesReport:
    series_uid: str
    slices: list[_Slice]
    summary: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)

    @property
    def errors(self) -> list[dict[str, str]]:
        return [i for i in self.issues if i["severity"] == "error"]

    def add(self, severity: str, code: str, message: str) -> None:
        self.issues.append({"severity": severity, "code": code, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary, "volume_ready": self.summary.get("volume_ready", False), "issues": self.issues}


def group_series(files: list[Path]) -> dict[str, list[_Slice]]:
    series: dict[str, list[_Slice]] = {}
    for path in files:
        header = read_dataset(path, pixels=False)
        uid = str(header.get("SeriesInstanceUID", "unknown"))
        for frame in range(frame_count(header)):
            series.setdefault(uid, []).append(_Slice(path, header, frame, geometry(header, frame)))
    return series


def analyze_series(series_uid: str, slices: list[_Slice]) -> SeriesReport:
    report = SeriesReport(series_uid, slices)
    first = slices[0].header
    modality = str(first.get("Modality", ""))
    report.summary = {
        "series_uid": series_uid, "modality": modality,
        "series_description": str(first.get("SeriesDescription", "")),
        "instances": len({s.path for s in slices}), "frames": len(slices),
    }
    planar = [s for s in slices if s.geo.has_plane]
    if not planar:
        if modality in PROJECTION_MODALITIES:
            report.add("info", "PROJECTION_IMAGE", f"{modality} images have no 3D plane geometry; volume checks do not apply.")
        else:
            report.add("error", "GEOMETRY_MISSING", "ImagePositionPatient/ImageOrientationPatient are missing.")
        report.summary.update(geometry=slices[0].geo.summary(), volume_ready=False)
        return report
    if len(planar) < len(slices):
        report.add("error", "GEOMETRY_MISSING", f"{len(slices) - len(planar)} frame(s) lack plane geometry.")

    ref = planar[0].geo
    for s in planar:
        g = s.geo
        if abs(float(np.dot(g.row_cosine, g.column_cosine))) > 1e-3 or not (
            math.isclose(np.linalg.norm(g.row_cosine), 1, abs_tol=1e-3) and math.isclose(np.linalg.norm(g.column_cosine), 1, abs_tol=1e-3)
        ):
            report.add("error", "NON_ORTHONORMAL_ORIENTATION", f"ImageOrientationPatient of {s.path.name} is not orthonormal.")
            break

    orientations: dict[tuple[float, ...], int] = {}
    for s in planar:
        key = tuple(np.round(np.concatenate([s.geo.row_cosine, s.geo.column_cosine]), 2))
        orientations[key] = orientations.get(key, 0) + 1
    if len(orientations) > 1:
        report.add("error", "MIXED_ORIENTATION",
                   f"{len(orientations)} different orientations in one series (counts {sorted(orientations.values(), reverse=True)}); "
                   "a localizer or a multi-plane acquisition is mixed in.")
        main = max(orientations, key=orientations.get)
        planar = [s for s in planar if tuple(np.round(np.concatenate([s.geo.row_cosine, s.geo.column_cosine]), 2)) == main]
        ref = planar[0].geo

    frames_of_reference = {s.geo.frame_of_reference for s in planar}
    if len(frames_of_reference) > 1:
        report.add("error", "MULTIPLE_FRAMES_OF_REFERENCE", f"{len(frames_of_reference)} FrameOfReferenceUIDs in one series.")
    if len({(s.geo.rows, s.geo.columns) for s in planar}) > 1:
        report.add("error", "INCONSISTENT_MATRIX", "Rows/Columns differ between frames.")
    spacings = {s.geo.spacing for s in planar}
    if None in spacings:
        report.add("error", "MISSING_PIXEL_SPACING", "Some frames have no pixel spacing.")
    elif len({tuple(round(v, 4) for v in sp) for sp in spacings}) > 1:
        report.add("error", "INCONSISTENT_PIXEL_SPACING", "PixelSpacing differs between frames.")

    label, obliquity = orientation_label(ref.normal)
    report.summary.update(
        orientation=label, obliquity_deg=round(obliquity, 2), rows=ref.rows, columns=ref.columns,
        pixel_spacing_mm=list(ref.spacing) if ref.spacing else None,
        slice_thickness_mm=ref.slice_thickness, frame_of_reference=ref.frame_of_reference,
    )
    if label.startswith("oblique"):
        report.add("info", "OBLIQUE", f"Slices are {obliquity:.1f} degrees from the {label.split()[-1].rstrip(')')} plane.")

    normal = ref.normal
    ordered = sorted(planar, key=lambda s: float(np.dot(normal, s.geo.position)))
    report.slices = ordered
    if len(ordered) < 2:
        report.summary["volume_ready"] = False
        report.add("info", "SINGLE_SLICE", "Only one slice; slice spacing cannot be checked.")
        return report

    positions = np.array([float(np.dot(normal, s.geo.position)) for s in ordered])
    gaps = np.diff(positions)
    duplicates = int(np.sum(gaps < 0.01))
    nonzero = gaps[gaps >= 0.01]
    if duplicates:
        report.add("error", "DUPLICATE_POSITIONS",
                   f"{duplicates} slice(s) share a position with another slice (multiple phases, echoes or repeated acquisitions).")
    if nonzero.size:
        median = float(np.median(nonzero))
        report.summary["slice_spacing_mm"] = {"median": round(median, 4), "min": round(float(nonzero.min()), 4), "max": round(float(nonzero.max()), 4)}
        report.summary["extent_mm"] = round(float(positions[-1] - positions[0]), 3)
        missing = [(i, g) for i, g in enumerate(gaps) if g > 1.5 * median]
        if missing:
            count = sum(round(g / median) - 1 for _, g in missing)
            where = ", ".join(f"after position {positions[i]:.2f} mm" for i, _ in missing[:5])
            report.add("error", "MISSING_SLICES", f"About {count} slice(s) appear to be missing ({where}).")
        tolerance = max(0.05, 0.02 * median)
        regular = nonzero[nonzero <= 1.5 * median]
        if regular.size and float(regular.max() - regular.min()) > tolerance:
            report.add("warning", "NON_UNIFORM_SPACING",
                       f"Slice spacing varies from {regular.min():.3f} to {regular.max():.3f} mm; resample before volumetric measurement.")
        thickness = ref.slice_thickness
        if thickness and thickness > median * 1.05:
            report.add("info", "SLICE_OVERLAP", f"Slice thickness {thickness} mm exceeds spacing {median:.3f} mm (overlapping slices).")
        elif thickness and thickness < median * 0.95:
            report.add("info", "SLICE_GAP", f"Slice thickness {thickness} mm is less than spacing {median:.3f} mm (gaps between slices).")

        displacement = ordered[-1].geo.position - ordered[0].geo.position
        norm = float(np.linalg.norm(displacement))
        if norm > 0:
            shear = math.degrees(math.acos(min(1.0, abs(float(np.dot(displacement, normal))) / norm)))
            if shear > 0.5:
                report.add("warning", "SHEARED_STACK",
                           f"Slices are offset {shear:.1f} degrees from the slice normal (e.g. gantry tilt); "
                           "reformats and volumes are distorted unless corrected.")

    numbers = [s.header.get("InstanceNumber") for s in ordered if s.frame == 0]
    if len(numbers) > 2 and all(n is not None for n in numbers):
        diffs = np.diff([int(n) for n in numbers])
        if not (np.all(diffs > 0) or np.all(diffs < 0)):
            report.add("info", "INSTANCE_ORDER_MISMATCH", "InstanceNumber order does not follow slice position; sort by position, not number.")

    report.summary["volume_ready"] = not report.errors
    return report


# ---- volumes and multiplanar reformatting --------------------------------------------------------

# Display convention per plane: (plane normal axis, (down axis, sign), (right axis, sign)) in LPS axes 0=x,1=y,2=z.
# Radiological display: patient right on image left; anterior at the top (axial); superior at the top.
_PLANES: dict[str, tuple[int, tuple[int, int], tuple[int, int]]] = {
    "axial": (2, (1, 1), (0, 1)),
    "coronal": (1, (2, -1), (0, 1)),
    "sagittal": (0, (2, -1), (1, 1)),
}


@dataclass
class Volume:
    values: np.ndarray  # (slice, row, column), modality values
    spacing: tuple[float, float, float]  # mm along each array axis
    axes: tuple[np.ndarray, np.ndarray, np.ndarray]  # patient direction of each array axis
    origin: np.ndarray
    units: str
    series_uid: str
    fingerprint: str


def load_volume(report: SeriesReport, max_voxels: int) -> Volume:
    if not report.summary.get("volume_ready"):
        codes = ", ".join(i["code"] for i in report.errors) or "fewer than two slices"
        raise DicomMcpError(f"Series {report.series_uid} cannot be stacked into a volume: {codes}. Run check_geometry for details.")
    slices = report.slices
    ref = slices[0].geo
    voxels = len(slices) * ref.rows * ref.columns
    if voxels > max_voxels:
        raise DicomMcpError(f"The volume has {voxels:,} voxels, above the configured limit of {max_voxels:,}.")
    values = np.empty((len(slices), ref.rows, ref.columns), dtype=np.float32)
    digest = hashlib.sha256()
    current: tuple[Path, Dataset] | None = None  # multi-frame files are read once
    for k, s in enumerate(slices):
        if current is None or current[0] != s.path:
            current = (s.path, read_dataset(s.path))
        ds = current[1]
        arr = stored_frame(ds, s.frame)
        values[k] = modality_values(ds, arr, s.frame)
        digest.update(pixel_fingerprint(arr, s.geo).encode())
    positions = [float(np.dot(ref.normal, s.geo.position)) for s in slices]
    slice_spacing = float(np.median(np.diff(positions)))
    return Volume(
        values=values,
        spacing=(slice_spacing, ref.spacing[0], ref.spacing[1]),
        axes=(ref.normal, ref.column_cosine, ref.row_cosine),
        origin=slices[0].geo.position,
        units=value_units(slices[0].header, slices[0].frame),
        series_uid=report.series_uid,
        fingerprint="sha256:" + digest.hexdigest(),
    )


def reformat(volume: Volume, plane: Plane, index: int | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    if plane not in _PLANES:
        raise DicomMcpError(f"Unknown plane '{plane}'. Use axial, coronal or sagittal.")
    normal_axis, (down_axis, down_sign), (right_axis, right_sign) = _PLANES[plane]
    dominant = [int(np.argmax(np.abs(v))) for v in volume.axes]
    if len(set(dominant)) < 3:
        raise DicomMcpError("The acquisition is too oblique to map onto standard planes.")
    fixed = dominant.index(normal_axis)
    count = volume.values.shape[fixed]
    index = count // 2 if index is None else index
    if not 0 <= index < count:
        raise DicomMcpError(f"Index {index} is out of range for the {plane} plane (0..{count - 1}).")
    image = np.take(volume.values, index, axis=fixed)
    remaining = [a for a in range(3) if a != fixed]
    if dominant[remaining[0]] != down_axis:
        image = image.T
        remaining.reverse()
    row_axis, col_axis = remaining
    down_vec, right_vec = volume.axes[row_axis].copy(), volume.axes[col_axis].copy()
    if np.sign(down_vec[down_axis]) != down_sign:
        image = image[::-1, :]
        down_vec = -down_vec
    if np.sign(right_vec[right_axis]) != right_sign:
        image = image[:, ::-1]
        right_vec = -right_vec
    obliquity = max(math.degrees(math.acos(min(1.0, abs(float(v[d]))))) for v, d in zip(volume.axes, dominant))
    position = volume.origin + index * volume.spacing[fixed] * volume.axes[fixed]
    info = {
        "plane": plane, "index": index, "count": count,
        "position_mm": {"xyz"[normal_axis]: round(float(position[normal_axis]), 3)},
        "pixel_spacing_mm": [volume.spacing[row_axis], volume.spacing[col_axis]],
        "edges": edge_labels(right_vec, down_vec),
    }
    if obliquity > 1:
        info["warning"] = f"Source slices are {obliquity:.1f} degrees oblique; the reformat is not resampled onto true anatomical planes."
    return np.ascontiguousarray(image), info
