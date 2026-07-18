# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

NIST SURF project: live two-channel RF visualization for SDRs (Deepwave
AIR8201B primary; PlutoSDR and generic SoapySDR supported). Project code lives
in `live/`. The `striqt/` subdirectory is an upstream NIST library (Dr. Dan
Kuester & Aric Sanders) included as a dependency — treat it as **read-only**
unless explicitly told otherwise.

## Architecture (2026-07 refactor)

All radio/DSP/config logic is in the shared package **`live/core/`**; the
scripts in `live/` are thin frontends over it. **Never fix a backend bug in a
frontend script — fix it once in `live/core/`.**

- `core/constants.py` — profiles (`DEVICE_PROFILES`), rates/nfft grids, defaults
- `core/state.py` — runtime device/channels/backend/fps; set once at startup via
  `state.configure_device()` etc., read at call time everywhere
- `core/striqt_compat.py` — defensive striqt imports + AIR-T pixi libstdc++ re-exec
  (must be imported first; `import core` guarantees it)
- `core/parsing.py` — freedom-model parsers, `ANALYSIS_TARGETS`, striqt scratch validators
- `core/config.py` — `RadioConfig` + `SharedConfig` (tier-1 clamp/snap, tier-2
  scratch probe, tier-3 compute backstop). `update()` returns the ack **plus
  `op_id`**; `take_dirty()` returns `(dirty, cfg, op_id, reconnect)`
- `core/dsp.py` — quicklook/calibrated/psd/ssb backends, `aligned_nfft` grid,
  `build_header` (frame-header contract)
- `core/devices/` — adapter layer: `resolve_device(selector)` handles
  `air8201b|air7201b|air7101b|pluto|soapy|demo|auto|driver=X,serial=Y`
  (SoapyAIRT rows are refined to the Deepwave model via `identify_deepwave`);
  adapters expose `create_source(source_config)`, `read_back()`,
  `hardware_expectations()` (accounts for striqt's intentional lo_shift LO
  offset + backend_sample_rate before judging readback), `verify()`,
  `describe_capabilities()`; `probe_channels()` discovers the real RX count
- `core/acquisition.py` — `Acquirer` (ring buffer + rearm + readback),
  `Computer`, `DemoAcquirer` (demo tones are fixed *stations*: they move across
  the band on retune, so tuning is testable without hardware)
- `core/operations.py` — `OPERATIONS` log: every radio-affecting change is an
  operation `requested → validated → applying → applied → readback → data-path
  → verdict {success|verified|unverified|mismatch|failed}`; events stream to
  stdout, `/operations`, and WS `{"op": ...}` messages
- `core/health.py` — `BOOT_ID` (restart proof), `health_snapshot()`
- `core/serialization.py` — `serialize_frame` / `parse_frame`

Frontends: `striqt_web_server.py` (canonical web UI; auth + routes + WS only),
`striqt_kiosk.py` (web UI fullscreen locally), `striqt_standalone_terminal.py`
(curses over SSH). **Deprecated, frozen, do not extend:** `striqt_standalone.py`,
`pluto_standalone.py`, `striqt_server_TCP.py`, `striqt_frontend_TCP.py`.

## Running

```sh
python3 live/striqt_web_server.py --demo            # no hardware, http://localhost:8000
python3 live/striqt_web_server.py                   # AIR8201B
python3 live/striqt_web_server.py --device auto     # SoapySDR enumeration
python3 live/striqt_web_server.py --ports 0         # explicit RX port list
bash live/run_web.sh [--tunnel]                     # launcher (tunnel optional)
python3 live/striqt_kiosk.py --demo                 # local fullscreen browser
python3 live/striqt_standalone_terminal.py --demo --backend quicklook
python3 live/radioctl.py status                     # SSH client for a RUNNING server
sudo bash setup.sh                                  # full installer + TUI
```

## Tests

```sh
cd live && python3 -m pytest tests/     # unit + fake-radio pipeline (no hardware)
python3 live/tools/hardware_qual.py --device auto   # ON the radio host: real readback qual
cd striqt && pytest tests/              # upstream library tests
```

The demo pipeline tests use the quicklook backend so they pass without striqt.
`radioctl.py self-test` qualifies settings THROUGH a running server and
restores the starting configuration afterwards.

The web UI is CDN-free: uPlot is vendored in `live/web/vendor/` (setup.sh
re-fetches it if missing) so hotspot/ethernet modes work fully offline.

## Verified operations / Reset Radio

