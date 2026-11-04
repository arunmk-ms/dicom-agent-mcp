"""Protocol-neutral DICOM operations. Each method picks DIMSE or DICOMweb from the node configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
from pydicom import dcmread
from pydicom.errors import InvalidDicomError

from . import contracts, imaging
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
from .verification import HARD_FAIL, PASS, SOFT_FAIL, Check, VerificationError, Verifier, digest, file_sha256, overall, summary, worst

MAX_STORE_FILES = 10_000
CLAIM_TOOLS = ("pixel_statistics", "measure_distance", "check_geometry")
METADATA_FIELDS = DEFAULT_RETURN_KEYS[Level.IMAGE] + [
    "Modality", "SeriesNumber", "SeriesDescription", "ImageType", "Rows", "Columns",
    "NumberOfFrames", "PixelSpacing", "SliceThickness", "ImagePositionPatient",
    "ImageOrientationPatient", "FrameOfReferenceUID", "PatientID", "PatientName",
]


class DicomService:
    def __init__(self, settings: Settings, http_transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.http_transport = http_transport
        self.verifier = Verifier(settings.verification, settings.ledger_path)

    # ---- verification ------------------------------------------------------------------------

    def _verified(
        self,
        tool: str,
        args: Mapping[str, Any],
        run: Callable[[dict[str, Any]], Any],
        *,
        setup: Callable[[dict[str, Any]], None] | None = None,
        pre: Callable[[dict[str, Any]], list[Check]] | None = None,
        post: Callable[[dict[str, Any], Any], Any] | None = None,
    ) -> Any:
        """Run a tool under its contract: preconditions, the tool, postconditions, one ledger record.

        ``ctx`` is a scratch dict shared by setup/pre/run/post so inputs are read once. Results that are
        (png, info) tuples get the verification block on ``info``.
        """
        ctx: dict[str, Any] = {}
        if not self.verifier.enabled:
            if setup:
                setup(ctx)
            return run(ctx)
        checks: list[Check] = []
        try:
            if setup:
                setup(ctx)
            checks = list(pre(ctx)) if pre else []
            if self.verifier.blocks(overall(checks), commit=False, accept_soft_fail=False):
                self.verifier.block(tool, args, checks, commit=False)
            result = run(ctx)
            extra: dict[str, Any] = {}
            if post:
                out = post(ctx, result)
                if isinstance(out, tuple):
                    out, extra = out
                checks += out
        except VerificationError:
            raise
        except DicomMcpError as exc:
            self.verifier.record(tool, "tool", args, [*checks, Check("post", "TOOL_ERROR", HARD_FAIL, str(exc))])
            raise
        info = result[1] if isinstance(result, tuple) else result
        fingerprints = extra.pop("input_fingerprints", None)
        if fingerprints is None:
            fingerprints = [info["input_fingerprint"]] if info.get("input_fingerprint") else []
        output = digest(info, result[0] if isinstance(result, tuple) else None)
        record = self.verifier.record(tool, "tool", args, checks, input_fingerprints=fingerprints, output_digest=output, **extra)
        info["verification"] = summary(record)
        return result

    def verify_claim(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        claimed: Mapping[str, Any],
        input_fingerprint: str | None = None,
        tolerance: float | None = None,
    ) -> dict[str, Any]:
        """Recompute a tool result independently and compare the claimed fields with it."""
        if tool not in CLAIM_TOOLS:
            raise DicomMcpError(f"verify_claim supports {', '.join(CLAIM_TOOLS)}; got '{tool}'.")
        if not claimed:
            raise DicomMcpError("claimed must name at least one field, e.g. {\"mean\": 41.2}.")
        if tolerance is not None and tolerance < 0:
            raise DicomMcpError("tolerance must not be negative.")
        args = dict(arguments)
        try:
            if tool == "pixel_statistics":
                actual = self._statistics_raw(args["path"], int(args.get("frame", 0)), args.get("roi"))
            elif tool == "measure_distance":
                actual = self._distance_raw(args["path"], args["start"], args["end"], int(args.get("frame", 0)))
            else:
                actual = self._geometry_raw(args["paths"])[0]
        except KeyError as exc:
            raise DicomMcpError(f"arguments for {tool} are missing {exc}.") from exc
        except TypeError as exc:
            raise DicomMcpError(f"Invalid arguments for {tool}: {exc}.") from exc

        checks: list[Check] = []
        if input_fingerprint:
            recomputed = actual.get("input_fingerprint")
            if recomputed is None:
                checks.append(Check("pre", "FINGERPRINT_UNAVAILABLE", SOFT_FAIL, f"{tool} results carry no input fingerprint to compare."))
            elif recomputed != input_fingerprint:
                checks.append(Check("pre", "FINGERPRINT_MISMATCH", HARD_FAIL,
                                    "The pixels at this path differ from the ones the claim was computed on.",
                                    {"claimed": input_fingerprint, "actual": recomputed}))
            else:
                checks.append(Check("pre", "FINGERPRINT_MATCH", PASS, "The claim was computed on these pixels."))
        fields = []
        for path, value in claimed.items():
            found, recomputed = contracts.lookup(actual, path)
            if not found:
                checks.append(Check("post", "UNKNOWN_FIELD", HARD_FAIL, f"'{path}' is not a field of the {tool} result."))
                fields.append({"field": path, "claimed": value, "verdict": HARD_FAIL})
                continue
            ok, tol = contracts.compare_claim(value, recomputed, tolerance)
            entry = {"field": path, "claimed": value, "actual": recomputed, "verdict": PASS if ok else HARD_FAIL}
            if tol is not None:
                entry["tolerance"] = tol
            fields.append(entry)
            checks.append(Check("post", "CLAIM_MATCH" if ok else "CLAIM_MISMATCH", entry["verdict"],
                                f"{path}: claimed {value!r}, recomputed {recomputed!r}" + (f" (tolerance {tol:g})" if tol is not None else "") + "."))
        out: dict[str, Any] = {"tool": tool, "verdict": overall(checks), "fields": fields}
        if actual.get("input_fingerprint"):
            out["input_fingerprint"] = actual["input_fingerprint"]
        if self.verifier.enabled:
            fps = [actual["input_fingerprint"]] if actual.get("input_fingerprint") else []
            record = self.verifier.record(tool, "claim", {"arguments": args, "claimed": dict(claimed), "tolerance": tolerance}, checks,
                                          input_fingerprints=fps)
            out["verification"] = summary(record)
        return out

    def get_verification(self, record_id: str | None = None, fingerprint: str | None = None, limit: int = 20) -> dict[str, Any]:
        ledger = self.verifier.ledger
        limit = max(1, min(limit, self.settings.max_results))
        if record_id:
            record = ledger.get(record_id)
            if record is None:
                raise DicomMcpError(f"No verification record '{record_id}' in {ledger.path}.")
            records = [record]
        elif fingerprint:
            records = ledger.find(fingerprint=fingerprint) or ledger.find(file_digest=fingerprint)
        else:
            records = ledger.records()
        return {
            "policy": self.verifier.policy, "ledger": str(ledger.path), "integrity": ledger.check_chain(),
            "count": len(records), "truncated": len(records) > limit, "records": records[-limit:],
        }

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
        out: dict[str, Any] = {
            "node": node, "operation": operation, "level": lvl.value,
            "status": "truncated" if truncated else ("ok" if results else "empty"),
            "count": len(results), "offset": offset, "truncated": truncated, "results": results,
        }
        if not results:
            protocol = "dimse" if operation == "C-FIND" else "qido"
            out["query"] = {"level": lvl.value, "filters": filters, "return_fields": return_fields or []}
            unsupported = contracts.optional_keys(protocol, lvl, filters)
            if unsupported:
                out["possibly_unsupported_keys"] = unsupported
            out["hint"] = (
                "No matches. An empty result is not proof that nothing exists: check wildcards (e.g. 'SMITH*'), "
                "date ranges ('20240101-20241231'), the level"
                + (", and keys the archive may not index (see possibly_unsupported_keys)." if unsupported else ".")
            )
        return out

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
        args = {"node": node, "study_uid": study_uid, "series_uid": series_uid, "sop_instance_uid": sop_instance_uid}

        def run(_: dict[str, Any]) -> dict[str, Any]:
            dest = self.settings.storage_dir
            dest.mkdir(parents=True, exist_ok=True)
            client = self._client(node)
            if isinstance(client, DimseClient):
                level, identifier = _retrieve_identifier(study_uid, series_uid, sop_instance_uid)
                result = client.get(level, identifier, dest)
            else:
                result = client.retrieve(study_uid, series_uid, sop_instance_uid, dest)
            errors = result.get("save_errors") or []
            failed = max(int(result.get("failed", 0) or 0), len(errors))
            items = [{"sop_instance_uid": u} for u in result.get("failed_sop_instance_uids", [])] + [{"error": e} for e in errors]
            out = {"node": node, "storage_dir": str(dest), **_with_outcome(result, _outcome(len(result["files"]), failed, result.get("ok", True)))}
            if items:
                out["failed_items"] = items
            return out

        def post(_: dict[str, Any], result: dict[str, Any]) -> tuple[list[Check], dict[str, Any]]:
            checks = contracts.retrieve_post(result, study_uid, series_uid, sop_instance_uid)
            return checks, {"output_files": {f: file_sha256(Path(f)) for f in result["files"]}}

        return self._verified("retrieve", args, run, post=post)

    def move(
        self, node: str, destination: str, study_uid: str, series_uid: str | None = None, sop_instance_uid: str | None = None
    ) -> dict[str, Any]:
        args = {"node": node, "destination": destination, "study_uid": study_uid, "series_uid": series_uid, "sop_instance_uid": sop_instance_uid}

        def run(_: dict[str, Any]) -> dict[str, Any]:
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
            result = client.move(level, identifier, destination_aet)
            out = {"node": node, **_with_outcome(result, _outcome(result.get("completed", 0), result.get("failed", 0), result["ok"]))}
            if result.get("failed_sop_instance_uids"):
                out["failed_items"] = [{"sop_instance_uid": u} for u in result["failed_sop_instance_uids"]]
            return out

        return self._verified("move", args, run, post=lambda _, r: contracts.move_post(r))

    def store(self, node: str, paths: list[str], accept_soft_fail: bool = False) -> dict[str, Any]:
        """Send files to an archive. Under TEVL the gate verdict is written to the ledger before anything is sent."""
        args = {"node": node, "paths": list(paths), "accept_soft_fail": accept_soft_fail}
        gate: dict[str, Any] | None = None
        try:
            client = self._writable(node)
            files, skipped = self._collect_dicom_files(paths)
            if not files:
                raise DicomMcpError("No DICOM files found to store." + (f" Skipped: {skipped[:5]}" if skipped else ""))
            if self.verifier.enabled:
                checks, extra = contracts.store_gate(files, self.verifier.ledger)
                verdict = overall(checks)
                if self.verifier.blocks(verdict, commit=True, accept_soft_fail=accept_soft_fail):
                    self.verifier.block("store", args, checks, commit=True, **extra)
                gate = self.verifier.record("store", "gate", args, checks, accepted_soft_fail=accept_soft_fail and verdict == SOFT_FAIL, **extra)
            sent = {str(dcmread(f, stop_before_pixels=True).get("SOPInstanceUID", "")) for f in files}
            result = client.store(files)
        except VerificationError:
            raise
        except DicomMcpError as exc:
            if self.verifier.enabled:
                extra_ids = {"gate_id": gate["id"]} if gate else {}
                self.verifier.record("store", "commit", args, [Check("post", "TOOL_ERROR", HARD_FAIL, str(exc))], **extra_ids)
            raise
        out: dict[str, Any] = {"node": node, "skipped": skipped, **_with_outcome(result, _outcome(result["stored"], result["failed"]))}
        items = [{"path": r["path"], "sop_instance_uid": r["sop_instance_uid"], "reason": r.get("error") or r.get("status")}
                 for r in result.get("results", []) if not r.get("ok")]
        items += list(result.get("failures", []))
        if items:
            out["failed_items"] = items
        if gate is not None:
            record = self.verifier.record(
                "store", "commit", args, contracts.store_post(out, sent),
                gate_id=gate["id"], gate_verdict=gate["verdict"], accepted_soft_fail=gate["accepted_soft_fail"],
                output_digest=digest(out),
            )
            out["verification"] = summary(record)
        return out

    def transfer(
        self,
        source: str,
        destination: str,
        study_uid: str,
        series_uid: str | None = None,
        sop_instance_uid: str | None = None,
        accept_soft_fail: bool = False,
    ) -> dict[str, Any]:
        """Copy data between any two nodes (DIMSE or DICOMweb) through the local cache."""
        self._writable(destination)
        retrieved = self.retrieve(source, study_uid, series_uid, sop_instance_uid)
        out: dict[str, Any] = {"source": source, "destination": destination}
        if not retrieved["files"]:
            out.update(status="empty" if retrieved["status"] == "empty" else "failed", retrieve=retrieved, store=None,
                       message=f"Nothing was retrieved from '{source}'; nothing was sent to '{destination}'.")
        else:
            stored = self.store(destination, retrieved["files"], accept_soft_fail)
            statuses = {retrieved["status"], stored["status"]}
            status = "failed" if stored["status"] == "failed" else "partial" if statuses & {"partial", "failed"} else "ok"
            out.update(status=status, retrieve=retrieved, store=stored)
        parts = [r["verification"] for r in (out["retrieve"], out["store"]) if r and "verification" in r]
        if parts:
            out["verification"] = {"verdict": worst(p["verdict"] for p in parts), "policy": self.verifier.policy, "records": [p["id"] for p in parts]}
        return out

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
        args = {"path": path, "frame": frame, "window": window, "window_center": window_center, "window_width": window_width, "max_size": max_size}

        def setup(ctx: dict[str, Any]) -> None:
            ctx["file"] = self._check_file(path)
            ctx["ds"] = imaging.read_dataset(ctx["file"])

        def run(ctx: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
            png, info = imaging.render_frame(ctx["ds"], frame, window, window_center, window_width, max_size)
            return png, {"path": str(ctx["file"]), **info, "png_path": self._save_png(png, f"{ctx['file'].stem}_f{frame}", info)}

        def post(ctx: dict[str, Any], result: tuple[bytes, dict[str, Any]]) -> list[Check]:
            info, geo = result[1], imaging.geometry(ctx["ds"], frame)
            return contracts.aspect_checks((geo.rows, geo.columns), geo.spacing, info["png_size"], max_size) + contracts.window_checks(info["window"])

        return self._verified("render_image", args, run, setup=setup,
                              pre=lambda ctx: contracts.render_pre(ctx["ds"], frame, window, window_center, window_width), post=post)

    def _statistics_inputs(self, path: str, roi: Mapping[str, Any] | None) -> tuple[Path, Any, imaging.Roi | None]:
        file = self._check_file(path)
        try:
            region = imaging.Roi(**roi) if roi else None
        except TypeError as exc:
            raise DicomMcpError(f"Invalid roi: {exc}.") from exc
        return file, imaging.read_dataset(file), region

    def _statistics_raw(self, path: str, frame: int = 0, roi: Mapping[str, Any] | None = None) -> dict[str, Any]:
        file, ds, region = self._statistics_inputs(path, roi)
        return {"path": str(file), **imaging.pixel_statistics(ds, frame, region)}

    def pixel_statistics(self, path: str, frame: int = 0, roi: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = {"path": path, "frame": frame, "roi": dict(roi) if roi else None}

        def setup(ctx: dict[str, Any]) -> None:
            ctx["file"], ctx["ds"], ctx["roi"] = self._statistics_inputs(path, roi)

        def run(ctx: dict[str, Any]) -> dict[str, Any]:
            return {"path": str(ctx["file"]), **imaging.pixel_statistics(ctx["ds"], frame, ctx["roi"])}

        return self._verified(
            "pixel_statistics", args, run, setup=setup,
            pre=lambda ctx: contracts.statistics_pre(ctx["ds"], frame, ctx["roi"]),
            post=lambda ctx, r: contracts.statistics_post(r, ctx["ds"], frame, ctx["roi"]),
        )

    def _distance_inputs(self, path: str, start: list[float], end: list[float]) -> tuple[Path, Any]:
        file = self._check_file(path)
        if len(start) != 2 or len(end) != 2:
            raise DicomMcpError("start and end must be [row, column] pairs.")
        return file, imaging.read_dataset(file)

    def _distance_raw(self, path: str, start: list[float], end: list[float], frame: int = 0) -> dict[str, Any]:
        file, ds = self._distance_inputs(path, start, end)
        return {"path": str(file), **imaging.measure_distance(ds, (start[0], start[1]), (end[0], end[1]), frame)}

    def measure_distance(self, path: str, start: list[float], end: list[float], frame: int = 0) -> dict[str, Any]:
        args = {"path": path, "start": list(start), "end": list(end), "frame": frame}

        def setup(ctx: dict[str, Any]) -> None:
            ctx["file"], ctx["ds"] = self._distance_inputs(path, start, end)

        def run(ctx: dict[str, Any]) -> dict[str, Any]:
            return {"path": str(ctx["file"]), **imaging.measure_distance(ctx["ds"], (start[0], start[1]), (end[0], end[1]), frame)}

        return self._verified("measure_distance", args, run, setup=setup,
                              pre=lambda ctx: contracts.distance_pre(ctx["ds"], frame), post=lambda _, r: contracts.distance_post(r))

    def _geometry_raw(self, paths: list[str]) -> tuple[dict[str, Any], list[imaging.SeriesReport]]:
        files, skipped = self._collect_dicom_files(paths)
        if not files:
            raise DicomMcpError("No DICOM files found.")
        reports = [imaging.analyze_series(uid, slices) for uid, slices in imaging.group_series(files).items()]
        result = {"ok": not any(r.errors for r in reports), "series": [r.to_dict() for r in reports], "skipped": skipped}
        return result, reports

    def check_geometry(self, paths: list[str]) -> dict[str, Any]:
        def run(ctx: dict[str, Any]) -> dict[str, Any]:
            result, ctx["reports"] = self._geometry_raw(paths)
            return result

        return self._verified("check_geometry", {"paths": list(paths)}, run, post=lambda ctx, _: contracts.geometry_post(ctx["reports"]))

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
        args = {"paths": list(paths), "plane": plane, "index": index, "series_uid": series_uid, "window": window,
                "window_center": window_center, "window_width": window_width, "max_size": max_size}

        def setup(ctx: dict[str, Any]) -> None:
            files, _ = self._collect_dicom_files(paths)
            groups = imaging.group_series(files)
            if series_uid:
                if series_uid not in groups:
                    raise DicomMcpError(f"Series {series_uid} is not among the given files.")
                groups = {series_uid: groups[series_uid]}
            elif len(groups) != 1:
                raise DicomMcpError(f"The files contain {len(groups)} series; pass series_uid to pick one: {', '.join(groups)}.")
            uid, slices = next(iter(groups.items()))
            ctx.update(uid=uid, slices=slices, report=imaging.analyze_series(uid, slices))

        def run(ctx: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
            uid, slices = ctx["uid"], ctx["slices"]
            volume = imaging.load_volume(ctx["report"], self.settings.max_volume_voxels)
            image, info = imaging.reformat(volume, plane, index)  # type: ignore[arg-type]
            ctx["shape"] = image.shape
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

        def post(ctx: dict[str, Any], result: tuple[bytes, dict[str, Any]]) -> list[Check]:
            info = result[1]
            return contracts.aspect_checks(ctx["shape"], info["pixel_spacing_mm"], info["png_size"], max_size) + contracts.window_checks(info["window"])

        return self._verified(
            "render_mpr", args, run, setup=setup,
            pre=lambda ctx: contracts.geometry_checks("render_mpr", ctx["report"].issues, "pre", ctx["uid"]), post=post,
        )

    def deidentify(
        self, paths: list[str], date_mode: str = "shift", keep_descriptions: bool = False, keep_private: bool = False
    ) -> dict[str, Any]:
        args = {"paths": list(paths), "date_mode": date_mode, "keep_descriptions": keep_descriptions, "keep_private": keep_private}

        def run(_: dict[str, Any]) -> dict[str, Any]:
            if date_mode not in ("shift", "keep"):
                raise DicomMcpError("date_mode must be 'shift' or 'keep'.")
            files, skipped = self._collect_dicom_files(paths)
            if not files:
                raise DicomMcpError("No DICOM files found to de-identify.")
            key = os.environ.get(self.settings.deid_key_env)
            deidentifier = Deidentifier(key.encode() if key else None, date_mode, keep_descriptions, keep_private)  # type: ignore[arg-type]
            result = deidentify_files(files, self.settings.storage_dir / "deidentified", deidentifier)
            return {**result, "skipped": skipped}

        return self._verified("deidentify", args, run, post=lambda _, r: contracts.deidentify_post(r, keep_private, keep_descriptions))

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


def _with_outcome(result: dict[str, Any], status: str) -> dict[str, Any]:
    """Put the explicit result state in ``status``; the protocol's own status text moves to ``protocol_status``."""
    out = dict(result)
    if "status" in out:
        out["protocol_status"] = out.pop("status")
    out["status"] = status
    return out


def _outcome(done: int, failed: int, ok: bool = True) -> str:
    """Explicit result state for retrieve/move/store: ok, empty, partial or failed."""
    if done == 0:
        return "failed" if failed or not ok else "empty"
    return "partial" if failed or not ok else "ok"


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
