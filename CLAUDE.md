# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

NIST SURF project: live two-channel RF visualization using the Deepwave AIR-T / AIR8201B SDR. Project-specific code lives in `live/`. The `striqt/` subdirectory is an upstream NIST library (authored by Dr. Dan Kuester & Aric Sanders) included as a dependency — treat it as read-only unless explicitly modifying it.

## Running the live scripts

All commands from the repository root:

```sh
# Full standalone GUI (AIR8201B + display on same machine)
python3 live/striqt_standalone.py

# Terminal monitor (SSH / no GUI)
python3 live/striqt_standalone_terminal.py --center-mhz 1955 --rate-msps 15.36 --nfft 1024 --rows 40 --fps 3

# Networked: server on AIR-T, viewer on another machine
python3 live/striqt_server_TCP.py
python3 live/striqt_frontend_TCP.py <server-ip>       # default port 5005

# PlutoSDR (Raspberry Pi, single-channel)
python3 live/pluto_standalone.py

# Web viewer (browser-accessible from any device)
python3 live/striqt_web_server.py --demo       # no hardware required
python3 live/striqt_web_server.py              # real AIR8201B radio
bash live/run_web.sh                           # radio + Cloudflare Tunnel (internet access)
```

## striqt library

Install (must have `from_spec` and `arm_spec` on `Airstack1Source`):
```sh
pip install 'striqt @ git+https://github.com/usnistgov/striqt'
```

Run striqt tests from the `striqt/` directory:
```sh
cd striqt && pytest tests/
```

Lint striqt code:
```sh
cd striqt && ruff check src/ && ruff format --check src/
```

## Architecture of the live scripts

All five live scripts share the same three-layer pattern:

1. **`SharedConfig`** — thread-safe dataclass (`center`, `sample_rate`, `gain`, `nfft`, `rows`) with a dirty flag. The GUI/browser writes to it; the acquirer polls `take_dirty()` and rearms the radio when set.

2. **`Acquirer`** (`threading.Thread`) — reads raw complex64 IQ from the SDR into a per-channel ring buffer (or newest-wins slot), computes the spectrogram, and publishes the result. Exposes `get_latest(n)` (ring-buffer scripts) or `latest()` (web/TCP scripts).

3. **Display layer** — in the Qt scripts this is `LocalReceiver` (QThread) + `LiveViewer` (QMainWindow). In `striqt_web_server.py` it's an `asyncio` broadcaster task that calls `acquirer.latest()` at `BROADCAST_FPS` and fans frames out to all WebSocket clients.

**Frame dict / header:** always `{center, fs, gain, nfft, rows, shape, channels, time}` plus `blocks` (list of `(rows, nfft)` float32 dB arrays, one per channel, oldest-row-first). The web server serializes this as a single binary WebSocket message: `[4-byte LE uint32 header length][JSON header bytes][block bytes...]`.

**Hardware path:**
- `Air8201BSourceSpec` + `Airstack1Source.from_spec()` → `source.arm_spec(SoapyCapture(...))` → `source._read_stream(buffers, ...)`
- The live scripts use `getattr`-based shims (`get_rx_stream`, `open_stream`, etc.) because the installed striqt build exposes different method names than the vendored source tree.
- `striqt.analysis`: `evaluate_spectrogram(iq, capture, spec, dB=True)` → `(spg, attrs)` where `spg` is `(channels, rows, nfft)` float32. Note: not re-exported at `striqt.analysis` top level — import via `from striqt.analysis.measurements import shared as striqt_shared`.

**Default radio config:** center 1955 MHz (Band 41), 15.36 MS/s (LTE/5G-NR standard rate), gain 0 dB, NFFT 1024, master clock 125 MHz (AIR8201B).

## Web viewer architecture (`live/striqt_web_server.py` + `live/web/`)

```
Acquirer thread → publish(header, blocks) ──────┐
                                                 ▼
FastAPI app (uvicorn)          _broadcaster() asyncio task
├─ GET /          → live/web/ (StaticFiles)   polls latest() @ BROADCAST_FPS
└─ WS  /ws        ←→ browsers                serializes once, fans out
        ↑ text JSON (control)
        ↓ binary  (frames)
```

