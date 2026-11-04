"""MCP server entry point: exposes DicomService operations as MCP tools."""

from __future__ import annotations

import argparse
import functools
import logging
import sys
from typing import Annotated, Any, Callable, Literal

import anyio
import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .config import DEFAULT_CONFIG_FILE, ENV_PREFIX, NODE_SPEC_HELP, Settings, build_settings
from .errors import DicomMcpError
from .imaging import WINDOW_PRESETS
from .service import DicomService

INSTRUCTIONS = """\
DICOM tools for searching, reading, retrieving, viewing, measuring and storing medical images.
Typical workflow: list_nodes -> echo -> search (STUDY, then SERIES, then IMAGE) -> get_metadata
-> retrieve (downloads to the local cache) -> read_local_metadata. Use store to send local files,
move for a DIMSE C-MOVE between archives, and transfer to copy between any two nodes.
Each tool works the same for DIMSE (C-FIND/C-MOVE/C-GET/C-STORE) and DICOMweb (QIDO/WADO/STOW) nodes.
Attributes are named by DICOM keyword (e.g. PatientID, StudyDate, Modality) or hex tag (00100020).
Matching supports DICOM wildcards (* and ?) and date ranges (20240101-20241231).
For pixels: run check_geometry on a retrieved series before measuring or reconstructing; use render_image
or render_mpr to look at images, pixel_statistics for HU/ROI values, measure_distance for mm distances
(pixel coordinates are [row, column] from the top-left), and deidentify before sharing data.
Results include an input_fingerprint of the exact pixels used; quote it when reporting findings.
Verification (TEVL): tool results carry a verification block with a verdict (PASS, SOFT_FAIL or HARD_FAIL),
the checks behind it and a ledger record id. Treat HARD_FAIL results as wrong, and mention SOFT_FAIL
caveats when you report. Before reporting a number, you can call verify_claim to recompute it
independently. search, retrieve, move, store and transfer return a status (ok, empty, partial,
truncated, failed): an empty search is not proof that nothing exists. Under the enforce policy, store
and transfer refuse on a HARD_FAIL gate verdict, and refuse a SOFT_FAIL one unless you pass
accept_soft_fail after reviewing the checks. get_verification reads the ledger.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

Node = Annotated[str, Field(description="Name of a configured DICOM node (see list_nodes).")]
StudyUid = Annotated[str, Field(description="StudyInstanceUID.")]
SeriesUid = Annotated[str | None, Field(description="SeriesInstanceUID to narrow the request.")]
SopUid = Annotated[str | None, Field(description="SOPInstanceUID to narrow the request (requires series_uid).")]
LocalFile = Annotated[str, Field(description="Path to a local DICOM file inside the allowed paths (e.g. from retrieve).")]
LocalPaths = Annotated[list[str], Field(description="Local DICOM files or directories inside the allowed paths.")]
Frame = Annotated[int, Field(ge=0, description="0-based frame index for multi-frame images.")]
AcceptSoftFail = Annotated[
    bool,
    Field(description="Under the enforce policy, proceed although the pre-commit gate returned SOFT_FAIL. Review the gate checks first."),
]
MaxSize = Annotated[int, Field(ge=64, le=2048, description="Longest edge of the PNG in pixels.")]
WindowName = Annotated[
    str,
    Field(description=f"Display window: auto (file window or percentiles), minmax, or a CT preset: {', '.join(WINDOW_PRESETS)}."),
]


class RoiModel(BaseModel):
    shape: Literal["rectangle", "ellipse"] = Field(description="Rectangle, or the ellipse inscribed in it.")
    row: float = Field(description="Top edge (row) of the bounding box.")
    column: float = Field(description="Left edge (column) of the bounding box.")
    height: float = Field(gt=0, description="Bounding-box height.")
    width: float = Field(gt=0, description="Bounding-box width.")
    units: Literal["pixel", "mm"] = Field("pixel", description="Units of row, column, height and width.")


def create_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    http_transport: httpx.BaseTransport | None = None,
) -> FastMCP:
    service = DicomService(settings, http_transport=http_transport)
    mcp = FastMCP("dicom-agent-mcp", instructions=INSTRUCTIONS, host=host, port=port)

    async def run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # pynetdicom and httpx calls block, so keep them off the event loop.
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    @mcp.tool(annotations=LOCAL_READ)
    async def list_nodes() -> list[dict[str, Any]]:
        """List the configured DICOM nodes (PACS, VNA, DICOMweb services) and their protocols."""
        return service.list_nodes()

    @mcp.tool(annotations=READ)
    async def echo(node: Node) -> dict[str, Any]:
        """Check connectivity to a node (C-ECHO for DIMSE, a minimal QIDO-RS request for DICOMweb)."""
        return await run(service.echo, node)

    @mcp.tool(annotations=READ)
    async def search(
        node: Node,
        level: Annotated[Literal["PATIENT", "STUDY", "SERIES", "IMAGE"], Field(description="Query/retrieve level.")] = "STUDY",
        filters: Annotated[dict[str, str] | None, Field(description="Matching keys, e.g. {\"PatientID\": \"123\", \"Modality\": \"CT\", \"StudyDate\": \"20240101-20241231\"}.")] = None,
        return_fields: Annotated[list[str] | None, Field(description="Extra attributes to return besides the level defaults.")] = None,
        limit: Annotated[int, Field(ge=1, description="Maximum results to return.")] = 50,
        offset: Annotated[int, Field(ge=0, description="Results to skip, for paging.")] = 0,
    ) -> dict[str, Any]:
        """Search for patients, studies, series or instances (C-FIND or QIDO-RS)."""
        return await run(service.search, node, level, filters, return_fields, limit, offset)

    @mcp.tool(annotations=READ)
    async def get_metadata(
        node: Node,
        study_uid: StudyUid,
        series_uid: SeriesUid = None,
        sop_instance_uid: SopUid = None,
        fields: Annotated[list[str] | None, Field(description="Limit or extend the returned attributes.")] = None,
        limit: Annotated[int, Field(ge=1, description="Maximum instances to return.")] = 50,
    ) -> dict[str, Any]:
        """Read instance-level metadata without downloading pixel data (WADO-RS metadata or C-FIND)."""
        return await run(service.get_metadata, node, study_uid, series_uid, sop_instance_uid, fields, limit)

    @mcp.tool(annotations=WRITE)
    async def retrieve(node: Node, study_uid: StudyUid, series_uid: SeriesUid = None, sop_instance_uid: SopUid = None) -> dict[str, Any]:
        """Download a study, series or instance into the local cache (C-GET or WADO-RS). Returns file paths."""
        return await run(service.retrieve, node, study_uid, series_uid, sop_instance_uid)

    @mcp.tool(annotations=WRITE)
    async def move(
        node: Node,
        destination: Annotated[str, Field(description="Destination node name or an AE title known to the source archive.")],
        study_uid: StudyUid,
        series_uid: SeriesUid = None,
        sop_instance_uid: SopUid = None,
    ) -> dict[str, Any]:
        """Ask a DIMSE archive to send data to another DICOM node (C-MOVE)."""
        return await run(service.move, node, destination, study_uid, series_uid, sop_instance_uid)

    @mcp.tool(annotations=WRITE)
    async def store(
        node: Node,
        paths: Annotated[list[str], Field(description="Local DICOM files or directories inside the allowed paths.")],
        accept_soft_fail: AcceptSoftFail = False,
    ) -> dict[str, Any]:
        """Send local DICOM files to a node (C-STORE or STOW-RS).

        A pre-commit gate checks the files first (earlier verdicts for these files, duplicate instances, mixed
        patients within a study, series geometry) and records its verdict before anything is sent.
        """
        return await run(service.store, node, paths, accept_soft_fail)

    @mcp.tool(annotations=WRITE)
    async def transfer(
        source: Annotated[str, Field(description="Node to read from.")],
        destination: Annotated[str, Field(description="Node to write to.")],
        study_uid: StudyUid,
        series_uid: SeriesUid = None,
        sop_instance_uid: SopUid = None,
        accept_soft_fail: AcceptSoftFail = False,
    ) -> dict[str, Any]:
        """Copy data between any two nodes, including DIMSE to DICOMweb, through the local cache.

        The store step passes the same pre-commit gate as the store tool.
        """
        return await run(service.transfer, source, destination, study_uid, series_uid, sop_instance_uid, accept_soft_fail)

    @mcp.tool(annotations=LOCAL_READ)
    async def list_local_files(
        study_uid: Annotated[str | None, Field(description="Only list files for this study.")] = None,
        limit: Annotated[int, Field(ge=1)] = 200,
    ) -> dict[str, Any]:
        """List DICOM files in the local cache."""
        return await run(service.list_local_files, study_uid, limit)

    @mcp.tool(annotations=LOCAL_READ)
    async def read_local_metadata(
        path: Annotated[str, Field(description="Path to a local DICOM file inside the allowed paths.")],
        max_depth: Annotated[int, Field(ge=0, le=5, description="How deep to expand sequences.")] = 2,
    ) -> dict[str, Any]:
        """Read the full header of a local DICOM file. Pixel data is not loaded."""
        return await run(service.read_local_metadata, path, max_depth)

    # ---- pixels and geometry ---------------------------------------------------------------

    @mcp.tool(annotations=LOCAL_READ, structured_output=False)
    async def render_image(
        path: LocalFile,
        frame: Frame = 0,
        window: WindowName = "auto",
        window_center: Annotated[float | None, Field(description="Explicit window center (overrides window).")] = None,
        window_width: Annotated[float | None, Field(description="Explicit window width (overrides window).")] = None,
        max_size: MaxSize = 512,
    ) -> list[Any]:
        """Render one frame of a local DICOM image as a PNG you can look at.

        Applies the modality rescale (e.g. HU) and a display window, corrects non-square pixels, and
        reports which patient direction each image edge faces. Also returns the input fingerprint.
        """
        png, info = await run(service.render_image, path, frame, window, window_center, window_width, max_size)
        return [Image(data=png, format="png"), info]

    @mcp.tool(annotations=LOCAL_READ)
    async def pixel_statistics(
        path: LocalFile,
        frame: Frame = 0,
        roi: Annotated[RoiModel | None, Field(description="Region of interest; omit for the whole frame.")] = None,
    ) -> dict[str, Any]:
        """Pixel value statistics in modality units (HU for CT): mean, std, min, max, percentiles and area in mm2."""
        return await run(service.pixel_statistics, path, frame, roi.model_dump() if roi else None)

    @mcp.tool(annotations=LOCAL_READ)
    async def measure_distance(
        path: LocalFile,
        start: Annotated[list[float], Field(min_length=2, max_length=2, description="[row, column] in pixels.")],
        end: Annotated[list[float], Field(min_length=2, max_length=2, description="[row, column] in pixels.")],
        frame: Frame = 0,
    ) -> dict[str, Any]:
        """Measure a distance in mm between two pixel positions and give their patient coordinates."""
        return await run(service.measure_distance, path, start, end, frame)

    @mcp.tool(annotations=LOCAL_READ)
    async def check_geometry(paths: LocalPaths) -> dict[str, Any]:
        """Check series geometry before measuring or reconstructing.

        Reports orientation, spacing and extent per series, and flags missing or duplicate slices, mixed
        orientations, non-uniform spacing, gantry tilt and multiple frames of reference.
        """
        return await run(service.check_geometry, paths)

    @mcp.tool(annotations=LOCAL_READ, structured_output=False)
    async def render_mpr(
        paths: LocalPaths,
        plane: Annotated[Literal["axial", "coronal", "sagittal"], Field(description="Plane to reconstruct.")] = "coronal",
        index: Annotated[int | None, Field(ge=0, description="Slice index in the reconstructed plane; default is the middle.")] = None,
        series_uid: Annotated[str | None, Field(description="Series to use when the paths contain several.")] = None,
        window: WindowName = "auto",
        window_center: Annotated[float | None, Field(description="Explicit window center.")] = None,
        window_width: Annotated[float | None, Field(description="Explicit window width.")] = None,
        max_size: MaxSize = 512,
    ) -> list[Any]:
        """Reconstruct an axial, coronal or sagittal view from a stack of slices and render it as a PNG.

        Refuses series that fail check_geometry (missing slices, mixed orientations, and so on).
        """
        png, info = await run(service.render_mpr, paths, plane, index, series_uid, window, window_center, window_width, max_size)
        return [Image(data=png, format="png"), info]

    @mcp.tool(annotations=LOCAL_WRITE)
    async def deidentify(
        paths: LocalPaths,
        date_mode: Annotated[Literal["shift", "keep"], Field(description="Shift dates by a per-patient offset, or keep them.")] = "shift",
        keep_descriptions: Annotated[bool, Field(description="Keep study/series descriptions and protocol names.")] = False,
        keep_private: Annotated[bool, Field(description="Keep private tags.")] = False,
    ) -> dict[str, Any]:
        """Write de-identified copies of local DICOM files (basic confidentiality profile subset) to the cache.

        UIDs are remapped consistently, names and IDs are pseudonymised, and pixel data is not changed.
        """
        return await run(service.deidentify, paths, date_mode, keep_descriptions, keep_private)

    # ---- verification ----------------------------------------------------------------------

    @mcp.tool(annotations=LOCAL_READ)
    async def verify_claim(
        tool: Annotated[Literal["pixel_statistics", "measure_distance", "check_geometry"], Field(description="Tool whose result is claimed.")],
        arguments: Annotated[dict[str, Any], Field(description="The arguments the claim was computed with, as passed to that tool.")],
        claimed: Annotated[
            dict[str, Any],
            Field(description="Claimed result fields by dotted path, e.g. {\"mean\": 41.2, \"distance_mm\": 23.5, \"series.0.volume_ready\": true}."),
        ],
        input_fingerprint: Annotated[str | None, Field(description="input_fingerprint quoted with the claim; checked against the pixels now at the path.")] = None,
        tolerance: Annotated[float | None, Field(ge=0, description="Absolute numeric tolerance. Default: half a unit in the claim's last decimal place.")] = None,
    ) -> dict[str, Any]:
        """Recompute a result independently and check claimed values against it before you report them.

        Numbers match within the tolerance; strings and booleans must be equal. The verdict is recorded in the ledger.
        """
        return await run(service.verify_claim, tool, arguments, claimed, input_fingerprint, tolerance)

    @mcp.tool(annotations=LOCAL_READ)
    async def get_verification(
        record_id: Annotated[str | None, Field(description="Ledger record id (verification.id in a tool result).")] = None,
        fingerprint: Annotated[str | None, Field(description="Find records by pixel input_fingerprint or file sha256 digest.")] = None,
        limit: Annotated[int, Field(ge=1, description="Maximum records to return (most recent).")] = 20,
    ) -> dict[str, Any]:
        """Read verification records from the hash-chained ledger and check the chain's integrity."""
        return await run(service.get_verification, record_id, fingerprint, limit)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dicom-agent-mcp",
        description=__doc__,
        epilog=(
            "A config file is optional. Settings are merged in this order: config file, DICOM_AGENT_MCP_* "
            "environment variables, then these arguments. Put secrets in environment variables and pass only "
            "their names (token-env=, password-env=)."
        ),
    )
    parser.add_argument("--config", default=None, help=f"Optional YAML config file (env {ENV_PREFIX}CONFIG; ./{DEFAULT_CONFIG_FILE} is used if present).")
    parser.add_argument("--node", action="append", default=[], metavar="SPEC", help=f"Add or override a node: {NODE_SPEC_HELP}. Repeatable.")
    parser.add_argument("--storage-dir", help="Local cache directory (default ~/.dicom-agent-mcp/cache without a config file).")
    parser.add_argument("--allow-path", action="append", default=[], metavar="DIR", help="Extra directory the local-file tools may read. Repeatable.")
    parser.add_argument("--calling-ae", help="AE title this server uses for DIMSE (default DICOM_MCP).")
    parser.add_argument(
        "--verification", choices=["off", "advisory", "enforce"],
        help=f"Verification policy (env {ENV_PREFIX}VERIFICATION, default advisory).",
    )
    parser.add_argument("--print-config", action="store_true", help="Print the resolved settings as JSON and exit.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    # Logs must go to stderr: stdout carries the MCP protocol in stdio mode.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper())
    try:
        settings = build_settings(
            args.config, nodes=args.node, storage_dir=args.storage_dir,
            allowed_paths=args.allow_path, calling_ae_title=args.calling_ae, verification=args.verification,
        )
    except (DicomMcpError, ValidationError) as exc:
        parser.error(str(exc))
    if args.print_config:
        print(settings.model_dump_json(indent=2))
        return
    if not settings.nodes:
        logging.warning("No DICOM nodes configured; only local-file and pixel tools are useful. Add --node to connect an archive.")
    create_server(settings, host=args.host, port=args.port).run(transport=args.transport)
