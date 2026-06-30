# REPO_OVERVIEW.md

> Generated documentation for **NIST-Omran**. This is a read-only description of
> the codebase as it exists on the `main` branch. Every claim below is grounded
> in the actual files; where something is unknown, absent, or contradictory it
> is called out explicitly rather than guessed.

The repository has two distinct parts:

1. **`live/`** — the project-specific code (Mustafa Omran, NIST SURF project): a
   family of live two-channel RF spectrogram + PSD viewers driven by a Deepwave
   AIR-T / AIR8201B SDR (and one PlutoSDR variant). This is the code the project
   owns and maintains.
2. **`striqt/`** — a **vendored upstream NIST library** (authored by
   Dr. Dan Kuester & Aric Sanders, `usnistgov/striqt`, version `0.8.0`) included
   as a dependency. Per `CLAUDE.md` it is to be treated as read-only. It is
   described here at module granularity, not line-by-line.

---

## 1. File tree

Excluded from the tree below (present in the repo but omitted as noise):
`.git/`, all `__pycache__/` directories, and the compiled `*.pyc` files under
them (notably `live/__pycache__/striqt_web_server.cpython-314.pyc` and many
under `striqt/src/.../__pycache__/`). There is **no** `node_modules/`, `.pixi/`,
or large data/capture artifact directory in the repo — no `.zarr`, `.nc`,
`.tdms`, or IQ capture files are committed. `striqt/tests/sweeps/outputs/` exists
but contains only `plot.yaml` (no captured datasets).

```
NIST-Omran/
├── CLAUDE.md                      # Claude Code project instructions (architecture, known bugs, constants)
├── README.md                      # Human overview of the live viewers + quick usage
├── REPO_OVERVIEW.md               # (this file)
├── bug_report.md                  # Read-only bug inspection of live/ (dated 2026-06-24, 498 lines)
├── INSTALLED_STRIQT_API.txt       # Dump of the installed striqt.analysis API + evaluate_spectrogram signature
├── setup.sh                       # One-time Raspberry Pi 5 setup for pluto_standalone.py (apt + pip + import checks)
├── .gitignore                     # Ignores CLAUDE.md (listed twice)
├── .claude/
│   └── settings.local.json        # Local Claude Code permission allowlist (python3, web_sim open, RADIO_USER/PASS run)
│
├── live/                          # ── PROJECT CODE ──
│   ├── striqt_web_server.py       # FastAPI/uvicorn WebSocket live viewer server (main web entry point)
│   ├── striqt_standalone.py       # Full standalone PyQt5 + pyqtgraph GUI (radio + display in one process)
│   ├── striqt_standalone_terminal.py  # curses terminal-only live monitor (SSH-friendly, no Qt)
│   ├── striqt_server_TCP.py       # TCP server: acquires on AIR-T, streams spectrogram frames over a socket
│   ├── striqt_frontend_TCP.py     # TCP client: PyQt6 + pyqtgraph viewer that connects to the TCP server
│   ├── pluto_standalone.py        # PlutoSDR single-channel variant of the standalone GUI (PyQt5)
│   ├── run_web.sh                 # Launcher: starts the web server + a Cloudflare quick tunnel
│   └── web/                       # Static frontend served by striqt_web_server.py
│       ├── index.html             # Page markup, controls, canvases; pulls uPlot from CDN
│       ├── app.js                 # WebSocket client, waterfall + PSD rendering, controls, exports
│       ├── colormap.js            # Viridis 256-entry RGBA LUT (polynomial approx), window.VIRIDIS_LUT
│       └── style.css              # Dark theme for the web viewer
│   └── web_sim/
│       └── index.html             # Self-contained browser SIMULATION of the standalone viewer (synthetic IQ, no server, no WS)
│
└── striqt/                        # ── VENDORED UPSTREAM LIBRARY (read-only) ──
    ├── LICENSE.md                 # NIST public-domain-style notice (US gov work, no domestic copyright)
    ├── README.md                  # striqt upstream README (CPU/GPU batched real-time signal analysis)
    ├── pyproject.toml             # Package metadata, deps, optional extras, console-script entry points
    ├── pixi.toml                  # pixi environment/task definitions
    ├── chores/push-docs.sh        # Doc publishing helper script
    ├── environments/
    │   ├── cpu.yml                # conda env spec (CPU only)
    │   └── gpu-cpu.yml            # conda env spec (GPU + CPU)
    ├── doc/                       # Sphinx documentation source
    │   ├── conf.py, index.rst, reference/*.rst, guide/01_introduction.md
    │   ├── calibration-sweep.yaml, reference-sweep.yaml   # example sweep specs
    │   └── _static/ (NIST css + logos), _templates/layout.html
    ├── notebooks/                 # Jupyter demos + background notebooks + run.yaml/summary.ipynb
    ├── tests/                     # pytest suite (see §6) + sweeps/*.yaml fixtures + conftest.py
    └── src/striqt/               # The library itself
        ├── analysis/             # Signal-analysis measurements (spectrogram, PSD, cellular 5G, histograms)
        │   ├── lib/              # source, io, dataarrays, filters, register, typing, util, cuda_kernels
        │   ├── measurements/     # one module per measurement + registry.py + shared.py (evaluate_spectrogram lives here)
        │   └── specs/            # msgspec spec structs/types/helpers/doc
        ├── sensor/               # Radio acquisition: sources, sinks, calibration, peripherals, controller
        │   ├── lib/sources/      # base, soapy, deepwave (AIR8201B), file, function, buffers
        │   ├── lib/compute/      # analyze, corrections, datasets, gpu, logs
        │   ├── specs/            # dataclasses/structs/types/helpers
        │   └── __about__.py      # __version__ = '0.8.0'
        ├── cli/                  # check_sweep, sensor_sweep, plot_capture, rechunk_zarr, convert_spec
        ├── figures/              # matplotlib styling + figure helpers (.mplstyle files, backend, labels, ticker)
        └── waveform/             # OFDM/Fourier/array waveform tooling + JIT (cpu/cuda) kernels
```

