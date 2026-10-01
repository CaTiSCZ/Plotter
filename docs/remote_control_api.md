# SCADA Remote Control API

Version: 1.0
HTTP API version: `v1`
Implementation: `remote_control.py` and `Plotter` in `scada.py`

This document is the agent-facing contract. Clients should discover widget IDs from the API instead of deriving IDs from Python names or source code.

## Availability and Base URL

The listener is controlled by the **Remote Control** checkbox in the SCADA window. When unchecked, the HTTP listener is stopped and requests cannot be made. Toggle it on in the GUI to start the listener. The **Web addr:port** field can be changed and applied while SCADA is running; a failed rebind leaves the previous listener active.

At startup, `REMOTE_CONTROL_ENABLED` and `REMOTE_CONTROL_ADDR_PORT` are loaded from `default_settings.py`. Runtime GUI changes are temporary and are not written back to that file. The default bind is `127.0.0.1:8765`. The GUI status label shows the active address; `GET /api/v1/status` also reports it while the listener is running. Port `0` asks the operating system for an available port; use the actual port shown by the status label or status endpoint.

Base URL examples:

```text
http://127.0.0.1:8765
http://192.168.1.25:8765
```

Only IPv4 bind addresses are supported. Binding to a non-loopback address, including `0.0.0.0`, requires `REMOTE_CONTROL_TOKEN` to be non-empty. When a token is configured, it is required on every request, including loopback requests:

```http
Authorization: Bearer <REMOTE_CONTROL_TOKEN>
```

The listener uses plain HTTP; bearer credentials and responses are not encrypted. Use a trusted isolated network or a VPN and firewall rules for LAN access. Do not expose the port to an untrusted network. The token is never included in API responses.

All responses set `Cache-Control: no-store`. There is no CORS support. Requests are not persisted across application restarts.

## Response Format

Successful requests return HTTP `200` and this envelope:

```json
{
  "api_version": 1,
  "request_id": "e2504ff44ec84daaaeed16943158c004",
  "ok": true,
  "data": {}
}
```

Errors return an HTTP error status and this envelope:

```json
{
  "api_version": 1,
  "request_id": "e2504ff44ec84daaaeed16943158c004",
  "ok": false,
  "error": {
    "code": "invalid_request",
    "message": "Details for a human or agent"
  }
}
```

The `request_id` is unique per HTTP request and is useful for correlating errors with the SCADA log. JSON request bodies must be objects and may be at most 64 KiB. At most eight HTTP requests are processed concurrently; excess requests receive `503 busy`. GUI-thread requests time out after 60 seconds. If an action has already begun on the GUI thread when the client times out, it may still finish; check the resulting state before retrying a non-idempotent action.

## Read Endpoints

### `GET /api/v1/status`

Returns listener and SCADA state without querying hardware:

```json
{
  "remote_control_enabled": true,
  "remote_control_address": "127.0.0.1:8765",
  "system_status": "System: RUNNING",
  "device_count": 2,
  "ptp_enabled": true,
  "capture_active": false
}
```

The endpoint only exists while Remote Control is enabled.

### `GET /api/v1/ui/state`

Returns a GUI-thread snapshot of all descendant Qt widgets in the SCADA window, including hidden controls and read-only elements. The response contains `captured_at` and a `widgets` array. A widget descriptor has an `id`, `type`, `object_name`, `visible`, `enabled`, `tooltip`, `capabilities`, and type-specific properties such as `text`, `checked`, `value`, range, or combo-box items.

Example descriptor:

```json
{
  "id": "posttrigger_ms",
  "type": "QSpinBox",
  "object_name": "posttrigger_ms",
  "visible": true,
  "enabled": true,
  "tooltip": "",
  "capabilities": ["set_value"],
  "value": 10,
  "minimum": 0,
  "maximum": 30000
}
```

Important object names include `remote_control_enabled`, `remote_control_addr_port`, `remote_control_apply`, `remote_control_status`, `receiver_addr_port`, `measurement_number`, `apply_device_list`, `apply_config`, `start_system`, `stop_system`, `system_status`, `pretrigger_ms`, `posttrigger_ms`, `start_sampling`, `start_sampling_on_trigger`, `save_measurement`, `save_calibration`, `force_trigger`, `reset_counter`, `reset_latched_faults`, `reset_devices`, `downsample_mode`, `downsample_factor`, `clip_to_view`, `statistics`, `log_visible`, and `log_output`. Device-row controls use names such as `device_0_enabled`, `device_0_address`, `device_0_leader`, `device_0_clock`, `device_0_trigger`, and `device_0_trigger_holdoff_us`; ISOMON controls use `isomon_*` names.

Some Qt widgets do not have an explicit object name. Their IDs are generated from their current widget hierarchy. Treat every returned ID as opaque, use it exactly as returned, and rediscover it after application upgrades or UI changes. Labels, plots, and other read-only widgets have an empty `capabilities` list. Text widgets are truncated to the most recent 16 KiB in the snapshot.

### `GET /api/v1/live`

Returns the latest per-device live snapshot: last DATA age, fault words and validity, analog values and validity. This is a point-in-time view, not a packet stream. Poll at a moderate rate (for example 2-10 Hz).

### `GET /api/v1/statistics`

Returns `captured_at`, plain `text`, and the current `html` value from the statistics label under the plots. The HTML is limited to 64 KiB. Statistics are refreshed by the normal GUI timer.

### `GET /api/v1/log?tail=N`

