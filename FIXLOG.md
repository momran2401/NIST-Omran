# FIXLOG.md — AUDIT_REPORT.md fixes applied in the sandbox

One entry per fix (one commit each). Each entry: what changed + the **Verify** step
(copied verbatim from the finding), tagged `[demo]` (checkable with `--demo` on port 8001)
or `[hardware]` (needs the real AIR8201B SDR). Human runs the demo tests on the radio.

---

## Cluster 1 — Cadence (the slideshow fix)

### LV-W1 — Fix `rowsForWindow`: track radio nfft separately from header bins; hop-aware window→rows
**File:** `live/web/app.js`
**Changed:** Added module-level `radioNfft` (the requested radio FFT size), updated **only** by
the `#nfft-sel` change handler and never from frame headers. Renamed `curNfft`→`curBins`
(per-frame block bin count from the header) at every site set from a frame header;
`buildFreqsMHz`/`updateMeta`(FFT label)/PNG-caption keep `curBins`, while
`rowsForWindow`/`computeDisplayDepth`/`rowsForCurrentSettings` now use `radioNfft`. Rewrote
`rowsForWindow(fs, radioNfft, windowMs, hopFrac)` → `rows = windowMs/1000·fs/(radioNfft·hopFrac)`
clamped `[1,300]`, added `backendHopFrac()` (=1 for quicklook, 15/28 otherwise), and fixed
`updateMeta`'s `winMs = depthRows·radioNfft·hopFrac/curFs·1e3`. Threaded the new args through
the connect, rate-sel, nfft-sel, and win/mode call sites.

**Verify [demo]/[hardware]:** run `--demo --backend quicklook`; open dev tools → WS frames: the
first control message after connect must carry `rows` = `round(0.02·15.36e6/1024)=300` for
quicklook but ≈ 160 (hop-aware) for calibrated with the same settings; server log
`[config] rows: 12 -> N` shows the new N. On hardware: `[radio] IQ …` cadence and frame rate in
the meta line should jump from ~0.2 fps to multiple fps (with LV-W3).

### LV-W3 — Snap calibrated nfft to smooth 28-multiples {252, 504, 1008, 2016, 4032}
**File:** `live/striqt_web_server.py`
**Changed:** Replaced `aligned_nfft`'s `round(n/28)·28` (which produced slow non-power-of-2 sizes
like 1036=2²·7·37 and 2044=2²·7·73) with a nearest-by-absolute-distance snap to
`ALIGNED_NFFTS = (252, 504, 1008, 2016, 4032)` — all ÷28 (satisfies the 15/28 window-fill
integrality), all ÷12 (so `averaging_factor` returns 12 at every FFT setting — restores Dan's
consistent bin-averaging), all 7-smooth (2^a·3^b·7 → fast FFT). `averaging_factor` and the
`Spectrogram` spec construction are unchanged. Unit-checked locally: every aligned size is
divisible by 28 and 12; requested {256,512,1024,2048,4096} → {252,504,1008,2016,4032}, all with
averaging factor 12.

**Verify [demo]/[hardware]:** unit-check in a REPL: `all(n % 28 == 0 and n % 12 == 0 for n in
ALIGNED_NFFTS)` (done here, passes); run `--demo --backend calibrated` and confirm header `nfft`
(bins) is 83 at FFT 1024 and the meta fps rises; on hardware time one `calibrated_spectrogram`
call before/after (wrap with `time.perf_counter` temporarily) — expect ≥3× faster at 1008 vs
1036 for the same rows.

### LV-W2 — Right-size STFT rows/samples (stop computing ~1.87× the rows we display)
**File:** `live/striqt_web_server.py`
**Changed:** Added helper `calibrated_sample_count(nfft, rows) = rows·hop + (nfft-hop)`,
`hop = nfft·15//28`. `samples_needed` now uses it for the `calibrated`/`ssb` base (was
`nfft·rows`); quicklook stays `nfft·rows`; the SSB discovery-period `max()` floor is unchanged.
**Also right-sized `calibrated_spectrogram`'s internal `needed`** from `rows·nfft` to the same
helper — required, and beyond the samples-only handoff: `calibrated_spectrogram` re-derives
`needed = rows·nfft` and pads/truncates to it, so cutting only `samples_needed` would have
zero-padded the newest rows into garbage (the finding's own risk note assumes the reduced count
reaches `evaluate_spectrogram`). Proven locally against striqt's real striding row count
(`sliding_window_view(x, nfft)` stepped by `hop`): `floor((N-nfft)/hop)+1` == `rows` **exactly**
for every aligned nfft at rows ∈ {12,60,300}, with ~46% fewer samples/FFTs at 300 rows — so no
front-pad and frames are unchanged.

