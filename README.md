# NIST-Omran

## Overview

Live RF visualization for SDR hardware (Deepwave AIR8201B, PlutoSDR, generic
SoapySDR radios, or a synthetic demo source): dual-channel spectrogram
waterfalls + PSD, viewable in any browser, in a terminal over SSH, or
fullscreen on the radio's own display.

## Architecture

All radio, DSP, and configuration logic lives in **`live/core/`** — one shared
backend that every frontend drives:

```
live/core/                  shared backend (Python 3.9+)
├── config.py               RadioConfig + SharedConfig (validated control path)
├── acquisition.py          Acquirer / Computer / DemoAcquirer threads
├── dsp.py                  calibrated / quicklook / psd / ssb backends, frame header
├── parsing.py              freedom-model parsers + striqt scratch validators
├── devices/                device adapters: air8201b, pluto, generic soapy, demo
│                           (discovery, capability envelopes, hardware readback)
├── operations.py           verified-operations log (requested → … → verdict)
├── health.py               /health model (boot_id, radio + frame liveness)
└── serialization.py        binary frame wire format (serialize/parse)

live frontends:
├── striqt_web_server.py    web UI server (FastAPI + WebSocket)  ← canonical
├── striqt_kiosk.py         same web UI fullscreen on the local display
├── striqt_standalone_terminal.py   curses monitor for SSH (ASCII waterfall)
├── radioctl.py             SSH client for the RUNNING server (status / set /
│                           logs / reversible self-test)
└── (deprecated, frozen: striqt_standalone.py, pluto_standalone.py,
     striqt_server_TCP.py, striqt_frontend_TCP.py)
```

Supported radios: Deepwave **AIR8201B / AIR7201B / AIR7101B** (auto-identified
from SoapyAIRT enumeration), **PlutoSDR**, any other SoapySDR radio
(best-effort generic adapter), and a synthetic **demo** source. RX channel
count is discovered from the live driver (`--ports 0,1` overrides it), and the
UI lays out one full-width pane per discovered channel. The web UI has **no
CDN dependencies** — uPlot is vendored — so hotspot/ethernet modes work with
zero internet.

`striqt/` is the supporting acquisition/analysis library (Dr. Dan Kuester &
Aric Sanders, NIST) — read-only here.

### Verified operations

Every radio-affecting change is a tracked **operation**: requested →
validated/clamped → applied to hardware → **driver readback** (does the radio
report the commanded frequency/rate/gain?) → data-path proof (a fresh frame
computed with the new config) → verdict (`verified` / `unverified` /
`mismatch` / `failed`). Readback compares against what striqt actually
programs — an intentional `lo_shift` LO offset or a `backend_sample_rate`
resample is expected, never a false mismatch. Operations stream to the
terminal, the journal, and the web UI's **OPS** tab (which also tails the
service journal for admins — a log view, never a shell). Source-spec settings
(clock/time source, calibration, …) now genuinely apply, via a verified
device **reconnect**; a failing source config auto-reverts to the last one
that worked. Reset Radio preflights the sudoers rule (real sudo error text on
failure), then the browser polls `/health` until the server's `boot_id`
changes — the only real proof of a restart.

## Quick start

```sh
# One-shot install + setup TUI (Debian-family Linux; idempotent):
sudo bash setup.sh            # asks: mode, port, hostname, credentials…
sudo bash setup.sh --defaults # no questions

# Just the Python deps (no root):
bash setup.sh --deps-only

# Try it with no hardware at all:
python3 live/striqt_web_server.py --demo        # → http://localhost:8000
```

Modes the installer can configure (all serve the same web UI):

| Mode       | What you get |
|------------|--------------|
| `web`      | web server on the existing network (`http://<host>.local:8000`) |
| `hotspot`  | the radio broadcasts its own Wi-Fi AP — connect and browse to `http://10.42.0.1:8000` |
| `ethernet` | plug a laptop straight into the radio's Ethernet port; it gets an address automatically (`http://10.42.0.1:8000`) |
| `kiosk`    | web UI fullscreen on the radio's own display |
| `terminal` | no service; run the curses monitor manually |

## Running things by hand

```sh
python3 live/striqt_web_server.py                  # AIR8201B
python3 live/striqt_web_server.py --device pluto   # PlutoSDR
python3 live/striqt_web_server.py --device auto    # enumerate SoapySDR
python3 live/striqt_web_server.py --device driver=plutosdr,serial=XYZ
python3 live/striqt_web_server.py --demo --quantize --fps 10

bash live/run_web.sh                # launcher (add --tunnel for Cloudflare)
python3 live/striqt_kiosk.py --demo # local fullscreen browser
python3 live/striqt_standalone_terminal.py --demo --backend quicklook
```

## Testing

```sh
cd live && python3 -m pytest tests/        # unit + fake-radio pipeline tests

# Bench qualification (exclusive radio access, no server running):
python3 live/tools/hardware_qual.py --device auto

# Qualify THROUGH the running service (reversible; restores the config):
python3 live/radioctl.py --user admin self-test
```

## Web UI notes

- Three roles (admin / viewer / intern). Production credentials are GENERATED
  by `setup.sh` into `/etc/radio-web/radio.env` — the defaults baked into the
  source are dev-only and the server warns loudly when they're active.
- DAN mode = full control surface; ARIC mode = simplified station tuner. Both
  tune through the identical validated path.
- PSD plot: wheel zoom, drag pan, Shift-drag box zoom, Alt-drag band-monitor
  selection, double-click reset.
- OPS tab: live operation trail + service health.
- The waterfall grid adapts to the device's channel list — a single-channel
  radio gets one full-width pane.

## License and Attribution

This work was developed in connection with the NIST SURF project “Development
of visualization frontends for cellular 5G-NR measurements.” Reuse,
redistribution, or derivative work should be approved by the appropriate NIST
project mentors (Dr. Aric Sanders & Dr. Dan Kuester) and the repository
maintainer before use outside the intended NIST research context.

This repository is currently not licensed for public reuse. Unless a separate
license is added, all rights are reserved for the project-specific code in
this repository.

The `striqt/` library included in this repository was developed separately and
is not authored by Mustafa Omran. Its own README, notices, and license terms
should be preserved and followed.
