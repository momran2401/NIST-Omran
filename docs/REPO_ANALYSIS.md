# REPO_ANALYSIS.md — NIST-Omran deep technical analysis

**Produced:** 2026-07-09 · read-only pass · **Ground truth = the files on disk at `main` (`be3e4e0`).**
**Scope:** all non-vendored code (`live/`, `live/web/`, `live/web_sim/`, root scripts/docs). `striqt/` is a vendored, read-only NIST library — read for its API surface only.

> **Read-this-first meta-finding.** The two in-repo "authoritative" docs describe an **older, smaller** codebase than what is on disk:
> - `docs/REPO_OVERVIEW.md` cites `striqt_web_server.py` lines that top out ≈ `:1128`.
> - `docs/AUDIT_REPORT.md` §0 states `striqt_web_server.py` is **1486 lines** and `app.js` **1114 lines**.
> - **On disk the server is 3,360 lines and `app.js` is 1,787 lines.**
>
> The gap is not duplication (a duplicate-definition scan found only legitimately-shared method names across the three acquirer classes). It is **applied work**: `docs/FIXLOG.md` records that the entire AUDIT_REPORT backlog (clusters LV-W/F/R, then Phase 1, Phase 2a, Phase 2b) was implemented. Every guard the audit *recommended* now exists in the source. **Therefore all line citations in this document are against the current files and were spot-verified; where a claim rests on an older doc it is marked as such.** Treat `REPO_OVERVIEW.md`/`AUDIT_REPORT.md` line numbers as historical.

---

## 1. Executive summary

NIST-Omran is a family of **live two-channel RF spectrogram + PSD viewers** for the Deepwave AIR-T / AIR8201B software-defined radio, built for the NIST SURF project *"visualization frontends for cellular 5G-NR measurements."* Raw complex IQ is pulled from both RX ports, turned into a rolling waterfall + power-spectral-density plot, and shown to an operator. The repo ships **six interchangeable front-ends** to the same idea (`README.md:15-51`): a browser viewer (`striqt_web_server.py` + `live/web/`, the actively-deployed one), a PyQt5 standalone GUI, a curses terminal monitor, a TCP server/client pair for split radio-host/display machines, and a PlutoSDR single-channel variant. All acquisition and calibrated DSP is delegated to **`striqt`** (`striqt/`), a vendored NIST library by Dr. Dan Kuester & Aric Sanders. The project code owns the threading, ring-buffering, wire protocol, web UI, and the "freedom-model" analysis-parameter validation layer; striqt owns the SDR driver plumbing and the calibrated spectrogram/PSD math. The web server has grown well past its documentation into a schema-driven measurement console (settings editor, `/config` + `/schema` endpoints, Basic-Auth + signed session cookie, single-viewer arbitration, three-tier analysis validation). Deployment on the radio is a systemd unit + Cloudflare tunnel (host-side, **not** in the repo).

---

## 2. Repository map

Every tracked non-vendored path (from `git ls-files`, 25 entries). Vendored `striqt/**` is deliberately excluded.