**Verify [demo]/[hardware]:** demo: displayed rows unchanged (header `rows` identical), server
CPU per frame drops (log timestamps); assert in a REPL that `evaluate_spectrogram` on the new
sample count returns ≥ rows STFT rows (it returns exactly `int((28/15)(N/nfft−1))+1`; with N as
above that is ≥ rows). [demo] confirms frames identical + header rows unchanged; [hardware]
confirms the per-frame CPU drop.

---

## Cluster 2 — Fidelity & labeling

### LV-F1 — Ship the true frequency axis in the header; use it in the client
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** The calibrated axis was invented client-side (`(g−73.5)/147·fs`), wrong by up to
~103 kHz because striqt's DC-centered bin grouping drops edge bins. Now the server ships the true
axis. Backend functions (`db_spectrogram`/`calibrated_spectrogram`/`ssb_spectrogram`) return
`(blocks, meta)` with `meta = {fft_nfft, bin_avg}`; `compute_blocks` returns `(blocks, meta)`
(adds executed `backend`); new `build_header(cfg, blocks, meta, demo)` centralizes the header and
emits `fft_nfft`, `bin_avg`, `freqs_hz_step = bin_avg·fs/fft_nfft`, and `freqs_hz_f0` (=`-fs/2` for
quicklook's fftshift convention, `-(bins-1)/2·step` for the DC-centered calibrated/ssb grid).
`Acquirer.publish`/`DemoAcquirer._publish` and their call sites thread `meta`. Client:
`buildFreqsMHz` prefers header `f0`/`step` (falls back to the old formula for old servers); axis
rebuilds when `freqs_hz_step` changes. Verified locally: calibrated DC bin sits exactly at center;
+2.5 MHz demo tone lands within ½-step (60 kHz < 91 kHz) of 2.5 MHz; quicklook axis byte-identical
to the old formula (stays exact).

**Verify [demo]:** `--demo` (demo tones at exactly +2.5 MHz / −1.8 MHz on ch0): before, the
calibrated peak marker read ≈ 2.45 MHz offset; after, 2.5 MHz ± ½ step. Browser console: the
DC bin `freqsMHz[(curBins-1)/2]*1e6 - curCenter` must be ~0 in calibrated mode (with LV-W3's
83-bin grid the DC bin is index 41); quicklook stays exact.

### LV-F2 — Honest backend reporting (SSB fallback flag; disable SSB at incompatible rates)
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** `compute_blocks` now pre-checks `ssb_grid_compatible(cfg.sample_rate)` when the
requested backend is `ssb`; when false it skips the doomed striqt call (which used to raise +
fall back every frame) and runs calibrated directly, recording `meta["backend"]="calibrated"`
and `meta["backend_requested"]="ssb"`. `build_header` (from LV-F1) already emits
`backend_requested` only when it differs from the executed `backend`. Client: `onFrame` warns
once per change (`setStatus(..., "warn")` + log) when `backend !== backend_requested`; the meta
"analysis" field now renders the executed `curBackend` (was the lying dropdown value); added
`ssbGridCompatible(fs)` (mirrors the server test) + `updateSsbOption()` which disables the SSB
`<option>` with a 420 kHz-grid tooltip when the current rate can't deliver it (called on tuning
change and at bootstrap). Confirmed locally: all four selectable rates (3.84/7.68/15.36/30.72)
are incompatible → SSB disabled + fallback reported; the 420 kHz rates (15.12/30.24) would be
compatible but aren't selectable.

**Verify [demo]:** `--demo --backend ssb`: header decodes to `backend:"calibrated"` with
`backend_requested:"ssb"`, the meta shows `calibrated` + a warning status line; switching rates
keeps it consistent; no striqt `ValueError` spam in the server output.

### LV-F3 — PSD "Mean" trace: average in linear power, not dB
**File:** `live/web/app.js`
**Changed:** `psdSeries` accumulated the mean trace as an arithmetic mean of dB values (biased
low for fluctuating signals). Now accumulates `10**(v/10)` per row and converts back with
`10·log10(max(mean/depth, 1e-20))`, mirroring the band monitor's (already-correct) linear
convention. Max/min traces are order statistics — left unchanged.
**Deferred (optional in the finding):** the same one-line change in the Qt viewers
(`striqt_standalone.py:1544`, `striqt_frontend_TCP.py:807`, `pluto_standalone.py`) — Qt-only,
not exercisable on port 8001. Ask if you want it applied.