The vendored `striqt/src/striqt/**` Python modules are not individually
documented here; see §6 and the upstream docs in `striqt/doc/`. The
project-specific code in `live/` is described fully in §2–§4.

**Key striqt symbols the live scripts depend on** (exact locations):
- `Air8201BSourceSpec` — `striqt/src/striqt/sensor/lib/sources/deepwave.py:36`
  (frozen msgspec spec; 125 MHz master clock, AIR-8201B).
- `Airstack1Source` — `…/sources/deepwave.py:46` (SoapySDR-backed Deepwave AIR-T
  source; `from_spec`/`arm_spec` come from its base classes).
- `arm_spec` → `Controller._arm_spec` at `…/sensor/lib/controller.py:346`.
- `from_spec` → `SpecBase.from_spec` at `…/analysis/specs/structs.py:66`.
- `evaluate_spectrogram` — `…/analysis/measurements/shared.py:122` (not
  re-exported at `striqt.analysis` top level; import via
  `from striqt.analysis.measurements import shared`).

---

## 2. Entry points & how things run

There are **six** runnable Python entry points plus one shell launcher and one
standalone HTML page. All Python commands are run from the repo root. Defaults
shared across the AIR8201B scripts: center 1955 MHz (Band 41), 15.36 MS/s, gain
0 dB, NFFT 1024, master clock 125 MHz, channels `(0, 1)`.

| Entry point | Type | Launch command |
|---|---|---|
| `live/striqt_web_server.py` | FastAPI/uvicorn web server | `python3 live/striqt_web_server.py [--demo] [--quantize] [--fps N] [--backend …] [--host H] [--port P]` |
| `live/striqt_standalone.py` | PyQt5 GUI | `python3 live/striqt_standalone.py` (no argparse) |
| `live/striqt_standalone_terminal.py` | curses TUI | `python3 live/striqt_standalone_terminal.py [flags]` |
| `live/striqt_server_TCP.py` | TCP server | `python3 live/striqt_server_TCP.py` (no argparse) |
| `live/striqt_frontend_TCP.py` | PyQt6 GUI client | `python3 live/striqt_frontend_TCP.py <host> [--port N]` |
| `live/pluto_standalone.py` | PyQt5 GUI (Pluto) | `python3 live/pluto_standalone.py` (no argparse) |
| `live/run_web.sh` | bash launcher | `bash live/run_web.sh [args passed through to web server]` |
| `live/web_sim/index.html` | static HTML | open in a browser (no server) |

### `striqt_web_server.py`
- **Interpreter:** any Python 3 with `numpy`, `fastapi`, `uvicorn[standard]`
  installed; `striqt` only required when **not** running `--demo`.
