# dicom-agent-mcp

<!-- mcp-name: io.github.arunmk-ms/dicom-agent-mcp -->

An MCP (Model Context Protocol) server that gives AI agents one set of tools for both **DIMSE** archives (C-ECHO, C-FIND, C-GET, C-MOVE, C-STORE) and **DICOMweb** archives (QIDO-RS, WADO-RS, STOW-RS).

It also includes pixel and geometry tools that work on local files: rendering, statistics, measurement, geometry checks, multiplanar reconstruction (MPR) and de-identification. This is the tool layer for Topic 3 (Validated Tool Execution). The verification loop comes later; see [ROADMAP.md](ROADMAP.md).

> **Not for clinical use.** This is a research scaffold. Use only with test or de-identified data.

## Tools

| Tool | DIMSE node | DICOMweb node | Notes |
|---|---|---|---|
| `list_nodes` | – | – | Lists configured nodes; secrets are never returned |
| `echo` | C-ECHO | `GET /studies?limit=1` | Connectivity check |
| `search` | C-FIND | QIDO-RS | Levels: patient, study, series, instance. Supports wildcards and date ranges. Paging uses `offset`/`limit`. QIDO has no patient level |
| `get_metadata` | C-FIND (indexed attributes only) | WADO-RS `/metadata` | Returns keyword-based JSON; bulk data is summarised |
| `retrieve` | C-GET | WADO-RS | Saves files to `storage_dir/<study>/<series>/<sop>.dcm` |
| `move` | C-MOVE | – | Destination is a node name or an AE title |
| `store` | C-STORE | STOW-RS | Accepts files or directories inside the sandbox |
| `transfer` | retrieve + store | retrieve + store | Works across protocols (e.g. PACS → DICOMweb) |
| `list_local_files` | – | – | Lists cached or allowed DICOM files |
| `read_local_metadata` | – | – | Reads a local file header without pixel data |

### Pixel and geometry tools (local files)