**Verify [demo]:** band monitor "RX1 x.x dB" over a dragged band equals the average of the
fixed mean trace over the same band within ~0.5 dB (previously the mean trace read lower).

### LV-F4 — Truthful labels: FFT vs bins, hop-aware window, units on PSD/CSV/PNG
**File:** `live/web/app.js` (no `index.html` change needed — the y-axis label is JS-driven)
**Changed:** Added client state `curFftNfft`/`curBinAvg` (from the LV-F1 header fields).
(1) `updateMeta` prints FFT as `${radioNfft}→${curFftNfft} (${curBins} bins × ${curBinAvg})` for
calibrated/ssb and plain `${radioNfft}` for quicklook; the window ms is already the LV-W1
hop-aware value. (2) New `psdYLabel()` sets the uPlot y-axis to "Integrated power (dB rel. FS)"
for calibrated/ssb vs "Power (dB rel. FS / bin)" for quicklook (re-applied when `initUplot`
rebuilds on backend/tuning change). (3) `savePsdCsv` prepends `# backend=`, `# fft_nfft=`,
`# bin_avg=`, `# units=dB (uncalibrated, band-integrated|per-bin)` comment lines. (4) `exportPng`
caption uses the real FFT size and the true hop-aware window ms.

**Verify [demo]:** switch backends in `--demo` and confirm the label changes and the ~8.5 dB
level shift is now explained on-screen; export CSV and check the header comment lines.

### LV-F5 — Expose Quicklook in the Analysis dropdown; rename PSD → "PSD view"
**Files:** `live/web/index.html`, `live/web/app.js`
**Changed:** Added `<option value="quicklook">Quicklook (raw FFT)</option>` to `#analysis-sel`
and extended `applyAnalysisMode` to send `{backend:"quicklook"}` for it (server already supports
quicklook via `BACKENDS`). Renamed the "PSD" option to "PSD view" (it only hides waterfalls; the
backend stays calibrated). Did **not** implement the striqt `power_spectral_density` backend this
pass (per the finding).

**Verify [demo]:** selecting Quicklook changes `header.backend` to `"quicklook"`, the DC spike
appears (no LO null), and levels drop ~8–9 dB (per-bin vs band-integrated — now labeled by LV-F4).

### LV-F6 — Settings editor: send hidden sweep params; ack applied/ignored/reconnect
**Files:** `live/striqt_web_server.py`, `live/web/app.js`, `live/web/index.html`
**Changed:** (1) `app.js` #settings-apply now deep-merges `hiddenSweepSettings` (the uploaded sweep
JSON's `captures[0]`/`source`) under the visible form values (form wins) before sending, so the
"hidden lower-level parameters" are actually applied instead of dropped. (2) `SharedConfig.update`
returns an ack `{applied, ignored, reconnect}` — `applied` = internal cfg keys that changed,
`ignored` = capture fields with no live mapping (analysis_bandwidth, port, lo_shift, host_resample,
backend_sample_rate), `reconnect` = source fields minus the skip set; `ws_endpoint` sends it back
as a `{message}` text frame **only** for settings-editor applies (messages containing `capture`/
`source`), which `app.js` already logs — avoids spamming the log on every slider change. (3)
`index.html` source badge → "requires reconnect — not applied live". Confirmed the ack lists
compute correctly on a representative payload.

**Verify [demo]:** upload a sweep JSON (Dan's §4 sample; `context/` is not in this sandbox clone,
so any valid sweep JSON with a `center_frequency` works) via Load JSON, press Apply, and check the
browser Log panel lists the three categories (applied/ignored/reconnect-only) and the radio
retunes to the file's `center_frequency`.

### LV-F7 — Populate the waterfall frequency axis (empty `.wf-freq-axis` overlay)
**Files:** `live/web/app.js`, `live/web/style.css`
**Changed:** The `.wf-freq-axis` overlay divs were styled but never populated. Added
`renderWfAxis()` which fills both divs with 5 evenly spaced ticks from the true axis
(`freqsMHz`, LV-F1) as `<span>` elements plus a right-aligned `↕ N ms` span for the current
hop-aware window (LV-W1). Called from the end of `updateMeta` (every frame, after `wfBuf` is
current) and from the Absolute-RF handler. Styled `.wf-freq-axis` as a `space-between` flex row
with a text-shadow for legibility over the waterfall (window span highlighted).

