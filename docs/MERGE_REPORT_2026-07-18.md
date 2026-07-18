# Combined Implementation Report — 2026-07-18

This is the authoritative record of the 2026-07-18 rework of the live viewer
in **this repository** (`/Users/mustafaomran/merge/NIST-Omran`). Two parallel
model implementations of the same request existed in *different checkouts*;
this tree contains the merged result: the shared-core architecture built here,
plus every improvement worth adopting from the other implementation's
transcripts. `striqt/` was never modified.

## Architecture

- **`live/core/`** — the single shared backend (Python 3.9-compatible):
  `constants` (profiles/grids), `state` (runtime device selection),
  `striqt_compat` (defensive imports + AIR-T pixi re-exec), `parsing`
  (freedom-model + striqt scratch validators), `config`
  (RadioConfig/SharedConfig), `dsp` (calibrated/quicklook/psd/ssb + headers),
  `devices/` (adapters, discovery, readback), `acquisition`
  (Acquirer/Computer/DemoAcquirer), `operations` (verified-operations log),
  `health` (boot_id + liveness), `serialization` (frame wire format).
- **Frontends** (all thin): `striqt_web_server.py` (canonical web UI),
  `striqt_kiosk.py` (same UI fullscreen locally), `striqt_standalone_terminal.py`
  (curses ASCII waterfall for SSH), `radioctl.py` (HTTP client: status / set /
  logs / reversible self-test). Legacy Qt/TCP scripts are frozen with
  deprecation banners.

## Verified operations

Every radio-affecting change is an operation:
`requested → validated → applying → applied → readback → data-path → verdict`
with verdicts `success | verified | unverified | mismatch | failed | superseded`.

- Readback queries the driver's own getters and judges them against
  **hardware expectations** derived from striqt's resampler/LO design, so an
  intentional `lo_shift` or `backend_sample_rate` never false-mismatches.
- Verification is **scoped to the changed fields**: a rows-only change is
  proven by validation + a fresh frame, and cannot be downgraded by an
  unrelated missing driver getter. Radio open/recovery and source reconnects
  still check the full recipe.
- Data-path proof: an operation only completes when a frame of the new ring
  generation actually computes.
- Operations stream to the terminal/journal, `/operations`, and the web OPS
  tab; `/ws/logs` (admin) additionally tails `journalctl` — a log view, never
  a shell.

## Fixed bugs (root causes)

- **DAN mode never tuned**: `collectSettings()` targeted a nonexistent
  `#settings-editor`, so Apply sent an empty payload ("applied []"). Fixed to
  the real form containers; plus MHz/MS/s display units (Hz mistypes used to
  clamp to the 300 MHz floor silently) and diff-only Apply.
- **Reset Radio was fire-and-forget**: now sudo-preflighted (`sudo -n -l`
  with real error text), stderr persisted to `RADIO_RESET_LOG`, 202 carries
  `{op_id, boot_id}`, and the browser verifies by polling `/health` until the
  boot_id changes.
- **Ctrl-C shutdown traceback**: lifespan swallowed `Exception` but not
  `CancelledError`; fixed.
- **Source settings silently unapplied** (old LV-G1): `{"source": {...}}` now
  genuinely applies via a verified device reconnect; explicit `null` clears an
  override; a failing source config auto-reverts to the last-good set.
- **Pluto master clock** (old bug P-1): the Pluto profile now uses 61.44 MHz,
  not the AIR-T's 125 MHz.

## Devices

Profiles: AIR8201B / AIR7201B / AIR7101B (SoapyAIRT rows refined by
`identify_deepwave`), PlutoSDR, generic SoapySDR (best-effort), demo.
Selection: profile name, `auto` (exactly one radio), or
`driver=X[,serial=Y]` when several are attached. RX channel count is probed
from the live driver (`--ports 0,1` overrides); the web UI builds one
full-width pane per channel. Demo tones are fixed *stations* that move across
the band on retune, so tuning is testable without hardware.

## UI

Interactive PSD (wheel zoom, drag pan, Shift-drag box zoom, Alt-drag band
selection, double-click reset; zoom survives frames, resets on retune); OPS
tab (op trail + health + admin journal); baseband axes labeled as Δ-offsets;
station chips gated by the active device's tuning envelope; **no CDN
dependencies** — uPlot is vendored in `live/web/vendor/`, fonts fall back to
system stacks, so hotspot/ethernet modes work fully offline.

## Deployment

`setup.sh` (idempotent, whiptail TUI or `--defaults`): apt deps + SDR driver
modules, repo venv, **generated** credentials + session secret into
`/etc/radio-web/radio.env` (0600), hardened systemd unit
(`deploy/radio-web.service.template` → `deploy/run_service.sh`;
`Restart=always`, `ProtectSystem/ProtectHome/PrivateTmp`, `LogsDirectory`,
and deliberately **no** `NoNewPrivileges` — it would break the reset sudo
rule), sudoers install, avahi mDNS (`<host>.local`), UFW port rule when
active, and mode-specific NetworkManager profiles: `hotspot` (AP, shared
IPv4) and `ethernet` (shared DHCP for a directly-cabled laptop). Modes:
web / hotspot / ethernet / kiosk / terminal. Kiosk uses a /tmp Chromium
profile (survives ProtectHome) and never weakens auth on its own.
`run_web.sh` runs LAN-only unless `--tunnel`.

## Verification status

- `cd live && python3 -m pytest tests/` — **40 tests**, all passing: config
  clamps/mapping/null-clears, source reconnect semantics, serialization
  round-trips, operation lifecycle + superseded, adapter contract incl.
  LO-expectation correction, scoped verification, demo pipeline E2E, and an
  auth-enabled HTTP integration suite (anonymous minimal health, login
  redirect, viewer 403, admin 200).
- Demo servers exercised over real HTTP + WebSocket: tune → op events → frame
  echo; `radioctl.py self-test` (7 verified ops, config restored); rapid
  supersession; single-channel layout; clean SIGINT shutdown.
- Static: py_compile everything, `node --check app.js`, `bash -n` all shell,
  DOM id cross-check (0 missing).

## Still requires the target hardware

1. `python3 live/tools/hardware_qual.py --device auto` on the AIR-T and Pluto
   hosts (exit 0 = verified, 1 = mismatch/failure, 2 = readback unsupported).
2. `python3 live/radioctl.py --user admin self-test` through the deployed
   service.
3. `sudo bash setup.sh` on a clean Debian-family host; hotspot/ethernet
   profiles; kiosk display startup.
4. A manual visual pass of PSD gestures and the OPS/LOGS panels (browser
   automation was unavailable during development).
