"""Per-tool TEVL contracts: precondition and postcondition checks built on the imaging and geometry code."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
from pydicom import Dataset

from . import imaging
from .datasets import Level, attribute_name, to_tag
from .deid import REMOVE
from .errors import DicomMcpError
from .verification import HARD_FAIL, PASS, SOFT_FAIL, Check, Ledger, file_sha256, geometry_checks, worst

INSTANCE_UIDS = ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "FrameOfReferenceUID")
TEXT_VRS = {"LO", "SH", "PN", "LT", "ST", "UT"}

# ---- fingerprints --------------------------------------------------------------------------------


def dataset_fingerprint(ds: Dataset) -> str | None:
    """Pixel fingerprint of a whole instance (all frames). None for objects without decodable pixels."""
    if "PixelData" not in ds:
        return None
    try:
        frames = [imaging.pixel_fingerprint(imaging.stored_frame(ds, f), imaging.geometry(ds, f)) for f in range(imaging.frame_count(ds))]
    except DicomMcpError:
        return None
    if len(frames) == 1:
        return frames[0]
    return "sha256:" + hashlib.sha256("|".join(frames).encode()).hexdigest()


def file_fingerprint(path: Path) -> str | None:
    return dataset_fingerprint(imaging.read_dataset(path))


# ---- shared checks -------------------------------------------------------------------------------


def spacing_checks(geo: imaging.FrameGeometry, phase: str = "pre") -> list[Check]:
    if geo.spacing is None:
        return [Check(phase, "NO_PIXEL_SPACING", SOFT_FAIL, "The image has no pixel spacing; mm values are unavailable or assume square pixels.")]
    if geo.spacing_source and geo.spacing_source.startswith("ImagerPixelSpacing"):
        return [Check(phase, "DETECTOR_PLANE_SPACING", SOFT_FAIL,
                      "Spacing comes from ImagerPixelSpacing (detector plane); anatomical sizes may differ through magnification.")]
    return []


def units_checks(ds: Dataset, frame: int) -> list[Check]:
    if ds.get("Modality") != "CT" or "ModalityLUTSequence" in ds:
        return []
    slope = imaging._value(ds, frame, "RescaleSlope", "PixelValueTransformationSequence")
    intercept = imaging._value(ds, frame, "RescaleIntercept", "PixelValueTransformationSequence")
    if slope in (None, "") or intercept in (None, ""):
        return [Check("pre", "UNITS_ASSUMED", SOFT_FAIL, "CT image without RescaleSlope/RescaleIntercept; stored values are reported as HU unverified.")]
    return []


def frame_checks(ds: Dataset, frame: int) -> list[Check]:
    n = imaging.frame_count(ds)
    if not 0 <= frame < n:
        return [Check("pre", "FRAME_OUT_OF_RANGE", HARD_FAIL, f"Frame {frame} is out of range; the instance has {n} frame(s).")]
    return []


def status_checks(result: Mapping[str, Any]) -> list[Check]:
    status = result.get("status")
    if status == "empty":
        return [Check("post", "RESULT_EMPTY", SOFT_FAIL, "The operation matched nothing; confirm the query before concluding nothing exists.")]
    if status == "partial":
        n = len(result.get("failed_items") or [])
        return [Check("post", "RESULT_PARTIAL", SOFT_FAIL, f"Only part of the operation succeeded ({n} failed item(s) listed in failed_items).")]
    if status == "truncated":
        return [Check("post", "RESULT_TRUNCATED", SOFT_FAIL, "More results exist than were returned; page with offset or narrow the filters.")]
    if status == "failed":
        return [Check("post", "RESULT_FAILED", HARD_FAIL, "The operation failed.")]
    return [Check("post", "RESULT_OK", PASS, "Operation completed.")] if status == "ok" else []


# ---- pixel_statistics ----------------------------------------------------------------------------


def _roi_box_px(roi: imaging.Roi, spacing: tuple[float, float] | None) -> tuple[float, float, float, float] | None:
    if roi.units == "mm":
        if spacing is None:
            return None
        return roi.row / spacing[0], roi.column / spacing[1], roi.height / spacing[0], roi.width / spacing[1]
    return roi.row, roi.column, roi.height, roi.width


def roi_clipped(roi: imaging.Roi | None, geo: imaging.FrameGeometry) -> bool:
    if roi is None:
        return False
    box = _roi_box_px(roi, geo.spacing)
    if box is None:
        return False
    r, c, h, w = box
    return r < 0 or c < 0 or r + h > geo.rows or c + w > geo.columns


def statistics_pre(ds: Dataset, frame: int, roi: imaging.Roi | None) -> list[Check]:
    checks = frame_checks(ds, frame)
    geo = imaging.geometry(ds, frame)
    checks += units_checks(ds, frame) + spacing_checks(geo)
    if roi_clipped(roi, geo):
        checks.append(Check("pre", "ROI_CLIPPED", SOFT_FAIL, "The ROI extends beyond the image; statistics cover only the part inside."))
    return checks


def statistics_post(result: Mapping[str, Any], ds: Dataset, frame: int, roi: imaging.Roi | None) -> list[Check]:
    checks = []
    n = result["pixel_count"]
    order = [result["min"], result["p05"], result["median"], result["p95"], result["max"]]
    eps = 1e-6 * max(1.0, abs(result["max"]), abs(result["min"]))
    consistent = (
        n > 0 and all(b >= a - eps for a, b in zip(order, order[1:]))
        and result["min"] - eps <= result["mean"] <= result["max"] + eps and result["std"] >= 0
    )
    checks.append(Check("post", "STATS_CONSISTENT" if consistent else "STATS_INCONSISTENT", PASS if consistent else HARD_FAIL,
                        "min <= p05 <= median <= p95 <= max and min <= mean <= max." if consistent
                        else f"Statistics are internally inconsistent: {dict(zip(['min', 'p05', 'median', 'p95', 'max'], order))}, mean {result['mean']}."))
    geo = imaging.geometry(ds, frame)
    if geo.spacing and "area_mm2" in result:
        expected = n * geo.spacing[0] * geo.spacing[1]
        ok = math.isclose(result["area_mm2"], expected, rel_tol=1e-3, abs_tol=1e-3)
        checks.append(Check("post", "AREA_CONSISTENT" if ok else "AREA_INCONSISTENT", PASS if ok else HARD_FAIL,
                            f"area_mm2 {result['area_mm2']} vs pixel_count x spacing = {expected:.4f}."))
    if roi is not None and not roi_clipped(roi, geo):
        box = _roi_box_px(roi, geo.spacing)
        if box is not None:
            _, _, h, w = box
            expected_px = h * w * (math.pi / 4 if roi.shape == "ellipse" else 1.0)
            tolerance = h + w + 2  # pixels cut by the boundary
            ok = abs(n - expected_px) <= tolerance
            checks.append(Check("post", "ROI_COVERAGE" if ok else "ROI_AREA_MISMATCH", PASS if ok else SOFT_FAIL,
                                f"ROI covers {n} pixels; its geometric area is {expected_px:.1f} pixels (tolerance {tolerance:.1f}).",
                                {"pixel_count": n, "expected_pixels": round(expected_px, 2)}))
    return checks


# ---- measure_distance ----------------------------------------------------------------------------


def distance_pre(ds: Dataset, frame: int) -> list[Check]:
    return frame_checks(ds, frame) + spacing_checks(imaging.geometry(ds, frame) if imaging.frame_count(ds) > frame >= 0 else imaging.geometry(ds))


def distance_post(result: Mapping[str, Any]) -> list[Check]:
    start, end = result["start"].get("patient_mm"), result["end"].get("patient_mm")
    if "distance_mm" not in result:
        return []
    if not start or not end:
        return [Check("post", "CROSSCHECK_UNAVAILABLE", SOFT_FAIL,
                      "The image has no patient-space geometry, so the mm distance cannot be cross-checked.")]
    independent = float(np.linalg.norm(np.subtract(end, start)))
    ok = abs(independent - result["distance_mm"]) <= 0.01 + 1e-4 * independent
    return [Check("post", "DISTANCE_CROSSCHECK" if ok else "DISTANCE_CROSSCHECK_FAILED", PASS if ok else HARD_FAIL,
                  f"In-plane distance {result['distance_mm']} mm vs patient-space distance {independent:.4f} mm.",
                  {"patient_space_mm": round(independent, 4)})]


# ---- rendering -----------------------------------------------------------------------------------


def render_pre(ds: Dataset, frame: int, window: str, center: float | None, width: float | None) -> list[Check]:
    checks = frame_checks(ds, frame)
    if checks:
        return checks
    explicit = center is not None and width is not None
    if not explicit and window in imaging.WINDOW_PRESETS and not imaging.is_color(ds):
        units = imaging.value_units(ds, frame)
        if units != "HU":
            checks.append(Check("pre", "PRESET_UNITS_MISMATCH", SOFT_FAIL, f"Preset '{window}' assumes HU but the image is in '{units}'."))
    return checks + spacing_checks(imaging.geometry(ds, frame))


def aspect_checks(image_shape: tuple[int, int], spacing: Iterable[float] | None, png_size: Iterable[int], max_size: int) -> list[Check]:
    rows, cols = image_shape
    sy, sx = (tuple(spacing) if spacing else (1.0, 1.0))
    expected = (rows * sy) / (cols * sx)
    width, height = png_size
    actual = height / width
    tolerance = 1.0 / min(width, height) + 0.01
    checks = []
    ok = abs(actual / expected - 1.0) <= tolerance
    checks.append(Check("post", "ASPECT_MATCHES_MM" if ok else "ASPECT_MISMATCH", PASS if ok else HARD_FAIL,
                        f"PNG {width}x{height} (h/w {actual:.4f}) vs physical h/w {expected:.4f}."))
    if max(width, height) > max_size:
        checks.append(Check("post", "PNG_TOO_LARGE", HARD_FAIL, f"PNG {width}x{height} exceeds max_size {max_size}."))
    return checks


def window_checks(window: Mapping[str, Any] | None) -> list[Check]:
    if window and window.get("width", 1) <= 0:
        return [Check("post", "INVALID_WINDOW", HARD_FAIL, f"Window width {window['width']} is not positive.")]
    return []


# ---- check_geometry ------------------------------------------------------------------------------


def geometry_post(reports: Iterable[imaging.SeriesReport]) -> list[Check]:
    checks = []
    for report in reports:
        checks += geometry_checks("check_geometry", report.issues, "post", report.series_uid)
    return checks or [Check("post", "GEOMETRY_CLEAN", PASS, "No geometry issues found.")]


# ---- deidentify ----------------------------------------------------------------------------------


def _text_values(ds: Dataset) -> list[str]:
    values: list[str] = []

    def visit(_: Dataset, elem: Any) -> None:
        if elem.VR in TEXT_VRS and elem.value not in (None, ""):
            values.append(str(elem.value))

    ds.walk(visit)
    return values


def _identifiers(ds: Dataset) -> list[str]:
    tokens = []
    patient_id = str(ds.get("PatientID", "")).strip()
    if len(patient_id) >= 3:
        tokens.append(patient_id)
    name = ds.get("PatientName")
    if name:
        tokens += [part for part in str(name).replace("^", " ").split() if len(part) >= 3]
    return tokens


def deidentify_post(result: Mapping[str, Any], keep_private: bool, keep_descriptions: bool) -> tuple[list[Check], dict[str, Any]]:
    leaks, uids, private, pixels = [], [], [], []
    inputs, outputs, files = [], [], {}
    for item in result["files"]:
        src, out = imaging.read_dataset(Path(item["source"])), imaging.read_dataset(Path(item["output"]))
        name = Path(item["output"]).name
        present = [k for k in REMOVE if k in out]
        text = " ".join(_text_values(out)).lower()
        found = [t for t in _identifiers(src) if t.lower() in text]
        if present or found:
            leaks.append({"file": name, "attributes": present, "values": found})
        same = [k for k in INSTANCE_UIDS if k in src and str(src.get(k)) == str(out.get(k))]
        if same:
            uids.append({"file": name, "attributes": same})
        if not keep_private and any(tag.is_private for tag in out.keys()):
            private.append(name)
        a, b = dataset_fingerprint(src), dataset_fingerprint(out)
        if a != b:
            pixels.append(name)
        if a:
            inputs.append(a)
        if b:
            outputs.append(b)
        files[item["output"]] = file_sha256(Path(item["output"]))

    def check(bad: list[Any], ok_code: str, bad_code: str, ok_msg: str, bad_msg: str) -> Check:
        if bad:
            return Check("post", bad_code, HARD_FAIL, bad_msg, {"count": len(bad), "examples": bad[:5]})
        return Check("post", ok_code, PASS, ok_msg)

    checks = [
        check(leaks, "IDENTIFIERS_REMOVED", "IDENTIFIER_PRESENT", "No removed attribute or source patient name/ID survives in the output.",
              "Identifying attributes or values survive in the de-identified output."),
        check(uids, "UIDS_REMAPPED", "UID_NOT_REMAPPED", "Study, series, instance and frame-of-reference UIDs were remapped.",
              "Some instance UIDs were not remapped."),
        check(pixels, "PIXELS_UNCHANGED", "PIXELS_CHANGED", "Output pixel fingerprints equal the source fingerprints.",
              "Pixel data or geometry changed during de-identification."),
    ]
    if not keep_private:
        checks.append(check(private, "PRIVATE_TAGS_REMOVED", "PRIVATE_TAGS_PRESENT", "No private tags remain.", "Private tags remain."))
    warnings = " ".join(result.get("warnings", []))
    if "burned-in" in warnings or "BurnedInAnnotation" in warnings:
        checks.append(Check("post", "BURNED_IN_TEXT_RISK", SOFT_FAIL, "Pixels may contain burned-in identifiers that were not removed."))
    if keep_private:
        checks.append(Check("post", "PRIVATE_TAGS_KEPT", SOFT_FAIL, "Private tags were kept on request; they may contain identifiers."))
    if keep_descriptions:
        checks.append(Check("post", "DESCRIPTIONS_KEPT", SOFT_FAIL, "Descriptions were kept on request; review them for identifiers."))
    return checks, {"input_fingerprints": sorted(set(inputs)), "output_fingerprints": sorted(set(outputs)), "output_files": files}


# ---- retrieve ------------------------------------------------------------------------------------


def retrieve_post(result: Mapping[str, Any], study_uid: str, series_uid: str | None, sop_uid: str | None) -> list[Check]:
    checks = status_checks(result)
    wanted = {"StudyInstanceUID": study_uid, "SeriesInstanceUID": series_uid, "SOPInstanceUID": sop_uid}
    mismatched, unreadable = [], []
    for f in result.get("files", []):
        try:
            ds = imaging.read_dataset(Path(f), pixels=False)
        except DicomMcpError:
            unreadable.append(f)
            continue
        if any(v and str(ds.get(k, "")) != v for k, v in wanted.items()):
            mismatched.append(Path(f).name)
    if unreadable:
        checks.append(Check("post", "UNREADABLE_FILE", HARD_FAIL, f"{len(unreadable)} retrieved file(s) cannot be read.", {"examples": unreadable[:5]}))
    if mismatched:
        checks.append(Check("post", "UID_MISMATCH", HARD_FAIL, f"{len(mismatched)} retrieved file(s) do not belong to the requested study/series/instance.",
                            {"examples": mismatched[:5]}))
    elif result.get("files"):
        checks.append(Check("post", "UIDS_MATCH_REQUEST", PASS, "Every retrieved file belongs to the requested study/series/instance."))
    return checks


def move_post(result: Mapping[str, Any]) -> list[Check]:
    checks = status_checks(result)
    if result.get("warning"):
        checks.append(Check("post", "SUBOPERATION_WARNINGS", SOFT_FAIL, f"{result['warning']} sub-operation(s) completed with warnings (e.g. coerced attributes)."))
    return checks


# ---- store gate and postconditions ---------------------------------------------------------------


def store_gate(files: list[Path], ledger: Ledger) -> tuple[list[Check], dict[str, Any]]:
    checks: list[Check] = []
    headers = [(f, imaging.read_dataset(f, pixels=False)) for f in files]

    sops: dict[str, list[str]] = defaultdict(list)
    patients: dict[str, set[str]] = defaultdict(set)
    for f, ds in headers:
        sops[str(ds.get("SOPInstanceUID", ""))].append(f.name)
        patients[str(ds.get("StudyInstanceUID", ""))].add(str(ds.get("PatientID", "")))
    duplicates = {uid: names for uid, names in sops.items() if len(names) > 1}
    if duplicates:
        checks.append(Check("gate", "DUPLICATE_SOP_INSTANCE", HARD_FAIL, f"{len(duplicates)} SOPInstanceUID(s) appear in more than one file.",
                            {"examples": [{"sop_instance_uid": k, "files": v} for k, v in list(duplicates.items())[:5]]}))
    mixed = {study: sorted(ids) for study, ids in patients.items() if len(ids) > 1}
    if mixed:
        checks.append(Check("gate", "PATIENT_MISMATCH", HARD_FAIL, "Files of the same study carry different PatientIDs.",
                            {"studies": [{"study_uid": k, "patient_ids": v} for k, v in list(mixed.items())[:5]]}))

    for uid, slices in imaging.group_series(files).items():
        checks += geometry_checks("store", imaging.analyze_series(uid, slices).issues, "gate", uid)

    index = _output_index(ledger)
    digests = {str(f): file_sha256(f) for f in files}
    prior = [index[d] for d in digests.values() if d in index]
    verdict = worst(r["verdict"] for r in prior)
    evidence = {"files_with_prior_record": len(prior), "files": len(files), "records": sorted({r["id"] for r in prior})[:10]}
    if prior and verdict != PASS:
        code = "PRIOR_HARD_FAIL" if verdict == HARD_FAIL else "PRIOR_SOFT_FAIL"
        checks.append(Check("gate", code, verdict, "A previous tool that produced some of these files did not pass verification.", evidence))
    elif prior:
        checks.append(Check("gate", "PRIOR_VERIFIED", PASS, "Files produced by earlier tools passed verification.", evidence))
    if len(prior) < len(files):
        checks.append(Check("gate", "NO_PRIOR_RECORD", PASS, f"{len(files) - len(prior)} file(s) were not produced by a verified tool call.",
                            {"files": len(files) - len(prior)}))
    return checks, {"file_digests": digests}


def _output_index(ledger: Ledger) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in ledger.records():
        for d in (record.get("output_files") or {}).values():
            index[d] = record  # the latest record that produced a file wins
    return index


def store_post(result: Mapping[str, Any], sent_uids: set[str]) -> list[Check]:
    checks = status_checks(result)
    if "stored_sop_instance_uids" in result:
        stored = set(result["stored_sop_instance_uids"])
    else:
        stored = {r["sop_instance_uid"] for r in result.get("results", []) if r.get("ok")}
    unexpected = stored - sent_uids
    if unexpected:
        checks.append(Check("post", "SOP_MISMATCH", HARD_FAIL, "The archive acknowledged instances that were not sent.", {"examples": sorted(unexpected)[:5]}))
    elif stored == sent_uids:
        checks.append(Check("post", "ALL_ACKNOWLEDGED", PASS, f"The archive acknowledged all {len(sent_uids)} instance(s) by SOPInstanceUID."))
    return checks


# ---- empty search hints --------------------------------------------------------------------------

_DIMSE_KEYS = {
    Level.PATIENT: {"PatientName", "PatientID"},
    Level.STUDY: {"StudyDate", "StudyTime", "AccessionNumber", "PatientName", "PatientID", "StudyID", "StudyInstanceUID"},
    Level.SERIES: {"Modality", "SeriesNumber", "SeriesInstanceUID"},
    Level.IMAGE: {"InstanceNumber", "SOPInstanceUID"},
}
_QIDO_KEYS = {
    Level.PATIENT: set(),
    Level.STUDY: {"StudyDate", "StudyTime", "AccessionNumber", "ModalitiesInStudy", "ReferringPhysicianName",
                  "PatientName", "PatientID", "StudyInstanceUID", "StudyID"},
    Level.SERIES: {"Modality", "SeriesInstanceUID", "SeriesNumber", "PerformedProcedureStepStartDate", "PerformedProcedureStepStartTime"},
    Level.IMAGE: {"SOPClassUID", "SOPInstanceUID", "InstanceNumber"},
}
_ORDER = [Level.PATIENT, Level.STUDY, Level.SERIES, Level.IMAGE]


def optional_keys(protocol: str, level: Level, filters: Iterable[str]) -> list[str]:
    """Filter keys outside the matching keys the standard requires an archive to support at this level (or above)."""
    table = _DIMSE_KEYS if protocol == "dimse" else _QIDO_KEYS
    supported = set().union(*(table[lvl] for lvl in _ORDER[: _ORDER.index(level) + 1]))
    return [k for k in (attribute_name(to_tag(f)) for f in filters) if k not in supported]


# ---- verify_claim --------------------------------------------------------------------------------


def lookup(result: Any, path: str) -> tuple[bool, Any]:
    value = result
    for part in path.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _decimal_tolerance(claimed: Any) -> float:
    text = claimed if isinstance(claimed, str) else repr(claimed)
    if isinstance(claimed, bool) or "e" in text.lower():
        return 1e-9
    decimals = len(text.split(".")[1]) if "." in text else 0
    return 0.5 * 10 ** -decimals


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def compare_claim(claimed: Any, actual: Any, tolerance: float | None) -> tuple[bool, float | None]:
    if isinstance(claimed, list) and isinstance(actual, list):
        if len(claimed) != len(actual):
            return False, tolerance
        results = [compare_claim(c, a, tolerance) for c, a in zip(claimed, actual)]
        return all(ok for ok, _ in results), max((t for _, t in results if t is not None), default=None)
    if isinstance(claimed, bool) or isinstance(actual, bool) or claimed is None or actual is None:
        return type(claimed) is type(actual) and claimed == actual, None
    c, a = _as_number(claimed), _as_number(actual)
    if c is not None and a is not None and not isinstance(actual, str):
        tol = tolerance if tolerance is not None else _decimal_tolerance(claimed)
        return abs(c - a) <= tol + 1e-9, tol
    return str(claimed).strip().lower() == str(actual).strip().lower(), None