**Verify [demo]:** labels appear under each waterfall and match the uPlot PSD x-axis extremes;
changing center/span/Absolute-RF updates them.

### LV-F8 — DC-null: proportional width, optional toggle, unconditional NaN scrub
**Files:** `live/striqt_web_server.py`, `live/web/index.html`, `live/web/app.js`
**Changed:** `fit_display_rows` now sizes the LO-null half-width as
`max(1, ceil((SSB_LO_BANDSTOP/2) / (bin_avg·fs/fft_nfft)))` bins (was a fixed ±2 bins that hid up
to ~3.7 MHz at coarse FFTs) — threaded `bin_avg`/`fft_nfft`/`sample_rate`/`lo_null` from the
calibrated/ssb backends. It **always** scrubs any remaining NaNs (striqt's `null_lo` leaves an
all-NaN DC group) to the per-row min, so the quantizer/client never see NaN garbage even with the
null disabled. Added `lo_null` to `RadioConfig`+`SharedConfig.update` (WS-controllable, default
True) gating only the overwrite, and a "LO null" checkbox in the Display group wired to send
`{lo_null}`. Unit-tested: null shrinks 5→3 bins at FFT 1024, and no NaN leaks with the null on
**or** off (no RuntimeWarning).

**Verify [demo]:** calibrated mode — the checkbox toggles the center stripe; at FFT 256 the null
shrinks from 5 bins toward the minimum; with the null off, no NaN garbage appears under
`--quantize`.

---

## Cluster 3 — Robustness bugs

### LV-R1 — Catch bare `RuntimeError` from the Soapy driver in the acquirer read loops
**Files:** `live/striqt_web_server.py`, `live/striqt_standalone.py`, `live/pluto_standalone.py`,
`live/striqt_standalone_terminal.py`
**Changed:** SoapySDR's C++ layer can surface a bare `RuntimeError` mid-read; an uncaught one
unwinds the acquirer thread and freezes the viewer permanently. Widened the read-loop `except`
from `(ReceiveStreamError, OverflowError, OSError)` to add `RuntimeError` in all four scripts, so
a driver fault routes to the existing `_recover`/retry path instead of killing the thread.

**Verify [hardware]:** monkeypatch `_read_stream` to raise `RuntimeError("injected")` once (a
temporary shim around the read in a dev run); the log shows `[radio] recovering after: injected`
and frames resume. (Not reproducible in `--demo`, which uses `DemoAcquirer` with no read loop.)

### LV-R2 — Harden the WS control channel (validate/snap; never let bad JSON kill the viewer)
**File:** `live/striqt_web_server.py`
**Changed:** (1) `ws_endpoint` now splits the receive from the parse: `receive_text` timeout is a
keepalive `continue`, and `json.loads` + `_shared.update` (+ the ack send) are wrapped in
`except (json.JSONDecodeError, ValueError, TypeError, AttributeError)` → replies
`{"message":"bad control ignored: …"}` and keeps looping, so one malformed message can't drop the
only viewer. (2) `SharedConfig.update` snaps `sample_rate` to the nearest of
`RATES_HZ = (3.84,7.68,15.36,30.72) MHz` and `nfft` to the nearest of
`NFFT_CHOICES = (256,512,1024,2048,4096)` (new `_snap` helper) before the existing clamps, so an
off-list value can't reach `arm_spec` or trip the calibrated `ValueError` guard. Verified snapping:
9 MS/s→7.68, 30.72M still retunes, 700→512.

**Verify [demo]:** in the browser console, `ws.send("garbage")`, `ws.send('{"center":"abc"}')`,
and `ws.send('{"sample_rate":9e6}')` leave the stream running and log an ignore message;
`ws.send('{"sample_rate":30720000}')` still retunes.