Returns the last `N` lines currently displayed in the GUI log pane, plus `captured_at`. `N` defaults to 100 and must be from 1 through 1000. Each line and the total response are bounded; the oldest text is truncated when needed. This reads the GUI log document, not the full disk log file.

### `GET /api/v1/plots?points=N`

Returns plot visibility, current x/y ranges and sampled series for `signals`, `detection`, and `isomon_resistance`. `N` defaults to 1000 and must be from 10 through 5000. Each series reports its original `total_points` and at most `N` evenly sampled `x`/`y` values. Non-finite values are returned as JSON `null`. A plot can have no series before data arrives.

## Measurement Endpoints

### `POST /api/v1/measurement/start`

Starts a new capture using the same Plotter workflow as the GUI buttons. The default mode is immediate; `trigger` arms the trigger-based workflow. Optional timing values are milliseconds and must fit the current GUI spinbox ranges.

```json
{
  "mode": "immediate",
  "pretrigger_ms": 100,
  "posttrigger_ms": 1000
}
```

Successful response data reports `accepted`, mode and timing values. The workflow schedules its start through Qt timers, so acceptance does not mean capture completion. Use status, live state, statistics and log to monitor it. PTP behavior follows the GUI's configured PTP mode.

### `POST /api/v1/measurement/stop`

Stops sampling on applied devices using the GUI's sampling-stop workflow. The response means the stop request was accepted by the GUI; use `/status` and `/live` to inspect resulting state.

### `POST /api/v1/measurement/save`

Saves the current measurement using the GUI measurement number and existing `RICE_mereni/NNNN_devX.csv` naming. On success, the measurement number increments and the response contains the written file paths. Saving an empty capture or an invalid measurement number returns an error instead of opening a modal dialog.

## Widget Actions

### `POST /api/v1/widgets/{widget_id}`

`widget_id` is the exact ID from `/ui/state`, URL-encoded as a path segment if needed. Only operations in that widget's `capabilities` are allowed. The response `data` is the updated widget descriptor.

Set text:

```json
{"action": "set_text", "text": "192.168.1.10:10578"}
```

Set a spinbox/slider value:

```json
{"action": "set_value", "value": 250}
```

Choose a combo option by text or index:

```json
{"action": "select_text", "text": "Peak"}
```

```json
{"action": "select_index", "index": 3}
```

Set a checkable button or checkbox:

```json
{"action": "set_checked", "checked": true}
```

Click a button:

```json
{"action": "click"}
```

Clicks use the existing GUI handlers. Clock and trigger EEPROM save clicks are treated as confirmed; this matches the configured global Remote Control enable gate and avoids opening a blocking confirmation dialog. `save_calibration` requires a destination path and cannot be clicked directly:

```json
{"action": "save_to_path", "path": "RICE_mereni/calibration_debug.json"}
```

Calibration paths must resolve inside the current working directory's `RICE_mereni` directory. Native file dialogs are never opened by an API request.

### `POST /api/v1/plots/view`

Sets the visible plot range; either range may be omitted. Plot names are `signals`, `detection`, and `isomon_resistance`. X ranges are linked by the GUI.

```json
{
  "plot": "signals",
  "x_range": [-0.1, 0.5],
  "y_range": [-300, 300]
}
```

Each range must contain two increasing finite numbers. This changes the displayed view, not the captured data.

## Common Workflow

1. Enable **Remote Control** in the GUI and read its status/address.
2. `GET /api/v1/ui/state`; locate `pretrigger_ms` and `posttrigger_ms` or other widgets by ID.
3. `POST /api/v1/measurement/start` with immediate or trigger mode.
4. Poll `/api/v1/status`, `/api/v1/live`, `/api/v1/statistics`, `/api/v1/log`, or `/api/v1/plots` as needed. The main SCADA window continues to show live curves.
5. `POST /api/v1/measurement/stop` when appropriate, then `POST /api/v1/measurement/save` after capture data is ready.

Equivalent widget interactions can be used for device configuration: set device values, then click `apply_device_list` or `apply_config`. The API preserves the existing GUI workflow and does not provide arbitrary Python method access.

## Errors

| HTTP | Error code examples | Meaning |
|---|---|---|
| `400` | `invalid_json`, `invalid_value`, `out_of_range`, `unsupported_action`, `invalid_mode` | Malformed request or rejected value/action |
| `401` | `unauthorized` | Missing or incorrect bearer token |
| `404` | `not_found`, `widget_not_found` | Unknown route or widget ID |
| `409` | `path_required`, `no_devices` | Valid operation cannot run in the current state |
| `413` | `request_too_large` | Body exceeds 64 KiB |
| `500` | `internal_error` | Unexpected operation failure; correlate with `request_id` |
| `503` | `busy`, `remote_disabled` | Request capacity is full or a queued request reached the GUI after Remote Control was disabled |
| `504` | `timeout` | GUI thread did not complete within 60 seconds |

Bind errors are shown in the GUI status label. If a runtime rebind fails, the old listener stays active. The HTTP service does not support CORS or IPv6.

## Python Example

```python
import json
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8765"
headers = {"Authorization": "Bearer replace-with-configured-token"}

def get(path):
    request = Request(base + path, headers=headers)
    with urlopen(request, timeout=5) as response:
        return json.load(response)

def post(path, payload):
    request = Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=65) as response:
        return json.load(response)

ui = get("/api/v1/ui/state")["data"]
print("widgets:", len(ui["widgets"]))
print(get("/api/v1/status")["data"])
print(post("/api/v1/measurement/start", {"mode": "immediate", "posttrigger_ms": 500}))
```