- **Flags** (`striqt_web_server.py:1044-1064`): `--demo` (synthetic IQ, no
  hardware), `--quantize` (uint8 waterfall, ~4× smaller frames), `--fps`
  (default from `BROADCAST_FPS=15`, floored to 0.5), `--backend
  {calibrated,quicklook}` (default from `SPEC_BACKEND` env, see §7),
  `--host` (default `0.0.0.0`), `--port` (default `8000`).
- **Env vars:** `RADIO_USER` / `RADIO_PASS` (Basic Auth, §3), `SPEC_BACKEND`
  (default `calibrated`). See §7.
- In `--demo` mode with `striqt.analysis` unavailable and a calibrated backend,
  it auto-falls back to `quicklook` (`:1066-1068`). Without `--demo`, if
  `striqt.sensor` failed to import it exits with an error (`:1072-1078`).

### `striqt_standalone.py` / `pluto_standalone.py`
- **No argparse.** `main()` builds `SharedConfig`, starts the `Acquirer` thread,
  sleeps 1 s, then launches a maximized `LiveViewer` Qt window
  (`striqt_standalone.py:1680-1701`).
- Both force `os.environ["PYQTGRAPH_QT_LIB"] = "PyQt5"` **before** importing
  pyqtgraph (`:46`) so pyqtgraph binds to PyQt5.
- `pluto_standalone.py` defines `CHANNELS = (0,)` (single channel) and a
  `PlutoSource(Airstack1Source)` subclass that opens SoapySDR with
  `driver='plutosdr'` (`pluto_standalone.py:62-77`).
- Env var `AIR_LIVE_ALLOW_UNSAFE_61_44=1` adds the 61.44 MS/s rate to the
  allowed list in both GUI scripts (`striqt_standalone.py:73,81-84`).

### `striqt_standalone_terminal.py`
- **Flags** (`:1024-1046`): `--center-mhz` (1955), `--rate-msps` (15.36),
  `--gain` (0), `--nfft` (1024), `--rows` (40), `--fps` (3.0),
  `--backend {calibrated,quick}` (default `calibrated`), `--ascii-width`,
  `--no-autoscale`. Note the terminal backend choices are `calibrated`/`quick`
  (not `quicklook`).