| Path | Purpose (one line) | Status |
|---|---|---|
| `CLAUDE.md` | Claude Code project instructions (run commands, architecture, constants). | Live — **but self-ignored** (see below) |
| `README.md` | Human overview of the six viewers + quick usage. | Live |
| `INSTALLED_STRIQT_API.txt` | Dump of the *installed* `striqt.analysis` API + `evaluate_spectrogram` signature. | Live reference |
| `setup.sh` | One-time Raspberry Pi 5 provisioning for `pluto_standalone.py` (apt + pip + import checks). | Live |
| `.gitignore` | Two lines, **both `CLAUDE.md`**. Ignores a file that is nonetheless committed. | Live (contradictory) |
| `.DS_Store` | macOS Finder cruft, **tracked in git**. | **Junk — should not be committed** |
| `context/AUDIT_CONTEXT.md` | Intent yardstick for the audit (Dan's emails, sample sweep JSON). Two sections still hold `(paste … here)` placeholders (`AUDIT_REPORT.md:16`). | Live doc |
| `docs/AUDIT_REPORT.md` (105 KB) | Fable-5 fidelity audit of `live/` (2026-07-06). Describes the **pre-fix 1486-line** server. | **Historical** |
| `docs/FIXLOG.md` (63 KB) | One-entry-per-fix log of the AUDIT_REPORT backlog + Phase 1/2a/2b features **as applied**. | Live — the bridge doc |
| `docs/bug_report.md` (25 KB) | 2026-06-24 read-only bug inspection. All items fixed per `CLAUDE.md:88`. | **Superseded/historical** |
| `docs/REPO_OVERVIEW.md` (32 KB) | Generated repo description. Cites root-level doc paths and `.claude/settings.local.json` that **no longer match the tree**. | **Partially stale** |
| `docs/SANDBOX_REPORT.md` (28 KB) | Log of remote NIST-Omran-Sandbox host work. **Sole source** of the systemd/pixi/8001/cloudflared facts (host-side, not in repo). | Live doc (describes host) |
| `live/striqt_web_server.py` (3360 ln) | FastAPI/uvicorn WebSocket viewer — **main web entry point**, started by `run_web.sh:47`. | Live |
| `live/run_web.sh` (70 ln) | Launcher: web server + Cloudflare **quick** tunnel. | Live |
| `live/striqt_standalone.py` (1701 ln) | Full PyQt5 + pyqtgraph GUI (radio + display, one process). | Live (manual) |
| `live/striqt_standalone_terminal.py` (1091 ln) | curses TUI monitor (SSH-friendly, no Qt). | Live (manual) |
| `live/striqt_server_TCP.py` (677 ln) | TCP server: acquires on AIR-T, streams frames over socket `:5005`. | Live (manual) |
| `live/striqt_frontend_TCP.py` (955 ln) | PyQt6 TCP client viewer (default `192.168.50.1:5005`). | Live (manual); **PyQt6 vs PyQt5 skew** |
| `live/pluto_standalone.py` (1747 ln) | PlutoSDR single-channel PyQt5 variant; target of `setup.sh`. | Live (manual) |
| `live/web/index.html` (419 ln) | Page markup, controls, canvases; pulls uPlot from CDN. | Live |
| `live/web/app.js` (1787 ln) | WS client, waterfall + PSD render, controls, settings/analysis editors, CSV/PNG export. | Live |
| `live/web/colormap.js` (43 ln) | Viridis 256×RGBA LUT (`window.VIRIDIS_LUT`, polynomial approx). | Live |
| `live/web/style.css` (659 ln) | Dark theme; DAN/ARIC mode-visibility rules. | Live |
| `live/web_sim/index.html` (789 ln) | Self-contained browser **simulation** of the standalone viewer (synthetic IQ, inline CSS/JS). **Not served by the server; grep finds it only in docs.** | **Orphaned demo-ware** |
| `live/__pycache__/striqt_web_server.cpython-314.pyc` | Committed bytecode; pins CPython **3.14** (newer than documented Pi/AIR). | **Junk — should be gitignored** |

**Dead / orphaned / duplicated flags:** `.DS_Store` and the committed `.pyc` (junk); `live/web_sim/index.html` (parallel UI that can drift — `AUDIT_REPORT.md:871`, `REPO_OVERVIEW.md:518-521`); `docs/bug_report.md` (superseded); `docs/REPO_OVERVIEW.md` stale paths. **`.claude/settings.local.json` referenced by `REPO_OVERVIEW.md:40-41` does not exist on disk** (`ls .claude` → absent; not in `git ls-files`; `git log --all -- .claude/` empty).

Stale in-code comments (not dead code): `striqt_frontend_TCP.py:6` and `striqt_server_TCP.py:627` still name old filenames (`live_viewer_full.py`/`live_viewer_mac.py`).

---

## 3. Runtime architecture (web path, antenna → browser)

The web server runs **three concurrent execution contexts** plus the uvicorn event loop. Boundary legend: 🟩 = project code (`live/`), 🟦 = vendored `striqt` call.

| Context | Class / task | Lines | Role |
|---|---|---|---|
| Thread 1 (real HW) | `Acquirer(threading.Thread)` | `striqt_web_server.py:2504-2737` | Drain-only DMA reader → raw IQ ring buffer. **No FFT here.** |
| Thread 2 (real HW) | `Computer(threading.Thread)` | `:2744-2809` | Pulls newest IQ, computes spectrogram, `publish()`es. |
| Thread 1 (demo) | `DemoAcquirer(threading.Thread)` | `:2816-2895` | Synthetic IQ + inline compute; `_computer is None`. |
| Event loop task | `_broadcaster()` | `:3098-3169` | Polls `latest()` @ `BROADCAST_FPS`, serializes once, fans out to all sockets. |
| Event loop route | `ws_endpoint()` (`@app.websocket("/ws")`) | control loop `:3172-3247` | Inbound control JSON, single-viewer arbitration, liveness ping. |

Mode is chosen in `main()` (`:3311-3318`). Startup wiring is the `lifespan` async context manager (`:2959-2981`): start acquirer (`:2962`) [+ computer if present `:2963-2964`], `sleep(1.2)` for the first frame (`:2966`), then `asyncio.create_task(_broadcaster())` (`:2967`). Shutdown: `_shared.stop()`, cancel the task, join threads with 3 s timeouts (`:2972-2980`).

### End-to-end frame path (ASCII)

```
 RF @ 2 antenna ports
   │  SoapyAIRT driver / JESD204B DMA  (AIR8201B, master clock 125 MHz)
   ▼
🟦 make_source()            :1802-1812   Air8201BSourceSpec(numpy, host time, gapless, retries=0)
   │                                     → Airstack1Source.from_spec()   (striqt boundary)
🟦 Acquirer.open_radio()    :2625-2636   open_stream → source.arm_spec(make_capture(cfg))
   │                                     → enable_stream(True)  (getattr shim :1751-1774)
   ▼
🟩 Acquirer.run() loop      :2674-2737   take_dirty()→rearm/recover; then repeatedly:
   │   🟦 source._read_stream(buffers, count=read_size, timeout=read_size/fs+0.1,
   │        on_overflow="log")                                :2702-2708
   │   🟩 iq = tmp[:, :got].copy(); _ring_write(iq)  ── DRAIN ONLY ── :2721-2724
   ▼
🟩 raw IQ ring buffer       :2530-2536   complex64 (len(CHANNELS), MAX_TAIL=4Mi); _gen counter
   ▼
🟩 Computer.run() @≤FPS      :2758-2809   need=samples_needed(cfg); (out,gen,avail)=get_latest(need)
   │   guard: skip if None, or gen≠g0, or avail<need         :2769-2781  (retune generation guard)
   │   🟩 blocks,meta = compute_blocks(samples, cfg)          :2404-2434
   │        └─🟦 evaluate_spectrogram / power_spectral_density / ssb  (calibrated path)
   │   🟩 acquirer.publish(cfg, blocks, meta) → build_header  :2547-2551 / :2437-2497
   ▼
🟩 newest-wins published slot  (_latest_header, _latest_blocks)   :2524-2526
   ▼
🟩 _broadcaster()           :3098-3169   sleep(1/FPS); skip if header["time"]==last_t (:3137-3140);
   │   🟩 serialize_frame(hdr, blocks, quantize)  :2902-2943  [4B LE len][JSON][blocks]
   │   ws.send_bytes(msg) to every _connections; prune dead via difference_update  :3150-3169
   ▼
   /ws  (binary WebSocket)
   ▼
🟩 app.js onFrame(data)     web/app.js:300-406   parse header + blocks (dequantize if uint8)
       updateWaterfall :427-485 (ImageData + VIRIDIS_LUT) · updatePSD (uPlot) · updateBandMonitor
   ▲
🟩 UI events → sendControl(json)  web/app.js:263-267  → /ws text → _shared.update() → take_dirty()
```

**Key boundary facts.** The only striqt calls on the hot path are `Airstack1Source.from_spec`/`arm_spec`/`_read_stream` (`:1812`, `:2628`/`:2643`, `:2702`), `Air8201BSourceSpec`/`SoapyCapture` (`:1803`, `:1823`), and the DSP entry points in §4. Everything else — ring buffer, threads, generation guard, serialization, auth, middleware, validation — is project code. Because the vendored `striqt/` tree exposes *different* method names than the installed build (vendored: `__init__`+`setup`/`arm`/`read`; installed: `from_spec`/`arm_spec`/`_read_stream`), the live scripts carry **getattr shims** (`:1711-1795`) for attribute divergence (`_device`↔`device`, `_rx_stream`↔`rx_stream`), but the four *action verbs* are installed-build-only (`AUDIT_REPORT.md:73` — the checked-out tree is **not** the radio's build).

**Why two threads (real HW).** The Acquirer loop is deliberately drain-only (`:2504-2513`, `:2721-2724`); moving FFT work to the separate Computer thread is the explicit DMA-overflow mitigation (see §9). Demo has no DMA, so `DemoAcquirer` computes inline (`:2842-2895`).

---

## 4. The DSP path

`compute_blocks(samples, cfg)` (`:2404-2434`) dispatches on `cfg.backend`:

| Backend | Function | Lines | striqt? | FFT / window / overlap | Output |
|---|---|---|---|---|---|
| `calibrated` (default) | `calibrated_spectrogram` | `:1916-1984` | 🟦 `evaluate_spectrogram` | aligned nfft; kaiser β=11.88; frac-overlap 13/28; window_fill 15/28 | float32 dB, averaged bins |
| `quicklook` | `db_spectrogram` | `:1840-1861` | 🟩 local | `np.hanning(nfft)`, **no overlap** (hop=nfft), power-of-2 | float32 dB, `bin_avg=1` |
| `psd` | `psd_traces` → `power_spectral_density` | `:2007-2066` | 🟦 | `time_statistic` traces | `(ch, n_stats, bins)` float32 dB |
| `ssb` | `ssb_spectrogram` → `cellular_5g_ssb_spectrogram` | `:2069-2144` | 🟦 | blackman-harris; nfft=2·fs/scs; one row/OFDM symbol | float32 dB |

**FFT-size snapping.** Requested `cfg.nfft` is first snapped to `NFFT_CHOICES = (256,512,1024,2048,4096)` (`:154`). For the grid backends (`CALIBRATED_GRID_BACKENDS = {calibrated, ssb, psd}`, `:172`) it is then snapped by `aligned_nfft()` (`:2155-2156`, verified) to `ALIGNED_NFFTS = (252,504,1008,2016,4032)` = 28·{9,18,36,72,144} (`:2152`). These are chosen (comment `:2147-2151`) so that: ÷28 makes `window_fill=15/28` yield integer zero-fill; ÷12 keeps `averaging_factor` = 12 at every setting (`AVG_BIN_GROUPS=12`, `:174`); and 7-smooth (2ᵃ·3ᵇ·7) keeps pocketfft fast. **This is the LV-W3 fix** — it replaced the old `round(n/28)·28` that produced slow sizes like 1036 = 2²·7·37 (`FIXLOG.md:29-44`).

**Row/sample sizing.** `analysis_hop(nfft, frac)` = `nfft − round(frac·nfft)` = `nfft·15/28` (`:1864-1872`). `calibrated_sample_count(nfft, rows, hop)` = `rows·hop + (nfft−hop)` (`:2166-2179`) right-sizes to exactly `rows` STFT rows (**LV-W2** — the old path computed ≈1.87× the displayed rows, `FIXLOG.md:46`).

**dB / dtype / calibration.** Every backend emits **float32** and dB is already applied — inside striqt for the calibrated/PSD/SSB paths (`evaluate_spectrogram(..., dtype="float32", dB=True)`, `:1956-1958`), or via `10·log10(power+1e-20)` in the local quicklook path (`:1857-1858`). **Calibration (PSD/ENBW dB scaling) is applied *only* inside the striqt calls — NOT in `db_spectrogram` (quicklook), and NOT on the synthetic demo IQ.** So quicklook and demo are uncalibrated relative magnitudes; only `calibrated`/`psd`/`ssb` carry striqt's calibration.

**Post-processing.** `fit_display_rows` (`:2231-2275`) crops/pads to the row contract, applies an optional **display-side LO null** sized by `lo_bandstop` (`:2261-2267`, the `lo_null` toggle), and **scrubs NaNs to the per-row min** (`:2269-2274`) — the unconditional NaN cleanup from LV-F8/R4.

**Frequency axis (LV-F1).** The calibrated path derives exact bin-group centers from striqt (`spectrogram_freqs`, `:1976-1983`) and ships `freqs_hz_f0` + `freqs_hz_step` in the header (`:2466-2484`); the client uses these instead of re-deriving the axis, closing the ≈52–103 kHz error the audit found (`AUDIT_REPORT.md:696`, `FIXLOG.md:70`).

**Cadence and what governs it.** `BROADCAST_FPS` (`:140`, default 15, overridable by `--fps` → `max(args.fps, 0.5)` at `:3308`) paces the Computer loop (`interval = 1/max(BROADCAST_FPS,1)`, `:2759`), the demo loop (`:2848`), and the broadcaster (`:3104`). On real hardware the *effective* rate is min(FPS, 1/compute-time): the Computer sleeps only after compute, so when calibrated FFT work exceeds the interval it publishes back-to-back and cadence = per-frame compute time (root cause of the historical "~1 frame/5 s", `AUDIT_REPORT.md:647-659`; mitigated by LV-W1/W2/W3). `max_live_rows` (`:2205-2228`) bounds how many rows the compute loop will attempt.

---

## 5. Wire protocol (`/ws`)

### Server → client — binary frame (`serialize_frame`, `:2902-2943`)

```
[ 4 bytes  LE uint32  header-JSON length ]   struct.pack("<I", len)   :2932/:2940
[ N bytes  UTF-8 JSON header             ]
[ block-0 bytes ][ block-1 bytes ] ...       one per channel, oldest-row-first
```

Default block encoding = **float32 LE** (`:2942`). With `--quantize` (`:2919-2937`): per-frame global range from `np.nanpercentile(all_vals, [1,99])` (`:2923-2925`, NaN/degenerate fallbacks `:2926-2929`), header gains `dtype:"uint8"` + `scale:[vmin,vmax]` (`:2930`), blocks become uint8 0–255 (`:2935-2936`). Client dequantizes `f = vmin + u8/255·(vmax−vmin)` (`web/app.js:322-330`).

**Header fields** (`build_header`, `:2437-2497`) → consumed by `onFrame` (`web/app.js:314-316`):

| Field | Meaning | Client use |
|---|---|---|
| `center`, `fs`, `gain` | Radio tuning | axis + meta |
| `nfft` | **averaged bin count** (not radio FFT size) | `curBins`, waterfall width |
| `rows`, `shape [rows,bins]`, `channels` | Frame geometry | buffer sizing |
| `backend`, `backend_requested` | Executed vs requested backend (differ on SSB fallback) | honesty labels (LV-F2) |
| `fft_nfft`, `bin_avg` | Radio FFT size + averaging factor | FFT label |
| `freqs_hz_f0`, `freqs_hz_step` | Exact bin-group axis | `buildFreqsMHz` (LV-F1) |
| `hop_size` | STFT hop × time bins | window-ms label |
| `time` | Frame timestamp | broadcaster new-frame gate + client dedup |
| `psd_stats`, `time_span_ms` | PSD-backend only | server-statistic traces |
| `dtype`, `scale` | quantize only | dequantize |
| `demo:true` | demo only | (ignored) |

**Server → client text messages:** `{"message":"ping"}` liveness (`:3202`); `{"message":"[server] …"}` notices (`:3118`); settings ack `{"message":…, "ack":{applied,ignored,reconnect,rounded,rejected}}` (`:3240-3242`, ack shape `:1682-1688`); `{"message":"bad control ignored: …"}` (`:3246`). Consumed by `ws.onmessage` / `handleAck` (`web/app.js:226-239`, `:280-294`).

### Client → server — control JSON (text)

Parsed in `ws_endpoint` via `json.loads(text)` then `_shared.update(ctrl)` off-thread (`:3210-3217`); validated in `SharedConfig.update` (`:1475-1688`). The complete set of outbound messages `app.js` can send (all via `sendControl`, `web/app.js:263-267`):

| Message | Source control | Server handling |
|---|---|---|
| `{center: Hz}` | NOOB station chip (`index.html:399`) | clamp 300 MHz–6 GHz (`:1607`) |
| `{sample_rate: Hz}` | (settings) | snap to `RATES_HZ`, then 1–125 MHz (`:1608-1612`) |
| `{gain: dB}` | (settings) | clamp −60…10 (`:1613-1614`) |
| `{nfft: int}` | `#nfft-sel` | snap to `NFFT_CHOICES`, then 128–8192 (`:1615-1617`) |
| `{rows: 12}` | Cool-mode select | clamp to `max_live_rows` (`:1604-1605`) |
| `{backend: str}` | `#analysis-sel` | one of `BACKENDS` |
| `{lo_null: bool}` | `#lo-null` | display LO-null toggle |
| `{capture:{duration}}` | Boring-mode / duration | hop-aware rows |
| `{capture:{…}, source:{…}}` | Settings **Apply** | `capture.*` mapped (`:1505-1523`); unmapped → `ignored`; `source.*` mostly → `reconnect` (`:1535-1540`) |
| `{analysis:{target, …}}` | Analysis **Apply** | `_validate_analysis` three-tier (`:1161-1227`, targets `:453-512`) |

Top-level analysis keys are stripped so clients can't bypass validation (`:1483`, `ANALYSIS_CFG_KEYS` `:517-521`).

---

## 6. Frontend — control wiring table

Every interactive control in `index.html`, its handler, and whether it reaches the server. The **only** server transport is `sendControl()` (`web/app.js:263-267`). "Server effect: —" ⇒ **client-only (dead to the server)**.

| element id | control | wired? | server effect |
|---|---|---|---|
| `ctrl-toggle` | button (collapse) | inline `<script>` `index.html:262-264` | — (layout) |
| `.mode-opt` ×2 | DAN/ARIC mode buttons | inline `index.html:291-293` | — (body class + localStorage) |
| `pause-btn` | Pause/Resume | `app.js:1257-1261` | — (client `paused`) |
| `mode-sel` | Boring/Cool display | `app.js:1263-1268` | **YES** — `{capture:{duration}}` (replace) or `{rows:12}` (scroll) |
| `analysis-sel` | Analysis backend | `app.js:1270-1273` | **YES** — `{backend}` |
| `dur-sel` | Duration preset | `app.js:1304` | **YES, replace-mode only** — `{capture:{duration}}` |
| `dur-custom` | Custom duration ms | `app.js:1305-1306` | **YES, replace-mode only** |
| `fps-sel` | Max render fps | `app.js:1308-1310` | — (client `maxFps`) |
| `auto-color` | Auto color | `app.js:1312-1314` | — (client) |
| `lo-null` | LO null | `app.js:1316-1318` | **YES** — `{lo_null}` |
| `abs-rf` | Absolute RF vs baseband | `app.js:1320-1326` | — (rebuilds axis client-side) |
| `reset-btn` | Reset view | `app.js:1328-1333` | — (uPlot autoscale) |
| `csv-btn` | Save PSD CSV | `app.js:1335` | — (blob download) |
| `png-btn` | Export PNG | `app.js:1336` | — (canvas download) |
| `diff-chk` | RX1−RX2 diff | `app.js:1338-1340` | — |
| `peak-chk` | Peak marker | `app.js:1342-1345` | — |
| `hold-chk` | Peak hold | `app.js:1347-1350` | — |
| `clear-hold-btn` | Clear hold | `app.js:1352-1355` | — |
| `min-chk` | Min trace | `app.js:1357-1360` | — |
| `cross-chk` | Crosshair | `app.js:1362-1364` | — |
| `yspan-sel` | Y span dB | `app.js:1366-1371` | — |
| `settings-upload` | Load sweep JSON | `app.js:1610-1620` | — (re-renders form only; server reached later via Apply) |
| `settings-apply` | Apply capture/source | `app.js:1595-1608` | **YES** — `{capture:{…}, source:{…}}` |
| `nfft-sel` | FFT size | `app.js:1244-1250` | **YES** — `{nfft}` |
| `analysis-apply` | Apply analysis | `app.js:1755-1768` | **YES** — `{analysis:{…}}` (no-op if target null) |
| `.freq-chip` (dynamic) | NOOB station tuner | inline `index.html:395-406` | **YES** — `{center}` |
| `#capture-settings-form` fields (dynamic) | schema editor | via Apply | **YES** — `capture.*` |
| `#source-settings-form` fields (dynamic) | schema editor | via Apply | **YES** — `source.*` |
| `#analysis-form` fields (dynamic) | analysis panel | via Apply | **YES** — `analysis.*` |

**Explicitly dead / no-op paths:**
- **Purely client-side (14 controls):** `pause-btn`, `fps-sel`, `auto-color`, `abs-rf`, `reset-btn`, `csv-btn`, `png-btn`, `diff-chk`, `peak-chk`, `hold-chk`, `clear-hold-btn`, `min-chk`, `cross-chk`, `yspan-sel`. These change only local display state — by design, not bugs.
- **Truly dead reference:** the NOOB chip handler sets `#center-mhz`.value (`index.html:396-397`) but **no element `#center-mhz` exists** — that assignment is inert; only `sendControl({center})` works.
- **Conditional:** `dur-sel`/`dur-custom` send nothing in Cool (scroll) mode (`app.js:1300` gate); `analysis-sel="quicklook"` yields `target:null` so `analysis-apply` early-returns and sends nothing (`app.js:1757`).

**Render path (for completeness):** waterfalls via `ImageData` + `window.VIRIDIS_LUT` → `putImageData` (`app.js:427-485`, LUT at `colormap.js`); PSD via **uPlot** (pinned `uplot@1.6.31` from jsDelivr, `index.html:10,243` — the **only** external network dependency); band monitor averages in the **linear** power domain (`app.js:955-1017`, correct), using a precomputed index range rather than `mask.includes` (LV-R6 fix).

---

## 7. Configuration and state

**Environment variables:**

| Var | Read at | Absent behavior |
|---|---|---|
| `RADIO_USER` / `RADIO_PASS` | `striqt_web_server.py:211-212` | Auth **disabled** unless *both* set; loud warning printed (`:3336-3346`) |
| `SPEC_BACKEND` | web `:166` (dflt `calibrated`); TCP `striqt_server_TCP.py:48` (dflt `quicklook`) | Falls to per-script default |
| `LD_LIBRARY_PATH` / `RADIO_WEB_LD_REEXEC` | `:63-71` | Triggers/guards the libstdc++ re-exec (§9) |
| `PORT` | `run_web.sh:19` | shell default `8000` |
| `AIR_LIVE_ALLOW_UNSAFE_61_44` | `striqt_standalone.py`, `pluto_standalone.py` | `=="1"` adds 61.44 MS/s to the rate list |
| `PYQTGRAPH_QT_LIB` | **set** (not read) → `PyQt5` in the GUI scripts | forces pyqtgraph binding |

**CLI (argparse `:3275-3292`):** `--demo` (forces `quicklook`, `:3296`), `--quantize`, `--fps` (dflt `BROADCAST_FPS`), `--backend` (choices=`BACKENDS`, dflt `SPEC_BACKEND`), `--host` (dflt `0.0.0.0`), `--port` (dflt `8000`).

**Behavior-changing constants** (`:127-199`, verified): `CHANNELS=(0,1)`; `DEFAULT_CENTER=1955e6`, `DEFAULT_SAMPLE_RATE=15.36e6`, `DEFAULT_GAIN=0.0`, `DEFAULT_NFFT=1024`, `DEFAULT_ROWS=12`; `MASTER_CLOCK_RATE=125e6`; `READ_SIZE=1<<18`; `MAX_TAIL=1<<22`; `DATA_STALE_SEC=1.0`; `BROADCAST_FPS=15`; `SCROLL_ROWS=12`; `MAX_ROWS_ABS=4096`; `RING_ROW_FILL=0.9`; `RATES_HZ`, `NFFT_CHOICES`, `ALIGNED_NFFTS`; `AVG_BIN_GROUPS=12`; SSB block (`:176-183`); `SESSION_TTL=86400` (`:262`); `AUTH_REALM="striqt live viewer"` (`:214`); `WEB_DIR = __file__.parent/"web"` (`:161`).

**Config files:** none in-repo beyond the two shell scripts. **No** `requirements.txt`, project `pyproject.toml`, project `pixi.toml`, `.env`, Dockerfile, or CI config. The only `pixi.toml`/`pyproject.toml` are vendored (`striqt/`). `.gitignore` ignores `CLAUDE.md` (twice) yet it is tracked. The frontend has zero env/config surface (defaults hard-coded in `index.html`/`app.js`).

---

## 8. Auth and deployment

### Middleware chain (registration `:2987-2988`, verified)

```
app.add_middleware(BasicAuthMiddleware)   # 2987
app.add_middleware(NoCacheMiddleware)     # 2988
```

Starlette applies middleware **last-added-outermost**, so per request the order is:

**`NoCacheMiddleware` (outer) → `BasicAuthMiddleware` → router (`/ws`, `/config`, `/schema`, StaticFiles mount `:3257-3258`).**

- **`NoCacheMiddleware`** (`:404-432`): HTTP only (`:415-417`, WS passthrough). Strips `cache-control/expires/pragma` and forces `no-store, max-age=0` + `pragma:no-cache` + `expires:0` (`:419-430`). *(This is new since the audit — it resolves `AUDIT_REPORT.md §3`'s "no cache headers" finding.)*
- **`BasicAuthMiddleware`** (`:320-401`): pure-ASGI, gates **both** http and websocket. Passthrough if `not AUTH_ENABLED` or scope not http/websocket (`:369-371`). Auth = `check_basic_auth(header) or _session_cookie_from_scope(scope)` (`:377`). On success: http gets a refreshed `Set-Cookie` (`:378-381`); ws passes through (`:382-383`).

**Exact rejection points (verified):**
- **WebSocket** unauthenticated → `send({"type":"websocket.close","code":1008})` **before `accept()`** at **`:386-389`**.
- **HTTP** unauthenticated → **401** with `WWW-Authenticate: Basic realm=…` at **`:391-401`**.

**Auth primitives.** `check_basic_auth` (`:217-243`) base64-decodes Basic, constant-time `secrets.compare_digest` on user+pass (`:241-243`), **returns True when auth disabled** (`:225-226`). Signed session cookie `radio_auth` (`:259-317`): secret = `sha256(user:pass)`, TTL 86400 s, token = `exp.hmac_sha256`, constant-time verify + expiry; cookie is `HttpOnly; SameSite=Lax`, and **`Secure` is omitted over plain HTTP** so Safari/iOS on LAN can reach `/ws` (`:348-363`, the LV-R8 fix). **Single-viewer:** `ws_endpoint` guards `_connections` with `_slot_lock`; a second client is accepted then closed with **code 4001** (`:3182-3187`) — distinct from the 1008 auth code (LV-R3). Liveness ping frees a dead slot after 2 misses (`:3196-3208`).

### Deployment topology

**In-repo (all that ships here):**
- `live/run_web.sh` — starts `striqt_web_server.py --port $PORT "$@"` in the background (`:47`) then a **Cloudflare quick tunnel** `cloudflared tunnel --url http://localhost:$PORT` (`:58`, ephemeral `*.trycloudflare.com`). Hard-fails if `cloudflared`/`fastapi`/`uvicorn` missing (`:22-42`); `cleanup()` trap kills both PIDs (`:62-69`). `PORT` default 8000 (`:19`).
- `setup.sh` — Raspberry Pi 5 provisioning for the **Pluto** GUI only (apt SDR/Qt + pip `striqt`/`pyqtgraph`/`numpy`/`psutil` + import checks). Does not touch the web server, systemd, or the tunnel.
- Ports that are real **in code**: **8000** (web, `:3290`, `run_web.sh:19`) and **5005** (TCP pair, `striqt_server_TCP.py:50-51`, `striqt_frontend_TCP.py:23`).

**Host-side only — documented in `docs/SANDBOX_REPORT.md`, NOT files in this repo (verified absent):**

| Claimed in analysis brief | Reality |
|---|---|
| systemd units | **Not in repo.** `radio-web.service` (`WorkingDirectory=/home/sensor/NIST-Omran`, `EnvironmentFile=/etc/radio-web.env`), `cloudflared.service`, `cloudflared-update.service` exist only on the radio — `SANDBOX_REPORT.md:31-33`. `REPO_OVERVIEW.md:377-379` confirms none in repo. |
| "8000 prod / 8001 sandbox" | **8001 appears in no source file** — only in `SANDBOX_REPORT.md` (sandbox run on the host, `--port 8001`, `:39,49,94-113`). Only 8000 is a code default. |
| pixi invocation | **No project `pixi.toml`.** Host ExecStart runs `pixi run --manifest-path /home/sensor/aggregate-directivity-acquisition/pixi.toml python …/live/striqt_web_server.py` (`SANDBOX_REPORT.md:33`) — a dir not in this repo. |
| Cloudflare config | **Not in repo.** Host has `/etc/cloudflared/config.yml` mapping `radio.mustafaomran.com → localhost:8000` (`SANDBOX_REPORT.md:37-38,52`). In-repo tunnel is ad-hoc. |

---

## 9. Known hazards

| Hazard | Symptom | Cause | Mitigation (file:line) | Complete? |
|---|---|---|---|---|
| **Single-holder SDR** | Second acquirer breaks the live viewer; device can't be opened twice | AIR8201B held by one process for the `Acquirer` lifetime; released only in `finally` | Structural only — device opened once in `open_radio` (`:2625-2636`), closed on thread exit. Web layer arbitrates *viewers* (slot 4001) but there is **no** cross-process "device busy" detection; a conflict surfaces as a generic read error → `_recover` retry (`REPO_OVERVIEW.md:171-192`) | **No** — no EBUSY/holder detection; this is the operational rule to respect |
| **DMA starvation** | Overflow / dropped IQ if the reader stalls | FFT on the drain loop would block the DMA reader | Drain-only Acquirer loop; FFT on a separate Computer thread (`:2504-2513`, `:2721-2724`); `on_overflow="log"` (`:2707`) | **Yes** for real HW; demo path never exercises it |
| **GLIBCXX / libstdc++ re-exec** | scipy/striqt import fails: `GLIBCXX_3.4.29 not found` (`SANDBOX_REPORT.md:102,117`) | System `libstdc++` older than the pixi env's | `_ensure_pixi_runtime_libs` at import (`:50-75`): if `<python>/../lib/libstdc++.so.6` exists, prepend to `LD_LIBRARY_PATH` and `os.execv` re-exec once (guarded by `RADIO_WEB_LD_REEXEC`) | **Mostly** — idempotent + loop-proof (`AUDIT_REPORT.md:661-666`), but depends on `sys.executable` living in the pixi env; the cleaner `run_web.sh`-export fix (LV-Q2) is **not** applied |
| **Driver hardcoding** | Only AIR8201B works on the web path | `Air8201BSourceSpec`/`Airstack1Source`/`SoapyCapture` fixed (`:1803`, `:1812`, `:1823`); `bindings.air8201b` for `/schema` (`:3005-3011`) | none (by design) | n/a |
| **Vendored ≠ installed striqt** | Fresh `pip install 'striqt @ git+…'` then run → `AttributeError` on `from_spec`/`arm_spec`/`_read_stream` | Checked-out tree exposes different method names than the radio's build | getattr shims cover *attributes* only (`:1711-1795`); the 4 action verbs are installed-build-only | **No** — running against this tree fails; radio-build-only (`AUDIT_REPORT.md:73,868`) |
| **Silent shim swallowing** | Stream enable/close failures vanish | `enable_stream` (`:1764-1774`) and `close_source` (`:1776-1783`) swallow all exceptions | none | **No** — intentional but opaque |
| **Broadcaster `UnboundLocalError`** | Task silently dies, zero frames, server still up | `_connections -= dead` would rebind the name as local | Load-bearing comment + in-place `difference_update` (`:3163-3169`) | **Yes** |
| **Compute-error backstop loop** | On a *non-analysis* persistent fault, the Computer emits a throttled notice and sleeps 0.1 s repeatedly | tier-3 `revert_analysis` only reverts analysis params (`:930-962`, caught `:2787-2801`) | reverts + keeps streaming for analysis-induced errors | **Partial** — non-analysis faults can loop |

Newer contributor traps: `.DS_Store` + committed `.pyc` are tracked; `CLAUDE.md` is `.gitignore`d yet committed; PyQt5 (standalone) vs PyQt6 (TCP frontend) skew; `SPEC_BACKEND` default differs web vs TCP.

---

## 10. Git state

- **Branch:** `main` @ `be3e4e0 Fable 5 Review. RIP friend`, tracking `origin/main`, reported **up to date**.
- **Working tree:** **clean** (`git status --porcelain` empty; `git ls-files -m` empty). No modified/uncommitted files.
- **Pushed?** Yes — `git log origin/main..HEAD` empty (nothing unpushed) and `git log HEAD..origin/main` empty (nothing behind). **HEAD == origin/main, no divergence.**
- **Remote:** `origin = https://github.com/momran2401/NIST-Omran` (fetch+push).
- **Tags (3), all behind HEAD on linear history:**

| Tag | Anchors | Meaning |
|---|---|---|
| `pre-newviewer` | `19d0635 Canvased it up baby!` | before the current web viewer |
| `pre-audit-fixes` | `da6f15c New striqt web server` | before the AUDIT_REPORT backlog was applied |
| `pre-phase12` | `97d4137 fable 5 review` | before Phase 1/2 features |

Recent history: `be3e4e0 ← ac9c9bb ← 97d4137(pre-phase12) ← da6f15c(pre-audit-fixes) ← 19d0635(pre-newviewer) ← …`. No tag sits at HEAD.

---

## 11. Open questions for the maintainer

1. **Which `striqt` build is authoritative?** The vendored tree cannot run the live code (method-name mismatch, §9). Is `striqt/` here purely for API reference, and the runtime always `pip install 'striqt @ git+…'` at whatever HEAD upstream is? If upstream renames again, the shims won't help. *(Cannot resolve from code — the installed build isn't in the repo.)*
2. **`SCROLL_ROWS` (`:141`) vs `DEFAULT_ROWS` (`:133`)** are both `12` and `SCROLL_ROWS` is not referenced elsewhere in the server (the client hard-codes `{rows:12}`). Is `SCROLL_ROWS` intended to be the single source of truth, or is it vestigial?
3. **`stream_buffers_for` return value** (`:1792-1795`) — `_make_read_buffers` discards the returned ports (`:2657`). Intended, or a dropped use?
4. **`context/AUDIT_CONTEXT.md` still has `(paste transcript here)` / `(paste architecture write-up here)` placeholders** (`AUDIT_REPORT.md:16`). Are Dan's full transcript/architecture notes captured anywhere, or only the HTML-comment summaries?
5. **Is `live/web_sim/index.html` maintained on purpose** (demo hand-out) or should it be retired before it drifts further from `app.js`?
6. **Should `.DS_Store`, `live/__pycache__/*.pyc`, and the self-referential `.gitignore`** be cleaned up, or are they intentional?

---

## 12. Risk register (ranked by likelihood × impact, evidence-cited)

| # | Risk | Likelihood | Impact | Evidence |
|---|---|---|---|---|
| 1 | **Doc rot misleads the next contributor.** `REPO_OVERVIEW.md`/`AUDIT_REPORT.md` describe a 1486-line server; the code is 3360 lines with different line numbers, endpoints, middleware, and cookie logic. A reader trusting them will edit the wrong lines or re-add existing guards. | High (already true) | High | §-meta; `AUDIT_REPORT.md:34` vs `wc -l = 3360` |
| 2 | **Fresh-machine bring-up fails at first radio call.** `pip install 'striqt @ git+…'` per `setup.sh:16` + `CLAUDE.md:30` yields a build whose method names differ from `from_spec`/`arm_spec`/`_read_stream`. | Med–High | High (no data) | `AUDIT_REPORT.md:73,868`; shims `:1711-1795` |
| 3 | **libstdc++ re-exec is the sole guard** and depends on `sys.executable` being inside the pixi env; a differently-activated `python3` skips it and scipy/striqt import dies before any frame. | Med | High (silent no-frames) | `:50-75`; `SANDBOX_REPORT.md:102,117`; LV-Q2 not applied |
| 4 | **Auth silently off.** If only one of `RADIO_USER`/`RADIO_PASS` is set (or neither), the server is fully open — only a console warning flags it, and via the public Cloudflare tunnel that exposes the radio. | Med | High | `:213`, `:225-226`, `:3336-3346` |
| 5 | **Cadence regression on large `rows`/FFT.** Effective fps = min(FPS, 1/compute); the client can still request heavy windows, and calibrated FFT on the Jetson CPU can exceed the interval. Mitigated (LV-W1/2/3) but not bounded by an adaptive guard. | Med | Med (slideshow) | §4; `AUDIT_REPORT.md:647-659`; `max_live_rows :2205-2228` |
| 6 | **Non-analysis fault loops in the Computer backstop** — throttled-notice + 0.1 s sleep with no escalation. | Low–Med | Med | `:2787-2801`, `revert_analysis :930-962` |
| 7 | **Quicklook/demo readings are uncalibrated** but visually identical to calibrated; an operator could misread absolute levels. | Low | Med | `db_spectrogram :1840-1861` (no calibration) |
| 8 | **`web_sim` / dead client controls drift** — parallel UI and 14 client-only controls can diverge from server semantics unnoticed. | Low | Low | §6; `AUDIT_REPORT.md:871` |
| 9 | **Committed junk** (`.DS_Store`, cpython-314 `.pyc`) pins/leaks environment assumptions and adds noise. | Low | Low | `git ls-files` |

---

*Method: three read-only sub-agent sweeps (server backend, web frontend, deployment/git) plus direct reads of `REPO_OVERVIEW.md`, `CLAUDE.md`, `INSTALLED_STRIQT_API.txt`, `AUDIT_REPORT.md`, `README.md`, `FIXLOG.md`, `SANDBOX_REPORT.md`. All current-file line citations spot-verified against disk. `striqt/` read for API surface only; no changes proposed to it. This document is the only file created.*