- Config changes are only trusted after driver readback + a fresh frame; the
  verdict is in the op log (terminal + web OPS tab). Readback is judged
  against `hardware_expectations()` (striqt's programmed LO/rate), so
  lo_shift/backend_sample_rate never false-mismatch. Demo devices report
  `success` with "no hardware readback" honestly.
- Source-spec fields (`{"source": {...}}`) genuinely APPLY via a verified
  device reconnect (`take_dirty` returns a 4-tuple with the reconnect flag;
  the Acquirer closes + reopens with `cfg.source_config` overrides, filtered
  by the spec class's `__struct_fields__`). Explicit JSON null CLEARS an
  override; a failing source config auto-reverts to the last-good set.
- `POST /admin/reset-radio` PREFLIGHTS the sudoers rule (`sudo -n -l`),
  writes stderr to RADIO_RESET_LOG (persistent under systemd), and returns
  `{op_id, boot_id}`; the browser polls `/health` until `boot_id` changes.
- `POST /config` is the HTTP twin of the WS control path (admin only) —
  `live/radioctl.py` uses it for `set` and the reversible `self-test`.
- `/ws/logs` (admin only) streams the journalctl tail into the OPS tab —
  a log view, never a shell.

## Auth & deployment

- Three roles (`admin`/`viewer`/`interns`); only admin mutates config. Browser
  auth is cookie-only (`/login` form); Basic Auth still accepted for curl/API.
  `RADIO_AUTH_DISABLE=1` for demo/dev.
- Production: `setup.sh` GENERATES credentials + `RADIO_SESSION_SECRET` into
  `/etc/radio-web/radio.env` (0600) and installs the systemd unit
  (`deploy/radio-web.service.template` → `deploy/run_service.sh`, mode from
  `RADIO_MODE`: web/hotspot/ethernet/kiosk). The in-source default passwords
  are dev-only; the server warns loudly when they're active.
- `/health` is auth-exempt but returns only `{status, boot_id, uptime_s}` to
  anonymous callers.

## Frontend (live/web/)

- DAN (`pro`) / ARIC (`noob`) are CSS-visibility modes; both tune through the
  same server path. Historical bug: `collectSettings()` targeted a nonexistent
  `#settings-editor`, so DAN's Apply sent an empty payload — fixed to the real
  form containers; keep selectors in sync with index.html.
- Capture form shows MHz / MS/s (converted to Hz on send via
  `FIELD_UNITS`/`dataset.unitScale`); Apply sends only fields changed vs the
  last server seed (`formBaseline`).
- PSD (uPlot): wheel zoom / drag pan / Shift-drag box zoom / Alt-drag band
  selection / double-click reset; zoom survives frames via
  `setData(data, psdZoomX === null)`.
- OPS rail tab renders `{"op": ...}` WS events + `/operations` backfill.
- Waterfall panes are cloned per header `channels`; one channel = full width.
- Read-only roles: `SAFE_SELECTOR` whitelist in app.js gates what they may touch.

## Key constants (core/constants.py)

| Constant | Value | Meaning |
|---|---|---|
| `MAX_TAIL` | `1 << 22` | Ring buffer capacity (4M samples) |
| `READ_SIZE` | `1 << 18` | Chunk size per `_read_stream` call |
| `RATES_HZ` | 3.84/7.68/15.36/30.72 MS/s | LTE/5G-NR grid; incoming rates snap to this |
| `NFFT_CHOICES` | 256…4096 | Valid FFT sizes; always snap |
| `ALIGNED_NFFTS` | 252/504/1008/2016/4032 | 28-multiples the calibrated STFT actually runs |
| `MASTER_CLOCK_RATE` | 125e6 | AIR8201B reference clock (reused best-effort for other Soapy radios) |

## striqt.analysis spectrogram contract

`evaluate_spectrogram` sets `nfft = round(sample_rate / frequency_resolution)`
internally. To guarantee `spg.shape == (channels, rows, nfft)`, pass
`frequency_resolution = sample_rate / nfft` and `duration = rows * nfft /
sample_rate`. The calibrated path snaps FFT size to `aligned_nfft` (multiple of
28 → integer zero-fill at `window_fill = 15/28`; also multiple of 12 for
consistent bin-averaging).

## Historical docs

`docs/` (AUDIT_REPORT, FIXLOG, bug_report, REPO_*) documents the pre-refactor
single-file era. Still useful for rationale (LV-*/P*-* references in comments),
but line numbers and file layout there predate `live/core/`.