### LV-R3 — Single-viewer: close the accept race, liveness ping + takeover, distinct close codes
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** Wrapped the single-viewer slot check + accept in a module `asyncio.Lock` (`_slot_lock`)
so two interleaving handshakes can't both pass the empty check. Busy refusals now use a distinct
**4001** close code (vs 1008 for auth). Added a liveness probe: on each 15 s receive timeout the
server sends a `{"message":"ping"}`; two consecutive send failures drop the client, freeing the
slot within ~30 s so a waiting viewer's 1.2 s reconnect loop takes over. Client `onclose` inspects
`event.code`: 1008 → "authentication failed — reload" and **stops** reconnecting; 4001 → "another
viewer is connected — retrying…"; else the existing reconnect.
**Deviation from the handoff:** for the busy refusal I `accept()` then `close(4001)` rather than
closing before accept — a close *before* accept aborts the WS handshake and the browser sees 1006
(abnormal), so the 4001 code would never reach the client; accept-then-close delivers it. The 2nd
socket is never added to `_connections`, so the broadcaster never sends to it.

**Verify [demo]:** two browser tabs — the second shows the busy state and takes over within ~2 s of
closing the first; kill the holder's network (devtools offline) and the slot frees within ~30 s;
a wrong-password reload shows the auth state instead of silent retry.

### LV-R4 — NaN-safe quantizer
**File:** `live/striqt_web_server.py`
**Changed:** `serialize_frame`'s quantize path used `np.percentile` and plain subtraction — a single
NaN made `vmin`/`vmax` NaN and turned the whole uint8 frame to garbage. Switched to
`np.nanpercentile` (with an all-NaN `-100..0` fallback) and `np.nan_to_num(block, nan=vmin)` before
scaling. Complements LV-F8's upstream scrub (which keeps calibrated/ssb NaN-free) as
defense-in-depth for any future NaN-producing config. Simulated: NaN pixel maps to the vmin bucket,
frame renders; all-NaN block hits the fallback.

**Verify [demo]:** temporarily set `blocks[0][0,0]=np.nan` in `compute_blocks` and run
`--demo --quantize` → the waterfall still renders normally (previously all-noise); remove the
injection.

