# ADAS HIL status

Answers three questions about a dSPACE ADAS HIL bench (ADAS ECU, camera, radar, LIDAR):

1. **Is the rig up?** Simulation state, KL30/KL15, read from the dSPACE model through ASAM XIL API.
2. **Which ECUs are connected?** Supply current per ECU (from the model) and a live link check per ECU:
   UDS TesterPresent over CAN (ISO-TP) or Ethernet (DoIP), or a TCP check for ECUs without UDS.
   A functional TesterPresent on 0x7DF also lists every CAN node that answers, including ones nobody listed.
3. **Do the versions match the inventory file?** SW/HW versions come from the bench's inventory text file
   and are checked against what the ECU reports (UDS 0x22, DIDs F195/F193/F18C by default, configurable per ECU).

Everything is read-only: TesterPresent and ReadDataByIdentifier in the default session. The tool never
starts/stops the simulation, never downloads an application and never switches power.

## Run

```bash
pip install -r requirements.txt
python -m hil_status --config config/rig.toml --json out/status.json --html out/status.html
```

### Live dashboard

```bash
python -m hil_status --config config/rig.toml --serve --interval 15
```

Open http://127.0.0.1:8790/. The page re-checks the bench every `--interval` seconds and shows:
overall PASS/WARN/FAIL, KPI tiles (ECUs connected, version mismatches, unlisted CAN nodes, simulation,
KL30/KL15, CAN state), an **Action needed** list (what to fix and how), the bench topology (rig → Ethernet /
CAN → ECUs), one card per ECU (power, link latency, SW/HW expected vs actual, serial, address), the
software baseline table and a history strip of the last checks. "Re-check now" runs a check immediately.
The XIL session stays open between checks, and the inventory file is re-read on every check. It binds to
this PC only; pass `--host 0.0.0.0` to share it on the lab network.

`--html out/status.html` writes the same dashboard as a single static snapshot file.

### Campaign, coverage and defect clustering

With a `[campaign]` section in the config, the dashboard gets three more tabs, fed by two CSV exports
from the test tool (`data/campaign/` has a synthetic ADAS example: ACC, AEB, LKA, TSR, diagnostics):

- `requirements.csv`: `id, title, feature, asil`
- `results.csv`: `test_id, title, requirements (; separated), feature, ecu, status, defect_id, failure_signature, duration_s, executed_at`
  with status `PASS`, `FAIL`, `BLOCKED` or `NOT_RUN`.

| Tab | Shows |
|---|---|
| Campaign status | executed/total, pass rate, failed, blocked, release under test, days to planned end, progress per feature |
| Coverage | requirements planned / executed / verified (all linked tests pass), by feature and by ASIL, filterable requirement list; results pointing at unknown requirements are reported as a traceability gap |
| Defect clustering | failures by feature × ECU, and clusters of failed/blocked tests grouped by defect ID, or by failure signature with numbers masked when no ticket exists yet |

The defect view uses the bench status: a cluster whose ECUs are all unhealthy on the bench right now
(wrong software, not powered, silent) is flagged **suspect bench**, with the reason, so the environment
gets checked before a product defect is raised. Untracked clusters on a healthy bench are flagged
**needs ticket**. The campaign files are re-read on every check. This is visibility for the test manager;
nothing signs a release off automatically, and the exit code reflects the bench only.

`config/rig.toml` ships with `backend = "sim"`: a virtual bench (simulated ECUs answering real UDS over a
python-can virtual bus and DoIP on localhost) with three demo faults: the camera has an older SW, the LIDAR
is switched off, and an unlisted ECU answers at 0x7EE. Delete the `[sim.*]` sections for a clean PASS.

Exit code: `0` PASS, `1` WARN (version mismatch, unlisted ECU, ECU not probed), `2` FAIL (rig not running,
ECU not powered / not answering), `3` bad config or inventory. A test job can run it before a campaign and
only start on `0`.

## Inputs

**Inventory text file** (`data/ecu_inventory.txt`): what should be on the bench. Layout is auto-detected:
pipe/CSV/semicolon/TAB tables (Markdown too), space-aligned tables, `[ECU]` INI sections, or `key: value`
blocks. Column names are matched by alias (`SW Version`, `Software`, `FW`, `HW Rev`, ...). Extra columns
are kept and shown in the JSON.

**Rig config** (`config/rig.toml`): how to reach each ECU, joined to the inventory by ECU name.

| transport | used for | live check | versions |
|---|---|---|---|
| `can`  | camera, radar | UDS over ISO-TP (`tx_id`/`rx_id`) | read and compared |
| `doip` | ADAS ECU, Ethernet cameras | UDS over DoIP (`host`, `logical_address`) | read and compared |
| `tcp`  | LIDAR (vendor protocol, no UDS) | TCP connect to `host:port` | from inventory |
| `none` | anything else | supply current only | from inventory |

## ECU states

| State | Meaning |
|---|---|
| OK | powered, answers, versions match (or nothing to compare) |
| VERSION_MISMATCH | answers, but SW/HW differs from the inventory: reflash or update the sheet |
| NO_RESPONSE | powered (or power not measured) but silent: wrong bus, bootloader, crashed, cable |
| NOT_POWERED | supply current below `min_current_a`: not connected or power channel off |
| NOT_PROBED | in the inventory but no `[ecus.NAME]` entry in the config |
| ERROR | protocol error (bad ISO-TP sequence, DoIP NACK, routing denied) |

## On the dSPACE PC (`backend = "xil"`)

1. `pip install pythonnet`.
2. In `[xil]`: copy `product_version` and the `clr.AddReference(...)` assembly strings from the XIL API Python
   demo that ships with **your** dSPACE release (they change between releases), and set `port_config` to
   the project's MAPort configuration XML.
3. Replace the placeholder model paths (`kl30_voltage_var`, `kl15_var`, each ECU's `current_var`) with the
   real paths of the power-supply / load channels in your model.
4. Set `[can] interface/channel/bitrate` for the diagnostic channel (Vector, PEAK, Kvaser...), the real
   CAN IDs, DoIP IPs and logical addresses, and per-ECU `dids` if your OEM uses F189/F191 instead of F195/F193.
5. Check once that `MAPort.Configure(..., forceConfig=False)` attaches to the running application on your
   release without re-downloading it.

Not verified against real hardware yet: the XIL API calls follow the ASAM XIL 2.x .NET API used by dSPACE's
Python demos; the UDS/ISO-TP/DoIP code is tested against the simulator only.

## Limits

- ISO-TP: classic 8-byte frames, single-frame requests (all the tool needs). CAN-FD ECUs that only accept
  FD-length frames need an ISO-TP-FD extension.
- DoIP: TCP only, no vehicle announcement / UDP discovery, no TLS (ISO 13400-2:2019 port 3496).
- Versions are compared exactly (trimmed, case-insensitive).

## Tests

```bash
python -m pytest -q
```

## Cursor rules

`.cursor/rules/` and `AGENTS.md` hold the Cursor agent rules this repo started with: a source-grounding
contract (cite files, label inference, say "Not in sources" instead of guessing) plus domain rules for web
UI, Android, technical docs and marketing copy. They load automatically when the repo is opened in Cursor.
