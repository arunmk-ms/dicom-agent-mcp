"""Helpers for converting DICOM datasets to agent-friendly JSON and building queries."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Iterable, Mapping

from pydicom.datadict import dictionary_VR, keyword_for_tag, tag_for_keyword
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pathlib import Path

from pydicom.tag import Tag

from .errors import DicomMcpError

BINARY_VRS = {"OB", "OD", "OF", "OL", "OV", "OW", "UN"}
INT_VRS = {"US", "UL", "SS", "SL", "UV", "SV"}
FLOAT_VRS = {"FL", "FD"}
_UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_HEX_TAG_RE = re.compile(r"^\(?([0-9A-Fa-f]{4}),?([0-9A-Fa-f]{4})\)?$")


class Level(str, Enum):
    PATIENT = "PATIENT"
    STUDY = "STUDY"
    SERIES = "SERIES"
    IMAGE = "IMAGE"


UNIQUE_KEY = {
    Level.PATIENT: "PatientID",
    Level.STUDY: "StudyInstanceUID",
    Level.SERIES: "SeriesInstanceUID",
    Level.IMAGE: "SOPInstanceUID",
}

DEFAULT_RETURN_KEYS: dict[Level, list[str]] = {
    Level.PATIENT: ["PatientID", "PatientName", "PatientBirthDate", "PatientSex"],
    Level.STUDY: [
        "StudyInstanceUID", "StudyDate", "StudyTime", "AccessionNumber", "StudyID",
        "StudyDescription", "ModalitiesInStudy", "PatientID", "PatientName",
        "ReferringPhysicianName", "NumberOfStudyRelatedSeries", "NumberOfStudyRelatedInstances",
    ],
    Level.SERIES: [
        "StudyInstanceUID", "SeriesInstanceUID", "Modality", "SeriesNumber",
        "SeriesDescription", "BodyPartExamined", "SeriesDate", "NumberOfSeriesRelatedInstances",
    ],
    Level.IMAGE: [
        "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "SOPClassUID", "InstanceNumber",
    ],
}


def parse_level(level: str) -> Level:
    value = level.strip().upper()
    if value == "INSTANCE":
        value = "IMAGE"
    try:
        return Level(value)
    except ValueError:
        raise DicomMcpError(f"Unknown query level '{level}'. Use PATIENT, STUDY, SERIES or IMAGE.") from None


def to_tag(name: str) -> int:
    """Resolve a DICOM keyword (``PatientID``) or hex tag (``00100020`` / ``(0010,0020)``)."""
    tag = tag_for_keyword(name)
    if tag is not None:
        return tag
    match = _HEX_TAG_RE.match(name.strip())
    if match:
        return int(match.group(1) + match.group(2), 16)
    raise DicomMcpError(f"Unknown DICOM attribute '{name}'. Use a keyword such as 'PatientID' or a hex tag.")


def attribute_name(tag: int) -> str:
    return keyword_for_tag(tag) or f"{Tag(tag).group:04X}{Tag(tag).element:04X}"


def check_uid(uid: str, field: str = "UID") -> str:
    uid = uid.strip()
    if len(uid) > 64 or not _UID_RE.match(uid):
        raise DicomMcpError(f"Invalid {field}: '{uid}'")
    return uid


def instance_path(root: Path, ds: Dataset) -> Path:
    """Cache location ``root/<study>/<series>/<sop>.dcm``; UIDs are validated so remote data cannot escape ``root``."""
    study = check_uid(str(ds.StudyInstanceUID), "StudyInstanceUID")
    series = check_uid(str(ds.SeriesInstanceUID), "SeriesInstanceUID")
    sop = check_uid(str(ds.SOPInstanceUID), "SOPInstanceUID")
    return root / study / series / f"{sop}.dcm"


def build_identifier(level: Level, filters: Mapping[str, Any], return_keys: Iterable[str]) -> Dataset:
    """Build a C-FIND/C-GET/C-MOVE identifier with matching keys and empty return keys."""
    ds = Dataset()
    ds.QueryRetrieveLevel = level.value
    for name in return_keys:
        tag = to_tag(name)
        if tag not in ds:
            vr = _vr(tag)
            ds.add_new(tag, vr, [] if vr == "SQ" else None)
    for name, value in filters.items():
        tag = to_tag(name)
        vr = _vr(tag)
        ds.add_new(tag, vr, _coerce(value, vr))
    return ds


def qido_params(filters: Mapping[str, Any], return_keys: Iterable[str]) -> list[tuple[str, str]]:
    params = [(attribute_name(to_tag(k)), str(v)) for k, v in filters.items()]
    params += [("includefield", attribute_name(to_tag(k))) for k in return_keys]
    return params


def to_friendly(ds: Dataset, max_depth: int = 2, max_text: int = 1024) -> dict[str, Any]:
    """Convert a dataset into ``{keyword: value}`` JSON that an LLM can read; bulk data is summarised."""
    out: dict[str, Any] = {}
    for elem in ds:
        if elem.tag.element == 0 or elem.tag.group == 0x0002:
            continue
        name = elem.keyword or f"{elem.tag.group:04X}{elem.tag.element:04X}"
        out[name] = _element_value(elem, max_depth, max_text)
    return out


def _element_value(elem, depth: int, max_text: int) -> Any:
    if elem.VR == "SQ":
        if depth <= 0:
            return f"<sequence with {len(elem.value)} item(s)>"
        return [to_friendly(item, depth - 1, max_text) for item in elem.value]
    if elem.VR in BINARY_VRS or elem.tag == 0x7FE00010:
        size = len(elem.value) if elem.value is not None else 0
        return f"<binary {elem.VR}, {size} bytes>"
    value = elem.value
    if value is None or value == "":
        return None
    if isinstance(value, (MultiValue, list, tuple)):
        return [_scalar(v, max_text) for v in value]
    return _scalar(value, max_text)


def _scalar(value: Any, max_text: int) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, bytes):
        return f"<binary, {len(value)} bytes>"
    text = str(value)
    return text if len(text) <= max_text else text[:max_text] + "..."


def _vr(tag: int) -> str:
    try:
        return dictionary_VR(tag)
    except KeyError:
        return "LO"


def _coerce(value: Any, vr: str) -> Any:
    if value is None:
        return None
    if vr in INT_VRS:
        return int(value)
    if vr in FLOAT_VRS:
        return float(value)
    if isinstance(value, (list, tuple)):
        return "\\".join(str(v) for v in value)
    return str(value)
