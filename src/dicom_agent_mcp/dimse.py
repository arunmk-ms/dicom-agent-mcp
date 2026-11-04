"""DIMSE (classic DICOM networking) client: C-ECHO, C-FIND, C-MOVE, C-GET and C-STORE."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.uid import (
    UID,
    DeflatedExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEG2000,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLosslessSV1,
    JPEGLSLossless,
    RLELossless,
)
from pynetdicom import AE, StoragePresentationContexts, build_role, evt
from pynetdicom.association import Association
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelGet,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)
from pynetdicom.status import code_to_category

from .config import DimseNode, Settings
from .datasets import Level, instance_path
from .errors import DicomMcpError

log = logging.getLogger(__name__)

UNCOMPRESSED = [ExplicitVRLittleEndian, ImplicitVRLittleEndian, DeflatedExplicitVRLittleEndian, ExplicitVRBigEndian]
# Accepted when acting as Storage SCP during C-GET, so the archive can send images without transcoding.
RECEIVE_TRANSFER_SYNTAXES = UNCOMPRESSED + [
    JPEGBaseline8Bit, JPEGExtended12Bit, JPEGLosslessSV1, JPEGLSLossless, JPEG2000Lossless, JPEG2000, RLELossless,
]
MAX_CONTEXTS = 128
PENDING = (0xFF00, 0xFF01)

_MODELS = {
    "find": (PatientRootQueryRetrieveInformationModelFind, StudyRootQueryRetrieveInformationModelFind),
    "move": (PatientRootQueryRetrieveInformationModelMove, StudyRootQueryRetrieveInformationModelMove),
    "get": (PatientRootQueryRetrieveInformationModelGet, StudyRootQueryRetrieveInformationModelGet),
}


def _model(kind: str, level: Level) -> UID:
    patient_root, study_root = _MODELS[kind]
    return patient_root if level is Level.PATIENT else study_root


def _status_text(code: int) -> str:
    return f"0x{code:04X} ({code_to_category(code)})"


class DimseClient:
    def __init__(self, name: str, node: DimseNode, settings: Settings):
        self.name = name
        self.node = node
        self.settings = settings

    def _ae(self) -> AE:
        ae = AE(ae_title=self.settings.calling_ae_title)
        t = self.settings.timeouts
        ae.acse_timeout = t.acse
        ae.dimse_timeout = t.dimse
        ae.network_timeout = t.network
        ae.connection_timeout = t.network
        return ae

    @contextmanager
    def _associate(self, ae: AE, **kwargs: Any) -> Iterator[Association]:
        assoc = ae.associate(self.node.host, self.node.port, ae_title=self.node.ae_title, **kwargs)
        if not assoc.is_established:
            raise DicomMcpError(
                f"Could not open an association with node '{self.name}' "
                f"({self.node.ae_title}@{self.node.host}:{self.node.port}): "
                f"{'rejected' if assoc.is_rejected else 'aborted or unreachable'}."
            )
        try:
            yield assoc
        finally:
            if assoc.is_established:
                assoc.release()

    def echo(self) -> dict[str, Any]:
        ae = self._ae()
        ae.add_requested_context(Verification)
        with self._associate(ae) as assoc:
            status = assoc.send_c_echo()
        if not status:
            raise DicomMcpError(f"C-ECHO to '{self.name}' got no response.")
        return {"ok": status.Status == 0x0000, "status": _status_text(status.Status), "operation": "C-ECHO"}

    def find(self, level: Level, identifier: Dataset, max_results: int) -> tuple[list[Dataset], bool]:
        """Return up to ``max_results`` matches and whether more were available."""
        model = _model("find", level)
        ae = self._ae()
        ae.add_requested_context(model)
        results: list[Dataset] = []
        truncated = False
        with self._associate(ae) as assoc:
            for status, ident in assoc.send_c_find(identifier, model):
                if not status:
                    raise DicomMcpError(f"C-FIND to '{self.name}' timed out or the association was aborted.")
                code = status.Status
                if code in PENDING:
                    if ident is not None:
                        results.append(ident)
                    if len(results) > max_results:
                        truncated = True
                        break
                elif code == 0x0000:
                    break
                else:
                    raise DicomMcpError(f"C-FIND on '{self.name}' failed with status {_status_text(code)}.")
            if truncated:
                assoc.abort()
        return results[:max_results], truncated

    def move(self, level: Level, identifier: Dataset, destination_aet: str) -> dict[str, Any]:
        model = _model("move", level)
        ae = self._ae()
        ae.add_requested_context(model)
        final: Dataset | None = None
        final_ident: Dataset | None = None
        with self._associate(ae) as assoc:
            for status, ident in assoc.send_c_move(identifier, destination_aet, model):
                if not status:
                    raise DicomMcpError(f"C-MOVE on '{self.name}' timed out or the association was aborted.")
                final, final_ident = status, ident
                if status.Status not in PENDING:
                    break
        return {"operation": "C-MOVE", "destination_ae_title": destination_aet, **_suboperations(final, final_ident)}

    def get(self, level: Level, identifier: Dataset, dest: Path) -> dict[str, Any]:
        """C-GET: the archive sends the matching instances back over the same association."""
        model = _model("get", level)
        storage = [cx.abstract_syntax for cx in StoragePresentationContexts][: MAX_CONTEXTS - 1]
        ae = self._ae()
        ae.add_requested_context(model)
        for sop_class in storage:
            ae.add_requested_context(sop_class, RECEIVE_TRANSFER_SYNTAXES)
        roles = [build_role(sop_class, scp_role=True) for sop_class in storage]

        files: list[str] = []
        errors: list[str] = []

        def on_store(event: evt.Event) -> int:
            try:
                ds = event.dataset
                ds.file_meta = event.file_meta
                path = instance_path(dest, ds)
                path.parent.mkdir(parents=True, exist_ok=True)
                ds.save_as(path, enforce_file_format=True)
                files.append(str(path))
                return 0x0000
            except Exception as exc:
                log.exception("Failed to save instance received by C-GET")
                errors.append(str(exc))
                return 0xA700

        final: Dataset | None = None
        final_ident: Dataset | None = None
        with self._associate(ae, ext_neg=roles, evt_handlers=[(evt.EVT_C_STORE, on_store)]) as assoc:
            for status, ident in assoc.send_c_get(identifier, model):
                if not status:
                    raise DicomMcpError(f"C-GET on '{self.name}' timed out or the association was aborted.")
                final, final_ident = status, ident
                if status.Status not in PENDING:
                    break
        return {"operation": "C-GET", "files": files, "save_errors": errors, **_suboperations(final, final_ident)}

    def store(self, paths: list[Path]) -> dict[str, Any]:
        headers = [(p, dcmread(p, stop_before_pixels=True)) for p in paths]
        contexts: dict[str, set[str]] = {}
        for _, ds in headers:
            ts = str(ds.file_meta.TransferSyntaxUID)
            syntaxes = contexts.setdefault(str(ds.SOPClassUID), set())
            syntaxes.add(ts)
            if UID(ts) in UNCOMPRESSED:
                syntaxes.update(str(u) for u in (ExplicitVRLittleEndian, ImplicitVRLittleEndian))
        if len(contexts) > MAX_CONTEXTS:
            raise DicomMcpError(f"Too many distinct SOP classes ({len(contexts)}) for one association; store in smaller batches.")

        ae = self._ae()
        for sop_class, syntaxes in contexts.items():
            ae.add_requested_context(sop_class, sorted(syntaxes))

        results = []
        with self._associate(ae) as assoc:
            for path, ds in headers:
                try:
                    status = assoc.send_c_store(path)
                except Exception as exc:  # e.g. no accepted presentation context for this SOP class
                    results.append({"path": str(path), "sop_instance_uid": str(ds.SOPInstanceUID), "ok": False, "error": str(exc)})
                    continue
                code = status.Status if status else None
                results.append({
                    "path": str(path),
                    "sop_instance_uid": str(ds.SOPInstanceUID),
                    "ok": code is not None and code_to_category(code) in ("Success", "Warning"),
                    "status": _status_text(code) if code is not None else "no response",
                })
        stored = sum(r["ok"] for r in results)
        return {"operation": "C-STORE", "stored": stored, "failed": len(results) - stored, "results": results}


def _suboperations(status: Dataset | None, identifier: Dataset | None = None) -> dict[str, Any]:
    if status is None:
        return {"ok": False, "status": "no response"}
    out: dict[str, Any] = {
        "ok": status.Status == 0x0000,
        "status": _status_text(status.Status),
        "completed": int(status.get("NumberOfCompletedSuboperations", 0) or 0),
        "failed": int(status.get("NumberOfFailedSuboperations", 0) or 0),
        "warning": int(status.get("NumberOfWarningSuboperations", 0) or 0),
    }
    failed_uids = identifier.get("FailedSOPInstanceUIDList") if identifier is not None else None
    if failed_uids:
        out["failed_sop_instance_uids"] = [str(failed_uids)] if isinstance(failed_uids, str) else [str(u) for u in failed_uids]
    return out
