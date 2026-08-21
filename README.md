# AutoCAD MCP v4

AutoCAD MCP v4 is a stateful MCP server for a live AutoCAD .NET bridge and an
optional headless DXF backend. Its live workflow is:

```text
CONNECT -> OBSERVE -> MAP -> PLAN -> PREVIEW -> APPROVAL -> APPLY -> VERIFY
```

The source drawing is immutable. An overlay always targets a separate output
file ending in `_overlay.dwg`.

## Why the transport changed

The former integration dispatched commands through AutoCAD's command line and
depended on foreground window state. Reads could fail or target the wrong UI
context when AutoCAD was not foreground. That path is retired.

```text
MCP client --stdio--> Python MCP server --authenticated loopback JSON--> AutoCADMcpBridge (.NET) --> AutoCAD database API
```

The direct bridge is loaded with `NETLOAD` and does not bring any window to the
foreground, send keystrokes, switch tabs, or use COM/ActiveX.

## Safety contract

- Live AutoCAD access uses only the direct .NET bridge.
- The agent never erases, hides, freezes, or overwrites the source drawing.
- Geometry is accepted only when the bridge returns real vertices, segments,
  bounds, and an explicit `closed` value.
- A plan, preview, and unexpired approval token are required before apply.
- Read requests may reconnect once; write requests are never retried.
- The default layout task budget is 12 MCP calls; the agent never calls a
  full-drawing `entity.list`.
- Missing geometry or capabilities fail closed with structured errors.

## Backends and setup

`direct_bridge` is the default live backend. `ezdxf` is available only when
explicitly selected for offline DXF tasks; a missing live bridge never falls
back to a headless document.

```text
AUTOCAD_MCP_BACKEND=direct_bridge   # default live bridge
AUTOCAD_MCP_BACKEND=ezdxf           # explicit headless DXF mode
AUTOCAD_MCP_BACKEND=auto            # normalized to direct_bridge
```

The legacy `file_ipc` setting remains only as a compatibility alias for
`direct_bridge`; it cannot activate the retired transport.

Install Python dependencies with:

```powershell
uv sync
```

The Python distribution can be built independently of AutoCAD:

```powershell
uv build
```

This produces a wheel and source archive for the MCP server. The AutoCAD
bridge is a separate .NET plugin and must be built against the installed
AutoCAD reference assemblies before it can be loaded with `NETLOAD`; a Python
wheel alone does not contain a usable bridge DLL.

Build `bridge/AutoCADMcpBridge/AutoCADMcpBridge.csproj` using the .NET 10 SDK
and reference assemblies that match the target AutoCAD release. The project
defaults to AutoCAD 2027 at:

```text
C:\Program Files\Autodesk\AutoCAD 2027
```

