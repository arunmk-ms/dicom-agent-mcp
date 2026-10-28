"""Protocol-neutral DICOM operations. Each method picks DIMSE or DICOMweb from the node configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import httpx
from pydicom import dcmread
from pydicom.errors import InvalidDicomError

from . import imaging
from .config import DicomWebNode, DimseNode, Settings
from .deid import Deidentifier, deidentify_files
from .datasets import (
    DEFAULT_RETURN_KEYS,
    Level,
    build_identifier,
    check_uid,
    parse_level,
    qido_params,
    to_friendly,
)
from .dicomweb import DicomWebClient
from .dimse import DimseClient
from .errors import DicomMcpError

MAX_STORE_FILES = 10_000
METADATA_FIELDS = DEFAULT_RETURN_KEYS[Level.IMAGE] + [
    "Modality", "SeriesNumber", "SeriesDescription", "ImageType", "Rows", "Columns",
    "NumberOfFrames", "PixelSpacing", "SliceThickness", "ImagePositionPatient",
    "ImageOrientationPatient", "FrameOfReferenceUID", "PatientID", "PatientName",
]


class DicomService:
    def __init__(self, settings: Settings, http_transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http_transport = http_transport

    # ---- nodes -------------------------------------------------------------------------------

    def _node(self, name: str) -> DimseNode | DicomWebNode:
        node = self.settings.nodes.get(name)
        if node is None:
            raise DicomMcpError(f"Unknown node '{name}'. Configured nodes: {', '.join(self.settings.nodes) or 'none'}.")
        return node

    def _client(self, name: str) -> DimseClient | DicomWebClient:
        node = self._node(name)
        if isinstance(node, DimseNode):
            return DimseClient(name, node, self.settings)
        return DicomWebClient(name, node, self.settings, self.http_transport)

    def _writable(self, name: str) -> DimseClient | DicomWebClient:
        if self._node(name).read_only:
            raise DicomMcpError(f"Node '{name}' is configured as read-only.")
        return self._client(name)

    def list_nodes(self) -> list[dict[str, Any]]:
        out = []
        for name, node in self.settings.nodes.items():
            entry: dict[str, Any] = {"name": name, "protocol": node.protocol, "description": node.description, "read_only": node.read_only}
            if isinstance(node, DimseNode):
                entry.update(ae_title=node.ae_title, host=node.host, port=node.port)
            else:
                entry.update(base_url=node.base_url)
            out.append(entry)
        return out

    def echo(self, node: str) -> dict[str, Any]:
        return {"node": node, **self._client(node).echo()}

    # ---- query -------------------------------------------------------------------------------

    def search(
        self,
        node: str,
        level: str,
        filters: Mapping[str, Any] | None = None,
        return_fields: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        lvl = parse_level(level)
        filters = dict(filters or {})
        limit = max(1, min(limit, self.settings.max_results))
        offset = max(0, offset)
        keys = list(dict.fromkeys([*DEFAULT_RETURN_KEYS[lvl], *(return_fields or [])]))
        client = self._client(node)
        if isinstance(client, DimseClient):
            # C-FIND has no paging, so fetch offset + limit and slice locally.
            identifier = build_identifier(lvl, filters, keys)
            found, truncated = client.find(lvl, identifier, offset + limit)
            found = found[offset:]
            operation = "C-FIND"
        else:
            found, truncated = client.search(lvl, qido_params(filters, keys), limit, offset)
            operation = "QIDO-RS"
        results = [{k: v for k, v in to_friendly(ds).items() if k != "QueryRetrieveLevel"} for ds in found]
        return {
            "node": node, "operation": operation, "level": lvl.value,
            "count": len(results), "offset": offset, "truncated": truncated, "results": results,
        }

    def get_metadata(
        self,
        node: str,
        study_uid: str,
        series_uid: str | None = None,
        sop_instance_uid: str | None = None,
        fields: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, self.settings.max_results))
        client = self._client(node)
        if isinstance(client, DimseClient):
            filters = {"StudyInstanceUID": check_uid(study_uid)}
            if series_uid:
                filters["SeriesInstanceUID"] = check_uid(series_uid)
            if sop_instance_uid:
                filters["SOPInstanceUID"] = check_uid(sop_instance_uid)
            keys = list(dict.fromkeys([*METADATA_FIELDS, *(fields or [])]))
            found, truncated = client.find(Level.IMAGE, build_identifier(Level.IMAGE, filters, keys), limit)
            note = "C-FIND returns only attributes the archive indexes; use retrieve + read_local_metadata for the full header."
            operation = "C-FIND"
        else:
            found = client.metadata(study_uid, series_uid, sop_instance_uid)
            truncated = len(found) > limit
            found = found[:limit]
            note = None
            operation = "WADO-RS"
        instances = []
        for ds in found:
            item = {k: v for k, v in to_friendly(ds).items() if k != "QueryRetrieveLevel"}
            if fields and operation == "WADO-RS":
                wanted = set(fields) | {"SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID"}
                item = {k: v for k, v in item.items() if k in wanted}
            instances.append(item)
        result = {"node": node, "operation": operation, "count": len(instances), "truncated": truncated, "instances": instances}
        if note:
            result["note"] = note
        return result

    # ---- retrieve / move / store -------------------------------------------------------------

    def retrieve(self, node: str, study_uid: str, series_uid: str | None = None, sop_instance_uid: str | None = None) -> dict[str, Any]:
        dest = self.settings.storage_dir
        dest.mkdir(parents=True, exist_ok=True)
        client = self._client(node)
        if isinstance(client, DimseClient):
            level, identifier = _retrieve_identifier(study_uid, series_uid, sop_instance_uid)
            result = client.get(level, identifier, dest)
        else:
            result = client.retrieve(study_uid, series_uid, sop_instance_uid, dest)
        return {"node": node, "storage_dir": str(dest), **result}

    def move(
        self, node: str, destination: str, study_uid: str, series_uid: str | None = None, sop_instance_uid: str | None = None
    ) -> dict[str, Any]:
        client = self._client(node)
        if not isinstance(client, DimseClient):
            raise DicomMcpError("C-MOVE is a DIMSE operation. For DICOMweb nodes use the 'transfer' tool instead.")
        dest_node = self.settings.nodes.get(destination)
        if dest_node is not None:
            if not isinstance(dest_node, DimseNode):
                raise DicomMcpError(f"Destination '{destination}' is a DICOMweb node; use the 'transfer' tool instead.")
            if dest_node.read_only:
                raise DicomMcpError(f"Destination '{destination}' is configured as read-only.")
            destination_aet = dest_node.ae_title
        else:
            destination_aet = destination  # A raw AE title the source archive already knows.
        level, identifier = _retrieve_identifier(study_uid, series_uid, sop_instance_uid)
        return {"node": node, **client.move(level, identifier, destination_aet)}

    def store(self, node: str, paths: list[str]) -> dict[str, Any]:
        client = self._writable(node)
        files, skipped = self._collect_dicom_files(paths)
        if not files:
            raise DicomMcpError("No DICOM files found to store." + (f" Skipped: {skipped[:5]}" if skipped else ""))
        result = client.store(files)
        return {"node": node, "skipped": skipped, **result}

    def transfer(
        self, source: str, destination: str, study_uid: str, series_uid: str | None = None, sop_instance_uid: str | None = None
    ) -> dict[str, Any]:
        """Copy data between any two nodes (DIMSE or DICOMweb) through the local cache."""
        self._writable(destination)
        retrieved = self.retrieve(source, study_uid, series_uid, sop_instance_uid)
        if not retrieved["files"]:
            raise DicomMcpError(f"Nothing was retrieved from '{source}'; nothing to transfer.")
        stored = self.store(destination, retrieved["files"])
        return {"source": source, "destination": destination, "retrieve": retrieved, "store": stored}

    # ---- pixels and geometry ------------------------------------------------------------------

    def render_image(
        self,
        path: str,
        frame: int = 0,
        window: str = "auto",
        window_center: float | None = None,
        window_width: float | None = None,
        max_size: int = 512,
    ) -> tuple[bytes, dict[str, Any]]:
        file = self._check_file(path)
        png, info = imaging.render_frame(imaging.read_dataset(file), frame, window, window_center, window_width, max_size)
        return png, {"path": str(file), **info, "png_path": self._save_png(png, f"{file.stem}_f{frame}", info)}

    def pixel_statistics(self, path: str, frame: int = 0, roi: Mapping[str, Any] | None = None) -> dict[str, Any]:
        file = self._check_file(path)
        region = imaging.Roi(**roi) if roi else None
        return {"path": str(file), **imaging.pixel_statistics(imaging.read_dataset(file), frame, region)}

    def measure_distance(self, path: str, start: list[float], end: list[float], frame: int = 0) -> dict[str, Any]:
        file = self._check_file(path)
        if len(start) != 2 or len(end) != 2:
            raise DicomMcpError("start and end must be [row, column] pairs.")
        ds = imaging.read_dataset(file, pixels=False)
        return {"path": str(file), **imaging.measure_distance(ds, (start[0], start[1]), (end[0], end[1]), frame)}

    def check_geometry(self, paths: list[str]) -> dict[str, Any]:
        files, skipped = self._collect_dicom_files(paths)
        if not files:
            raise DicomMcpError("No DICOM files found.")
        reports = [imaging.analyze_series(uid, slices) for uid, slices in imaging.group_series(files).items()]
        return {
            "ok": not any(r.errors for r in reports),
            "series": [r.to_dict() for r in reports],
            "skipped": skipped,
        }

    def render_mpr(
        self,
        paths: list[str],
        plane: str = "coronal",
        index: int | None = None,
        series_uid: str | None = None,
        window: str = "auto",
        window_center: float | None = None,
        window_width: float | None = None,
        max_size: int = 512,
    ) -> tuple[bytes, dict[str, Any]]:
        files, _ = self._collect_dicom_files(paths)
        groups = imaging.group_series(files)
        if series_uid:
            if series_uid not in groups:
                raise DicomMcpError(f"Series {series_uid} is not among the given files.")
            groups = {series_uid: groups[series_uid]}
        elif len(groups) != 1:
            raise DicomMcpError(f"The files contain {len(groups)} series; pass series_uid to pick one: {', '.join(groups)}.")
        uid, slices = next(iter(groups.items()))
        volume = imaging.load_volume(imaging.analyze_series(uid, slices), self.settings.max_volume_voxels)
        image, info = imaging.reformat(volume, plane, index)  # type: ignore[arg-type]
        header = slices[0].header
        c, w, source = imaging.resolve_window(header, image, window, window_center, window_width)
        pixels = imaging.apply_window(image, c, w)
        if header.get("PhotometricInterpretation") == "MONOCHROME1":
            pixels = 255 - pixels
        png, size = imaging.to_png(pixels, tuple(info["pixel_spacing_mm"]), max_size)
        info = {
            "series_uid": uid, **info,
            "window": {"center": c, "width": w, "source": source, "units": volume.units},
            "png_size": list(size), "input_fingerprint": volume.fingerprint,
        }
        info["png_path"] = self._save_png(png, f"mpr_{plane}_{info['index']}", info)
        return png, info

    def deidentify(
        self, paths: list[str], date_mode: str = "shift", keep_descriptions: bool = False, keep_private: bool = False
    ) -> dict[str, Any]:
        if date_mode not in ("shift", "keep"):
            raise DicomMcpError("date_mode must be 'shift' or 'keep'.")
        files, skipped = self._collect_dicom_files(paths)
        if not files:
            raise DicomMcpError("No DICOM files found to de-identify.")
        key = os.environ.get(self.settings.deid_key_env)
        deidentifier = Deidentifier(key.encode() if key else None, date_mode, keep_descriptions, keep_private)  # type: ignore[arg-type]
        result = deidentify_files(files, self.settings.storage_dir / "deidentified", deidentifier)
        return {**result, "skipped": skipped}

    def _check_file(self, path: str) -> Path:
        file = self._check_path(path)
        if not file.is_file():
            raise DicomMcpError(f"Not a file: {file}")
        return file

    def _save_png(self, png: bytes, stem: str, info: Mapping[str, Any]) -> str:
        folder = self.settings.storage_dir / "renders"
        folder.mkdir(parents=True, exist_ok=True)
        tag = str(info.get("input_fingerprint", ""))[7:15]
        out = folder / f"{stem}_{tag}.png"
        out.write_bytes(png)
        return str(out)

    # ---- local files ---------------------------------------------------------------------------

    def read_local_metadata(self, path: str, max_depth: int = 2) -> dict[str, Any]:
        file = self._check_path(path)
        if not file.is_file():
            raise DicomMcpError(f"Not a file: {file}")
        try:
            ds = dcmread(file, stop_before_pixels=True)
        except InvalidDicomError as exc:
            raise DicomMcpError(f"Not a DICOM file: {file}") from exc
        return {
            "path": str(file),
            "transfer_syntax": str(ds.file_meta.get("TransferSyntaxUID", "")),
            "attributes": to_friendly(ds, max_depth=max_depth),
        }

    def list_local_files(self, study_uid: str | None = None, limit: int = 200) -> dict[str, Any]:
        root = self.settings.storage_dir
        base = root / check_uid(study_uid) if study_uid else root
        files = sorted(str(p) for p in base.rglob("*.dcm")) if base.exists() else []
        return {"storage_dir": str(root), "count": len(files), "truncated": len(files) > limit, "files": files[:limit]}

    def _check_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.settings.storage_dir / candidate
        resolved = candidate.resolve()
        roots = self.settings.readable_roots()
        if not any(resolved.is_relative_to(root) for root in roots):
            raise DicomMcpError(
                f"Path '{path}' is outside the allowed directories: {', '.join(str(r) for r in roots)}."
            )
        return resolved

    def _collect_dicom_files(self, paths: list[str]) -> tuple[list[Path], list[str]]:
        files: list[Path] = []
        skipped: list[str] = []
        for raw in paths:
            path = self._check_path(raw)
            candidates = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for candidate in candidates:
                if len(files) >= MAX_STORE_FILES:
                    raise DicomMcpError(f"More than {MAX_STORE_FILES} files; store in smaller batches.")
                try:
                    dcmread(candidate, stop_before_pixels=True)
                    files.append(candidate)
                except (InvalidDicomError, OSError):
                    skipped.append(str(candidate))
        return files, skipped


def _retrieve_identifier(study_uid: str, series_uid: str | None, sop_instance_uid: str | None):
    if sop_instance_uid and not series_uid:
        raise DicomMcpError("sop_instance_uid requires series_uid.")
    filters = {"StudyInstanceUID": check_uid(study_uid)}
    level = Level.STUDY
    if series_uid:
        filters["SeriesInstanceUID"] = check_uid(series_uid)
        level = Level.SERIES
    if sop_instance_uid:
        filters["SOPInstanceUID"] = check_uid(sop_instance_uid)
        level = Level.IMAGE
    return level, build_identifier(level, filters, [])
