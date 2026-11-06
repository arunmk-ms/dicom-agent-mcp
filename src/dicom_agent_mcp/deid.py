"""De-identification based on the DICOM PS3.15 Basic Application Confidentiality Profile (subset).

Options applied: Retain Patient Characteristics, and (by default) Retain Longitudinal Temporal
Information with Modified Dates. Pixel data is not inspected or altered: burned-in text is flagged,
not removed. This is a research aid, not a validated de-identification product.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Any, Literal

from pydicom import Dataset
from pydicom.dataelem import DataElement
from pydicom.sequence import Sequence
from pydicom.uid import UID

from .datasets import check_uid
from .imaging import read_dataset

DateMode = Literal["shift", "keep"]

# Attributes deleted outright (identifying, or free text that often contains identifiers).
REMOVE = [
    "OtherPatientIDs", "OtherPatientIDsSequence", "OtherPatientNames", "PatientBirthName", "PatientBirthTime",
    "PatientMotherBirthName", "PatientAddress", "PatientTelephoneNumbers", "PatientTelecomInformation",
    "CountryOfResidence", "RegionOfResidence", "MilitaryRank", "BranchOfService", "MedicalRecordLocator",
    "Occupation", "EthnicGroup", "PatientReligiousPreference", "AdditionalPatientHistory", "PatientComments",
    "IssuerOfPatientID", "IssuerOfPatientIDQualifiersSequence", "PatientInsurancePlanCodeSequence",
    "InsurancePlanIdentification", "ResponsiblePerson", "ResponsibleOrganization", "MedicalAlerts", "Allergies",
    "PerformingPhysicianName", "NameOfPhysiciansReadingStudy", "OperatorsName", "PhysiciansOfRecord",
    "RequestingPhysician", "ScheduledPerformingPhysicianName", "ContentCreatorName", "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers", "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName",
    "StationName", "DeviceSerialNumber", "RequestAttributesSequence", "ReferencedPatientSequence",
    "CurrentPatientLocation", "PatientInstitutionResidence", "AdmissionID", "ServiceEpisodeID", "ImageComments",
    "RequestedProcedureDescription", "PerformedProcedureStepDescription", "PerformedProcedureStepID",
    "ScheduledProcedureStepDescription", "AdmittingDiagnosesDescription", "DerivationDescription", "TextComments",
    "VisitComments", "InterpretationText", "StudyComments", "FillerOrderNumberImagingServiceRequest",
    "PlacerOrderNumberImagingServiceRequest", "RequestingService",
]
# Type 2 attributes kept but emptied.
EMPTY = ["PatientBirthDate", "ReferringPhysicianName", "AccessionNumber", "StudyID"]
# Descriptions that can contain identifiers; emptied unless keep_descriptions is set.
DESCRIPTIONS = ["StudyDescription", "SeriesDescription", "ProtocolName"]
# Content that may carry identifiers inside the pixels.
PIXEL_RISK_MODALITIES = {"US", "SC", "OT", "XC", "ES", "DOC", "SM"}
METHOD = "dicom-agent-mcp basic profile"


class Deidentifier:
    def __init__(self, key: bytes | None, date_mode: DateMode = "shift", keep_descriptions: bool = False, keep_private: bool = False):
        # Without a configured key, pseudonyms are random per call and cannot be linked across calls.
        self.linkable = key is not None
        self.key = key or secrets.token_bytes(32)
        self.date_mode = date_mode
        self.keep_descriptions = keep_descriptions
        self.keep_private = keep_private

    def _digest(self, *parts: str) -> str:
        return hmac.new(self.key, "|".join(parts).encode(), hashlib.sha256).hexdigest()

    def uid(self, value: str) -> str:
        # generate_uid(prefix=None) ignores entropy_srcs, so derive the 2.25 UUID-style UID from the HMAC directly.
        return f"2.25.{int(self._digest('uid', value)[:32], 16)}"

    def date_offset(self, patient_id: str) -> dt.timedelta:
        return dt.timedelta(days=-(1 + int(self._digest("date", patient_id), 16) % 730))

    def apply(self, ds: Dataset) -> list[str]:
        """De-identify in place and return warnings about residual risk."""
        warnings: list[str] = []
        patient_id = str(ds.get("PatientID", ""))
        pseudonym = self._digest("patient", patient_id)[:10].upper()
        offset = self.date_offset(patient_id)

        if ds.get("BurnedInAnnotation") == "YES":
            warnings.append("BurnedInAnnotation is YES: the pixels contain text that was not removed.")
        elif ds.get("Modality") in PIXEL_RISK_MODALITIES or "Secondary Capture" in UID(str(ds.get("SOPClassUID", ""))).name:
            warnings.append(f"{ds.get('Modality')} images often carry burned-in identifiers; review the pixels.")

        if not self.keep_private:
            ds.remove_private_tags()
        for keyword in REMOVE:
            if keyword in ds:
                delattr(ds, keyword)
        for keyword in EMPTY + ([] if self.keep_descriptions else DESCRIPTIONS):
            if keyword in ds:
                ds[keyword].value = ""
        for tag in [t for t in ds.keys() if t.group & 0xFF00 in (0x5000, 0x6000) and t.group % 2 == 0]:
            del ds[tag]  # curves and overlays can hold annotations

        def visit(dataset: Dataset, elem: DataElement) -> None:
            if elem.VR == "UI" and elem.value:
                elem.value = self._map_uids(elem.value)
            elif elem.VR == "PN" and elem.keyword != "PatientName":
                elem.value = ""
            elif elem.VR in ("DA", "DT") and elem.value and self.date_mode == "shift":
                elem.value = _shift(elem.value, elem.VR, offset)

        ds.walk(visit)
        ds.PatientName = f"ANON^{pseudonym}"
        ds.PatientID = f"ANON{pseudonym}"
        ds.PatientIdentityRemoved = "YES"
        ds.LongitudinalTemporalInformationModified = "MODIFIED" if self.date_mode == "shift" else "UNMODIFIED"
        if self.keep_descriptions:
            warnings.append("Study/series descriptions and protocol names were kept unreviewed.")
        if self.keep_private:
            warnings.append("Private tags were kept; they may contain identifiers.")
        ds.DeidentificationMethod = METHOD
        ds.DeidentificationMethodCodeSequence = Sequence([_code(v, m) for v, m in self.profile_codes()])
        if "SOPInstanceUID" in ds and hasattr(ds, "file_meta"):
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        return warnings

    def profile_codes(self) -> list[tuple[str, str]]:
        """The Basic Profile and options applied (CID 7050 codes), as written to DeidentificationMethodCodeSequence."""
        codes = [("113100", "Basic Application Confidentiality Profile"), ("113108", "Retain Patient Characteristics Option")]
        if self.date_mode == "shift":
            codes.append(("113107", "Retain Longitudinal Temporal Information Modified Dates Option"))
        else:
            codes.append(("113106", "Retain Longitudinal Temporal Information Full Dates Option"))
        if self.keep_private:
            codes.append(("113111", "Retain Safe Private Option"))
        return codes

    @property
    def key_id(self) -> str | None:
        """A one-way identifier of the configured key, so runs with the same key can be matched. None for random keys."""
        return f"hmac-sha256:{self._digest('key-id')[:16]}" if self.linkable else None

    def _map_uids(self, value: Any) -> Any:
        if isinstance(value, (list, tuple)) or (hasattr(value, "__iter__") and not isinstance(value, str)):
            return [self._map_one(v) for v in value]
        return self._map_one(value)

    def _map_one(self, value: str) -> str:
        uid = UID(str(value))
        # Registered UIDs (SOP classes, transfer syntaxes, coding schemes) identify standards, not people.
        return str(uid) if uid.name != str(uid) else self.uid(str(uid))


def _code(value: str, meaning: str) -> Dataset:
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = "DCM"
    item.CodeMeaning = meaning
    return item


def _shift(value: Any, vr: str, offset: dt.timedelta) -> Any:
    if not isinstance(value, str) and hasattr(value, "__iter__"):
        return [_shift(v, vr, offset) for v in value]
    text = str(value)
    try:
        shifted = (dt.datetime.strptime(text[:8], "%Y%m%d") + offset).strftime("%Y%m%d")
    except ValueError:
        return ""  # unparseable dates are dropped rather than kept
    return shifted if vr == "DA" else shifted + text[8:]


def _short(uid: Any) -> str:
    return hashlib.sha256(check_uid(str(uid)).encode()).hexdigest()[:16]


def deidentify_files(files: list[Path], output_root: Path, deidentifier: Deidentifier) -> dict[str, Any]:
    outputs: list[dict[str, str]] = []
    warnings: set[str] = set()
    for path in files:
        ds = read_dataset(path)
        warnings.update(deidentifier.apply(ds))
        # Short digests keep paths under Windows MAX_PATH; the full UIDs stay inside the files.
        target = output_root / _short(ds.StudyInstanceUID) / _short(ds.SeriesInstanceUID)
        target.mkdir(parents=True, exist_ok=True)
        out = target / f"{_short(ds.SOPInstanceUID)}.dcm"
        ds.save_as(out, enforce_file_format=True)
        outputs.append({
            "source": str(path), "output": str(out),
            "study_uid": str(ds.StudyInstanceUID), "series_uid": str(ds.SeriesInstanceUID), "sop_instance_uid": str(ds.SOPInstanceUID),
        })
    return {
        "count": len(outputs), "output_dir": str(output_root), "files": outputs,
        "linkable": deidentifier.linkable, "date_mode": deidentifier.date_mode,
        "method": METHOD, "profile": [f"{v} {m}" for v, m in deidentifier.profile_codes()], "key_id": deidentifier.key_id,
        "keep_private": deidentifier.keep_private, "keep_descriptions": deidentifier.keep_descriptions,
        "warnings": sorted(warnings),
        "note": "Basic-profile subset; pixel data is not scrubbed. Review before sharing outside your organization.",
    }