### `striqt_server_TCP.py` + `striqt_frontend_TCP.py`
- Server: no argparse; binds `HOST="0.0.0.0"`, `PORT=5005`
  (`striqt_server_TCP.py:50-51`), `SPEC_BACKEND` defaults to `quicklook` here
  (`:48`, different from the web server's `calibrated`).
- Frontend: `main()` takes positional `host` (default `192.168.50.1`) and
  `--port` (default `5005`) (`striqt_frontend_TCP.py:940-950`). Uses **PyQt6**
  (the standalone GUIs use PyQt5).

### `run_web.sh`
- Checks `cloudflared` is on `PATH` and that `fastapi`/`uvicorn` import; starts
  `striqt_web_server.py --port $PORT "$@"` in the background, sleeps 1.5 s, then
  runs `cloudflared tunnel --url http://localhost:$PORT`. `PORT` env overrides
  the default 8000. Cleans up both processes on exit (`run_web.sh:62-69`).

### SDR "single-holder" constraint in code
There is **no explicit conflict-detection code** (no "device busy" / `EBUSY` /
"already in use" handling anywhere in `live/` — confirmed by grep). The
single-holder behavior is structural:

- Each acquiring process opens the device exactly once via `make_source()` →
  `Airstack1Source.from_spec(...)` then `open_stream(source)` (e.g.
  `striqt_web_server.py:373-383, 614-625`; `striqt_standalone.py:319-330, 564`).
  The `source` object is held for the lifetime of the `Acquirer` thread and only
  released by `close_source()` in the thread's `finally` block
  (`striqt_web_server.py:724-726`; `striqt_standalone.py:732-734`).
- Because the AIR8201B can only be held by one process, a second acquirer (e.g.
  starting the web server while the standalone GUI is running) will fail when it
  tries to open/arm/read the stream. That failure surfaces as an exception from
  `from_spec` / `open_stream` / `arm_spec` / `_read_stream`, which the acquirer
  loop routes into `recover_radio()` / `_recover()` — i.e. it is treated as a
  generic stream error and retried, **not** recognized as a "another process
  holds the radio" condition. The web server's loop additionally guards
  `if self.source is None: … continue` (`striqt_web_server.py:686-688`).
- The web server does enforce single-*viewer-set* exclusivity only at the radio
  layer; multiple **browsers** can connect simultaneously (all share the one
  radio via the broadcaster — see §3).

---

## 3. Web server architecture (`live/striqt_web_server.py`)

A single FastAPI app served by uvicorn. One radio is drained by a background
thread; computed frames are fanned out to all WebSocket clients by an asyncio
broadcaster. Threads: **Acquirer** (DMA drain) + **Computer** (spectrogram) for
real hardware, or a single **DemoAcquirer** in `--demo` mode; plus the asyncio
**`_broadcaster()`** task.

### Threading / data-flow model
```
[real radio]  Acquirer thread ──ring buffer──> Computer thread ──publish()──┐
                (drains IQ)        (FFT/dB)                                  ▼
[--demo]      DemoAcquirer thread ───────────────self-publishes────────> latest() slot
                                                                            │
                       asyncio _broadcaster() polls latest() @ BROADCAST_FPS │
                       serialize_frame() once ──> ws.send_bytes() to all ────┘
```
- `Acquirer` (`:492-726`): tight `_read_stream` loop into a per-channel
  complex64 ring buffer (`MAX_TAIL = 4M` samples); no FFT on this loop (prevents
  DMA overflow). `get_latest(n)` returns the newest `n` samples or `None` if the
  ring is empty/stale (`DATA_STALE_SEC = 1.0`). Handles retune via
  `take_dirty()` → `rearm()` and stream errors via `_recover()`.
- `Computer` (`:733-770`): pulls `get_latest(nfft*rows)`, calls
  `compute_blocks`, and `publish()`es; paced to `BROADCAST_FPS`.
- `DemoAcquirer` (`:777-846`): synthetic noise + CW tones, computes inline and
  self-publishes (no Computer thread needed); header carries `"demo": True`.
- `_broadcaster()` (`:936-996`): every `1/BROADCAST_FPS` s, if there are
  connections, reads `latest()`, skips if the frame timestamp is unchanged,
  serializes once, sends to every socket in `_connections`, prunes dead sockets
  with `_connections.difference_update(dead)` (a deliberate in-place mutation —
  there is a long code comment at `:990-994` explaining that
  `_connections -= dead` would rebind the name and crash the task).

### Routes / endpoints
- **`WS /ws`** (`@app.websocket("/ws")`, `:999-1025`): accepts the socket, adds
  it to `_connections`, then loops `receive_text()` with a 15 s timeout
  (timeouts are treated as keepalive). Each text message is a JSON control dict
  (`center`, `sample_rate`, `gain`, `nfft`, `rows` — any subset) applied via
  `_shared.update(ctrl)`. Binary spectrogram frames flow the other way via the
  broadcaster. Removes the socket from `_connections` on disconnect.
- **`GET /` and all other paths** — served by a `StaticFiles` mount (see below).
  If `live/web/` does not exist, a fallback `@app.get("/")` returns a JSON error
  (`:1029-1037`).

### Lifespan / startup / shutdown
- `lifespan` async context manager (`:905-927`), passed to
  `FastAPI(..., lifespan=lifespan)`: on startup starts `_acquirer` (and
  `_computer` if present), sleeps 1.2 s to let the first frame arrive, then
  creates the `_broadcaster()` asyncio task. On shutdown: `_shared.stop()`,
  cancels the broadcaster task, joins the computer and acquirer threads
  (timeouts 3 s).
- `uvicorn.run(app, host, port, log_level="warning")` (`:1128`).

### `BasicAuthMiddleware` (auth)
- Defined at `:158-201`, wired via `app.add_middleware(BasicAuthMiddleware)`
  (`:933`). Pure-ASGI middleware wrapping the **entire** app, so it gates the
  static page, every asset, **and** the `/ws` upgrade.
- Reads env vars `RADIO_USER` and `RADIO_PASS` (`:123-125`).
  `AUTH_ENABLED = bool(user and pass)` — auth is **off** unless *both* are set
  (so `--demo`/local dev is unaffected). `AUTH_REALM = "striqt live viewer"`.
- `check_basic_auth()` (`:129-155`) parses the `Authorization: Basic` header and
  compares both username and password with `secrets.compare_digest`
  (constant-time, no short-circuit).
- On failure: HTTP → `401` with `WWW-Authenticate: Basic realm=…`; WebSocket →
  the upgrade is closed with code `1008` before `accept()`.
- Startup prints a **loud warning** when auth is disabled or only one of
  the two vars is set (`:1106-1118`).

### Static file serving — cache headers
- The frontend is served by **`StaticFiles`**:
  `app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")`
  (`:1029-1030`), mounted **after** the `/ws` route so the WebSocket route takes
  priority. `WEB_DIR = Path(__file__).parent / "web"` (`:108`).
- **No custom HTTP response headers are set anywhere in this file.** There is
  **no** `Cache-Control`, `ETag`, `Expires`, or `Pragma` header configured by
  the application code. The only explicit headers in the file are on the `401`
  path (`WWW-Authenticate`, `Content-Type`, `Content-Length`) in
  `BasicAuthMiddleware`. Any caching behavior for `index.html` / `app.js` /
  `colormap.js` / `style.css` is therefore whatever Starlette's `StaticFiles`
  emits by default (it sends a `Last-Modified` and `ETag` and honors
  conditional requests, but the app sets no `Cache-Control`/`Expires`/`Pragma`
  and does no cache-busting). **State: the application sets no cache-control
  headers.**

### Frame wire format (`serialize_frame`, `:853-890`)
`[4-byte LE uint32 header length][UTF-8 JSON header][block-0 bytes][block-1 …]`.
Header always includes `{center, fs, gain, nfft, rows, shape, channels, time}`
(+ `demo` in demo mode). Blocks are float32 LE `(rows, nfft)` per channel,
oldest-row-first; with `--quantize` the header gains `dtype:"uint8"` and
`scale:[vmin,vmax]` (1st/99th percentile) and blocks become uint8.

---

## 4. Frontend (`live/web/`)

### `index.html`
Page markup: control groups (Radio: center / span / gain / FFT / "Tune to
band"; Display: pause / Boring⟷Cool mode / window-ms / auto-color / absolute-RF /
reset / CSV / PNG; PSD tools: diff / peak marker / peak hold / min trace /
crosshair / Y-span), a status bar, band monitor, two waterfall `<canvas>`
elements (`wf0`, `wf1`), the PSD container (`psd-plot` + a `band-canvas`), and a
log `<pre>`. Loads `colormap.js`, then **uPlot from CDN**, then `app.js`
(`index.html:146-148`).

### `app.js`
The whole client: WebSocket connection, binary frame parsing (`onFrame`,
`:168-231`), waterfall rendering into `ImageData` via the viridis LUT
(`updateWaterfall`, `:252-305`), PSD via uPlot (`initUplot`/`updatePSD`),
draggable band selection + linear-domain band-power monitor
(`updateBandMonitor`, `:575-624` — explicitly averages in **linear** domain),
peak marker / peak hold / min trace, CSV + PNG export, and all the control
event handlers. Several long comments document uPlot footguns worked around
(`:370-373` empty series must be null-arrays; `:543-551` `scale.auto` must stay a
function).

**WebSocket logic (`connect`, `:133-156`):**
- URL is built from the page: `proto = location.protocol === "https:" ? "wss:" :
  "ws:"`, then `new WebSocket(`${proto}//${location.host}/ws`)`. So it always
  connects to the same host/port that served the page, upgrading to `wss` under
  HTTPS (works through the Cloudflare tunnel automatically).
- **Auth:** `app.js` sends **no** credentials itself. It relies on the browser
  replaying the page's cached Basic-Auth credentials on the WS upgrade request —
  exactly what `BasicAuthMiddleware`'s docstring assumes (`:158-171`).
- **Reconnect:** `onclose` schedules `setTimeout(connect, 1200)` (fixed 1.2 s
  retry, no backoff). `onerror` just calls `ws.close()` (which triggers the
  reconnect). **No WebSocket close-code is inspected** — a `1008` auth rejection
  is handled identically to any other close, so an unauthorized client silently
  retries forever.
- On open it sends one control message with the initial `rows` for the current
  window size.

### `style.css`
Dark theme (CSS variables for bg/border/text, RX1 cyan / RX2 orange accents),
flex layout for controls + waterfall row + PSD, uPlot overrides, custom
scrollbars, and a `@media (max-width:900px)` rule that stacks the waterfalls.

### `colormap.js`
Builds `window.VIRIDIS_LUT`, a `Uint8ClampedArray(256×4)` RGBA table from a
public-domain polynomial approximation of matplotlib viridis (IIFE, no exports
beyond the global).

### Caching-relevant items
- **No `<meta http-equiv>` cache tags** in `index.html` (only `charset` and
  `viewport`).
- **No service worker** anywhere (`navigator.serviceWorker` is never referenced).
- **No cache-busting query strings** on `colormap.js`, `app.js`, or `style.css`
  (`<script src="app.js">`, `<link href="style.css">` — plain paths).
- **CDN includes:** uPlot CSS and JS are pulled from
  `cdn.jsdelivr.net/npm/uplot@1.6.31/...` (`index.html:9,147`) — version-pinned,
  not vendored. The comment says it "can be vendored." This is the only external
  network dependency of the frontend.
- **No `localStorage` / `sessionStorage` / cookie usage** in `app.js`
  (confirmed by grep). Exports use `Blob` + `URL.createObjectURL` /
  `toDataURL` only.

### `live/web_sim/index.html` (separate, not served by the server)
A **single self-contained HTML file** (~790 lines, inline CSS + JS) that
simulates the *standalone* viewer entirely in the browser with a synthetic
cellular-band RF scene — no WebSocket, no server, no radio. It is a
development/demo artifact mirroring `striqt_standalone.py`'s UI; it is **not**
loaded by `striqt_web_server.py` (which serves `live/web/` only). The
`.claude/settings.local.json` allowlist references opening it directly.

---

## 5. Deployment & infra files

Present in the repo:
- **`live/run_web.sh`** — the only deployment/launcher script for the web viewer.
  Starts the server + a Cloudflare *quick tunnel* (`cloudflared tunnel --url`),
  which prints an ephemeral `*.trycloudflare.com` URL. Full contents summarized
  in §2. No named/persistent tunnel config.
- **`setup.sh`** (repo root) — one-time **Raspberry Pi 5** provisioning for
  `pluto_standalone.py`: `apt-get install python3-soapysdr
  soapysdr-module-plutosdr python3-pyqt5 libgl1`; `pip install` of
  `striqt @ git+https://github.com/usnistgov/striqt`, `pyqtgraph`, `numpy`,
  `psutil`; then import/enumeration sanity checks. CPU-only (no `[gpu]` extra).
- **`striqt/chores/push-docs.sh`** — upstream library's doc-publishing helper
  (part of the vendored lib, not project deployment).

**Documented but NOT present in the repo:**
- **No systemd unit files** anywhere (no `*.service`). `CLAUDE.md` / `README.md`
  describe running via `bash live/run_web.sh`; any service definition would live
  only on the host, not in this repo.
- **No Dockerfile / docker-compose** of any kind.
- **No `cloudflared` config file** (`config.yml`/`cert.pem`/credentials). The
  tunnel is created ad-hoc by `run_web.sh`; the `cloudflared` binary is expected
  to be installed manually (install commands are documented in `CLAUDE.md` and
  echoed by `run_web.sh` when missing).
- **No CI config** (`.github/`, etc.) at the repo root.

---

## 6. Dependencies & environment

### Project (`live/`) Python dependencies (from imports)
- **Web server:** `numpy`, `fastapi`, `uvicorn[standard]`; `striqt` only for real
  hardware (imports are wrapped in `try/except` so `--demo` runs without it).
- **Standalone GUIs (`striqt_standalone.py`, `pluto_standalone.py`):** `numpy`,
  `PyQt5`, `pyqtgraph` (+ `pyqtgraph.exporters`), `striqt`. Pluto additionally
  needs `SoapySDR` with the plutosdr module.
- **Terminal monitor:** `numpy`, `curses` (stdlib), `striqt`. No Qt.
- **TCP server:** `numpy`, `striqt`, stdlib `socket`/`select`/`struct`/`json`.
- **TCP frontend:** `numpy`, **`PyQt6`**, `pyqtgraph`. (Note the version skew:
  standalone GUIs use PyQt5, the TCP frontend uses PyQt6.)
- The calibrated spectrogram path uses
  `striqt.analysis.measurements.shared.evaluate_spectrogram` and
  `striqt.analysis.specs` (imported defensively; not re-exported at the
  `striqt.analysis` top level — see `CLAUDE.md` and `INSTALLED_STRIQT_API.txt`).
- There is **no `requirements.txt`, `pyproject.toml`, or pixi file for the
  project root** — project deps are implied by imports + `setup.sh` + `CLAUDE.md`.

### Frontend dependencies
- **uPlot 1.6.31** via jsDelivr CDN (CSS + IIFE JS). Not vendored.
- Viridis colormap is hand-vendored in `colormap.js` (no dependency).
- `web_sim/index.html` has zero external dependencies (fully inline).

### striqt library (vendored, `striqt/pyproject.toml`)
- **Name/version:** `striqt` `0.8.0` (`striqt/src/striqt/sensor/__about__.py`),
  author Dan Kuester (NIST). **`requires-python = ">=3.9"`.**
- **Core deps:** `array-api-compat>=1.6`, `dask`, `netCDF4`, `numba`,
  `numexpr~=2.8`, `numpy>=1.21,<3`, `pandas>=1.5`, `scipy>=1.13`,
  `xarray>=2024.3`, `zarr>=2.18,<4`, `matplotlib~=3.7`, `kitcat>=1.3.0`,
  `matplotlib-backend-sixel`, `msgspec>=0.19.0`, `ipython>=8.18.1`,
  `platformdirs>=4.2.0`, `psutil>=7.0.0`, `eval_type_backport`,
  `exceptiongroup>=1.2`, `typing-extensions`.
- **Optional extras:** `dev` (jupyter, ruff, ty), `doc` (sphinx, sphinx-click,
  toml, myst_nb), `gpu` (`cupy>=12.2`, `cython`), `test` (pytest, nptdms,
  pytest-order).
- **Console scripts** (`[project.scripts]`): `check-sweep`, `sensor-sweep`,
  `plot-capture`, `rechunk-zarr`, `convert-spec` → `striqt.cli.*`.
- Conda env specs in `striqt/environments/cpu.yml` (CPU-only, conda-forge, pip-
  installs striqt from GitHub) and `gpu-cpu.yml` (adds the `nvidia` channel +
  `cupy`); `pixi.toml` defines a pixi workspace (platforms osx-arm64 /
  linux-aarch64 / linux-64 / win-64, adds `soapysdr`, py39/py313 + cupy feature
  envs) and declares workspace version `0.1.0` — **distinct** from the package
  version `0.8.0` in `__about__.py`. Tests in `striqt/tests/` (`test_imports.py`,
  `test_cpu_runs.py`, `test_json_schema.py`, `test_yaml_loads.py`, `conftest.py`,
  plus `sweeps/*.yaml` fixtures and `sweeps/src/extensions.py`).

### Version / environment sensitivity
- **`striqt` requires Python ≥ 3.9**; `numpy<3` and `zarr<4` are upper-pinned.
  The committed `*.pyc` files are `cpython-314` (compiled under CPython 3.14 on
  whatever machine last ran it) — note this is *newer* than what `setup.sh`/the
  Pi would typically use; the `.pyc` cache is environment-specific and not
  authoritative.
- **AIR8201B / Deepwave** is **aarch64 (ARM64)** — `CLAUDE.md` and `run_web.sh`
  instruct downloading the `cloudflared-linux-arm64` binary for it.
- **PlutoSDR path** targets a **Raspberry Pi 5** (Debian/`apt`, `setup.sh`) and
  needs `SoapySDR` + `soapysdr-module-plutosdr` + system PyQt5 + `libgl1`.
- The GPU striqt extra needs CUDA/`cupy`; the Pi setup deliberately installs
  CPU-only striqt.
- **Not stated anywhere in the repo:** specific glibc / Ubuntu 18.04 / exact
  Python micro-version requirements. The README only says "the needed SDR
  hardware, drivers, Python packages, and `striqt` imports" must be present.
  (CLAUDE.md's session context mentioned such constraints generically, but no
  file pins them — stated here as **unknown**.)

---

## 7. Config & secrets surface

Environment variables read by the code:

| Var | Read at | Default / behavior |
|---|---|---|
| `RADIO_USER` | `striqt_web_server.py:123` | empty → with `RADIO_PASS` gates Basic Auth; if either unset, auth **disabled** (loud warning) |
| `RADIO_PASS` | `striqt_web_server.py:124` | empty → see above |
| `SPEC_BACKEND` | `striqt_web_server.py:111`; `striqt_server_TCP.py:48` | web server default `calibrated`; TCP server default `quicklook`; `.strip().lower()` |
| `AIR_LIVE_ALLOW_UNSAFE_61_44` | `striqt_standalone.py:73`; `pluto_standalone.py:98` | `=="1"` adds 61.44 MS/s to the allowed rate list |
| `PYQTGRAPH_QT_LIB` | **set** (not read) in `striqt_standalone.py:46`, `pluto_standalone.py:45` | forced to `"PyQt5"` before importing pyqtgraph |
| `PORT` | `run_web.sh:19` | shell default `8000`; passed as `--port` |

The frontend uses no env/config; CDN URLs and defaults are hard-coded in
`index.html` / `app.js`. The web server's network defaults (`--host 0.0.0.0`,
`--port 8000`) and radio defaults are module constants.

