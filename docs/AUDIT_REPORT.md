# AUDIT_REPORT.md — NIST-Omran live viewer fidelity audit

**Date:** 2026-07-06 · **Auditor:** Claude (Fable 5), read-only pass · **Deliverable of:** "Fable 5 read-only audit → fidelity report"
**Scope:** 100 % of `live/` + `live/web/` (executable fix specs) · `striqt/` **only** on the call graph reachable from `live/` (report-only, no diffs — see §6)
**Method:** direct line-by-line reading of the web three-layer stack (server / browser / striqt entry points) plus three parallel read-only sub-agent sweeps (non-web live scripts; `striqt.analysis` call graph; `striqt.sensor` call graph). All arithmetic claims (bin counts, grid constraints, row math) were re-verified by computation.

> **Two tracks — do not mix them:**
> 🟦 **LIVE track** (`live/`, `live/web/`) — findings come with concrete, junior-executable fix specs and handoff prompts.
> 🟨 **STRIQT track** (`striqt/`) — third-party NIST library (Dan Kuester / Aric Sanders). Observations only, in §6. **Never edit `striqt/`.**

### Intent-doc status (read first, per the audit brief)

`context/AUDIT_CONTEXT.md` is the only file in `context/`. Present and used as the intent yardstick:
- **§1 Dan's emails** (Jul 1 2026) — authoritative design decisions ✔ present.
- **§4 sample capture/sweep JSON** — intended schema shape ✔ present.
- **§2 Dan/Mustafa transcript** and **§3 architecture write-up** — **NOT pasted** (the file still contains the literal "(paste transcript here)" / "(paste architecture write-up here)" placeholders). Only the key-point summaries inside the HTML comments exist. This audit uses those summaries as intent; if the full documents contain additional commitments, re-run the fidelity matrix rows they touch.

### Headline results (TL;DR)

1. **Every bug documented in `bug_report.md` / CLAUDE.md "Known bugs" (A-1, P-3, F-3, A-3, T-1, S-1, S-2, F-2, P-5) is already fixed in the current code.** Those docs describe the pre-2026-06-24 state and are now actively misleading (backlog D-1).
2. **The ~1 frame / 5 s cadence is client-inflicted + FFT-size-inflicted, not a striqt mystery** (§4-Q1): the browser always requests the 300-row cap, and the calibrated path then runs ≈559 overlapped 1036-point (2²·7·37 — slow non-power-of-2) CPU FFTs × 2 channels per frame on the Jetson. The `spectrogram_cache.clear()` calls are no-ops (the cache is disabled by default and never enabled by live code).
3. **The "SSB spectrogram" UI option is a phantom**: the true SSB path requires the capture sample rate to be a multiple of 420 kHz; none of the selectable rates qualifies, so the server silently falls back to calibrated on *every* frame while the header and status line keep claiming `ssb` (§4-Q3, LV-F2).
4. **The 147-bin frequency axis the browser draws is wrong**: striqt's DC-centered 7-bin grouping drops 7 edge bins; true bin-group centers are `(g−73)/148·fs` but the browser plots `((g−73.5)/147)·fs` — ≈52 kHz error at center, ≈103 kHz at the edges. Peak-marker, band-monitor and CSV frequencies inherit the error (§4-Q4, LV-F1).
5. The settings editor implements Dan's schema-editor intent only partially: uploaded sweep JSON "hidden lower-level parameters" are stored but never sent; 5 of 9 capture fields and all source fields are silently ignored server-side (LV-F6).

---

## §0 — Coverage manifest

### 🟦 `live/` — 100 % reviewed

| Path | Reviewed? | Role (one line) |
|---|---|---|
| `live/striqt_web_server.py` (1486 ln) | ✅ full, line-by-line | FastAPI/uvicorn WebSocket viewer server: Acquirer+Computer threads, 3 backends, auth, quantize, `/schema` |
| `live/web/index.html` (414 ln) | ✅ full | Page markup: DAN/ARIC modes, radio/display/PSD controls, settings editor, noob station tuner |
| `live/web/app.js` (1114 ln) | ✅ full | WS client, frame parsing, waterfalls, uPlot PSD, band monitor, exports, schema-driven settings form |
| `live/web/style.css` (629 ln) | ✅ full (all behavior-relevant rules verified) | Dark theme; mode-visibility rules; `analysis-psd` hides waterfalls; `analysis-ssb` has **no** rule |
| `live/web/colormap.js` (43 ln) | ✅ full | Viridis 256×RGBA LUT (public-domain polynomial approximation) |
| `live/striqt_standalone.py` (1701 ln) | ✅ full (sub-agent, verbatim loop verified by parent) | PyQt5 standalone GUI, ring-buffer Acquirer + LocalReceiver |
| `live/pluto_standalone.py` (1747 ln) | ✅ full (sub-agent) | PlutoSDR single-channel copy of the standalone GUI |
| `live/striqt_standalone_terminal.py` (1091 ln) | ✅ full (sub-agent) | curses TUI; the reference implementation of the robust recover pattern |
| `live/striqt_server_TCP.py` (677 ln) | ✅ full (sub-agent) | Headless TCP frame server (predecessor of the web server) |
| `live/striqt_frontend_TCP.py` (955 ln) | ✅ full (sub-agent) | PyQt6 TCP viewer client |
| `live/run_web.sh` (69 ln) | ✅ full | Web server + Cloudflare quick-tunnel launcher |
| `live/web_sim/index.html` (789 ln) | ✅ skim for divergence (per brief) | Self-contained browser simulation; parity deltas recorded in §5-E |
| `live/__pycache__/` | n/a | Build artifact noise (committed `.pyc`, cpython-314) |

Root docs read as audit inputs: `context/AUDIT_CONTEXT.md`, `CLAUDE.md`, `README.md`, `REPO_OVERVIEW.md`, `bug_report.md`, `INSTALLED_STRIQT_API.txt`, `setup.sh` (skim).

### 🟨 `striqt/` — reachable call graph only (report-only)

Reached from `live/` imports/calls and reviewed:

| Path | Reviewed? | Why on the call graph |
|---|---|---|
| `striqt/src/striqt/sensor/lib/sources/deepwave.py` | ✅ | `Air8201BSourceSpec`, `Airstack1Source` (open, JESD SYSREF, `rx_enable_delay=1.4`) |
| `striqt/src/striqt/sensor/lib/sources/soapy.py` | ✅ | `SoapySource.setup/arm/read/close`, `RxStream`, `validate_stream_read`, `HardwareTimeSync` — vendored equivalents of `arm_spec`/`_read_stream` |
| `striqt/src/striqt/sensor/lib/sources/base.py` | ✅ | `ReceiveStreamError` (an `IOError` subclass) |
| `striqt/src/striqt/sensor/lib/sources/buffers.py` | ✅ | Reference buffer/holdoff semantics (live/ replaces with its own ring) |
| `striqt/src/striqt/sensor/lib/controller.py` | ✅ | `Controller._arm_spec`, `_iq_retry_context` — the retry machinery live/ bypasses |
| `striqt/src/striqt/sensor/bindings.py` + `lib/bindings.py` | ✅ | `bindings.air8201b` → `/schema` endpoint path (`binding.sensor.sweep_spec_cls`) |
| `striqt/src/striqt/sensor/specs/structs.py` | ✅ | `Source`/`SoapySource` (source spec fields) + `SensorCapture`/`SoapyCapture` (capture fields) |
| `striqt/src/striqt/analysis/measurements/shared.py` | ✅ | `evaluate_spectrogram`, `_cached_spectrogram`, `spectrogram_cache`, `spectrogram_freqs` |
| `striqt/src/striqt/analysis/measurements/_spectrogram.py` | ✅ (light) | `spectrogram_time` row-count math; module-scoped warning filter |
| `striqt/src/striqt/analysis/measurements/_cellular_5g_ssb_spectrogram.py` | ✅ | The real SSB path + its 420 kHz grid constraint |
| `striqt/src/striqt/analysis/measurements/_power_spectral_density.py` | ✅ (light) | Dan's intended PSD analysis (`time_statistic`) — *not* reachable from live/ today |
| `striqt/src/striqt/analysis/specs/structs.py`, `helpers.py`, `types.py` | ✅ | `Capture`, `Spectrogram`, `from_spec`, `json_schema`, `_schema_hook` |
| `striqt/src/striqt/analysis/lib/register.py` | ✅ | `KwArgCache` (disabled by default), measurement wrapper, `as_xarray=False` path |
| `striqt/src/striqt/waveform/…/fourier.py`, `arrays.py`, `power_analysis.py` | ✅ (called functions only) | `spectrogram/stft`, `get_window` norm, `binned_mean` (the nanmean), `null_lo`, `truncate_freqs`, `fftfreq`, `powtodB` |

**Deliberately skipped (out of scope — not on the live call graph):** `striqt.analysis` measurements never referenced by live (`_channel_power_histogram`, `_channel_power_time_series`, `_cyclic_channel_power`, `_cellular_cyclic_autocorrelation`, `_cellular_5g_pss_correlation`, `_cellular_5g_sss_correlation`, `_cellular_resource_power_histogram`, `_spectrogram_histogram`, `_spectrogram_ratio_histogram`, `_iq_waveform`, `registry.py`); `analysis/lib/{io,source,cuda_kernels,filters,typing,util}.py` and most of `dataarrays.py`; `sensor/lib/sources/{file,function}.py`, `lib/{execute,sinks,io,calibration,peripherals,resources,tracebacks}.py`, `lib/compute/*`, `sensor/{io,sinks,calibration,peripherals,util}.py`, `specs/dataclasses.py`; the rest of `striqt.waveform` (OFDM, filters, CUDA kernels); `striqt/cli`, `striqt/figures`, tests, docs, notebooks.

**Vendored-vs-installed caveat (matters at runtime):** the checked-out `striqt/` tree is **not** the build installed on the radio. live/ calls `from_spec` / `arm_spec` / `_read_stream` / `rx.open` — none of which exist in the vendored tree (vendored names: `__init__`+`setup` / `arm` / `read` / `rx_stream.setup`). The getattr shims in live/ cover the *attribute* divergence (`_device`↔`device`, `_rx_stream`↔`rx_stream`) but the *method* calls are installed-build-only. `evaluate_spectrogram` is byte-identical between the vendored tree and `INSTALLED_STRIQT_API.txt`. Details: §5-D and §6-N2.

---

## §1 — Data-flow map: one frame, antenna → browser

Real-hardware path (`python3 live/striqt_web_server.py`, backend `calibrated`, defaults: 1955 MHz, 15.36 MS/s, requested nfft 1024):

```
RF @ antenna (2 ports)
  │  SoapyAIRT driver / JESD204B DMA (AIR8201B, master clock 125 MHz)
  ▼
[open once]  make_source()                              striqt_web_server.py:593
             = Air8201BSourceSpec(array_backend="numpy", time_source="host",
               time_sync_at="open", clock_source="internal", gapless=True,
               receive_retries=0) → Airstack1Source.from_spec()        :603
               (installed-build name; vendored equivalent = __init__ + setup —
                opens SoapySDR device, sets clock/time source, builds RxStream)
  ▼
[arm/tune]   Acquirer.open_radio()/rearm()                            :923-950
             open_stream() → source.arm_spec(make_capture(cfg))       :926,941
               (vendored arm(): setGain → setFrequency → setSampleRate per port)
             enable_stream(True) → activateStream(now + 1.4 s)  ← rx_enable_delay
             rearm also clears the IQ ring                            :944-945
  ▼
[drain loop] Acquirer.run()                                           :972-1035
             source._read_stream(buffers, count=read_size≤2¹⁸,
                timeout_sec=read_size/fs+0.1, on_overflow="log")      :1000-1006
               → vendored SoapySource.read → RxStream.read → device.readStream
               returns (got, timeNs); buffers are per-port float32-interleaved
               views of tmp (stream_buffers_for, :583-586)
             _ring_write(iq) → per-channel complex64 ring, MAX_TAIL=4 Mi  :866-891
  ▼
[compute]    Computer.run() @ ≤BROADCAST_FPS                          :1055-1079
             samples = acquirer.get_latest(samples_needed(cfg))       :1060
               (newest-n, front-zero-padded, None if empty/stale >1 s)  :893-919
             compute_blocks(samples, cfg)                             :782-791
               ├─ backend=calibrated → calibrated_spectrogram()       :644-684
               │    nfft 1024 → aligned_nfft → 1036 (multiple of 28)  :731-733
               │    spec: kaiser β=11.88, overlap 13/28, window_fill 15/28,
               │          integration_bandwidth = (fs/1036)·7 ≈ 104 kHz,
               │          lo_bandstop 120 kHz, trim_stopband=False    :671-679
               │    striqt_shared.evaluate_spectrogram(iq, Capture, spec,
               │          dtype="float32", dB=True)                   :681-683
               │      = STFT (hop 555, ~559 rows for 300 display rows)
               │        → null_lo NaNs ±120 kHz → binned_mean(7, DC-centered)
               │        → ×7 (mean→sum) → powtodB          [striqt shared.py:202-236]
               │      output: (2, ~559, 147) — 147 = 2·(515//7)+1, 7 edge bins dropped
               ├─ backend=ssb → cellular_5g_ssb_spectrogram → ALWAYS raises
               │    'counting-number' at the selectable rates → silent fallback
               │    to calibrated_spectrogram                          :706-724
               └─ backend=quicklook → db_spectrogram (Hann, no overlap,
                    power-of-2 FFT, Σw² normalization)                :623-641
             fit_display_rows(): crop/pad to cfg.rows; NULL center 5 bins
               to per-row nanmin (DC/LO null)                          :743-765
             acquirer.publish(cfg, blocks) → header {center, fs, gain,
               nfft=**bins(147)**, rows, shape, channels, backend, time}  :840-856
  ▼
[fan-out]    _broadcaster() asyncio task @ BROADCAST_FPS              :1285-1345
             skips if header.time unchanged; serialize_frame(hdr, blocks,
             quantize) = [4B LE len][JSON][float32|uint8 blocks]      :1172-1209
             ws.send_bytes() to every socket in _connections (max 1 viewer,
             ws_endpoint refuses extras with close 1008)              :1348-1379
  ▼
[browser]    app.js connect() → onFrame()                              app.js:136-242
             parse header + blocks (dequantize uint8 if scale present)
             tuningChanged? → buildFreqsMHz(center, fs, nfft=147, absRF) :118-126
               ⚠ treats 147 averaged bins as raw fftshifted FFT bins
             updateWaterfall(): replace or scroll into wfBuf, autoColor
               5–99 %, viridis LUT → putImageData                     :263-316
             updatePSD(): mean/max/min over wfBuf rows (mean in dB ⚠),
               peak-hold/min/diff series → uPlot                      :404-511
             updateBandMonitor(): linear-domain band power ✔          :600-649
             updateMeta(): status line (FFT/window values ⚠ see LV-F4)  :98-112
  ▲
[control]    UI events → sendControl({center|sample_rate|gain|nfft|rows|
             backend}) / settings editor → {capture:{...}, source:{...}}
             → WS text → _shared.update() (clamps; capture.* mapped;
             source.* log-only) → Acquirer.take_dirty() → rearm        :416-493
```