### LV-R5 — Suppress zero-padded / mislabeled frames across a retune (generation tag)
**File:** `live/striqt_web_server.py`
**Changed:** Added `Acquirer._gen`, bumped inside `_clear_ring_locked` (i.e. on every retune/recover
ring clear). `get_latest` now returns `(out, gen, avail)`; new `generation()` reads it under the
lock. `Computer.run` snapshots `g0 = generation()` before `get_latest` and skips the frame when
`gen != g0` (ring cleared mid-read) or `avail < need` (ring hasn't refilled) — so no dark
zero-padded rows flash and no frame is computed from a ring cleared under it. `DemoAcquirer`
(no ring) is untouched.
**Residual (acknowledged in the finding):** a ≤1-frame (~one read cycle) window remains where the
SharedConfig is updated but the Acquirer hasn't yet processed the dirty flag to clear the ring;
fully closing it would require tagging ring samples with their cfg. The dark-flash (zero-pad) case
is fully eliminated.

**Verify [hardware]:** not reproducible in `--demo` (no ring). On hardware, rapid center changes —
the waterfall must never flash dark rows nor show the previous band's energy under the new center
label.

### LV-R6 — Band monitor: replace `mask.includes()` scan with an index range
**File:** `live/web/app.js`
**Changed:** `updateBandMonitor` built an in-band index array and tested `mask.includes(i)` inside
the per-row/per-bin loop — O(rows·nfft·bins), ~5·10⁸ ops/frame at nfft 4096 → tab freeze. Since
`freqsMHz` is sorted ascending, it now computes `loIdx`/`hiIdx` once and uses an O(1) index-range
test; `nBins = hiIdx-loIdx+1`. Output strings unchanged.

**Verify [demo]:** switch to Quicklook at FFT 4096 with a 300-row window and drag the band — the
page stays at full fps (`performance.now()` delta around the call < 5 ms).

### LV-R7 — Cool-mode scroll: fix within-block row order
**File:** `live/web/app.js`
**Changed:** The scroll branch of `updateWaterfall` prepended each new block unchanged, but the
block is oldest-first while row 0 of a downward-scrolling waterfall must be the newest row — so
each 12-row band was internally time-reversed (zigzag on bursty signals). Now the block is written
reversed into the front (`buf.set(block.subarray((newRows-1-r)*nfft, (newRows-r)*nfft), r*nfft)`),
making time continuous across band boundaries.

**Verify [demo]:** in Cool Mode, temporarily modulate a demo tone on/off in `DemoAcquirer` — pulse
stripes must form continuous diagonals across frame boundaries, not a sawtooth; revert the edit.

### LV-R8 — Drop the `Secure` session cookie over plain HTTP (iOS/Safari LAN lockout)
**File:** `live/striqt_web_server.py`
**Changed:** `BasicAuthMiddleware._set_cookie_send` now takes the ASGI `scope` and omits the
`Secure;` cookie attribute unless the request is HTTPS (`scope["scheme"] == "https"` or
`x-forwarded-proto: https`). Over `http://<radio-ip>` on the LAN, Safari/iOS refused to store a
`Secure` cookie *and* won't replay Basic on the WS upgrade — locking out exactly the clients the
cookie exists for. `HttpOnly` and `SameSite=Lax` stay unconditional; via the HTTPS tunnel `Secure`
is still present. Verified the decision table (plain-http→no Secure; https/tunnel→Secure).

**Verify [demo]:** with `RADIO_USER`/`RADIO_PASS` set, `curl -v http://localhost:8001/ -u user:pass`
shows `Set-Cookie` without `Secure`; via an HTTPS tunnel the `Secure` attribute is present. On an
iPhone on LAN http, page login → live frames.

### LV-R9 — Minor state/logic nits (3 one-liners)
**Files:** `live/web/app.js`, `live/striqt_web_server.py`
**Changed:** (a) `initUplot` re-applies `uplot.cursor.show = cross-chk.checked` after rebuilding the
plot, so the crosshair toggle no longer silently resets to "on" on every retune. (b)
`SharedConfig.update` maps `duration→rows` using `capture.get("sample_rate")`/`capture.get("nfft")`
from the same message when present (falling back to current cfg), instead of the pre-update values.
(c) The "Tune to band" handler logs `"Tune to band needs Absolute RF enabled"` when Absolute RF is
off, instead of silently no-op'ing.

**Verify [demo]:** (a) toggle the crosshair off, change center — it stays off. (b) via Load JSON +
Apply, a sweep that sets both sample_rate and duration yields the rows count for the new rate. (c)
turn off Absolute RF and click Tune to band — the log shows the warning.

---

## Cluster 4 — UX/parity + docs

### LV-U1 — Web parity extras: Max-fps throttle, per-channel peak markers, harmonized gain range
**Files:** `live/web/index.html`, `live/web/app.js`
**Changed:** (a) Added a "Max fps" select (15/10/5/2/1, default 15) to the Display group; `onFrame`
parses the header then skips the block parse + render when `now - lastRender < 1000/maxFps`, so the
meta fps reflects the actual (throttled) render rate. (b) Peak marker now renders one marker per
visible channel — `drawPeakMarker(s1x, s2x, freqArr)` computes the strongest bin for each of RX1/RX2
max (null-safe), drawn in `COL.rx1Max`/`COL.rx2Max` with "RX1"/"RX2" labels (was RX1-only with a
generic label). (c) `#gain` range changed from −30…0 to **−60…+10** to match the server clamp, with
a "AIR-T RX gain" tooltip. (Finding's (d) README parity note is not in the handoff prompt — skipped.)

**Verify [demo]:** the fps throttle changes the meta fps; both peak markers track their channels;
the gain spinner accepts −60…10.

### LV-D1 — Retire the stale documentation
**Files:** `CLAUDE.md`, `bug_report.md` (docs only — no code)
**Changed:** (1) Replaced CLAUDE.md's "Known bugs" list (which described the pre-fix state and told
sessions to re-add guards that already exist) with a note that all `bug_report.md` items were
verified fixed 2026-07-06 (`AUDIT_REPORT.md` §5.C), pointing to `AUDIT_REPORT.md` §2/§5 and
`FIXLOG.md` for the current backlog, and flagging S-5 as erroneous. (2) In the "spectrogram
contract" section, removed the prescriptive "Always clear `striqt_shared.spectrogram_cache`"
sentence (the cache is disabled by default → the `.clear()` calls are no-ops) and documented the
`aligned_nfft`/28 constraint instead. (3) Added a HISTORICAL banner to the top of `bug_report.md`.
The commit hash `da6f15c` from the handoff does not exist in this repo, so I referenced the audit
verification date/section instead.

**Verify [docs]:** `grep spectrogram_cache CLAUDE.md` returns only the debunking note (nothing
prescriptive); the "Always clear …" instruction is gone; `bug_report.md` starts with the HISTORICAL
banner. (Confirmed here.)