**Secrets:** No secrets are committed. `RADIO_USER`/`RADIO_PASS` are read only
from the environment (never written to disk, never defaulted to a real value).
`.claude/settings.local.json` contains a permission allowlist that includes the
literal example `RADIO_USER=alice RADIO_PASS=s3cret python3 *` — these are
**example/dev credentials in a permission rule, not a deployed secret**. No API
tokens, keys, `.env` files, or `cloudflared` credentials are present.

---

## 8. Open questions / oddities

1. **`bug_report.md` vs. current code (A-1 / P-3 appears already fixed in
   `striqt_standalone.py`).** `CLAUDE.md` and `bug_report.md` (Bug A-1,
   "Critical") say the standalone Acquirer dies because there is *no*
   `if self.source is None:` guard before `_read_stream`. But the current
   `striqt_standalone.py:681-690` **does** contain that guard (it calls
   `recover_radio` when `source is None` and `continue`s). So either the bug was
   fixed after the 2026-06-24 report was written, or the report describes a prior
   state. Worth reconciling. I have not re-verified the Pluto variant line-by-line
   beyond confirming it shares the same loop structure.
2. **`np.hanning` deprecation note is stale.** `bug_report.md` S-5 warns
   `np.hanning` is deprecated and to "use `np.hann`". `np.hann` does **not**
   exist in NumPy (`numpy.hanning` is the real, non-deprecated function used at
   `striqt_web_server.py:416`). The suggested fix in the report is itself wrong;
   the current usage is fine.