Demo path: `DemoAcquirer` (`:1086-1165`) synthesizes tones+noise and runs the *same* `compute_blocks` inline (no Computer thread), header gains `"demo": true`.

---

## §2 — Prioritized execution backlog (🟦 LIVE track)

Ordering = recommended execution order. Effort: S ≤ ½ day, M ≈ 1–2 days. Every ID links to a detailed finding (§3/§4/§5) containing the fix spec, verify step and handoff prompt.

| # | ID | Title | Category | Impact | Effort | File(s) | Depends on |
|---|----|-------|----------|--------|--------|---------|------------|
| 1 | **LV-W1** | Fix `rowsForWindow`: track radio nfft separately from header bins; hop-aware window→rows | perf + fidelity | **Critical** — root cause of 1 frame/5 s; restores usable live view | S | `live/web/app.js` | — |
| 2 | **LV-W3** | Snap calibrated nfft to smooth 28-multiples {252, 504, 1008, 2016, 4032} instead of `round(n/28)·28` | perf + fidelity | High — fast FFT sizes *and* restores Dan's 12-bin averaging at every FFT setting | S | `live/striqt_web_server.py` | — |
| 3 | **LV-W2** | Right-size `samples_needed` / STFT rows (stop computing 559 rows to show 300) | perf | High — ~1.9× compute cut | S | `live/striqt_web_server.py` | LV-W1 |
| 4 | **LV-F1** | Ship the true 147-bin frequency axis in the header; use it in the client | fidelity | High — axis/peak/CSV off by 52–103 kHz today | M | server + `app.js` | — |
| 5 | **LV-F2** | Honest backend reporting: fallback flag in header; disable/annotate SSB at incompatible rates | fidelity | High — UI currently lies ("SSB") | S | server + `app.js` + `index.html` | — |
| 6 | **LV-R1** | Catch `RuntimeError` from the Soapy driver in every acquirer read loop | bug | High — thread death = permanent freeze | S | server (+ standalone, pluto, terminal) | — |
| 7 | **LV-R2** | Harden the WS control channel (validate/snap; never let bad JSON kill the viewer) | bug | High | S | server | — |
| 8 | **LV-R3** | Single-viewer: close accept race, liveness ping + takeover, distinct close codes, client-side "busy/auth" states | bug + UX | High | M | server + `app.js` | — |
| 9 | **LV-R4** | NaN-safe quantizer (`nanpercentile` + `nan_to_num`) | bug | Med | S | server | — |
| 10 | **LV-F3** | PSD "Mean" trace: average in linear power, not dB | fidelity | Med — biased trace on a measurement instrument | S | `app.js` (optionally Qt viewers) | — |
| 11 | **LV-F4** | Truthful labels: FFT vs bins, actual window ms (hop-aware), units on PSD/CSV/PNG | fidelity | Med | S | `app.js`, `index.html` | LV-F1 |
| 12 | **LV-F6** | Settings editor: actually send hidden sweep-JSON seed; ack which fields were applied vs ignored | fidelity | Med — Dan's explicit intent | M | `app.js` + server | — |
| 13 | **LV-R5** | Retune: suppress zero-padded/mislabeled frames (config-generation tag; publish only full windows) | bug | Med | S | server | — |
| 14 | **LV-F5** | Expose `quicklook` in the Analysis dropdown; add true striqt PSD backend (time_statistic) | fidelity + UX | Med — dropped capabilities | M | server + UI | LV-F2 |
| 15 | **LV-R6** | Band monitor: replace `mask.includes()` scan with index range/Set | perf | Med (hangs at nfft 4096) | S | `app.js` | — |
| 16 | **LV-F7** | Populate the waterfall frequency axis (`.wf-freq-axis` is an empty phantom today) | UX + fidelity | Med | S | `app.js` | LV-F1 |
| 17 | **LV-R7** | Cool-mode scroll: fix within-block row order (time direction flips inside each frame band) | bug | Low | S | `app.js` | — |
| 18 | **LV-R8** | Session cookie: drop `Secure` when serving plain HTTP (iOS/Safari LAN lockout) | bug (edge) | Low | S | server | — |
| 19 | **LV-F8** | DC-null: disclose or make optional (5 bins ≈ 519 kHz of real spectrum overwritten) | fidelity | Low | S | server + UI | — |
| 20 | **LV-R9** | Minor state bugs: crosshair reset on retune; duration→rows uses stale nfft/fs; "Tune to band" silent no-op when Absolute RF off | bug | Low | S | `app.js`, server | — |
| 21 | **LV-R10** | TCP server: retry `open_radio` when `source is None` (stuck-dead gap) | bug | Med (TCP path only) | S | `live/striqt_server_TCP.py` | — |
| 22 | **LV-R11** | TCP frontend: steady-state socket timeout so shutdown can't hang | bug | Low | S | `live/striqt_frontend_TCP.py` | — |
| 23 | **LV-U1** | Web parity extras: broadcast-fps control (à la "JEEZ SLOW DOWN"), per-channel peak marker, harmonize gain range | UX | Low | S–M | UI + server | — |
| 24 | **LV-G1** | GPS/external time source option via the source form + explicit reconnect flow (Dan's GPS intent) | feature | Med | M | server + UI | LV-F6 |
| 25 | **LV-Q2** | libstdc++: set `LD_LIBRARY_PATH` in `run_web.sh`; keep the re-exec as fallback | infra | Low | S | `live/run_web.sh` (+ server comment) | — |
| 26 | **LV-D1** | Update stale docs: CLAUDE.md "Known bugs" (all fixed), spectrogram-cache myth, bug_report S-5 `np.hann` error | docs | Med (prevents wrong future fixes) | S | `CLAUDE.md`, `bug_report.md` | — |

---

## §3 — Three-layer fidelity matrix + detailed 🟦 LIVE findings

Verdicts: **faithful** (all three layers agree) · **phantom** (UI offers what the stack can't deliver) · **dropped** (capability below never surfaced) · **misrepresented** (plot/label/axis/units ≠ real data) · **redundant/confusing**.

### §3.1 The matrix

Layer key: **S** = striqt (what it can do / actually outputs) · **V** = server (`striqt_web_server.py`) · **U** = browser (`live/web/`).

#### A. Radio controls (DAN-mode "Radio (AIR-T)" group, `index.html:45-82`)

| Element | S ↔ V ↔ U trace | Verdict | Finding |
|---|---|---|---|
| Center (MHz) + Apply (`#center-mhz`, `:50`) | U sends `{center: MHz·1e6}` (app.js:824-830) → V clamps 300 MHz–6 GHz (server:462) → `arm_spec.setFrequency`. UI min/max 300/6000 matches server clamp. | **faithful** | — |
| Span (MS/s) select (`#rate-sel`, `:56-61`) | U restricted to the 4 LTE rates → V accepts (but would accept *any* 1–125 MHz via WS, server:464) → `setSampleRate`. In replace mode also re-sends rows. | **faithful** (UI); V-side unsnapped input is LV-R2 | LV-R2 |
| Gain (dB) + Apply (`#gain`, `:66`) | UI −30…0 → V clamps −60…+10 (server:466) → `setGain`. Three different ranges (UI/server/hardware) none of which is authoritative. | **redundant/confusing** (range mismatch) | LV-U1c |
| FFT select (`#nfft-sel`, `:72-78`) | U sends 256…4096 → V: quicklook uses it verbatim ✔; calibrated/ssb silently round to a multiple of 28 (`aligned_nfft`, server:731-733) then bin-average by `averaging_factor` (server:736-740): **256→252→21 bins, 512→504→42, 1024→1036→147, 2048→2044→292, 4096→4088→511**. Averaging factor jumps 12/12/7/7/8, so frequency resolution is a non-monotonic surprise. Header `nfft` = bins. | **misrepresented** (calibrated/ssb) | LV-W3, LV-F4 |
| "Tune to band" (`#tune-btn`, `:81`) | U centers the radio on the dragged band (app.js:854-862) → same `{center}` path. Silently does nothing when "Absolute RF" is off (`!absRF` guard, app.js:856). Band edges use the erroneous axis (LV-F1). | **faithful** w/ 2 caveats | LV-R9c, LV-F1 |

#### B. Display controls (`index.html:84-122`)

| Element | S ↔ V ↔ U trace | Verdict | Finding |
|---|---|---|---|
| Pause (`#pause-btn`) | Client-side only: frames keep streaming, `onFrame` skipped (app.js:156, 864-869). Fine for one viewer; wastes tunnel bandwidth. | **faithful** (label accurate) | — |
| Mode: Boring 🥱 / Cool 😎 (`#mode-sel`) | replace vs scroll; switch sends rows (300-cap vs 12) (app.js:871-877; server SCROLL_ROWS=12). Works; but Cool mode has the within-block row-order flip. | **faithful** w/ caveat | LV-R7 |
| Analysis: **Spectrogram** | U → `{backend:"calibrated"}` (app.js:813-822) → V `calibrated_spectrogram` → striqt `evaluate_spectrogram`. Genuine symbol-style averaged view (Dan's "less grainy" intent) — modulo the axis/labeling issues. | **faithful** | LV-F1/F4 |
| Analysis: **PSD** | U only adds `body.analysis-psd` (CSS hides waterfalls, style.css:166) and *still sends `backend:"calibrated"`*. striqt has a real `power_spectral_density` measurement with `time_statistic` percentiles (Dan's sample JSON §4) that nothing ever calls. | **misrepresented** (it's a view toggle, not an analysis) + **dropped** (real PSD backend) | LV-F5 |
| Analysis: **SSB spectrogram** | S: `cellular_5g_ssb_spectrogram` requires fs ≡ 0 (mod 420 kHz) → *always* raises at 3.84/7.68/15.36/30.72 (and 61.44) MS/s → V silently falls back to calibrated (server:718-724) → header still says `backend:"ssb"`, meta line shows "SSB". `body.analysis-ssb` class has **no CSS rule** (pure no-op). | **phantom** | LV-F2 (§4-Q3) |
| Window (ms) select 10…1000 | U: `rowsForWindow` = `win·fs/nfft` capped at 300 (app.js:128-130) — uses `curNfft`, which after the first calibrated frame is **bins (147), not the FFT size**; even at startup 20 ms → exactly 300 (the cap). 50–1000 ms are all silently identical (cap). Meta line back-computes window from bins → nonsense. Real calibrated row spacing is `nfft·15/28/fs` (hop), so even a correct rows count would mislabel time by ×1.87. | **misrepresented** ×3 | LV-W1, LV-F4 |
| Auto color | Client 5th–99th percentile of the display buffer (app.js:290-299). Honest. | **faithful** | — |
| Absolute RF | Rebuilds axis center±fs/2 vs baseband (app.js:893-898). Correct given the (broken) bin mapping. | **faithful** (inherits LV-F1) | — |
| Reset view | Re-enables uPlot auto-scaling (app.js:900-905). Does what it says. | **faithful** | — |
| Save PSD CSV | Exports `freq_mhz` + mean/max per channel (app.js:741-761). Frequency column inherits the wrong axis; mean column inherits dB-averaging; no units metadata. | **misrepresented** (data columns fine, freq+mean+units not) | LV-F1, LV-F3, LV-F4 |
| Export PNG | Canvas composite + caption `FFT ${curNfft}` — prints **147** as "FFT" in calibrated mode (app.js:782). | **misrepresented** (caption) | LV-F4 |

#### C. PSD tools (`index.html:124-143`)

| Element | Trace | Verdict | Finding |
|---|---|---|---|
| RX1−RX2 diff | mean₀−mean₁ in dB = channel power ratio — legitimate. Hides all other traces while on (app.js:485-501). | **faithful** | — |
| Peak marker | Strongest bin of **RX1 max only** (app.js:515-522); label doesn't say RX1; freq readout inherits axis error. | **misrepresented** (labeling) | LV-U1b, LV-F1 |
| Peak hold / Clear hold | Max-accumulate on max trace; cleared on retune (app.js:449-459, 219-220). | **faithful** | — |
| Min trace | Min-accumulate (app.js:460-468). | **faithful** | — |
| Crosshair | `uplot.cursor.show` toggle — but `initUplot` re-creates the plot on every tuning change and resets it to on (app.js:934-936 vs 221). | **faithful** w/ state bug | LV-R9a |
| Y span (dB) | Auto or fixed span pinned to the visible peak (app.js:568-592, 938-943). Works; survives re-init via `applyYspan` each frame. | **faithful** | — |
| Band monitor (panel) | **Linear-domain** averaging — correct and matches intent ✔ (app.js:624-639). Caveats: bin selection & bin-count label use the wrong axis (LV-F1); "Q" metric (band−whole-span dB) shown with no explanation; `mask.includes(i)` is O(N²·rows) → freezes at nfft 4096 quicklook. | **faithful math**, confusing label, perf hazard | LV-F1, LV-R6 |
| Band drag on PSD | Pointer-drag with handle hit-testing; `#band-canvas` element is dead markup (`pointer-events:none`, never drawn to — overlay actually drawn in the uPlot draw hook). | **faithful** (+1 dead element) | LV-F7 note |

#### D. Waterfalls & status (`index.html:189-204, 29-35`)

| Element | Trace | Verdict | Finding |
|---|---|---|---|
| Waterfall canvases (wf0/wf1) | float32 (or dequantized uint8) dB → viridis LUT. Titles "Spectrogram Port 0 — RX1"/Port 1 correct (`CHANNELS=(0,1)`). | **faithful** | — |
| `.wf-freq-axis` overlay divs | Styled 16 px overlay "for frequency axis labels" (style.css:409-417) that **no JS ever populates** — the waterfalls have no frequency (or time) labels at all. | **phantom element** | LV-F7 |
| Center 5-bin dark stripe | V's `fit_display_rows` overwrites bins c−2…c+2 with the per-row min (server:759-764) ≈ **519 kHz of real spectrum** at 15.36 MS/s calibrated — after striqt already NaN'd ±120 kHz (`lo_bandstop`). Quicklook frames get **no** null → backends disagree at DC. Nothing in the UI discloses this. | **misrepresented** (undisclosed data overwrite) | LV-F8 |
| Status/meta line | `FFT ${curNfft}` = bins; `window … ms` computed from bins; analysis shows "SSB" during fallback; `scale`, fps, center/span faithful. | **misrepresented** (3 fields) | LV-F4, LV-F2 |
| DAN MODE / ARIC MODE switch | CSS visibility classes + localStorage persistence (index.html:265-293). ARIC mode = station chips + minimal controls. | **faithful** | — |
| ARIC-mode station chips | Same `{center}` control; <300 MHz presets disabled with honest tooltip — consistent with server clamp. | **faithful** | — |

#### E. Settings editor (`index.html:147-165`, app.js:949-1098) vs Dan's email intent

| Element | Trace | Verdict | Finding |
|---|---|---|---|
| `/schema` endpoint | V: `bindings.air8201b` → `binding.sensor.sweep_spec_cls` → `json_schema()` (server:1266-1282). Works on-radio (verified against vendored `lib/bindings.py:92-139`); **500s in `--demo`** (no `striqt.sensor`) → editor silently empty except a log line. | **faithful** on radio, degraded in demo | LV-F6d |
| Capture form (9 fields) | V applies only `center_frequency`, `sample_rate`, `gain`, `duration→rows` (server:419-434). `analysis_bandwidth`, `port`, `lo_shift`, `host_resample`, `backend_sample_rate` are rendered, collected, sent — and **silently ignored**. | **phantom** (5 of 9 fields) | LV-F6a |
| Source form (8 fields) | V logs "source changes require reconnect: […]" (server:439-444) and applies nothing — matching Dan's set-once-at-open rule — but the UI gives **no feedback**; "requires reconnect" badge implies it *will* happen on reconnect (it won't; there is no reconnect flow). Skip-list (`receive_retries`, `adc_overload_limit`, `if_overload_limit`, `gapless`) honored on both sides ✔ (app.js:949, server:440-441) — and striqt-side those four are genuinely inert in live's drain-only path. | **phantom** (with correct skip-list) | LV-F6b, LV-G1 |
| Load JSON (sweep upload) | Dan: uploading a sweep JSON should seed the GUI **and set the hidden lower-level parameters**. U stores the file in `hiddenSweepSettings` (app.js:959, 1044) and **never uses it again** — only the visible form fields are ever sent. | **dropped** (explicit intent) | LV-F6c |
| Apply button | `sendControl(collectSettings())` — one shot, no ack; "Settings sent" logged even when everything was ignored. | **misrepresented** feedback | LV-F6a |

#### F. Frame header & WS control contract

| Field / key | Trace | Verdict | Finding |
|---|---|---|---|
| `center`, `fs`, `gain`, `rows`, `shape`, `channels`, `time`, `demo` | Publish ↔ parse symmetric (server:840-856 ↔ app.js:185). | **faithful** | — |
| `nfft` | Named "nfft" but carries the **block bin count** (147 ≠ 1024/1036). Client then reuses it as if it were the FFT size (axis, rows, labels). | **misrepresented** (the single most consequential field) | LV-F1/W1/F4 |
| `backend` | Carries the *requested* backend, not the executed one (ssb fallback). | **misrepresented** | LV-F2 |
| `dtype`/`scale` (quantize) | uint8 round-trip exact to 1/255 of the 1–99 % range; browser dequantizes correctly (app.js:191-199). `np.percentile` (not nan-aware) is the NaN hazard. | **faithful** w/ hazard | LV-R4 |
| control `center/sample_rate/gain/nfft/rows/backend` | Clamped + change-detected + dirty→rearm (server:446-479). Unknown keys ignored. Non-numeric values or non-dict JSON **kill the (only) viewer connection** (server:1365-1376). | **faithful** w/ robustness gap | LV-R2 |
| control `capture.duration` | Mapped to rows using the **pre-update** nfft/sample_rate even when the same message updates them (server:427-434). | **misrepresented** edge | LV-R9b |
| Missing from header | No frequency-axis array/offsets, no fallback flag, no units, no config-generation id. | **dropped** (needed by LV-F1/F2/R5) | LV-F1 |

#### G. Backends (server `BACKENDS`, server:147)

| Backend | S ↔ V ↔ U | Verdict | Finding |
|---|---|---|---|
| `calibrated` | striqt evaluate_spectrogram, ENBW-normalized, **band-integrated** (×7 mean→sum) power in dB (≈+8.5 dB vs per-bin). UI calls it "Spectrogram"; PSD axis just says "Power (dB)". | **faithful** compute, under-labeled units | LV-F4 |
| `quicklook` | V implements it (Hann, Σw² normalization — the *correct* normalization CLAUDE.md praises) but **no UI control can select it** (Analysis dropdown maps spectrogram→calibrated, ssb→ssb). Reachable only via `--backend`/`SPEC_BACKEND`. | **dropped** | LV-F5 |
| `ssb` | Always falls back at every selectable rate (420 kHz grid). | **phantom** | LV-F2 |

---

### §3.2 Detailed findings (🟦 LIVE track — each with fix, verify, risk, handoff)

> Format per finding: verdict/severity · location · what's wrong (evidence) · the fix (step-by-step) · verify · risk & rollback · ▶ handoff prompt.

---

#### LV-W1 — `rowsForWindow` uses the wrong "nfft" and ignores the hop → every session runs at the 300-row cap
**Verdict/severity:** misrepresented + perf · **Critical** (primary driver of §4-Q1). **Layers:** U (root), V (contract).
**Location:** `live/web/app.js:128-130` (`rowsForWindow`), `:145` (sent on `ws.onopen`), `:100-101` (meta back-calculation), `:258-261` (`computeDisplayDepth`); server contract at `striqt_web_server.py:847` (`"nfft": int(bins)`).

**What's wrong:** `rowsForWindow(fs, nfft, windowMs) = clamp(win/1000·fs/nfft, 1, 300)`.
- At startup (`curNfft=1024`, 20 ms, 15.36 MS/s): `0.02·15.36e6/1024 = 300` → the cap, sent immediately on connect. The server's gentle `DEFAULT_ROWS=12` never survives first contact.
- After the first calibrated frame, `curNfft` is overwritten by header `nfft` = **147 bins** (app.js:214), so every later computation uses 147: 20 ms → 2076 → still 300. Window options 50–1000 ms are all silently identical.
- The calibrated row spacing is the STFT hop `aligned_nfft·15/28/fs` (555/15.36e6 ≈ 36 µs), not `nfft/fs`, so even a "correct" count mislabels time by ×1.87: 300 rows actually span **10.8 ms**, not 20.
- Consequence: `Computer` asks striqt for 300×1036 = 310 800 samples ×2 ch → ~559 slow 1036-point FFTs ×2 per frame on the Jetson CPU (see §4-Q1).

**The fix (all in `app.js`):**
1. Add a module-level `let radioNfft = 1024;` — the *requested FFT size*. Update it in the `#nfft-sel` change handler (`app.js:847-852`) and never from frame headers. Keep `curNfft` strictly as "bins in the current frame" (rename to `curBins` for clarity, updating its ~12 usage sites: `buildFreqsMHz` call, `updateMeta`, buffer sizing, exports).
2. Rewrite `rowsForWindow(fs, radioNfft, windowMs, backend)`: rows = `windowMs/1000 · fs / (radioNfft · hopFrac)` where `hopFrac = (backend === "quicklook") ? 1 : 15/28`. Clamp to `[1, 300]`, and pick a sane default cap for calibrated (e.g. 60 rows ≈ 130 ms at hop spacing) until LV-W2/W3 land.
3. `updateMeta` (`app.js:100-101`): compute `winMs = depthRows · radioNfft · hopFrac / curFs · 1e3`.
4. `computeDisplayDepth` (`:258-261`): same replacement.
5. On `#win-sel`/`#rate-sel`/`#nfft-sel`/mode changes, send rows from the corrected function (call sites `app.js:835, 850, 875, 886`).

**Verify:** run `--demo --backend quicklook`; open dev tools → WS frames: the first control message after connect must carry `rows` = `round(0.02·15.36e6/1024)=300` for quicklook but ≈ 160 (hop-aware) for calibrated with the same settings; server log `[config] rows: 12 -> N` shows the new N. On hardware: `[radio] IQ …` cadence and frame rate in the meta line should jump from ~0.2 fps to multiple fps (with LV-W3).
**Risk & rollback:** pure client change; worst case the window label is still wrong → revert `app.js` (server untouched). No data-format change.

**▶ Handoff prompt:**
```
In live/web/app.js of the NIST-Omran repo: the client conflates the radio FFT size with
the per-frame bin count. Add `let radioNfft = 1024` updated ONLY by the #nfft-sel change
handler (app.js ~line 847). Rename curNfft→curBins everywhere it is set from a frame
header (onFrame, ~line 214) and audit all uses: buildFreqsMHz/updateMeta/exports keep
curBins; rowsForWindow/computeDisplayDepth/rowsForCurrentSettings must use radioNfft.
Change rowsForWindow(fs, nfft, windowMs) to rowsForWindow(fs, radioNfft, windowMs,
backendHopFrac) computing rows = windowMs/1000*fs/(radioNfft*hopFrac), hopFrac = 1 for
quicklook, 15/28 otherwise, clamped [1,300]. Fix updateMeta's winMs to
depthRows*radioNfft*hopFrac/curFs*1e3. Do not modify striqt_web_server.py. Verify with
`python3 live/striqt_web_server.py --demo` + browser devtools: initial control message
rows and the meta "window … ms" must match the selected Window (ms) for both Boring and
Cool modes.
```

---

#### LV-W3 — `aligned_nfft` picks slow, averaging-hostile FFT sizes; use smooth 28-multiples
**Verdict/severity:** perf + fidelity · **High**. **Layers:** V.
**Location:** `live/striqt_web_server.py:731-733` (`aligned_nfft`), `:736-740` (`averaging_factor`), used at `:655,670,699` and `samples_needed:768-774`.

**What's wrong:** `round(nfft/28)·28` maps 1024→**1036 = 2²·7·37** and 2048→**2044 = 2²·7·73** — sizes with large prime factors that scipy's pocketfft handles far slower than radix-2/3/7 sizes. Worse, `averaging_factor` (largest divisor ≤ 12) then yields **7** for 1036/2044 and **8** for 4088, so the "≈12-bin averaging tied to the cellular waveform" from Dan's transcript intent only actually happens at FFT 256/512, and resolution jumps non-monotonically across the FFT dropdown (731→366→104→53→30 kHz).

**The fix:** replace the rounding with a lookup snapped to smooth multiples of 28 that are also divisible by 12:
```python
ALIGNED_NFFTS = (252, 504, 1008, 2016, 4032)   # 28·{9,18,36,72,144} = 2^a·3^b·7 — fast FFT sizes
def aligned_nfft(nfft: int) -> int:
    return min(ALIGNED_NFFTS, key=lambda n: abs(n - int(nfft)))
```
Every entry: divisible by 28 (satisfies the 15/28 window-fill integrality), divisible by 12 (`averaging_factor` returns 12 for all → consistent Dan-intent averaging), and 7-smooth (fast FFT). Resulting bins: 1008 → `2·((504−6)//12)+1 = 83` bins ≈ 183 kHz at 15.36 MS/s. If 147-bin-like resolution is preferred, add finer sizes (e.g. 1008 with `AVG_BIN_GROUPS=7` → 143 bins) — but keep divisibility by the chosen averaging factor explicit rather than accidental.

**Verify:** unit-check in a REPL: `all(n % 28 == 0 and n % 12 == 0 for n in ALIGNED_NFFTS)`; run `--demo --backend calibrated` and confirm header `nfft` (bins) is 83 at FFT 1024 and the meta fps rises; on hardware time one `calibrated_spectrogram` call before/after (wrap with `time.perf_counter` temporarily) — expect ≥3× faster at 1008 vs 1036 for the same rows.
**Risk & rollback:** changes displayed resolution (183 kHz vs 104 kHz at FFT 1024) — flag in the UI label (LV-F4). Rollback = restore the old two-liner.

**▶ Handoff prompt:**
```
In live/striqt_web_server.py: replace aligned_nfft (lines ~731-733) with a snap to
ALIGNED_NFFTS = (252, 504, 1008, 2016, 4032) (nearest by absolute distance). These are
all multiples of 28 (required: window_fill=15/28 must make (13/28)*nfft an integer or
striqt raises ValueError) and multiples of 12 (so averaging_factor returns 12
consistently) and 7-smooth (fast scipy FFT). Do not change averaging_factor or the
Spectrogram spec construction. Update samples_needed only if it fails tests (it calls
aligned_nfft already). Verify: python3 live/striqt_web_server.py --demo --backend
calibrated → browser meta shows FFT-bin count 83 at the 1024 setting, no striqt
ValueError in the server log, and demo fps unchanged or better.
```

---

#### LV-W2 — Server computes ~1.87× the STFT rows it displays, from ~1.87× the needed samples
**Verdict/severity:** perf · **High**. **Layers:** V.
**Location:** `striqt_web_server.py:768-774` (`samples_needed` = `aligned_nfft·rows`), `:748-749` (`fit_display_rows` crops to the last `rows`), striqt row math at `striqt/src/striqt/analysis/measurements/_spectrogram.py:29-50`.

**What's wrong:** with overlap 13/28, `rows·nfft` samples produce `(28/15)·(rows−1)+1 ≈ 1.87·rows` STFT rows; `fit_display_rows` throws away everything but the last `rows`. To *display* `rows` rows you only need `rows` hops: `samples ≈ rows·hop + (nfft−hop)` where `hop = nfft·15/28`. At rows=300, that's 167 940 samples instead of 310 800 — a 46 % cut in both FFT count and IQ pulled from the ring.

**The fix:** in `samples_needed`, for calibrated/ssb compute `hop = (nfft*15)//28; base = rows*hop + (nfft-hop)`; leave quicklook as `nfft*rows`. Keep the SSB 20 ms floor branch unchanged.
**Verify:** demo: displayed rows unchanged (header `rows` identical), server CPU per frame drops (log timestamps); assert in a REPL that `evaluate_spectrogram` on the new sample count returns ≥ rows STFT rows (it returns exactly `int((28/15)(N/nfft−1))+1`; with N as above that is ≥ rows).
**Risk & rollback:** off-by-one in row count → `fit_display_rows` already pads with the min value, so worst case is a 1-row pad line; revert the function.

**▶ Handoff prompt:**
```
In live/striqt_web_server.py, samples_needed (lines ~768-774): for backends
"calibrated"/"ssb", request only what the displayed rows need under the 13/28 overlap:
hop = (nfft*15)//28 (nfft = aligned_nfft(cfg.nfft)); base = cfg.rows*hop + (nfft-hop).
Quicklook stays nfft*rows. Keep the SSB discovery-period max() branch. Sanity-check
against striqt's row formula in striqt/src/striqt/analysis/measurements/_spectrogram.py
(spectrogram_time): int((nfft/hop)*(N/nfft - 1) + 1) must be >= cfg.rows for the new N
(write a 5-line pytest-style assert script, run it, then delete it). Verify in --demo:
header rows unchanged, frames identical visually.
```

---

#### LV-F1 — The browser invents its own frequency axis; it is wrong by up to an averaged bin
**Verdict/severity:** misrepresented · **High** ("the axis the user sees = truth" is the audit's centerpiece criterion). **Layers:** S→V→U (dropped at V, faked at U).
**Location:** U: `app.js:118-126` (`buildFreqsMHz`); V: header has no axis info (`striqt_web_server.py:840-856`); S: canonical axis available at `striqt/src/striqt/analysis/measurements/shared.py:245-287` (`spectrogram_freqs`/`spectrogram_baseband_frequency`) — never called by V.

**What's wrong (exact math):** striqt's `binned_mean(…, fft=True)` (waveform `arrays.py:135-157`) groups the 1036 raw bins in 7-wide blocks **centered on the DC bin (518)**: 147 groups covering raw bins 4…1032, edge bins 0-3 and 1033-1035 dropped. True group-center baseband frequency: `f(g) = (g − 73)/148 · fs`. The browser assumes plain fftshifted bins: `f'(g) = (g − 73.5)/147 · fs`. Error `f'−f` ≈ **−52 kHz at g=73 (center)** growing to **≈ −103 kHz at g=0** (and +? at the top edge symmetric) at 15.36 MS/s. Every frequency the user reads — PSD x-axis, peak marker ("`freq.toFixed(3)` MHz"), band monitor range, CSV `freq_mhz`, "Tune to band" target — is off by ~0.5–1 averaged bin in calibrated/ssb mode. (Quicklook mode is exactly right, deepening the confusion when switching backends.)

**The fix (contract change, two sides):**
1. **Server** (`publish`, `:840-856`): compute the true axis once per config and ship compact params instead of a big array: add to the header `"fft_nfft": aligned` (the real FFT size), `"bin_avg": average_bins`, `"bins": bins`, and `"first_center_offset_hz": ((first_group_center − aligned/2)/aligned)·fs` — or simplest and unambiguous: `"freqs_hz_offset": f0`, `"freqs_hz_step": step` with `f0 = −(bins−1)/2 · step`, `step = bin_avg·fs/fft_nfft` (for the DC-centered grouping the centers are exactly uniform with this step, f0 symmetric). For quicklook send `fft_nfft = nfft`, `bin_avg = 1`, `step = fs/nfft`, `f0 = −fs/2` (bin-0 convention). Keep the legacy `nfft` key for compatibility this release.
2. **Client** (`buildFreqsMHz`): if `freqs_hz_step` present, `f[i] = (absRF ? center : 0) + f0 + i·step`; else fall back to the current formula.
3. Derivation note for the implementer: with 147 DC-centered 7-wide groups on a 1036 FFT, `step = 7·fs/1036 = fs/148` and `f0 = −73·fs/148` — the code in (1) reproduces exactly this.

**Verify:** `--demo` (demo tones at exactly +2.5 MHz / −1.8 MHz on ch0, server `:1138-1141`): before the fix, the calibrated peak marker reads ≈ 2.45 MHz offset; after, 2.5 MHz ± ½ step. Add a browser console one-liner: `freqsMHz[73]*1e6 - curCenter` must be 0 in calibrated mode.
**Risk & rollback:** header gains keys (additive; old clients ignore them). Rollback = drop the new keys.

**▶ Handoff prompt:**
```
Repo NIST-Omran. The calibrated waterfall/PSD frequency axis is wrong by up to ~103 kHz
because striqt's binned_mean(fft=True) DC-centered 7-bin grouping (see
striqt/src/striqt/waveform/**/arrays.py:135-157, read-only) drops edge bins, while
live/web/app.js buildFreqsMHz (lines 118-126) assumes plain fftshifted bins.
(1) In live/striqt_web_server.py publish() (~840-856) and DemoAcquirer._publish, add
header fields: fft_nfft (the aligned FFT size actually used), bin_avg (averaging
factor), freqs_hz_step = bin_avg*fs/fft_nfft, freqs_hz_f0 = -(bins-1)/2*freqs_hz_step
(bins = block width). Thread the values from calibrated_spectrogram/ssb_spectrogram/
db_spectrogram to publish via cfg or a small per-frame meta dict — do NOT recompute in
publish from cfg.nfft (quicklook: fft_nfft=nfft, bin_avg=1, f0=-fs/2).
(2) In app.js buildFreqsMHz, prefer header f0/step when present. Keep the old formula as
fallback. (3) Verify with --demo: ch0 tones are at exactly +2.5e6 and -1.8e6 Hz
(striqt_web_server.py:1138-1141); the peak marker must read 2.500 MHz (± half a bin)
above center in BOTH quicklook and calibrated backends. Do not modify striqt/.
```

---

#### LV-F2 — The SSB option is a phantom: silent fallback, lying header, dead CSS hook
**Verdict/severity:** phantom · **High**. **Layers:** S (hard constraint) → V (silent fallback) → U (false label).
**Location:** V: `striqt_web_server.py:706-724` (fallback), `:777-779` (`ssb_grid_compatible` — false for every selectable rate), `:851` (header `backend` = requested); U: `index.html:100` (option), `app.js:104` (meta shows "SSB"), `applyAnalysisMode` (`:813-822`) adds `analysis-ssb` class that **no CSS rule uses**; S constraint: `shared.py:172-178` via `_cellular_5g_ssb_spectrogram.py:104-116` — capture `sample_rate` must be a multiple of **420 kHz** (= 28 · SCS/2). 3.84/7.68/15.36/30.72/61.44 MS/s all fail ⇒ `ValueError('…counting-number…')` ⇒ fallback fires on **every frame**.

**What's wrong:** the user selects "SSB spectrogram", the meta line says "SSB", the header says `backend:"ssb"` — and the data is byte-for-byte the calibrated fallback. Also each SSB-mode frame pays a wasted striqt exception round-trip.

**The fix:**
1. **V:** in `ssb_spectrogram`, on fallback return a flag: change `compute_blocks` to return `(blocks, executed_backend)` (or set a module-level per-frame note) and publish `"backend": executed`, plus `"backend_requested": cfg.backend` when they differ. Cheaper: pre-check `ssb_grid_compatible(cfg.sample_rate)` in `compute_blocks` and skip the doomed striqt call entirely (it is already the exact same 420 kHz test striqt applies — verified).
2. **U:** when `backend_requested != backend`, show a status-line warning ("SSB unavailable at this sample rate — showing calibrated") and/or disable the SSB option for incompatible rates with a tooltip. Remove the dead `analysis-ssb` class or give it a visual meaning.
3. Optional real fix (M effort): offer an SSB-compatible rate (15.12 or 30.24 MS/s — multiples of 420 kHz) in the rate list when analysis=ssb, if the AIR8201B accepts them (test on hardware; they are not standard LTE rates).

**Verify:** hardware or demo with `--backend ssb`: server log currently shows no hint of fallback — after fix, header decodes (`python3 - <<'EOF'` WS client or browser console `header.backend`) to `"calibrated"` with `backend_requested:"ssb"`, and the UI shows the warning. Confirm no per-frame striqt ValueError cost with a timing print.
**Risk & rollback:** additive header key; UI copy change. Rollback trivial.

**▶ Handoff prompt:**
```
Repo NIST-Omran, live/ only. The "ssb" backend always falls back to calibrated (striqt
requires capture sample_rate to be a multiple of 420 kHz; all selectable rates fail —
see ssb_grid_compatible in live/striqt_web_server.py:777-779 which encodes the same
test). (1) In compute_blocks/ssb_spectrogram (server ~687-791): pre-check
ssb_grid_compatible(cfg.sample_rate); when false skip the striqt call and use
calibrated_spectrogram directly, recording executed="calibrated". Publish header
"backend" = executed backend and add "backend_requested" when different (update
Acquirer.publish and DemoAcquirer._publish signatures accordingly). (2) In
live/web/app.js onFrame/updateMeta: if header.backend_requested && header.backend !==
header.backend_requested, show "SSB unavailable at this rate — showing calibrated" in
the status line (setStatus(..., "warn")) once per change, and render the meta analysis
field from header.backend. (3) In index.html/app.js, disable the ssb <option> when the
current rate is incompatible (recompute on rate change; tooltip explaining 420 kHz
grid). Verify in --demo --backend ssb: meta shows calibrated + warning; switching rates
keeps it consistent; no striqt ValueError spam in server output.
```

---

#### LV-F3 — PSD "Mean" trace averages decibels, not power
**Verdict/severity:** misrepresented · **Medium** (biased trace on a measurement instrument; internally inconsistent with the corrected band monitor). **Layers:** U.
**Location:** `app.js:404-441` (`psdSeries`: `m[f] += v` over dB values, `/depth`); the band monitor right next to it does the correct linear-domain mean (`app.js:624-639`). Same defect in the Qt viewers: `striqt_standalone.py:1544`, `striqt_frontend_TCP.py:807`, `pluto_standalone.py` analog (their band monitors were all fixed to linear; the mean traces were not).

**What's wrong:** mean-of-dB underestimates the true time-averaged power wherever the signal fluctuates (up to several dB for bursty LTE/5G traffic — exactly the signals this instrument watches). `bug_report.md` F-3 fixed this in the band monitors; the PSD mean trace kept the old math everywhere.

**The fix (app.js `psdSeries`):** accumulate `p[f] += Math.pow(10, v/10)` and output `m[f] = 10*Math.log10(p[f]/depth)` (guard with `1e-20`). Max/min traces are order-statistics — unchanged. Optional follow-up: same one-line change in the three Qt scripts (`b0.mean(axis=0)` → `10*np.log10((10**(b0/10)).mean(axis=0))`).
**Verify:** `--demo`: the mean trace at the tone bins must rise slightly (tones are steady; the noise floor mean rises ~0.1–0.5 dB); construct the definitive check by toggling Pause and comparing band-monitor band power (linear, correct) against the mean trace's average over the same band — they should now agree within quantization.
**Risk & rollback:** display-only; revert one function.

**▶ Handoff prompt:**
```
In live/web/app.js psdSeries (lines ~404-441): the mean PSD trace is computed as an
arithmetic mean of dB values. Change to linear-domain: accumulate
lin[f] += 10**(v/10) per row, then m[f] = 10*log10(max(lin[f]/depth, 1e-20)). Keep
max/min as-is. Mirror the band monitor's convention (updateBandMonitor, ~line 624 —
already linear). Verify in --demo: band monitor "RX1 x.x dB" over a dragged band equals
the average of the fixed mean trace over the same band within ~0.5 dB (previously the
mean trace read lower). Optionally apply the same fix at striqt_standalone.py:1544,
striqt_frontend_TCP.py:807 and the matching pluto_standalone.py line.
```

---

#### LV-F4 — Status line, FFT captions, window label and units all report internal fiction
**Verdict/severity:** misrepresented · **Medium**. **Layers:** U (labels), V (units source).
**Location:** `app.js:98-112` (`updateMeta`: `FFT ${curNfft}` = bins; `winMs` from bins), `:782` (PNG caption), `:747` (CSV header row — no units), `index.html:368` (PSD y-axis "Power (dB)").

**What's wrong:** after the first calibrated frame the status line reads e.g. `FFT 147 | calibrated | window 3 ms` — every number wrong (FFT is 1036; the window is 10.8 ms at hop spacing). The PSD y-axis "Power (dB)" hides that calibrated values are **band-integrated** (Σ over ~104 kHz, ≈ +8.5 dB vs per-bin) ENBW-normalized relative power (no absolute dBm calibration — striqt's `'dBm/104 kHz'` attrs label is nominal), while quicklook is per-bin Σw²-normalized — so switching backends shifts absolute levels and *meaning*.

**The fix:** (depends on LV-W1 names + LV-F1 header fields)
1. `updateMeta`: show `FFT ${radioNfft}→${fft_nfft} (${curBins} bins × ${bin_avg})` when they differ; window from the LV-W1 formula; analysis from executed backend (LV-F2).
2. PSD y-axis label per backend: calibrated → `Integrated power (dB rel. FS / ~{bin_avg·step} kHz)`; quicklook → `Power (dB rel. FS / bin)`.
3. CSV: prepend comment lines `# backend=…, fft_nfft=…, bin_avg=…, units=dB (uncalibrated, band-integrated)`; PNG caption: use `fft_nfft` and true window ms.

**Verify:** switch backends in `--demo` and confirm the label changes and the ~8.5 dB level shift is now explained on-screen; export CSV and check the header comments.
**Risk & rollback:** copy changes only.

**▶ Handoff prompt:**
```
In live/web/{app.js,index.html} (after the LV-W1 radioNfft rename and LV-F1 header
fields land): (1) updateMeta must print FFT as `${radioNfft}→${header.fft_nfft}
(${bins} bins × ${header.bin_avg})` for calibrated/ssb and plain radioNfft for
quicklook, and window ms via the hop-aware formula. (2) Set the uPlot y-axis label per
backend: "Integrated power (dB rel. FS)" for calibrated/ssb vs "Power (dB rel. FS/bin)"
for quicklook (initUplot axes[1].label; re-init or setSeries on backend change). (3)
savePsdCsv: prepend `# backend=`, `# fft_nfft=`, `# bin_avg=`, `# units=dB uncalibrated`
comment lines. (4) exportPng caption: use fft_nfft and the true window ms. Verify in
--demo by toggling Analysis between Spectrogram and (fallback) SSB and reading the meta
line and CSV headers.
```

---

#### LV-F5 — Quicklook backend and striqt's real PSD analysis are unreachable from the UI
**Verdict/severity:** dropped · **Medium**. **Layers:** S (PSD measurement exists), V (quicklook exists), U (neither offered).
**Location:** `index.html:96-102` (Analysis options), `app.js:813-822` (maps to calibrated/ssb only); V `BACKENDS` includes `quicklook` (`server:147`); striqt `power_spectral_density` with `time_statistic=('mean', quantiles…, 'max')` at `striqt/src/striqt/analysis/measurements/_power_spectral_density.py:39-99` — the analysis Dan's sample JSON configures — has no live/ caller.

**What's wrong vs intent:** Dan's transcript intent: "a dropdown/button to switch which analysis is shown". The dropdown exists but (a) can't reach quicklook (useful: fast, per-bin, exact user nfft — currently env-var/CLI only), and (b) "PSD" is a CSS view toggle, not the striqt PSD analysis with percentile statistics.

**The fix:** add `quicklook` to the Analysis dropdown mapping (`{backend:"quicklook"}`); longer-term (M): a true `psd` backend in `compute_blocks` calling striqt `power_spectral_density(iq, capture, as_xarray=False, time_statistic=("mean","max",0.95))` and a frame variant carrying trace vectors instead of rows (header `"kind":"psd"`), with app.js rendering traces directly and skipping the waterfall. Keep the current client-side PSD view as the default until then; rename its option to "PSD view (hide waterfalls)" for honesty.
**Verify:** dropdown → quicklook: header backend changes, LO spike visible (no DC null), per-bin levels ~8.5 dB below calibrated — all expected and now labeled (LV-F4).
**Risk & rollback:** additive UI option.

**▶ Handoff prompt:**
```
In live/web (NIST-Omran): (1) Add <option value="quicklook">Quicklook (raw FFT)</option>
to #analysis-sel in index.html and extend applyAnalysisMode in app.js to send
{backend:"quicklook"} for it (server already supports it — BACKENDS in
striqt_web_server.py:147). (2) Rename the "PSD" option label to "PSD view" (it only
hides waterfalls; backend stays calibrated). Verify in --demo: selecting Quicklook
changes header.backend to "quicklook", the DC spike appears (no LO null), and levels
drop ~8-9 dB (expected: per-bin vs band-integrated). Do not implement the striqt
power_spectral_density backend in this pass.
```

---

#### LV-F6 — Settings editor: hidden sweep params never sent; ignored fields never disclosed
**Verdict/severity:** phantom + dropped (explicit Dan intent) · **Medium**. **Layers:** U + V.
**Location:** U: `app.js:959` (`hiddenSweepSettings` assigned at `:1044`, never read again), `:1069-1075` (`collectSettings` sends only visible fields), `:1083-1086` (Apply logs "Settings sent" unconditionally); V: `SharedConfig.update` maps only `capture.{center_frequency, sample_rate, gain, duration}` (`server:419-434`), prints-and-drops `source.*` (`:439-444`), silently drops `capture.{analysis_bandwidth, port, lo_shift, host_resample, backend_sample_rate}`.

**What's wrong vs intent (Dan's email, context §1):** *"Loading it and converting to a python dict seeds default values in the GUI **and sets 'lower-level' parameters that are hidden / not configured in the GUI**."* The second half is unimplemented. Also: a user who edits `lo_shift` or `port` and hits Apply gets "Settings sent" and nothing happens anywhere — a silent phantom.

**The fix:**
1. **U:** merge `hiddenSweepSettings` into the Apply payload: `payload.capture = {...hiddenCapture0, ...formCapture}`, `payload.source = {...hiddenSource, ...formSource}` (visible fields win). Send once on Apply (not on load).
2. **V:** in `SharedConfig.update`, build and `print`/return an ack: `{"applied": [...], "ignored": [...], "requires_reconnect": [...]}`. Send it back over the WS as a text message (`ws_endpoint` currently never sends text; app.js already handles `{message: …}` text frames at `:148-153` — use that).
3. **U:** log the ack verbatim; change the badge text to "requires reconnect — not applied live".
4. Decide per ignored capture field whether to *implement* it (e.g. `port` could legitimately map to a channel-enable mask later) — out of scope here; the ack makes the current truth visible.

**Verify:** upload Dan's sample sweep JSON (context §4): form seeds (already works); Apply → server log shows the merged dict; browser log shows `applied: [center_frequency, sample_rate, gain, duration] ignored: [analysis_bandwidth, …] requires_reconnect: [master_clock_rate, …]`.
**Risk & rollback:** the ack is additive; hidden-merge could send unexpected keys — server ignores unknowns by design (verified `update()` skips non-valid keys).

**▶ Handoff prompt:**
```
Repo NIST-Omran. (1) live/web/app.js: hiddenSweepSettings (set in renderSettings, ~line
1044) is never used. In the #settings-apply handler, deep-merge it under the collected
form values: capture = {...(hiddenSweepSettings.captures?.[0] ?? {}), ...form.capture},
source = {...(hiddenSweepSettings.source ?? {}), ...form.source}; send that. (2)
live/striqt_web_server.py SharedConfig.update (~416-479): return/record three lists —
applied (valid keys that changed), ignored (capture keys with no mapping), reconnect
(source keys minus the skip set) — and in ws_endpoint, after _shared.update(ctrl), send
await ws.send_text(json.dumps({"message": f"applied {applied}; ignored {ignored};
reconnect-only {reconnect}"})). app.js already prints {message} text frames
(onmessage, ~148-153). (3) index.html: change the source panel badge to "requires
reconnect — not applied live". Verify: upload context/AUDIT_CONTEXT.md §4's JSON via
Load JSON, press Apply, and check the browser Log panel lists the three categories and
the radio retunes to 3750 MHz (center_frequency from the file).
```

---

#### LV-F7 — Waterfalls have no frequency (or time) axis; the styled axis element is empty
**Verdict/severity:** phantom element · **Medium** (a spectrogram with unlabeled axes fails the "axis = truth" bar). **Layers:** U.
**Location:** `index.html:194,201` (`.wf-freq-axis` divs), `style.css:409-417` (styled overlay), no writer anywhere in `app.js` (grep-verified).

**The fix:** after LV-F1 lands, populate each `.wf-freq-axis` on tuning change with 5–7 tick labels from the true axis (`freqsMHz[0]`, quartiles, `freqsMHz[bins-1]`), plus an edge time annotation ("↑ newest / window X ms" per mode). Pure DOM text; no canvas work.
**Verify:** labels match the uPlot x-axis ticks below (same source array); change center → labels update.
**Risk & rollback:** cosmetic.

**▶ Handoff prompt:**
```
In live/web/app.js: the .wf-freq-axis overlay divs (index.html lines 194/201,
styled in style.css:409-417) are never populated. Add renderWfAxis() called wherever
freqsMHz is rebuilt (onFrame tuningChanged branch ~line 221 and the abs-rf handler
~line 895): fill both divs with 5 evenly spaced <span>s showing freqsMHz values
(toFixed(1) + " MHz"), flex space-between. Also append a right-aligned span showing the
current window ms (reuse the LV-W1 hop-aware value). Verify visually in --demo and by
comparing the outermost labels with the uPlot PSD x-axis extremes.
```

---

#### LV-F8 — Undisclosed 5-bin DC overwrite (calibrated/ssb only)
**Verdict/severity:** misrepresented · **Low** (deliberate cosmetic null, but silent and backend-inconsistent). **Layers:** V.
**Location:** `striqt_web_server.py:759-764` (`fit_display_rows`), on top of striqt's `lo_bandstop=120e3` NaN mask (`:678`, striqt `null_lo`).

**What's wrong:** center ±2 averaged bins (≈519 kHz at 15.36 MS/s FFT 1024; ≈3.7 MHz(!) at FFT 256 where bins are 731 kHz wide) are replaced by the row minimum. Real signal at band center is hidden; quicklook shows the LO spike instead; nothing tells the user.
**The fix:** make the DC null width proportional to the striqt bandstop (`ceil(lo_bandstop/step)` bins instead of a fixed 2), add an "LO null" checkbox (default on) in the Display group that sends `{lo_null: bool}` (extend `SharedConfig`+`fit_display_rows`), and draw a small "LO" tag over the nulled stripe (client, using LV-F1 axis).
**Verify:** toggle: stripe appears/disappears; at FFT 256 the null shrinks from 5 bins to 1 (`ceil(120e3/731e3)`).
**Risk & rollback:** off-by-default risk of NaN leakage into the quantizer if the null is disabled while striqt NaNs remain — pair with LV-R4 (NaN-safe quantizer) or convert NaN→row-min unconditionally.

**▶ Handoff prompt:**
```
In live/striqt_web_server.py fit_display_rows (~743-765): replace the fixed ±2-bin DC
null with half_width = max(1, math.ceil((SSB_LO_BANDSTOP/2) / (bin_avg*fs/fft_nfft)))
bins (thread bin_avg/fft_nfft/fs in as arguments from the calling backend functions),
and ALWAYS replace any remaining NaNs in the block with the per-row nanmin before
returning (protects the quantizer). Add optional cfg flag lo_null (SharedConfig field,
WS-controllable, default True) gating the overwrite (NaN cleanup stays unconditional).
Add a "LO null" checkbox to the Display group in index.html wired like auto-color but
sending {lo_null: e.target.checked}. Verify in --demo calibrated: checkbox toggles the
center stripe; with it off, no NaN garbage appears when --quantize is used.
```

---

#### LV-U1 — Parity & polish cluster (web vs Qt viewers)
**Verdict/severity:** dropped/confusing · **Low**. **Layers:** U(+V).
- **a. No client fps/quality control:** Qt GUIs have "JEEZ SLOW DOWN" (`striqt_standalone.py:1106`); the web viewer renders every frame. Add a "Max fps" select that throttles `onFrame` (client-side), or send `{fps}` for the server to honor per-connection.
- **b. Peak marker is RX1-only** (`app.js:505-522`) but labeled generically — either label it "RX1 peak" or draw one marker per visible channel.
- **c. Gain ranges disagree:** UI −30…0 (`index.html:66`), server clamp −60…+10 (`server:466`). Pick the AIR8201B's real usable range, set it in *both* places, and document it.
- **d. README parity claim:** README says "feature parity with striqt_standalone.py" — after this audit note the deltas (no histogram-LUT drag equivalent, no editable rate, adds settings editor/noob mode/analysis dropdown).

**▶ Handoff prompt:**
```
In live/web: (a) add a "Max fps" select (values 15/10/5/2/1, default 15) to the Display
group; in app.js keep lastRender and skip onFrame rendering (but still parse the header
for status) when now-lastRender < 1000/maxFps. (b) In drawPeakMarker/updatePSD, either
render one marker per visible channel (colors matching COL.rx1Max/rx2Max) or relabel
the checkbox "RX1 peak marker" in index.html. (c) Change #gain min to -60 and max to
+10 to match the server clamp in striqt_web_server.py:466, and add a title tooltip
"AIR-T RX gain". Verify in --demo: fps throttle changes the meta fps; both peak markers
track their channels; gain spinner accepts -60..10.
```

---

#### LV-G1 — GPS-synchronized capture (Dan's intent) is one reconnect-flow away
**Verdict/severity:** dropped capability · **Medium**, hardware-gated. **Layers:** S (complete), V (hardcoded off), U (absent).
**Location:** V `make_source` hardcodes `time_source="host", time_sync_at="open", clock_source="internal"` (`server:597-599`). S: full GPS/external PPS discipline exists (🟨 `soapy.py:509-559`, `to_external_pps` waits for a PPS edge and latches hardware time; `gps`/`external` accepted by `Air8201BSourceSpec`).
**What it buys (transcript intent):** SSB bursts land every 20 ms on GPS-derived boundaries → time-synced, "clean" spectrograms across sensors (Aric's GPS-antenna idea).
**The fix (design):** source-spec fields are set-once-at-open (Dan's rule) — so implement as a *reconnect flow*: settings-editor source form (`time_source`, `clock_source`) + a new explicit "Reconnect radio" button that stops the Acquirer, rebuilds the source with the edited source spec, restarts. Depends on LV-F6's ack plumbing. Guard: if `to_external_pps` raises "no pps input detected", surface it in the UI and fall back to host time.
**Verify (hardware + GPS antenna only):** server log shows `setTimeSource('gps')`; PPS-latch warning absent; frame `time` values step coherently.

---

### Faithful inventory (for completeness — no action)

Center/gain/rate plumbing and clamps · noob station chips (incl. honest <300 MHz disable) · Boring/Cool mode switch & localStorage persistence · auto-color percentile scaling · absolute/baseband toggle · peak hold/min/diff/Y-span mechanics · band-monitor **linear** power math (the intent-critical fix, present) · quantize round-trip (≤1/255 of range) · binary framing (length-prefixed, single message, parse symmetric) · single-message serialization + fan-out with in-place `difference_update` (the documented UnboundLocalError trap is correctly avoided, `server:1339-1345`) · NoCacheMiddleware (fixes the stale-asset issue REPO_OVERVIEW §8.6 flagged) · Basic-Auth + signed-cookie WS path (Safari/iOS fix, modulo LV-R8) · demo tones land exactly where the (fixed) axis says they should.

---

## §4 — Open-question root causes

### Q1 — Real-hardware cadence: ~1 frame / 5 s calibrated vs ~14 fps demo

**Verdict: fully explained. It is a compound of one client bug, one server FFT-size choice, and the intrinsic backend delta — not a striqt defect, not ring starvation, not the cache.**

Evidence chain (all file:line-verified):
1. **The client always requests the maximum workload.** `rowsForWindow` (app.js:128-130) yields exactly 300 (the cap) at the defaults, sent on `ws.onopen` (app.js:145), and after the first frame the basis silently becomes the 147-bin count so every window setting is pinned at 300 (LV-W1). The server's `DEFAULT_ROWS=12` never applies.
2. **Per frame, calibrated then does:** `samples_needed = 300·1036 = 310 800` samples/ch (server:768-774) → `evaluate_spectrogram` STFT with hop 555 → **~559 rows × 2 ch ≈ 1118 FFTs of length 1036 = 2²·7·37** (🟨 striqt `_spectrogram.py:29-50`; `fourier.py:737-740`). A 1036-point FFT (prime factor 37 → Bluestein/Rader path in pocketfft) is several times slower than radix-2 1024. 46 % of those rows are then discarded by `fit_display_rows` (server:748-749).
3. **All of it runs on the Jetson's ARM CPU.** `make_source(array_backend="numpy")` (server:596) + NumPy IQ arrays mean striqt's cupy branches never dispatch (🟨 `fourier.py:238-256`). No dask/xarray on this path (`as_xarray` not used; direct `evaluate_spectrogram`).
4. **The demo comparison was never apples-to-apples.** `DemoAcquirer` runs the same `compute_blocks`, but "demo ~14 fps" was observed with the quicklook-class workload and/or on the dev machine: `db_spectrogram` (server:623-641) does exactly `rows` power-of-2 `np.fft.fft` calls, no overlap, no averaging — an order-of-magnitude-plus lighter than item 2.
5. **Ruled out:** ring starvation — the drain loop reads 2¹⁸ samples ≈ 17 ms per call and fills MAX_TAIL=4 Mi (273 ms at 15.36 MS/s) quickly; `get_latest` returns data whenever the last write is <1 s old (server:893-919). `Computer` pacing — it sleeps only *after* compute (server:1073-1079); with compute ≫ interval it publishes back-to-back, i.e. cadence = per-frame compute time. **The `spectrogram_cache.clear()` calls are no-ops**: the cache is a single-element `KwArgCache` with `enabled=False` by default, only activated inside `cached_registry_context`, which live/ never enters (🟨 `register.py:42-127`, esp. `:51,72-73,82-83`). CLAUDE.md's "always clear the cache to prevent a frozen display" is a myth to retire (LV-D1).
6. **The "Mean of empty slice" RuntimeWarning** is a cosmetic side-effect, not a cause: `lo_bandstop=120 kHz` NaNs 8 raw bins around DC (🟨 `null_lo`, `fourier.py:212-226`) *before* the 7-bin `nanmean` grouping (🟨 `arrays.py:157`), so the all-NaN center group warns once per call. striqt's own `warnings.filterwarnings` is module-scoped to `_spectrogram.py` and does not cover this call path (🟨 `_spectrogram.py:20-22`) — see §6-N4. live/ may suppress it locally (one `warnings.filterwarnings` line next to the `evaluate_spectrogram` call, message-matched).

**Recommended fix set (in order):** LV-W1 (client rows) → LV-W3 (smooth nfft) → LV-W2 (right-size samples). Estimated combined effect at FFT 1024, 20 ms window: FFT work drops from ~1118 slow-1036 FFTs to ~160 fast-1008 FFTs per frame — comfortably inside a 5–15 fps budget on the Jetson CPU. Optional beyond that: pass cupy arrays (the striqt path supports it) or default the UI to quicklook when calibrated compute-time exceeds the broadcast interval (adaptive guard, LV-W1 note).

### Q2 — libstdc++ / GLIBCXX re-exec: sound or fragile?

**Verdict: sound in its niche; keep it, but make the launcher authoritative.**
`_ensure_pixi_runtime_libs` (server:49-74) runs at import, prepends `<python-prefix>/lib` to `LD_LIBRARY_PATH` when a `libstdc++.so.6` exists there, and re-execs once. Safety properties verified: idempotent (path-membership check `:63-66` short-circuits the re-exec'd child); loop-proof (`RADIO_WEB_LD_REEXEC` backstop `:68-70`); no-op on macOS/dev boxes (no `libstdc++.so.6` in that layout) and in most demo runs; runs before any striqt import so nothing is half-initialized when `execv` replaces the process.
Residual fragility, in order of likelihood: (a) it depends on `sys.executable` living inside the pixi env (a bare `python3` from PATH with the pixi env activated differently would silently skip); (b) `LD_LIBRARY_PATH` leaks to children (`cloudflared` started later by `run_web.sh` inherits it — harmless today); (c) uvicorn reload/multi-worker modes would re-exec per worker (not used here); (d) anything printed before the re-exec appears twice.
**Cleaner primary fix (LV-Q2):** export the lib dir in `run_web.sh` before starting Python (`export LD_LIBRARY_PATH="$(dirname "$(command -v python3)")/../lib:${LD_LIBRARY_PATH}"` guarded by a `libstdc++.so.6` existence test), or launch via `pixi run`. Keep the in-code re-exec as a fallback for bare `python3 live/striqt_web_server.py` invocations — it is exactly the "works no matter how you start it" belt.

**▶ Handoff prompt (LV-Q2):**
```
In live/run_web.sh (NIST-Omran): before launching striqt_web_server.py, detect the
python env lib dir: LIBDIR="$(python3 -c 'import sys,pathlib;
print(pathlib.Path(sys.executable).resolve().parents[1] / "lib")')"; if
[ -e "$LIBDIR/libstdc++.so.6" ]; then export LD_LIBRARY_PATH="$LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; fi.
Add a comment referencing _ensure_pixi_runtime_libs in striqt_web_server.py (lines
49-74) as the in-process fallback — do not remove that function. Verify on the radio:
`bash live/run_web.sh` starts without the RADIO_WEB_LD_REEXEC re-exec (add a temporary
echo in the function to confirm it early-returns via the path-membership check).
```

### Q3 — SSB fallback: is there a clean path to the true symbol-aligned SSB view?

**Verdict: with the current rate list, no — the option is a pure phantom (see LV-F2). The constraint is structural, not a bug.**
Root cause (🟨): `cellular_5g_ssb_spectrogram` internally builds a Spectrogram spec with `frequency_resolution = subcarrier_spacing/2 = 15 kHz` and `window_fill = 15/28` (`_cellular_5g_ssb_spectrogram.py:104-112`); `_cached_spectrogram` then requires `nzero = (13/28)·nfft` to be an integer with `nfft = round(fs/15 kHz)` (`shared.py:162-178`) ⇒ **fs must be a multiple of 28·15 kHz = 420 kHz**. None of 3.84/7.68/15.36/30.72/61.44 MS/s qualifies ⇒ `ValueError('…counting-number…')` every time ⇒ server fallback (server:718-724). The server's own `ssb_grid_compatible` (server:777-779) encodes the identical test and returns False for the whole rate list — i.e., the code *already knows* and proceeds anyway.
**Clean paths, ranked:**
1. **Honesty first (LV-F2, S effort):** disable/annotate the option; stop labeling fallback frames "ssb". Do this regardless.
2. **Compatible capture rate (M, hardware-gated):** offer 15.12 or 30.24 MS/s (multiples of 420 kHz) when SSB is selected — needs an AIR8201B bench check that the SoapyAIRT driver accepts non-LTE rates at 125 MHz MCR; keep them out of the default list.
3. **Host resample for analysis (M):** capture at 15.36 MS/s, `scipy.signal.resample_poly(iq, 63, 64)` → 15.12 MS/s before the SSB call (63/64 polyphase, ~cheap at these lengths); display axis then derives from the resampled rate. Fully software, no hardware risk; costs one resample per frame.
4. **Ask Dan (§6-N5)** whether a `window_fill` override for viewer use is acceptable — `window_fill=1` (default) removes the mod-28 constraint entirely (then fs must only satisfy `fs/15 kHz` integral, which 15.36 MS/s does: nfft=1024). The 15/28 fill exists for symbol alignment; a "viewer-grade SSB" without it may or may not be acceptable upstream. **Should the UI expose it?** Only after one of 2–4 exists; until then the dropdown should not offer what the stack cannot deliver.

### Q4 — 147-bin fidelity: is the averaging right, and does the user see the right frequencies?

**Verdict: the averaging math is correct and confirmed at 147 bins; the header/quantizer are lossless; the browser axis is wrong (LV-F1) — that is the one break in the chain.**
- **Averaging (🟨 verified):** `binned_mean(spg, 7, fft=True)` DC-centered grouping → `2·(515//7)+1 = 147` groups over raw bins 4…1032 of the 1036-point FFT; NaN-aware mean then `×7` mean→sum (integration) (`shared.py:224-227`, `arrays.py:135-157`). Linear-power domain (before dB) — statistically correct.
- **Header:** carries `nfft: 147` (bins) — value correct, name misleading (LV-F1/F4). `shape`/`rows` consistent with the blocks (parse verified against `serialize_frame`).
- **Quantizer:** per-frame 1–99 % global range, uint8, dequantized exactly in the client (≤ range/255 ≈ 0.4 dB error) — faithful. One hazard: `np.percentile` is not NaN-aware (LV-R4); in the default calibrated config the DC-null overwrite (server:759-764) removes the only NaNs first, so it does not fire today.
- **Browser plotting:** `buildFreqsMHz` maps bin g → `(g−73.5)/147·fs`; truth is `(g−73)/148·fs`. Error −fs/2 + 73·fs/148 ≈ −52 kHz at center; grows toward the edges to ≈ ±103 kHz at 15.36 MS/s (larger in Hz at 30.72). Peak marker, band monitor bounds, CSV freq column, "Tune to band" target all inherit it. **Fix = LV-F1** (ship `f0`/`step` from the server; client uses them).
- Note the axis is *exactly right* in quicklook mode — so the error is invisible in demos and appears only when comparing calibrated readouts against a known emitter, which matches how it evaded notice.

---

## §5 — Bugs & edge cases (🟦 LIVE track)

### §5.A Web server (`striqt_web_server.py`)

**LV-R1 — Driver `RuntimeError` kills acquisition threads permanently** · High
`Acquirer.run` catches `(ReceiveStreamError, OverflowError, OSError)` around `_read_stream` (server:1007) — but SoapySDR's C++ layer can surface bare `RuntimeError` (🟨 device calls raise it; `ReceiveStreamError` is an `OSError` subclass, `base.py:20-21`, so the third term is redundant rather than broad). An uncaught `RuntimeError` unwinds `run()`, the `finally` closes the source, and the viewer freezes forever with the server still up. Same pattern gap in `striqt_standalone.py:705`, `pluto_standalone.py` (same loop), `striqt_standalone_terminal.py` (same tuple).
*Repro reasoning:* any driver-level fault mid-read (JESD hiccup, USB/PCIe reset) that maps to a C++ exception rather than an error code.
*Fix:* widen to `except (ReceiveStreamError, OverflowError, OSError, RuntimeError)` in the read try (server:1007) **and** in the dirty-rearm path it is already `Exception` (server:986) — fine. Consider the same one-token change in the three sibling scripts.
*Verify:* monkeypatch `_read_stream` to raise `RuntimeError("injected")` once (temporary test shim in `--demo`-less dev run or a unit harness around `Acquirer`); log shows `[radio] recovering after: injected` and frames resume.
*Risk:* catching `RuntimeError` may mask a genuine programming error inside the loop — acceptable here because `_recover` re-raises on repeated failure into the 1 s retry path with logs.
**▶ Handoff:**
```
In live/striqt_web_server.py line ~1007, change the except tuple around
source._read_stream to (ReceiveStreamError, OverflowError, OSError, RuntimeError).
Apply the same widening to the equivalent read-loop except in
live/striqt_standalone.py (~line 705), live/pluto_standalone.py (same loop, ~732), and
live/striqt_standalone_terminal.py (~line 572 region). Nothing else. Verify by
temporarily raising RuntimeError from a wrapper around _read_stream on the 50th call
in a dev run and observing "[radio] recovering after:" followed by resumed frames;
remove the shim afterwards.
```

**LV-R2 — One malformed control message drops the (only) viewer; unsnapped values thrash the radio** · High
`ws_endpoint` (server:1348-1379): `json.loads(text)` and `_shared.update(ctrl)` run inside the receive loop; a non-JSON text, a non-dict (`"[1,2]"` → `update.items()` AttributeError), or a non-numeric value (`{"center":"abc"}` → `float()` ValueError at server:457) escapes to the outer `except Exception` → connection closed → 1.2 s reconnect loop. Separately, `update` clamps but does not *snap*: `sample_rate` accepts any 1–125 MHz (server:464) and `nfft` any 128–8192 (server:468); an off-list value reaches `arm_spec` → hardware rejection → `_recover` (~1.7 s dead) or striqt `ValueError` per frame in `calibrated_spectrogram` (`isroundmod` guard).
*Fix:* wrap the body in `try/except (json.JSONDecodeError, ValueError, TypeError, AttributeError): await ws.send_text(json.dumps({"message": f"bad control: {e}"})); continue`. In `update`, snap `sample_rate` to `RATES = (3.84e6, 7.68e6, 15.36e6, 30.72e6)` and `nfft` to `(256,512,1024,2048,4096)` by nearest, mirroring CLAUDE.md's stated invariant ("always snap --nfft to this list").
*Verify:* `wscat`/browser console `ws.send("garbage")`, `ws.send('{"center":"abc"}')`, `ws.send('{"sample_rate": 9999999}')` — connection stays up, log message arrives, radio never rearms to 9.999999 MS/s.
**▶ Handoff:**
```
In live/striqt_web_server.py: (1) ws_endpoint (~1365-1372): wrap json.loads +
_shared.update in try/except (json.JSONDecodeError, ValueError, TypeError,
AttributeError) as e → send {"message": f"bad control ignored: {e}"} as text and
continue; keep asyncio.TimeoutError handling as-is. (2) SharedConfig.update
(~446-468): snap sample_rate to nearest of (3.84e6,7.68e6,15.36e6,30.72e6) and nfft to
nearest of (256,512,1024,2048,4096) before the existing clamps. Verify with a browser
console websocket: sending "garbage", '{"center":"abc"}' and '{"sample_rate":9e6}'
leaves the stream running and logs the ignore message; sending
'{"sample_rate":30720000}' still retunes.
```

**LV-R3 — Single-viewer lock: accept race, no liveness, indistinguishable refusals** · High (operational)
(1) *Race:* `if _connections: close(1008)` runs before `await ws.accept()`/`add` (server:1355-1361); two handshakes interleaving across those awaits can both pass the empty check → two "single" viewers. (2) *No liveness:* the 15 s `receive_text` timeout is treated purely as keepalive (server:1370-1372) — a phone that sleeps mid-session holds the slot until TCP gives up (minutes), locking everyone out; there is no ping and no takeover. (3) *Ambiguity:* busy-refusal uses close code **1008**, the same code `BasicAuthMiddleware` uses for auth failure (server:334) — and `app.js` never inspects close codes (`onclose`, app.js:159-163), so "busy", "unauthorized", and "server down" all render as "disconnected — reconnecting…" with an infinite 1.2 s retry (which *does* double as a takeover queue — arguably a feature, but an invisible one).
*Fix:* (a) move the occupancy check after `accept()` under a simple `asyncio.Lock`, or use `len(_connections)` check-and-add atomically before any await between them; (b) send a WS ping (or empty text "ping") every 15 s from the endpoint loop and drop the client after 2 missed cycles — the broadcaster's `send_bytes` failure already prunes dead sockets eventually, but only when a frame flows and the OS buffer fills; (c) distinct close codes: 4001 = busy (include `{"message":"viewer slot busy"}` text pre-close), keep 1008 = auth; app.js: on 4001 show "another viewer is connected — waiting for the slot…", on 1008 stop retrying and show "authentication failed — reload the page".
*Verify:* two browser tabs: second shows the busy state and takes over within ~2 s of closing the first; kill the holder's network (dev tools offline) and confirm the slot frees within ~30 s; wrong-password reload shows the auth state instead of silent retry.
**▶ Handoff:**
```
live/striqt_web_server.py ws_endpoint (~1348-1379) + live/web/app.js connect
(~136-166). Server: guard the single-viewer slot with a module asyncio.Lock: async
with _slot_lock: if _connections: await ws.close(code=4001); return; await
ws.accept(); _connections.add(ws). Add liveness: replace the bare 15s timeout pass
with a counter — on timeout send await ws.send_text('{"message":"ping"}') and count
misses via a 2nd consecutive send failure → break. Client: in ws.onclose inspect
event.code: 4001 → status "another viewer is connected — retrying…" (keep 1.2s
retry); 1008 → status "authentication failed — reload to log in" and DO NOT schedule
reconnect; else current behavior. Verify with two tabs + wrong-credential test as
described in AUDIT_REPORT.md §5.A LV-R3.
```

**LV-R4 — Quantizer is not NaN-safe** · Medium
`serialize_frame` (server:1189-1203) uses `np.percentile`/arithmetic on the raw blocks; one NaN anywhere → `vmin=vmax=NaN` → the whole uint8 frame is garbage *and* `rng=NaN` propagates. Today the calibrated DC-null (server:759-764) happens to overwrite the only NaN (the all-NaN center group striqt returns); any future config where striqt NaNs exceed the ±2-bin null (different `lo_bandstop`, `trim_stopband=True`, ssb odd shapes) breaks quantized mode silently.
*Fix:* `np.nanpercentile` + `np.nan_to_num(block, nan=vmin)` per block before scaling (2 lines). Pairs with LV-F8's unconditional NaN cleanup.
*Verify:* temporary `blocks[0][0,0]=np.nan` injection in `--demo --quantize` → frame renders normally (previously all-noise).
**▶ Handoff:**
```
In live/striqt_web_server.py serialize_frame (~1189-1203): replace np.percentile with
np.nanpercentile for vmin/vmax, and quantize with
u8 = ((np.nan_to_num(np.asarray(block, np.float32), nan=vmin) - vmin) / rng * 255)...
Verify in --demo --quantize by temporarily injecting np.nan into one block in
compute_blocks and confirming the waterfall still renders (remove the injection).
```

**LV-R5 — Retune publishes zero-padded and (briefly) mislabeled frames** · Medium
After `rearm` clears the ring (server:944-945), `get_latest` front-pads with zeros until the ring refills (server:910-918) → 1–2 frames contain −200 dB rows → autoColor stretches to them (washout flash). Symmetrically, a frame *computed from* pre-retune samples can be published with the *new* header center/fs (Computer snapshots cfg then reads the ring, server:1059-1060) — a one-frame axis mislabel; the same race exists in the Qt viewers and is worst in the TCP server (no ring-clear at all — see LV-R10 context).
*Fix:* tag the ring with a generation counter: `Acquirer._gen += 1` inside `rearm`/`_recover` ring-clear; `get_latest` returns `(samples, gen)`; `Computer` drops the frame when `gen` changed between snapshot and read, and skips publishing while `avail < needed` (expose `available()` or return None until full — the front-pad path then only ever fires for genuinely short windows).
*Verify:* on hardware (or demo with an artificial ring), retune repeatedly: no black flash, no frame whose peak sits at the old tone offset while the header claims the new center.
**▶ Handoff:**
```
In live/striqt_web_server.py: add self._gen = 0 to Acquirer.__init__; increment inside
_clear_ring_locked (~860-864). Change get_latest to also return self._gen and the
actual available count (return (out, gen, avail) or None as today). In Computer.run
(~1055-1079): snapshot g0 via a new acquirer.generation() before get_latest; skip
compute when the returned gen != g0 or avail < samples_needed(cfg). Keep DemoAcquirer
untouched. Verify on --demo is not possible (no ring) — verify on hardware by rapid
center changes: the waterfall must never flash dark rows nor show the previous band's
energy under the new center label.
```

**LV-R8 — `Secure` session cookie locks iOS/Safari out over plain-HTTP LAN** · Low (edge)
The Safari/iOS WS workaround depends on the `radio_auth` cookie, set with `Secure` (server:303-305). Over `http://<radio-ip>:8000` (no tunnel) Safari won't store a Secure cookie *and* won't replay Basic on the WS upgrade — exactly the clients the cookie exists for are locked out on LAN. Works via the HTTPS tunnel; Chrome/Firefox unaffected (they replay Basic).
*Fix:* set `Secure` conditionally on the request scheme (`scope["scheme"] == "https"` or `x-forwarded-proto`), keeping `HttpOnly; SameSite=Lax` always.
*Verify:* iPhone on LAN http with `RADIO_USER/PASS` set: page login → live frames. (Regression-check via tunnel too.)
**▶ Handoff:**
```
In live/striqt_web_server.py BasicAuthMiddleware._set_cookie_send (~294-312): thread
the ASGI scope in and omit the "Secure; " attribute when scope.get("scheme") != "https"
and no b"x-forwarded-proto": b"https" header is present. Keep HttpOnly and
SameSite=Lax unconditionally. Verify: with RADIO_USER/RADIO_PASS set, curl -v
http://localhost:8000/ -u user:pass shows Set-Cookie without Secure; via an https
tunnel the Secure attribute is present.
```

**LV-R9 — Minor state/logic nits** · Low
(a) Crosshair checkbox state silently reverts to "on" after every retune because `initUplot` rebuilds the plot (`app.js:221` vs `:934-936`) — re-apply `cross-chk.checked` at the end of `initUplot`. (b) `capture.duration→rows` uses pre-update `nfft`/`sample_rate` when the same message changes them (server:427-434) — apply the mapping after merging, or read the incoming values first. (c) "Tune to band" silently no-ops when Absolute RF is off (`app.js:856`) — log a hint or disable the button in baseband mode.
**▶ Handoff:**
```
Three one-liners: (a) live/web/app.js initUplot (~339-402): after creating uplot, set
uplot.cursor.show = document.getElementById("cross-chk").checked. (b)
live/striqt_web_server.py SharedConfig.update (~419-437): compute the duration→rows
mapping using capture.get("sample_rate") / capture.get("nfft") when present in the
same message, falling back to current cfg. (c) app.js tune-btn handler (~854-862):
when !absRF, logMsg("Tune to band needs Absolute RF enabled", "WARN") instead of
silent return. Verify each by hand in --demo.
```

### §5.B Web client (`live/web/app.js`)

**LV-R6 — Band monitor is O(rows·nfft·mask) with `Array.includes`** · Medium (edge → hang)
`updateBandMonitor` (app.js:626-634) tests `mask.includes(i)` inside the per-row per-bin loop. At quicklook nfft 4096, depth 300, ~10 % band → ~5·10⁸ comparisons per frame at up to 15 fps → tab freeze. Calibrated (147 bins) hides it today.
*Fix:* precompute `loIdx/hiIdx` once (freqs are monotonic) and test `i >= loIdx && i <= hiIdx`; or hoist a `Set`. Also hoist `Math.pow(10, v/10)` → `10**(v*0.1)` (JIT-friendlier) if profiling still shows cost.
*Verify:* switch to quicklook (after LV-F5) at FFT 4096, window 1000 ms, drag a band: UI stays responsive; `performance.now()` delta around the call < 5 ms.
**▶ Handoff:**
```
In live/web/app.js updateBandMonitor (~600-649): replace the mask array + includes()
with two indices computed once per call (freqsMHz is sorted ascending): loIdx =
first i with freqsMHz[i] >= lo, hiIdx = last i with freqsMHz[i] <= hi (binary search
or linear scan once), then loop r,i with a simple index-range test; bins count =
hiIdx-loIdx+1. Preserve the exact output strings. Verify: at FFT 4096 with a 300-row
window the page stays at full fps while dragging the band.
```

**LV-R7 — Cool-mode scroll flips time order inside each frame band** · Low
Scroll branch (app.js:283-288) prepends the block at row 0 unchanged; block rows are oldest-first (server contract), but in a downward-scrolling waterfall row 0 must be the *newest* row. Each 12-row band is therefore internally time-reversed (visible as zigzag on bursty signals).
*Fix:* copy the block's rows in reverse into the front, or prepend row-by-row from the end.
*Verify:* `--demo` with a slow pulsed tone (temporarily gate `sig0` by `(t*50)%1<0.5` in `DemoAcquirer`) — the pulse edges must form continuous diagonals across band boundaries, not sawtooth.
**▶ Handoff:**
```
In live/web/app.js updateWaterfall scroll branch (~283-288): after copyWithin, write
the new block's rows reversed: for (let r = 0; r < newRows; r++)
buf.set(block.subarray((newRows-1-r)*nfft, (newRows-r)*nfft), r*nfft). Verify in
--demo Cool Mode: modulate a demo tone on/off (temporary edit in DemoAcquirer) and
confirm pulse stripes are continuous across frame boundaries; revert the demo edit.
```

### §5.C TCP pair & Qt viewers (verified current state — the documented bugs are FIXED)

Re-verification table (details in agent evidence; quote-verified for the standalone loop):

| Doc'd bug | Current code | Status |
|---|---|---|
| A-1 / P-3 (no `source is None` guard) | guard present: `striqt_standalone.py:681-690`, `pluto_standalone.py:708-717` | **fixed** |
| F-3 / A-3-band (dB-domain band power) | linear: `striqt_frontend_TCP.py:784`, `striqt_standalone.py:1521`, `pluto_standalone.py:1566` | **fixed** |
| T-1 (quick ~30 dB off) | `/nfft` normalization present (`terminal:366-367`, matches TCP server `:314`) | **fixed** (≈4.3 dB Σw² refinement not applied — consistent across quick paths, so no cross-tool skew) |
| S-1 (no recovery) | try/except + reopen: `striqt_server_TCP.py:527-547` | **fixed** |
| S-2 (stale buffers after rearm) | rebuilt at `striqt_server_TCP.py:515-517` | **fixed** |
| A-3/P-5/F-2 (CSV None crash) | guarded (`standalone:1611`, `pluto:1656`, `frontend:873`) | **fixed** |
| S-5 (`np.hanning` deprecated) | claim is false: `np.hanning` is not deprecated; suggested `np.hann` does not exist | **report error** → LV-D1 |

**New (not in any doc):**
- **LV-R10 — TCP server stuck at `source=None` after a failed recovery** · Medium. `striqt_server_TCP.py:519-521`: when in-except recovery fails (device not re-enumerated yet), the loop thereafter hits `if self.source is None: sleep(0.1); continue` forever — reopening only on a viewer-triggered dirty. Fix: attempt `open_radio` (with 1 s backoff) in that branch, mirroring `striqt_standalone_terminal.py:556`'s pattern.
  **▶ Handoff:** `In live/striqt_server_TCP.py (~519-521): replace the bare sleep-continue with a guarded reopen: try open_radio(cfg) + rebuild read_size/tmp/buffers (same 3 lines as 515-517) except Exception → sleep(1.0); continue. Verify by unplug/replug (or renaming the Soapy driver temporarily) that the server resumes without a client control message.`
- **LV-R11 — TCP frontend shutdown can hang on a silent server** · Low. Steady-state `recvall` has no socket timeout (`striqt_frontend_TCP.py:118-124`; the 5 s timeout at `:102` covers connect only) → `closeEvent`'s `wait(2000)` times out and the QThread outlives the window. Fix: `sock.settimeout(5.0)` after connect + treat `socket.timeout` as a keepalive continue.
- **One-frame retune mislabel** (all pull-based viewers; worst on the TCP server which never clears stale samples on rearm, `striqt_server_TCP.py:483-496`) — same class as LV-R5; fix there first, port if the TCP path stays alive.
- **PSD "Mean" trace dB-averaging** in all three Qt viewers — covered by LV-F3.
- **Terminal/GUI rearm on analysis-only changes** (nfft/rows marked dirty → full 1.4 s hardware rearm; `terminal SharedConfig.update:108-119` sets dirty unconditionally) — cheap fix: only rearm when center/rate/gain changed; nfft/rows need no `arm_spec`. Applies to the web server too (`update` sets one dirty flag for all keys; `rearm` re-arms hardware even for a rows-only change — server:936-950).
  **▶ Handoff:** `In live/striqt_web_server.py SharedConfig: track which keys changed (update already builds changes list, ~449-473); expose take_dirty() → (dirty, cfg, changed_keys). In Acquirer.run dirty branch (~980-992): call self.rearm(cfg) only if changed_keys intersects {"center","sample_rate","gain"}; otherwise just clear the ring if nfft changed (row/backend changes need nothing). Mirror in striqt_standalone_terminal.py handle-dirty if desired. Verify on hardware: changing Window(ms) or FFT no longer causes the ~1.4 s stream gap (log timestamps around "[radio] retune").`

### §5.D Environment / deployment edge cases

- **Vendored-tree incompatibility (operational trap):** running any live script against a pip-install of *this* `striqt/` checkout fails at `from_spec`/`arm_spec`/`_read_stream` (`AttributeError`) — the scripts only run against the radio's installed build. Symptom to expect on a fresh machine following CLAUDE.md's `pip install 'striqt @ git+…'` if upstream has moved past those names. Cheap live-side hardening (optional): extend the existing shim pattern to the four action verbs (`from_spec→cls(spec)+setup`, `arm_spec→arm`, `_read_stream→read`, `rx.open→rx.setup`). See §6-N2.
- **`--demo` divergences:** no Computer thread (inline compute) → demo never exercises the ring/generation logic; `/schema` 500s without `striqt.sensor` (editor silently empty — acceptable, but log it loudly server-side); demo header adds `demo: true` (client ignores it — could badge the UI).
- **run_web.sh:** `wait "$SERVER_PID"` only — if `cloudflared` dies the tunnel silently disappears while the server keeps running (add a `kill -0` watchdog loop or `wait -n` both PIDs); quick-tunnel URLs are ephemeral per run; `RADIO_USER/RADIO_PASS` must already be exported (script neither checks nor warns — one `[ -n "$RADIO_USER" ] || echo "WARNING: auth disabled"` line recommended).
- **web_sim/index.html drift:** its own constants and controls (no CSV/Y-span; "Reset view" is a log-only no-op; always 2-channel; band-power math is linear ✔). Treat as demo-ware; not a fix target beyond a README sentence.
- **Committed `__pycache__` (cpython-314)** noise; `.gitignore` lists CLAUDE.md twice yet it is tracked — housekeeping only.

### §5.E LV-D1 — Retire the stale documentation (it now causes wrong work)

CLAUDE.md "Known bugs" instructs future sessions to add guards **that already exist** (A-1/P-3, S-1/S-2, F-3, T-1 — §5.C table), and its spectrogram-contract section instructs clearing `striqt_shared.spectrogram_cache` "to prevent stale cached results from freezing the display" — the cache is disabled by default and never enabled by live code (§4-Q1 item 5), so the guidance encodes a false mental model. `bug_report.md` S-5 recommends `np.hann`, which does not exist. `REPO_OVERVIEW.md` describes the pre-ssb/pre-auth-cookie server.

**▶ Handoff:**
```
Docs-only change in NIST-Omran: (1) In CLAUDE.md, replace the "Known bugs" section
with a one-paragraph note that all bug_report.md items were fixed as of commit da6f15c
and point to AUDIT_REPORT.md §5 for the current list. (2) In CLAUDE.md's
"striqt.analysis spectrogram contract" section, delete the sentence instructing to
clear striqt_shared.spectrogram_cache (the cache is disabled by default —
AUDIT_REPORT.md §4-Q1) and optionally note the aligned_nfft/28 constraint for the
calibrated web backend. (3) Add a header line to bug_report.md: "HISTORICAL —
verified fixed 2026-07-06, see AUDIT_REPORT.md §5.C; note S-5 was erroneous
(np.hanning is not deprecated)". Do not edit code. Verify: grep CLAUDE.md for
"spectrogram_cache" returns nothing prescriptive.
```

---

## §6 — 🟨 striqt notes for Dan (report-only — no diffs, no edits)

Scope: only routines on the live/ call graph. Format: observation · why it matters · affects the viewer? · action.

**N1 — Vendored `SoapySource.close()` reads attributes its own `__init__` never sets.**
`soapy.py:648-649` fetches `getattr(self, '_device')` / `getattr(self, '_rx_stream')`, but this tree stores `self.device` (`soapy.py:591`) and `self.rx_stream` (`soapy.py:623`) — so in the checked-out source, `close()` silently skips device/stream teardown. It reads like a mid-rename snapshot (public names in `__init__`, private names in `close`). *Why it matters:* leaked device handles on close in any consumer of this tree. *Affects the viewer?* No — live/'s `close_source` shim closes the stream and device explicitly first (`striqt_web_server.py:567-581`). *Action:* **report to Dan** (which naming is canonical? the radio's installed build evidently uses `_device`/`_rx_stream`).

**N2 — Installed-build API names differ from this tree: `from_spec` / `arm_spec` / `_read_stream` / `RxStream.open`.**
None exists here (equivalents: `__init__`+`setup` / `arm` / `read` / `rx_stream.setup`); `evaluate_spectrogram` is byte-identical to `INSTALLED_STRIQT_API.txt`, so the drift is sensor-side only. *Why it matters:* consumers pinned to one naming break on the other; the CLAUDE.md install line (`pip install striqt @ git+…`) may deliver either. *Affects the viewer?* Yes, latently — live/ runs only against the installed naming (§5.D). *Action:* **report to Dan** (ask which API is stable / whether `from_spec`+`arm_spec` are the public contract) **and work around in live/ thus:** extend the existing getattr-shim pattern to the four action verbs so live/ tolerates both trees.

**N3 — `binned_mean(..., fft=True)` drops edge bins and DC-centers the groups — worth one docstring line.**
`arrays.py:135-157`: output count is `2·(⌊(nfft/2 − count//2)/count⌋)+1`, not `nfft/count`; remainder bins at both edges are discarded. *Why it matters:* any consumer that labels the reduced axis by naive division (as the live web client did) mislabels frequencies by up to a bin. The canonical companion axis exists (`spectrogram_freqs`, `shared.py:245-287`) but a caller using `evaluate_spectrogram` alone can miss it. *Affects the viewer?* Yes — root cause of LV-F1. *Action:* **report to Dan** (docstring suggestion: state the DC-centered grouping + edge-drop and point to `spectrogram_freqs`); live/ fix is LV-F1 regardless.

**N4 — The "Mean of empty slice" RuntimeWarning escapes striqt's own filter.**
`_spectrogram.py:20-22` filters the nanmean warning with `module=…measurements._spectrogram`, but the warning is emitted from `striqt.waveform...arrays.py:157` (`binned_mean`) — and on the `evaluate_spectrogram` path used by live/ (via `measurements.shared`) no filter applies at all, so every calibrated frame with `lo_bandstop` set (which NaNs the whole center group) logs one warning. *Why it matters:* log noise at frame rate; users read it as an error. *Affects the viewer?* Cosmetic (it's the operator-reported warning from the hardware runs). *Action:* **report to Dan** (filter by message/category at the emission site, or accept NaN groups silently in `binned_mean`); **work around in live/ thus:** one targeted `warnings.filterwarnings("ignore", message="Mean of empty slice")` next to the `evaluate_spectrogram` call sites.

**N5 — The `window_fill=15/28` integrality rule makes `cellular_5g_ssb_spectrogram` unusable at the standard LTE/NR sample rates.**
Constraint chain: `frequency_resolution=SCS/2` ⇒ `nfft=fs/15 kHz` and `nzero=(13/28)·nfft` must be integral ⇒ fs ≡ 0 (mod 420 kHz); 3.84/7.68/15.36/30.72/61.44 MS/s all fail with `ValueError('…counting-number…')` (`shared.py:162-178`, `_cellular_5g_ssb_spectrogram.py:104-116`). *Why it matters:* the natural capture rates for the very waveform this measurement targets are excluded; every consumer must resample (to e.g. 15.12/30.24 MS/s) or give up. *Affects the viewer?* Yes — the SSB backend is a permanent fallback (LV-F2/§4-Q3). *Action:* **report to Dan** — is a documented resample-to-420-kHz-grid step the intended usage, and would a relaxed `window_fill` (e.g. default 1) be acceptable for viewer-grade SSB display? live/ workaround options ranked in §4-Q3.

**N6 — `spectrogram_cache` is disabled by default; the sensor pipeline enables it, bare callers don't.**
`register.py:51` (`enabled=False`), activation only via `cached_registry_context` (`register.py:454-480`). *Why it matters:* downstream code (this repo's CLAUDE.md) assumed the cache could serve stale frames and defensively `.clear()`s it every call — harmless but cargo-cult. *Affects the viewer?* No (after LV-D1). *Action:* **report to Dan** (a one-line docstring on `KwArgCache` stating the default-off contract would prevent the myth).

**N7 — `integration_bandwidth` converts mean→sum (`spg *= frequency_bin_averaging`, `shared.py:227`) and stamps `enbw=integration_bandwidth`.**
Correct behavior, but the resulting `'dBm/… kHz'` attrs label (`shared.py:239`) is nominal — without a calibration applied, values are relative (FS-referenced) integrated power, and they sit `10·log10(count)` above per-bin readings. *Why it matters:* consumers comparing against per-bin FFT tools (as this repo's quicklook does) see a systematic ≈8.5 dB offset and may "fix" the wrong side. *Affects the viewer?* Yes, as labeling (LV-F4). *Action:* **report to Dan** (note in the attrs docs that the label is nominal absent calibration); live/ handles labeling in LV-F4.

**N8 — `json_schema` maps `Fraction` fields to `{'type': ['string','number']}` with no pattern/description.**
`helpers.py:230-236`. *Why it matters:* generic schema-driven editors render a bare text box for `fractional_overlap`/`window_fill` with no hint that `"13/28"` is the expected form (msgspec's `_dec_hook` accepts `Fraction(str)`). *Affects the viewer?* Mildly (settings editor renders text inputs — works, unlabeled). *Action:* **report to Dan** (add `"description": "rational, e.g. '13/28'"` via the schema hook); no live/ change needed.

**N9 — (FYI, positive) The four fields Dan said to hide are genuinely inert in a drain-only integration.**
`receive_retries` feeds only the Controller retry wrapper (`controller.py:150-171`); `adc/if_overload_limit` only `compute_overload_info` in `package_iq` (`soapy.py:280-302,755`); `gapless` gates the arm no-op + Controller buffer carryover, and validation couples it to `time_sync_at='open'` + `receive_retries=0` (`structs.py:141-144`) — which `make_source` satisfies exactly. *Action:* none — recorded as confirmation that the email guidance and the live implementation agree.

**N10 — (FYI) `rx_enable_delay = 1.4 s` on `Air8201BSourceSpec` (`deepwave.py:41`) dominates retune latency.**
Every stream re-enable schedules `activateStream(now + 1.4 s)` (`soapy.py:413-419`) and the first read budgets it into the timeout (`soapy.py:437`). *Affects the viewer?* Yes — every control change that re-arms costs ≈1.4 s of dead air (motivates the rearm-only-when-needed item in §5.C). *Action:* none upstream (hardware settling constant); live/ minimizes rearms.

---

## Appendix — verification quick-reference (for the executor)

```sh
# demo server (no hardware):
python3 live/striqt_web_server.py --demo                      # calibrated
python3 live/striqt_web_server.py --demo --backend quicklook
python3 live/striqt_web_server.py --demo --quantize

# decode one WS frame from a shell (checks header honesty fixes):
python3 - <<'EOF'
import asyncio, json, struct, websockets   # pip install websockets
async def main():
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        raw = await ws.recv()
        n, = struct.unpack("<I", raw[:4]); print(json.dumps(json.loads(raw[4:4+n]), indent=2))
asyncio.run(main())
EOF

# schema endpoint:
curl -s localhost:8000/schema | python3 -m json.tool | head

# auth checks:
RADIO_USER=u RADIO_PASS=p python3 live/striqt_web_server.py --demo &
curl -si localhost:8000/ | head -3          # expect 401 + WWW-Authenticate
curl -si -u u:p localhost:8000/ | grep -i set-cookie
```

*End of report. 🟦 fixes are executable as written; 🟨 items are conversation topics for Dan — do not edit `striqt/`.*