- `DemoAcquirer` generates synthetic IQ (noise + CW tones) for development without hardware; `--demo` flag enables it.
- `serialize_frame(header, blocks, quantize)` packs the frame: `[4-byte LE len][JSON][float32 or uint8 blocks]`. With `--quantize`, waterfall payload is ~4× smaller.
- `live/web/app.js` parses binary frames, renders waterfalls via `ImageData` + viridis LUT (`colormap.js`), and renders PSD via uPlot.
- Band monitor computes power in the **linear** domain (correct; avoids the dB-averaging error in older scripts).

### Auth, sign-in, and Reset Radio

- Three roles (`admin` / `viewer` / `interns`); only `admin` may mutate config.
  Credentials + `RADIO_SESSION_SECRET` are documented in `run_web.sh`'s header.
- Browser auth is **cookie-only**: unauthenticated page loads are redirected to a
  `GET /login` form (`POST /login` sets the signed `radio_auth` cookie), and
  `GET /logout` clears it. This is why the header "Sign out" button reliably
  switches users — the old Basic-Auth 401 challenge is no longer sent to browsers
  (a Basic header is still *accepted* for `curl -u`/API). The WS role message
  includes `auth_enabled` so the UI hides sign-out in `--demo`/`RADIO_AUTH_DISABLE=1`.
- Read-only roles may use a whitelist of harmless, client-only controls (DAN/ARIC
  switch, Controls collapse, Pause, Max fps, Auto color, Absolute RF, CSV/PNG export,
  and the local PSD display toggles: peak marker/hold, RX1−RX2 diff, min trace, clear
  hold, crosshair, Y span — see `SAFE_SELECTOR` in `app.js`); everything that calls
  `sendControl` stays blocked.
- **Reset Radio** (admin-only `POST /admin/reset-radio`) runs
  `sudo -n systemctl restart $RADIO_SERVICE_NAME` (default `radio-web`) detached.
  It needs the passwordless sudoers rule installed once via
  `live/install_radio_web_sudoers.sh` (no password is stored anywhere).

## Known bugs

Every issue in `bug_report.md` (A-1/P-3, F-3/A-3, T-1, S-1/S-2, F-2, P-5) was already
fixed in the current tree — verified 2026-07-06 (see `AUDIT_REPORT.md` §5.C). The old
"Known bugs" list here described the pre-fix state and was actively misleading (it told
future sessions to re-add guards that already exist), so it has been removed. For the
**current** backlog of live-viewer issues and their fixes, see `AUDIT_REPORT.md` §2/§5 and
`FIXLOG.md` at the repo root. Note `bug_report.md` S-5 was erroneous: `np.hanning` is not
deprecated and the suggested `np.hann` does not exist.

## Key constants

| Constant | Value | Meaning |
|---|---|---|
| `CHANNELS` | `(0, 1)` | Both RX ports on the AIR8201B |
| `MAX_TAIL` | `1 << 22` | Ring buffer capacity (4M samples) in standalone scripts |
| `READ_SIZE` | `1 << 18` | Chunk size per `_read_stream` call |
| `MASTER_CLOCK_RATE` | `125e6` | AIR8201B reference clock |
| `RATES_MHZ` | `[3.84, 7.68, 15.36, 30.72]` | Allowed sample rates (LTE/5G-NR multiples of 1.92 MHz) |
| `NFFTS` | `[256, 512, 1024, 2048, 4096]` | Valid FFT sizes; always snap `--nfft` to this list |

## striqt.analysis spectrogram contract

`evaluate_spectrogram` sets `nfft = round(sample_rate / frequency_resolution)` internally. To guarantee `spg.shape == (channels, rows, nfft)`, pass `frequency_resolution = sample_rate / nfft` and `duration = rows * nfft / sample_rate`.

The calibrated web backend snaps the requested FFT size to a multiple of 28 (see `aligned_nfft` / `ALIGNED_NFFTS` in `striqt_web_server.py`) so `window_fill = 15/28` yields an integer zero-fill; the value is also a multiple of 12 for consistent bin-averaging. (Note: the old advice to clear `striqt_shared.spectrogram_cache` "to prevent a frozen display" was a myth — the cache is disabled by default and never enabled by the live path, so the `.clear()` calls are no-ops. See `AUDIT_REPORT.md` §4-Q1.)

## Web viewer setup (Cloudflare Tunnel for internet access)

1. Install: `pip install fastapi 'uvicorn[standard]'`
2. Install `cloudflared` binary (ARM64 for Deepwave):
   ```sh
   wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
        -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
   ```
3. Run: `bash live/run_web.sh` — starts the server and tunnel; cloudflared prints the public URL.