3. **PyQt version skew.** Standalone + Pluto GUIs import **PyQt5** (and force
   `PYQTGRAPH_QT_LIB=PyQt5`); the TCP frontend imports **PyQt6**. A machine
   running both viewers needs both Qt bindings installed.
4. **`SPEC_BACKEND` default differs by script** — `calibrated` (web) vs
   `quicklook` (TCP server). Intentional per `CLAUDE.md`, but easy to trip over.
5. **Auth + WebSocket close code not handled client-side.** A `1008` auth
   rejection on `/ws` is indistinguishable from a normal disconnect in
   `app.js`; the client retries every 1.2 s forever with no user-visible "auth
   failed" state. Also, the auth model assumes the browser replays cached Basic
   credentials on the WS upgrade — true for same-origin browser WebSockets but
   fragile for non-browser clients.
6. **No cache headers on static assets** (see §3). Because `app.js`/`style.css`
   have no version query strings and the server sets no `Cache-Control`, a
   browser may serve a stale `app.js` after a deploy until its heuristic/ETag
   revalidation kicks in. (`StaticFiles` does send `ETag`/`Last-Modified`, so
   it's not unbounded, but there is no explicit cache policy.)
7. **`PlutoSource` reuses `Air8201BSourceSpec` with `master_clock_rate=125e6`.**
   `bug_report.md` P-1 (High) flags that the AIR8201B spec/clock is used for the
   Pluto; the Pluto subclass overrides the driver but the spec object still
   carries AIR-specific fields. Not verified end-to-end here.
8. **`web_sim/index.html` is parallel, partly-divergent code.** It re-implements
   the viewer UI and constants in plain JS (e.g. `MAX_ROWS=20000`,
   `PSD_YRANGE=[-80,20]`, its own viridis anchors) and can drift from
   `striqt_standalone.py` / `live/web/app.js`. It is a demo, not a tested path.
9. **`.gitignore` lists `CLAUDE.md` twice** and ignores it — yet `CLAUDE.md` is
   committed and tracked. Minor inconsistency.
10. **Committed `*.pyc` (cpython-314) under `live/__pycache__/` and the striqt
    tree.** Build artifacts checked into git; harmless but noise, and they pin an
    unexpectedly new interpreter (3.14) that doesn't match the documented Pi/AIR
    environments.
11. **No automated tests for `live/`.** The only test suite is the vendored
    `striqt/tests/`; the project code has no tests, and `bug_report.md` is the
    only QA artifact.

---

*Generated by reading the repository on the `main` branch. The vendored
`striqt/` library is summarized at module level only; for its internals see
`striqt/doc/` and the upstream project `usnistgov/striqt`.*