Load the built assembly in AutoCAD with `NETLOAD`. It writes the authenticated
loopback discovery record to `%LOCALAPPDATA%\AutoCAD-MCP\bridge.json`. If it
is absent, the server returns `AUTOCAD_NOT_CONNECTED` rather than simulating a
live connection.

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "C:\\path\\to\\autocad-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "autocad_mcp"],
      "env": { "AUTOCAD_MCP_BACKEND": "direct_bridge" }
    }
  }
}
```

| Variable | Default | Purpose |
|---|---|---|
| `AUTOCAD_MCP_BACKEND` | `direct_bridge` | Select direct bridge or `ezdxf` |
| `AUTOCAD_MCP_BRIDGE_DISCOVERY` | `%LOCALAPPDATA%\\AutoCAD-MCP\\bridge.json` | Discovery record override |
| `AUTOCAD_MCP_BRIDGE_TIMEOUT` | `30` | Per-request deadline, in seconds |
| `AUTOCAD_MCP_ONLY_TEXT` | `false` | Suppress MCP image attachments |

## Capability contract

The handshake and `session(operation="capabilities_list")` expose a flat
boolean capability map. Important operations are:

- `session.handshake`, `session.health`, `capabilities.list`
- `drawing.get_state`, `drawing.get_fingerprint`, `drawing.get_variables`
- `view.get_state`, `view.get_screenshot`
- `entity.search_text`, `entity.search_text_batch`, `entity.get`,
  `entity.get_geometry`, `entity.get_geometry_batch`, `entity.query`,
  `entity.query_spatial`, `entity.count_by_layer_type`
- `batch.preview`, `batch.apply`, `batch.rollback`, `batch.status`,
  `batch.get_screenshot`

`drawing.get_state` includes `document_id`, absolute path, active space, units,
DBMOD, fingerprint, current layer, and viewport. The fingerprint combines the
document name, database fingerprint, database version GUID, and DBMOD so a
saved source revision cannot reuse the previous plan. Geometry responses include
the source document ID and never infer vertices or `closed`.

`drawing.info` does not enumerate model-space entities by default. Request the
expensive count explicitly with `include_entity_count=true`.

The current bridge deliberately advertises `batch.get_screenshot=false`,
`batch.preview=false`, and `batch.apply=false` until there is a trustworthy
off-screen renderer for the output clone. The mutation agent therefore stops
before preview/apply in this state. A screenshot of the active source document
is never accepted as output-clone evidence.

## Agent tool

The `agent` tool exposes the state machine:

```text
agent interpret    {"request":"find PM4"}
agent execute      {"request":"find PM4", "mode":"read_only"}
agent resume       {"session_id":"...", "request":"find PM5"}
agent cancel       {"session_id":"...", "reason":"user stopped the task"}
agent start       {"mode":"read_only"|"mutation", "max_calls":12}
agent connect     {"session_id":"..."}
agent observe     {"session_id":"...", "labels":["WAREHOUSE","PM4"]}
agent map         {"session_id":"...", "boundary_handles":["..."]}
agent plan        {"session_id":"...", "actions":[...], "target_path":"plant_overlay.dwg"}
agent preview     {"session_id":"..."}
agent approve     {"session_id":"...", "approval_token":"...", "confirmed":true}
agent apply       {"session_id":"...", "approval_token":"..."}
agent verify      {"session_id":"..."}
```

`execute` is the intent-aware orchestration entry point. It accepts `query`,
`inspect`, `overlay`, `modify`, or `generate` (or `intent="auto"`) and advances
only to the next safe boundary. When no `session_id` is supplied, the server
creates a session and returns it in the response. It reports `facts`,
`evidence`, `assumptions`, `unknowns`, `questions`, `concept_model`, `answer`, and
`next_action`; it never silently promotes a read-only task to mutation.
Generation requests produce a proposal-only concept brief until the user
supplies explicit constraints and a separate output plan; they do not create
geometry implicitly.

`resume` continues a non-terminal session with new user context. A changed
request invalidates the old answer and re-runs only the bounded observation
phase. `cancel` marks the session terminal and rolls back an unapplied preview
when the bridge exposes `batch.rollback`; it never deletes an already-applied
output automatically. Every session snapshot includes a machine-readable
`status` (`answered`, `needs_clarification`, `ready_for_preview`,
`awaiting_approval`, `verified`, `blocked`, or `cancelled`) in addition to the
internal workflow `state`.

Examples:

```json
{
  "operation": "execute",
  "data": {
    "request": "layout hiện tại có bao nhiêu khu vực chính?",
    "intent": "auto",
    "mode": "read_only"
  }
}
```

```json
{
  "operation": "execute",
  "data": {
    "session_id": "...",
    "intent": "overlay",
    "labels": ["WAREHOUSE", "PM4", "PM5", "TM1", "TM2", "TM3", "TM4", "TM5", "TM6"],
    "boundary_handles": ["AB12"],
    "actions": [
      {"action":"copy_to_overlay", "source_handle":"AB12", "target_layer":"VIS_OVERLAY_BOUNDARY"}
    ],
    "target_path": "C:/work/plant_overlay.dwg"
  }
}
```

For inspect/overlay requests without explicit labels, the runtime uses the
bounded factory-layout label set and a polyline type filter; it does not call a
full-drawing `entity.list`. Any unresolved relationship remains an `unknown`.

The plan is bound to source `document_id`, fingerprint, DBMOD, target path, and
a plan hash. Valid action kinds are `preserve`, `copy_to_overlay`,
`simplify_copy`, and `create_connector_line`. `removed_handles` must be empty
for the first immutable-source overlay workflow. Connector endpoints and
simplification vertices must reference real source geometry indices. Required
labels are carried as verified `preserve` actions and are checked in the
source-to-overlay handle map.

### Read-only example

```text
1. agent start {"mode":"read_only"}
2. agent connect {"session_id":"..."}
3. agent observe {"session_id":"...", "labels":["WAREHOUSE","PM4","PM5","TM1","TM2","TM3","TM4","TM5","TM6"]}
4. agent map {"session_id":"...", "boundary_layers":["BOUNDARY"]}
5. agent status {"session_id":"..."}
```

This produces facts, verified geometry, label-to-boundary relations, process
mapping, topology, unknowns, and a change table without editing the drawing.

### Mutation example

```json
{
  "operation": "plan",
  "data": {
    "session_id": "...",
    "target_path": "C:/work/plant_overlay.dwg",
    "actions": [
      {"action":"copy_to_overlay", "source_handle":"AB12", "target_layer":"VIS_OVERLAY_BOUNDARY"},
      {"action":"simplify_copy", "source_handle":"CD34", "target_layer":"VIS_OVERLAY_LINE", "vertex_indices":[0,2,5]}
    ]
  }
}
```

Review the plan, call `preview`, inspect source-to-overlay handles and the
output-clone screenshot, then explicitly approve and apply. Verify must prove
that source fingerprint and DBMOD are unchanged and output handles exist in the
separate file.

## Structured errors and tests

Stable errors include `AUTOCAD_NOT_CONNECTED`, `DOCUMENT_NOT_RESOLVED`,
`UNSUPPORTED_CAPABILITY`, `GEOMETRY_UNAVAILABLE`, `REQUEST_TIMEOUT`,
`TRANSACTION_FAILED`, `VERIFICATION_FAILED`, `APPROVAL_REQUIRED`,
`APPROVAL_EXPIRED`, `SOURCE_IMMUTABLE`, `SOURCE_UNSAVED`, and
`SOURCE_FINGERPRINT_CHANGED`. Failed results retain a legacy message and add an
`error_info` object with `code`, `message`, and `details`.

Run the Python suite without AutoCAD or a user DWG:

```powershell
python -m compileall -q src
uv run pytest -q
git diff --check
```

The tests cover direct transport handshake, reconnect and cancellation;
capability fail-closed behavior; geometry validation; state transitions; call
budgets; approval expiry; source immutability; output-path safety; and
screenshot scope.

The C# bridge cannot be claimed compiled unless a compatible .NET SDK and the
AutoCAD reference assemblies are available. Core development does not open or
modify a user DWG.

## Retired files

The old AutoLISP/file-dispatch files remain only for source-history compatibility
and are not loaded or called by the v4 Python runtime. They must not be used as a
live transport.

## License

MIT