| Tool | What it does |
|---|---|
| `render_image` | Renders one frame to a PNG and returns it alongside JSON. Windowing comes from explicit centre/width values, a preset (`lung`, `mediastinum`, `soft_tissue`, `abdomen`, `liver`, `bone`, `brain`, `subdural`, `stroke`), the file's window, or 0.5–99.5 percentiles. Corrects for non-square pixels and MONOCHROME1, and labels the edges with patient directions (for example R/L, A/P) |
| `pixel_statistics` | Reports min, max, mean, standard deviation, percentiles and a voxel count for a frame or a rectangular/elliptical ROI. ROIs can be given in pixels or mm. Values are in modality units (HU for CT) and the ROI area is reported in mm² |
| `measure_distance` | Measures the in-plane distance in mm between two pixels and returns both points in patient (LPS) coordinates |
| `check_geometry` | Groups files into series and checks whether each one can be treated as a volume. Issues are reported with codes such as `MISSING_SLICES`, `NON_UNIFORM_SPACING`, `DUPLICATE_POSITIONS`, `MIXED_ORIENTATION`, `OBLIQUE`, `SHEARED_STACK` and `INSTANCE_ORDER_MISMATCH` |
| `render_mpr` | Renders an axial, coronal or sagittal slice reformatted from a series that passes `check_geometry`. The PNG uses true mm aspect and radiological orientation |
| `deidentify` | Writes de-identified copies under `storage_dir/deidentified` using a subset of the Basic Profile. See [De-identification](#de-identification) |

Every pixel result includes an `input_fingerprint`: a SHA-256 hash of the decoded pixels and geometry. It does not change with the transfer syntax, and the verification loop uses it to bind results to their inputs.

### Verification tools

| Tool | What it does |
|---|---|
| `verify_claim` | Re-runs `pixel_statistics`, `measure_distance` or `check_geometry` and compares the values the agent intends to report (for example `{"mean": 47.2}`) with the fresh result. It returns a verdict for each field. The default tolerance is half a unit in the last decimal of the claimed value |
| `get_verification` | Looks up ledger records by id or by fingerprint, and checks the integrity of the hash chain |

See [Verification loop](#verification-loop-tevl).

Each tool carries MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) so a harness can decide which tools need approval.

## Install

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
# Optional: decoders for JPEG, JPEG-LS, JPEG 2000 and RLE compressed pixel data
.venv\Scripts\python -m pip install -e ".[codecs]"
```

## Configure

No config file is needed. Pass nodes and paths as arguments in the harness config; add a YAML file only for advanced settings.

### Arguments

| Argument | Meaning |
|---|---|
| `--node SPEC` | Adds a node. Repeatable. See the node spec below |
| `--storage-dir DIR` | Local cache for retrieved files and renders. Default `~/.dicom-agent-mcp/cache` |
| `--allow-path DIR` | Extra directory that the local-file and pixel tools may read. Repeatable |
| `--calling-ae AE` | AE title this server uses for DIMSE. Default `DICOM_MCP`; register it on your PACS |
| `--verification MODE` | `off`, `advisory` (default) or `enforce`. See [Verification loop](#verification-loop-tevl) |
| `--config FILE` | Optional YAML file (see below) |
| `--print-config` | Prints the resolved settings and exits. Useful for checking a harness setup |

**Node spec:** `name=URL[;option;option…]`

| URL | Node type |
|---|---|
| `dimse://AE_TITLE@host:port` | DIMSE (C-FIND, C-GET, C-MOVE, C-STORE) |
| `http://…` or `https://…` | DICOMweb base URL (QIDO-RS, WADO-RS, STOW-RS) |

| Option | Meaning |
|---|---|
| `read-only` | `store`, `move` and `transfer` refuse to write to this node |
| `token-env=VAR` | DICOMweb bearer token read from environment variable `VAR` |
| `user=NAME;password-env=VAR` | DICOMweb basic auth; the password is read from `VAR` |
| `no-verify-tls` | Skip TLS certificate checks (test servers only) |
| `transfer-syntax=UID` | Transfer syntax requested from WADO-RS. Empty means the server default; the default `*` keeps the stored encoding |

Examples:

```text
--node orthanc=dimse://ORTHANC@localhost:4242
--node orthanc-web=http://localhost:8042/dicom-web
--node "azure=https://<workspace>-<service>.dicom.azurehealthcareapis.com/v2;token-env=AZURE_DICOM_TOKEN;read-only"
```

**Secrets never go in arguments.** Arguments are visible in process lists and are often committed in `mcp.json`. Put tokens and passwords in environment variables and pass only the variable name.

With no `--node`, the server still starts: the local-file and pixel tools work on files under the allowed paths.

### Environment variables

Every argument has an environment-variable equivalent, which suits Docker and CI:

| Variable | Equivalent |
|---|---|
| `DICOM_AGENT_MCP_NODES` | `--node`; several specs separated by spaces |
| `DICOM_AGENT_MCP_STORAGE_DIR` | `--storage-dir` |
| `DICOM_AGENT_MCP_ALLOWED_PATHS` | `--allow-path`; several paths separated by `;` on Windows or `:` elsewhere |
| `DICOM_AGENT_MCP_CALLING_AE` | `--calling-ae` |
| `DICOM_AGENT_MCP_VERIFICATION` | `--verification` |
| `DICOM_AGENT_MCP_CONFIG` | `--config` |

### Optional YAML file (advanced)

Use a file for settings that have no argument: timeouts, `max_results`, `max_volume_voxels`, `deid_key_env` and node descriptions. See `config.example.yaml`. If `./dicom-agent-mcp.yaml` exists in the working directory, it is loaded automatically. Relative paths in a file resolve against the file's directory; relative paths in arguments and environment variables resolve against the working directory.

**Precedence:** config file, then environment variables, then arguments. Later sources override single values, nodes are merged by name (a `--node` with the same name replaces the file's node), and allowed paths are combined.

## Run

```powershell
dicom-agent-mcp --node orthanc=dimse://ORTHANC@localhost:4242                                   # stdio
dicom-agent-mcp --node orthanc=dimse://ORTHANC@localhost:4242 --transport streamable-http --port 8000
dicom-agent-mcp --node orthanc=dimse://ORTHANC@localhost:4242 --print-config                    # check settings
```

### VS Code (`.vscode/mcp.json`)

```json
{
  "inputs": [
    { "id": "azureToken", "type": "promptString", "description": "Azure DICOM access token", "password": true }
  ],
  "servers": {
    "dicom": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/Scripts/dicom-agent-mcp",
      "args": [
        "--node", "orthanc=dimse://ORTHANC@localhost:4242",
        "--node", "orthanc-web=http://localhost:8042/dicom-web",
        "--node", "azure=https://<workspace>-<service>.dicom.azurehealthcareapis.com/v2;token-env=AZURE_DICOM_TOKEN;read-only",
        "--storage-dir", "${workspaceFolder}/dicom-cache",
        "--allow-path", "${workspaceFolder}/inbox"
      ],
      "env": { "AZURE_DICOM_TOKEN": "${input:azureToken}" }
    }
  }
}
```

### Claude Desktop / Copilot CLI / other harnesses

```json
{
  "mcpServers": {
    "dicom": {
      "command": "dicom-agent-mcp",
      "args": [
        "--node", "orthanc=dimse://ORTHANC@localhost:4242",
        "--allow-path", "C:/data/dicom-inbox"
      ]
    }
  }
}
```

Use absolute paths here: these harnesses may start the server in any working directory. To use a file instead, pass `"args": ["--config", "C:/path/to/dicom-agent-mcp.yaml"]`.
### Local Orthanc for testing

```powershell
docker-compose up -d   # DIMSE on 4242, DICOMweb on http://localhost:8042/dicom-web (compose file not yet tested)
```

## Sample prompts

These prompts assume three nodes: `orthanc` (DIMSE), `orthanc-web` (DICOMweb) and `azure-dicom`, for example:

```text
--node orthanc=dimse://ORTHANC@localhost:4242 --node orthanc-web=http://localhost:8042/dicom-web
--node "azure-dicom=https://<workspace>-<service>.dicom.azurehealthcareapis.com/v2;token-env=AZURE_DICOM_TOKEN"
```

The tool the agent is expected to call is shown after each prompt.

### Nodes and connectivity

| Prompt | Tool |
|---|---|
| "Which DICOM nodes are configured, and which ones are read-only?" | `list_nodes` |
| "Check whether `orthanc` and `azure-dicom` are reachable." | `echo` |

### Search and metadata

| Prompt | Tool |
|---|---|
| "Find all CT studies on `orthanc` from the last 30 days." | `search` (C-FIND) |
| "List the series in study `1.2.840…` on `orthanc-web`, with modality and number of images." | `search` (QIDO-RS) |
| "Find patients whose name starts with `DOE^`." | `search` at patient level (DIMSE nodes only) |
| "Show the acquisition parameters for series `1.2.840…`: slice thickness, kVp and pixel spacing." | `get_metadata` |

### Moving data

| Prompt | Tool |
|---|---|
| "Download the chest CT series from `orthanc` into the local cache." | `retrieve` |
| "Send study `1.2.840…` from `orthanc` to the AE title `WORKSTATION1`." | `move` (C-MOVE) |
| "Upload the files in `./inbox/case42` to `orthanc-web`." | `store` (STOW-RS) |
| "Copy series `1.2.840…` from `orthanc` to `azure-dicom`." | `transfer` (works across protocols) |

### Local files

| Prompt | Tool |
|---|---|
| "What DICOM files do I have locally?" | `list_local_files` |
| "Read the header of `./dicom-cache/…/x.dcm`. Is it compressed? What is the photometric interpretation?" | `read_local_metadata` |

### Pixels and geometry

| Prompt | Tool |
|---|---|
| "Show me slice 40 of that series in a lung window." | `render_image` (preset) |
| "Render it again with centre 40 and width 400." | `render_image` (explicit window) |
| "What is the mean HU in a 10 mm circle centred at row 256, column 300?" | `pixel_statistics` (ellipse in mm) |
| "Give me the minimum, maximum and 95th percentile for the whole frame." | `pixel_statistics` |
| "How far apart in mm are pixel (120, 200) and pixel (180, 260)?" | `measure_distance` |
| "Can this series be used as a 3D volume? Report any missing slices or uneven spacing." | `check_geometry` |
| "Show the middle coronal slice in a bone window." | `render_mpr` |
| "Show sagittal slice 100." | `render_mpr` |

### De-identification

| Prompt | Tool |
|---|---|
| "De-identify the downloaded study, shift the dates and drop private tags." | `deidentify` |
| "De-identify it but keep the original dates and the series descriptions." | `deidentify` (`date_mode=keep`, `keep_descriptions`) |

### Multi-step workflows

- "Find the latest CT for patient MRN123 on `orthanc`, download it, check its geometry, and show me an axial and a coronal view in a soft-tissue window."
- "Measure the mean HU of the liver at slice 60 (a 15 mm circle at row 300, column 180) and report the input fingerprint."
- "Download study X from `orthanc`, de-identify it, and upload the de-identified copy to `azure-dicom`."
- "Measure the mean HU in that ROI, then verify the number before you report it." (`pixel_statistics`, then `verify_claim`)
- "Show me the verification record for that measurement and confirm the ledger is intact." (`get_verification`)

### Prompts the server should refuse

| Prompt | Expected result |
|---|---|
| "Store these files to `<a node marked read_only>`." | Refused because the node is read-only |
| "Read `C:\Windows\System32\config`." | Refused because the path is outside the sandbox |
| "Render a coronal MPR of a series that has missing slices." | Refused, with the geometry issues listed |
| "Upload this series with a missing slice to `orthanc`." (with `--verification enforce`) | Blocked by the store gate until the call passes `accept_soft_fail` |
| "Upload the de-identified copy whose verdict was HARD_FAIL." (with `--verification enforce`) | Blocked with `PRIOR_HARD_FAIL` |

## Verification loop (TEVL)

TEVL stands for Tool Execution Verification Loop. Each tool has a contract: preconditions on its inputs and postconditions on its outputs. Every check produces a verdict:

- `PASS`: the check succeeded.
- `SOFT_FAIL`: the result can be used, but it needs a caveat. Examples are a clipped ROI, a preset applied to non-HU data, a missing slice in data about to be stored, an empty search, or a partial transfer.
- `HARD_FAIL`: the result is wrong or unsafe. Examples are statistics that are inconsistent with each other, an aspect ratio that does not match the mm spacing, identifiers left after de-identification, or an MPR of a series that has gaps.

The result of each verified tool includes a `verification` block with the record `id`, the overall `verdict`, the policy and the individual checks. The geometry verifiers reuse the `check_geometry` issue codes.

**Ledger.** Each verdict is appended to `storage_dir/verification/ledger.jsonl`. Every record holds the SHA-256 of the previous record, so editing or deleting a line breaks the chain, and `get_verification` reports where. Records bind results to their inputs through the input fingerprints, to their outputs through a digest of the result, and to files through their SHA-256.

**Reproducibility.** Each record also stores what is needed to re-run and compare the operation:

- `environment`: the versions of Python, dicom-agent-mcp, pydicom, pynetdicom, numpy, Pillow, httpx and mcp.
- `effective_params`: the parameters the tool actually applied, after defaults were resolved. Examples are the resolved window, frame, PNG size and resampling, MPR sampling, ROI bounds, and the pixel-spacing source. For `deidentify` they include the method, the profile and its options (PS3.15 codes), the date mode, and `key_id`. `key_id` is a one-way HMAC identifier of the pseudonymisation key and never the key itself. Date offsets are not recorded.
- `input_files`: for each input file, its SHA-256, its transfer syntax, and `produced_by`. `produced_by` is the id of the `retrieve` or `deidentify` record that wrote that file, so a measurement chains back to the de-identification and to the network fetch.
- `source` (on `retrieve`, `move`, `store` and `transfer`): the protocol (`dimse` or `dicomweb`), the operation (C-GET, C-MOVE, C-STORE, WADO-RS or STOW-RS), the endpoint without credentials or query string, the calling AE title or requested transfer syntax, and the transfer syntaxes received or sent. This lets you compare the DIMSE and DICOMweb paths for the same study.

The de-identification validation checks are written in the same record as these parameters.

**Store gate.** Before `store` (or the store step of `transfer`) sends anything, it runs a gate and writes the gate record first. The gate checks for:

- duplicate SOP Instance UIDs;
- more than one patient in a batch;
- the geometry of each series;
- the most recent ledger verdict for each file, for example the `deidentify` run that produced it.

The commit record refers back to the gate record.

**Policy** (`--verification` or `verification:` in YAML):

| Mode | Behaviour |
|---|---|
| `off` | No checks and no ledger |
| `advisory` (default) | Verdicts are recorded and returned, and nothing is blocked |
| `enforce` | `HARD_FAIL` blocks the tool. A `SOFT_FAIL` at the store gate blocks the commit unless the call passes `accept_soft_fail: true` |

**Result states.** `search`, `retrieve`, `move`, `store` and `transfer` return a `status` of `ok`, `empty`, `partial`, `truncated` or `failed`. The protocol's own status text is kept in `protocol_status`. An empty search echoes the query and flags filter keys the archive may not support. Partial results list the failed items in `failed_items`. A `transfer` that retrieves nothing returns `empty` instead of storing nothing.

Verification records are not yet written as DICOM SR objects; see [ROADMAP.md](ROADMAP.md).

## Safety

- **Path sandbox.** `store`, `list_local_files`, `read_local_metadata` and the pixel tools only accept paths under `storage_dir` or `allowed_paths`.
- **UID validation.** UIDs are checked before they are used in file paths or URLs.
- **`read_only` nodes.** `store`, `move` and `transfer` refuse to write to them.
- **Result caps.** `max_results` limits results; truncation is reported with `truncated: true`.
- **Pixels in context only by request.** Raw pixel arrays are never returned. `render_image` and `render_mpr` do send a downsampled PNG to the model; a copy is saved under `storage_dir/renders`.
- **Volume cap.** `render_mpr` refuses series larger than `max_volume_voxels`, and refuses series that fail `check_geometry`.

### De-identification

`deidentify` does the following:

- removes or empties identifying attributes;
- drops private tags and overlay or curve groups;
- replaces PatientName and PatientID with pseudonyms;
- remaps instance UIDs consistently;
- by default, shifts dates by a per-patient offset of 1 to 730 days.

It records what it did in `DeidentificationMethodCodeSequence`.

To get the same pseudonyms and UIDs across runs, set a secret key in the environment variable named by `deid_key_env` (default `DICOM_AGENT_MCP_DEID_KEY`). Without a key, each call uses a random key and the result reports `linkable: false`.

It does **not** remove text burned into the pixels; affected modalities produce a warning. It is a research aid, not a certified de-identification tool.

## Architecture

```
server.py  (FastMCP tools)  →  service.py  (routing, sandbox, paging)
                                   ├── dimse.py     (pynetdicom SCU)
                                   ├── dicomweb.py  (httpx QIDO/WADO/STOW)
                                   ├── imaging.py   (windowing, geometry, ROI stats, MPR, fingerprints)
                                   ├── deid.py      (Basic Profile subset)
                                   ├── contracts.py (TEVL pre/postconditions, store gate, claim comparison)
                                   └── verification.py (verdicts, policy, hash-chained ledger)
```

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

The tests run end to end against an in-process pynetdicom PACS (two AEs so C-MOVE can be tested) and a mocked DICOMweb server. They need no network. The pixel tests use a synthetic CT series with known HU values, spacing and positions.

## Limitations

- STOW-RS builds the whole request in memory, and WADO-RS reads the whole response into memory.
- DIMSE `get_metadata` only returns attributes that the archive indexes for C-FIND.
- `retrieve` uses C-GET. Archives that only support C-MOVE need `move` to a storage SCP.
- No TLS for DIMSE yet.
- Pixel tools have been tested on synthetic CT and pydicom sample files only. Compressed transfer syntaxes, enhanced multi-frame objects and real-world data are not yet tested.
- MPR uses nearest-slice sampling and does not resample to isotropic voxels.
- Pixel tools work on local files; use `retrieve` first.

## Roadmap

See [ROADMAP.md](ROADMAP.md).
