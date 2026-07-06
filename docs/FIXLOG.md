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

---

# Phase 1 — Consolidate the control surface

Make the schema-driven Capture Settings form the single place to tune the radio: retire the
redundant "Radio (AIR-T)" bar, wire four rendered-but-ignored capture fields, switch the time
control to a duration, and remove the artificial 300-row cap that made duration inert. One
change per commit (`P1-1 … P1-5`). Edits under `live/` only; `striqt/` untouched.

### P1-1 — LO-null checkbox sense (verified already correct — no inversion)
**Files:** `docs/FIXLOG.md` (docs only — no code change)
**Finding as briefed:** `#lo-null` was said to behave backwards (checked *shows* the spike,
unchecked *hides* it). **Verified in the current tree it is already correct** and needs no change:
the checkbox is `checked` by default, its handler sends `{lo_null: e.target.checked}`, `RadioConfig.lo_null`
defaults `True`, and `fit_display_rows` overwrites the center bins with the per-row min **only when
`lo_null` is truthy**. So `checked ⇒ lo_null=true ⇒ spike hidden` already holds — this is the
LV-F8 behaviour. A deterministic check of the null logic confirms it: with a bright center bin,
`lo_null=True` drives that bin to the row minimum (hidden) and `lo_null=False` leaves it at 0 dB
(visible). **Inverting the sense would re-break it** (checked would reveal the spike), so no code
was changed — this matches CLAUDE.md's warning that the old "known bugs" notes describe a pre-fix
state. The box stays checked by default.

**Verify [demo]:** calibrated mode — box checked → center stripe blanked; unchecked → the LO
spike (real hardware DC leakage) is visible at band center. In `--demo` (no synthetic DC tone)
the center stripe darkens to the row-min when checked and shows noise when unchecked.

### P1-2 — Wire the four "dead" capture fields so they actually apply
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** `make_capture` used to hardcode `host_resample=False`,
`analysis_bandwidth=inf`, `lo_shift="none"`, `backend_sample_rate=sample_rate`, so those four
schema-editor fields were rendered but ignored. Added them to `RadioConfig` (defaults reproduce
the old hardcoded values: `analysis_bandwidth=inf`, `lo_shift="none"`, `host_resample=False`,
`backend_sample_rate=0.0` where 0 ⇒ "track sample_rate") and to `snapshot()`. In
`SharedConfig.update` they pass through the `capture` branch (same field names → `mapped`), were
added to `capture_mapped` (so they're no longer reported as "ignored") and to `valid`, with light
validation: `analysis_bandwidth` must be `>0` or `inf`; `lo_shift` ∈ `{left,right,none}` (read
from striqt `types.LOShift = Literal['left','right','none']`); `host_resample` coerced to bool;
`backend_sample_rate` `0` or a positive float. Changing any sets `_dirty` (takes effect on the
next re-arm, like `sample_rate`). `make_capture` now reads `cfg.analysis_bandwidth`,
`cfg.lo_shift`, `cfg.host_resample`, and `cfg.backend_sample_rate or cfg.sample_rate`. **`port`
stays fixed at `CHANNELS`** (two-waterfall UI depends on it) and is **excluded** from the editable
`captureFields` list in `app.js`; the four wired fields remain editable.
**Note:** striqt raises `ValueError('lo_shift requires host_resample=True')` if `lo_shift != none`
without `host_resample`; that coupling is left to the operator (a bad combo surfaces as a compute
error in the server log, not a crash — `compute_blocks` is wrapped).

**Verify [hardware]:** changing `lo_shift` moves where the center notch sits; narrowing
`analysis_bandwidth` trims the band edges. **Verify [demo]:** apply values from the Capture form —
the `applied` ack lists `analysis_bandwidth/lo_shift/host_resample/backend_sample_rate`, `ignored`
no longer lists them, and no error occurs (a local `SharedConfig.update` check confirms valid
values are applied and `lo_shift="sideways"` / `analysis_bandwidth=-3` are rejected).

### P1-3 — Delete the "Radio (AIR-T)" bar; move FFT into the Capture panel
**Files:** `live/web/index.html`, `live/web/app.js`
**Changed:** Removed the entire `#radio-ctrl` control group (`center-mhz`+`center-btn`,
`rate-sel`, `gain`+`gain-btn`, `nfft-sel`, `tune-btn`) and its now-dead handlers in `app.js`
(center apply/Enter, rate-sel, gain apply/Enter, tune-btn). Center / span (sample_rate) / gain are
already settable from the schema Capture Settings form (they map to live params in
`SharedConfig.update`), so they need no new home. **FFT** got a new home: a static `FFT size`
`<select>` (256/512/1024/2048/4096) in the DAN-mode Capture panel (`#settings-panel`, `pro-only`),
placed outside the schema-rendered form (which is cleared on reload) so it isn't wiped. It keeps
the id `#nfft-sel`, so the existing change handler — the sole updater of `radioNfft`, sending
`{nfft}` (server snaps+validates) — is retained. **"Tune to band" is gone** in Phase 1; the PSD
band-drag selection stays (still drives the band monitor). The NOOB station tuner's
`getElementById("center-mhz")` is already guarded by `if (input)` and tunes via
`sendControl({center})` directly, so it is unaffected by the input's removal.

**Verify [demo]:** the top Radio bar is gone; setting center/rate/gain from the Capture form and
FFT from the new select retunes the radio (the `applied` ack lists them; waterfall/labels update).
Confirmed structurally: served HTML has no `radio-ctrl`/`tune-btn`; `#nfft-sel` lives in
`.settings-static`; a WS `{nfft:2048}` yields frames with `nfft=2048`.

### P1-4 — Replace Window (ms) with a Duration control (preset + custom)
**Files:** `live/web/index.html`, `live/web/app.js`
**Changed:** Removed the `Window (ms)` `#win-sel` control and its handler. Added a **Duration (ms)**
control to the Display row: a `#dur-sel` `<select>` of presets **2/5/10/20/50/100 ms** plus a
final **custom…** option, and a `#dur-custom` number box revealed when custom is chosen. The
preset select is available in **both** modes; the `custom…` option and the number box are
`pro-only` (DAN only). Duration is the **single owner of the time axis**: `applyDuration()` sets
the existing `windowMs` (ms), which drives the display window via the hop-aware
`rowsForCurrentSettings()` exactly as the old Window control did — so the `↕ N ms` waterfall-axis
label and the meta `window … ms` track the selection. In replace (Boring) mode it sends
`{rows}`; in scroll (Cool) mode the client depth follows `windowMs`. `duration` was removed from
the schema-form `captureFields` so exactly one duration input owns `rows` (kills the old
Window-vs-duration fight).
**Deviation (noted):** the control sends `{rows}` (client-computed, hop-aware) rather than a raw
`{duration: seconds}` message. The server has no hop-aware duration→rows mapping (its
`capture.duration` path uses `duration·fs/nfft` with **no** 15/28 hop factor), so sending raw
duration would desync the `↕ ms` label from the real window and break the LV-W1/LV-W2 time-axis
contract. `{rows}` is the proven, contract-preserving path; `cfg.rows` still drives the radio's
capture `duration` in `make_capture`, so the hardware captures the right span.
**Depends on P1-5:** presets above ~10–20 ms exceed the old 300-row cap and are clamped until
P1-5 raises it; after P1-5 the full 2→100 ms range renders honestly.

**Verify [demo]:** picking each preset changes the displayed time span (and the `↕ N ms` label);
"custom…" reveals the box and a typed value applies. In ARIC mode only the presets are offered
(no custom box).

### P1-5 — Remove the 300-row cap so duration actually renders more time
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** Replaced the flat `MAX_LIVE_ROWS = 300` clamp (which pinned every long duration to
300 rows — why duration "did nothing" past ~10-20 ms) with `max_live_rows(cfg)`: the largest rows
the IQ ring can actually supply for the current backend/nfft, bounded so
`samples_needed(rows) ≤ RING_ROW_FILL·MAX_TAIL` (0.9·4M) — keeping the Computer's `avail ≥ need`
gate reachable — and never above an absolute `MAX_ROWS_ABS = 4096` ceiling. Both server clamp
sites (`rows` control key and the `capture.duration→rows` mapping) now use it. Client-side,
`rowsForWindow`'s hard `Math.min(…, 300)` became `Math.min(…, CLIENT_MAX_ROWS=4096)`. The ceiling
protects browser render + ring depth, not the fps: a longer duration means more FFTs per calibrated
frame, so **fps may fall — left honest** (the meta fps is unchanged in spirit from the audit). At
settings where the requested span can't fit the ceiling, the rows are clamped and the meta/axis
`ms` label honestly reflects the **actual** rows shown, not the request.
**Honest consequence:** large row counts also mean large frames (≈ rows·nfft·4·2 bytes) — e.g. an
800-row nfft-1024 quicklook frame is ~6.5 MB; over a slow link this raises bandwidth and can drop
fps. Use `--quantize` (uint8, ~4× smaller) for constrained links. This is expected, not hidden.
**Scope note:** only the web viewer's `MAX_LIVE_ROWS` was removed; the separate
`striqt_standalone.py` / `pluto_standalone.py` / `web_sim` scripts keep their own 300-row GUI caps
(out of scope for Phase 1).

**Verify [demo]:** a 100 ms duration renders visibly more time depth than 10 ms (more rows); the
meta `window/duration` label matches the selected value rather than clamping. Confirmed over WS: a
`{rows:800}` request now yields 800-row frames (was capped at 300), and `{rows:99999}` is honestly
clamped to the ring capacity (3686 rows at quicklook/nfft-1024), with `samples_needed < MAX_TAIL`
for every backend/nfft.

## Phase 2a — Analysis wiring + freedom model

### P2a-1 — Parameterize the calibrated spectrogram from cfg (server plumbing)
**Files:** `live/striqt_web_server.py`
**Changed:** `calibrated_spectrogram` no longer hardcodes the striqt Spectrogram recipe. The
seven analysis knobs now live in `RadioConfig` (immutable values, copied by `snapshot()`):
`window` (scipy `get_window` spec, default `("kaiser", 11.88)`), `fractional_overlap`
(`Fraction`, default `13/28`), `window_fill` (`Fraction`, default `15/28`),
`integration_bandwidth` (`"auto"` = the old `frequency_resolution × averaging_factor(nfft)`
coupling | `None` | Hz), `lo_bandstop` (`None` | Hz, default 120 kHz), `trim_stopband` (default
False). `make_analysis_spec(cfg, nfft, fs)` builds the striqt spec;
`calibrated_spectrogram`/`ssb_spectrogram` now take `(samples, cfg)`. `frequency_resolution`
stays derived (`sample_rate / aligned_nfft(nfft)`) — **`cfg.nfft` is the single authoritative
owner** of that quantity (the freq-res field arrives in P2a-2/6 as another view of it).
The hop math is generalized: `analysis_hop(nfft, fractional_overlap)` computes
`nfft − round(overlap·nfft)` exactly as striqt does, and `calibrated_sample_count`,
`samples_needed`, `max_live_rows` and the new `row_hop(cfg)` all use it (no more hardwired
15/28). The capture handed to striqt now carries the real `cfg.analysis_bandwidth` so
`trim_stopband=True` has something to trim to (inert at the inf default). The display LO-null in
`fit_display_rows` is sized to the configured `lo_bandstop` (skipped entirely when `None` — the
DC leak then shows, honestly). **Header (additive only):** new `hop_size` field (samples per
displayed row) so the client can label the time axis for any overlap; the calibrated path now
ships striqt's own `spectrogram_freqs` coordinates as `freqs_hz_f0`/`freqs_hz_step` — exact for
any trim/averaging combination. Note this corrects the old symmetric-about-DC approximation by
half a raw FFT bin (~7.6 kHz at 15.36 MS/s / nfft 1008): striqt's averaged-group centers are the
mean of the member bin frequencies, which is what the data actually represents.
**Behaviour at defaults is unchanged** (verified: nfft 1008, bin_avg 12, hop 540, 83 bins, same
units/attrs) apart from the half-bin axis correction above.

**Verify [demo]:** frames on port 8001 (`--demo`, calibrated) still show `fft_nfft 1008 /
bin_avg 12`, the same waterfall, and now include `hop_size: 540`; PSD tone positions unchanged
(≤ half a raw bin shift from the axis correction). **Verify [hardware]:** live frames identical
to pre-P2a at the default settings.

### P2a-2 — Freedom-model validation (tiers 1–2) + extended ack
**Files:** `live/striqt_web_server.py`
**Changed:** Control messages gain an `"analysis"` block (`window`, `frequency_resolution`,
`fractional_overlap`, `window_fill`, `integration_bandwidth`, `lo_bandstop`, `trim_stopband`),
gated by the freedom model in `SharedConfig._validate_analysis` — the live radio can never
receive a config that would crash the compute:
- **Tier 1 (knowable → snap and tell):** `frequency_resolution` is a second view of the FFT size
  (`cfg.nfft` owns it) — an edit snaps to the nearest `NFFT_CHOICES` size, and the executed
  resolution (`fs / aligned_nfft`, the 28-multiple grid) is reported. `fractional_overlap` /
  `window_fill` snap to the nearest k/nfft (integer overlap samples / integer zero-fill).
  `integration_bandwidth` snaps to an integer multiple of the frequency resolution (accepts
  `"auto"` = the old nfft-tracking default, and `"none"`). `lo_bandstop` accepts `"none"` and is
  clamped to the sampled span. Every snap is reported as
  `rounded: [{field, requested, used, reason}]`.
- **Tier 2 (only striqt can judge):** each surviving field is applied one at a time onto a
  scratch copy and judged by `scratch_validate_analysis` — the exact spec the Computer would run,
  evaluated on a tiny 2-row zero buffer (a few nfft of samples, single channel), never touching
  the live ring/acquirer. A striqt exception rejects that field with the striqt error text:
  `rejected: [{field, requested, reason}]`; survivors still apply (per-field attribution).
- The ack is now `{applied, ignored, reconnect, rounded, rejected}`; `ws_endpoint` sends a
  human-readable summary plus the structured ack (`{"message": …, "ack": …}`) for any
  capture/source/analysis message. Unknown analysis keys (Phase 2b params) are reported as
  `ignored: ["analysis.<key>"]`, not dropped silently.
- Analysis keys are **only** settable through the validated block — top-level occurrences are
  stripped, so no client can bypass the gate. When a change alters the per-row hop (overlap/nfft/
  backend), `rows` is re-clamped against `max_live_rows` so the Computer's `avail ≥ need` gate
  stays reachable (no starved display).
- Where the installed striqt lacks `striqt.analysis` (quicklook-only installs), tier 2 is a
  no-op — tier 1 still applies, and the P2a-3 backstop covers the rest.

**Verify [demo]:** on 8001, `{"analysis":{"window":"notawindow"}}` returns
`rejected window: Invalid window name …` and the stream keeps running; `window_fill: "1/3"`
applies (integral at nfft 1008); `frequency_resolution: 15000` rounds to 15238.1 with the nfft
reason; `fractional_overlap: 0.464` snaps to `13/28`; a top-level `{"window": "boxcar"}` is
stripped. All confirmed against the vendored striqt build. **Verify [hardware]:** same messages
against the installed striqt build; a rejected value never interrupts the live feed.

### P2a-3 — Compute backstop: catch, revert to last-good, keep streaming, surface reason
**Files:** `live/striqt_web_server.py`
**Changed:** Belt-and-suspenders tier 3 of the freedom model. `SharedConfig` now tracks the
analysis params of the last config that **demonstrably computed a frame**
(`note_good_analysis`, called by the Computer and the DemoAcquirer after every successful
publish) plus a bounded notice queue (`push_notice`/`drain_notices`, newest 20 kept). If the
live compute throws anyway — a param that slipped past tiers 1–2, or an installed-striqt
behavior difference — the Computer/DemoAcquirer exception handler calls
`revert_analysis(reason)`: it restores the last-good analysis params (escalating to the shipped
defaults when the last-good set itself is what is failing), re-clamps `rows` against the
restored hop, and queues a `[server] analysis error: … — reverted …` notice. When nothing
differs from every revert target (i.e. the error is not analysis-induced), it reverts nothing
and a throttled (≥5 s apart) `compute error: …` notice is queued instead — the stream is never
silently stalled. The broadcaster flushes queued notices to every connected viewer each tick,
even on ticks with no new frame, so app.js logs the reason while the last good frame keeps
displaying. The viewer never freezes: the very next Computer iteration re-snapshots the
reverted config and streaming resumes.

**Verify [demo]:** force a bad value directly into the running cfg (bypassing validation) —
the next compute logs the striqt error, reverts (`[server] analysis error: … — reverted window
to last-good values` appears in the browser log), and frames keep flowing. Confirmed offline
against the vendored striqt: revert restores last-good, escalates to defaults when last-good
fails, and the reverted config computes again. **Verify [hardware]:** same, with the installed
striqt build under live DMA load.

### P2a-4 — Duration as a first-class, hop-aware server param
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** Fixes the Phase-1 deviation where the client sent pre-computed `{rows}` because the
server's `capture.duration→rows` math omitted the hop factor. `RadioConfig` gains `duration`
(seconds; 0 = legacy rows-driven). A JSON `capture.duration` now maps straight through, and
**rows are derived hop-aware from the final state of each update**
(`round(duration · fs / row_hop(cfg))`, clamped to `max_live_rows`), re-deriving automatically
whenever nfft / backend / `fractional_overlap` / sample_rate change — one owner, no drift. An
explicit top-level `{"rows": N}` control reclaims rows ownership (zeroes `duration`); scroll
(Cool) mode uses this for its fixed 12-row frame chunks. `make_capture` arms the radio with the
owned duration (snapped to an integer sample count, which striqt's Capture validation requires).
Client: the Duration control now sends `{capture:{duration}}` in replace mode (no client rows
math); the nfft select sends bare `{nfft}` and lets the server re-derive; `sendTimeControl()`
replaces the removed `rowsForCurrentSettings`/`rowsForWindow`/`backendHopFrac`. **The `↕ N ms`
axis label, meta window label, and PNG caption are now computed from the header's exact
`hop_size`** (`depthRows · hop_size / fs`) instead of the approximate `radioNfft·15/28` — which
was off by 1.6 % (1024 vs the executed 1008) and would have been wrong for any non-default
overlap. Scroll-mode display depth uses the same exact hop.

**Verify [demo]:** applying `{"capture":{"duration":0.02}}` yields rows = round(0.02·fs/540) =
569 at defaults; changing nfft to 2048 re-derives 284; overlap 0 re-derives 152; quicklook 150;
an explicit rows control zeroes duration and later nfft changes leave rows alone; a 10 s request
clamps to the ring bound (3744). All confirmed offline; on 8001 the `↕ ms` label matches the
selected duration exactly. **Verify [hardware]:** the armed capture duration follows the
Duration control (radio log line), and the label stays truthful across nfft changes.

### P2a-5 — /config endpoint, server-seeded forms, and compute-thread tier-2 probes
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed (server):** New `GET /config` returns the live `RadioConfig` as JSON — a `capture`
view (center_frequency, sample_rate, gain, the four P1-2 knobs, duration, nfft) and an
`analysis` view (window, executed frequency_resolution, overlap/fill as fraction strings,
integration_bandwidth, lo_bandstop, trim_stopband) plus backend/rows/lo_null.
**Changed (client):** `loadSchema()` now seeds the Capture form from `/config` instead of the
striqt schema defaults, fixing the Phase-1 bug where a bare Apply silently flipped untouched
fields whose schema default differs from the server default (e.g. `host_resample` true vs
false). The FFT select (+ `radioNfft`) and Duration control also seed from it. Every settings
ack triggers a debounced `/config` re-fetch so the forms and `radioNfft` re-sync with what the
server actually runs after rounding. `handleAck` surfaces the P2a-2 structured ack: rounded →
`invalid X=… → using Y (reason)` log lines + an "adjusted …" status; rejected → ERROR log lines
+ a "rejected … — kept last-good config" status. Idle `ping` keepalives no longer spam the log.
**Fix discovered by live testing:** striqt's `get_window` carries a persistent on-disk cache
whose handle binds to the thread that first uses it (Python ≥3.13 `dbm.sqlite3` refuses
cross-thread use), so tier-2 probes run from the WebSocket thread falsely rejected legal NEW
windows with a SQLite threading error. Probes now execute **on the compute thread** (Computer /
DemoAcquirer service a single-slot mailbox each loop, `SharedConfig.probe_analysis`/
`service_probe`), with an inline fallback if unserviced for 2 s; `ws_endpoint` runs
`SharedConfig.update` in an executor so a multi-field apply cannot stall the broadcaster. This
is also the correct home on the radio's own striqt build regardless of its dbm backend.

**Verify [demo]:** `/config` mirrors every applied change; on 8001 `{"analysis":{"window":
"hann"}}` now applies (was falsely rejected), `notawindow` is rejected with the real striqt
reason, and a bare Apply after page load changes nothing (`applied []`). A no-striqt stock
`python3 --demo` boot still works (quicklook, tier 2 a no-op). **Verify [hardware]:** apply a
new window on the installed build — no SQLite/threading rejection; `/config` reflects it.

### P2a-6 — Analysis panel UI (DAN mode)
**Files:** `live/web/index.html`, `live/web/app.js`, `live/web/style.css`
**Changed:** New **Analysis** column in the DAN-mode settings panel (`#settings-panel` is
`pro-only`, so ARIC mode never sees it): free-text inputs for `window` (accepts `hann` or
`kaiser, 11.88` shorthand), `frequency resolution (Hz)`, `fractional overlap`, `window fill`
(fraction strings like `13/28` or decimals), `integration bandwidth` (`auto | none | Hz`),
`LO bandstop` (`none | Hz`), and a `trim stopband` checkbox, plus an **Apply analysis** button.
No client-side guardrails by design (the freedom model): values are sent raw as
`{"analysis": {...}}`; the server snaps knowable constraints (shown by `handleAck` as
`invalid X=… → using Y (reason)` + an "adjusted" status) or lets striqt scratch-validate and
reject (`rejected X: <striqt reason>` + a "kept last-good config" status) — the stream never
freezes either way. Fields seed from `/config` on load and re-seed after every ack, so the
panel always shows what the server actually runs, a bare Apply changes nothing
(`applied []`), and a rounded value (e.g. frequency resolution) snaps back to the executed one
in the box. `#settings-editor` switched to an auto-fit grid to hold the third column.

**Verify [demo]:** on 8001 in DAN mode the Analysis column renders (absent in ARIC); bare
Apply → `applied []`; editing window to `blackmanharris` applies and the waterfall texture
changes; `notawindow` is rejected in the log/status and the stream keeps running; setting
capture `analysis_bandwidth` 10 MHz + trim stopband shrinks the frame to 53 bins with an exact
±4.76 MHz axis (confirmed over WS with panel-identical payloads). **Verify [hardware]:** each
param visibly changes the calibrated spectrogram (window sidelobes, overlap row density,
integration smoothing, LO notch width); a deliberately-illegal param is caught and reported
without killing the live feed.

### P2b-1 — Freedom model generalized to analysis targets
**Files:** `live/striqt_web_server.py`
**Changed:** Refactors the P2a validation machinery so the SAME three tiers can govern every
analysis (spectrogram / PSD / SSB) instead of only the calibrated spectrogram — no second,
parallel validation path. New `ANALYSIS_TARGETS` descriptor table maps each target's message
fields onto `RadioConfig` attributes and fixes its tier-2 application order;
`ANALYSIS_CFG_KEYS` (top-level strip + backstop key set) is now derived as the union across
targets. The `"analysis"` control block gains an optional `"target"` key (routing; unknown
targets are rejected with the known list); no target means `"spectrogram"`, so the P2a wire
format is unchanged. `scratch_validate_analysis` became a `SCRATCH_VALIDATORS[target]`
dispatcher (the P2a validator is now `scratch_validate_spectrogram`), and the compute-thread
probe mailbox (`probe_analysis`/`service_probe`) carries the target through. The tier-1 rules
for the `FrequencyAnalysisSpecBase` fields shared by spectrogram and PSD moved into
`_tier1_freq_fields(cfg_prefix, ack_prefix, …)` so later targets reuse them verbatim against
prefixed cfg keys. Behavior for existing clients is unchanged (pure refactor).

**Verify [demo]:** with the vendored striqt on the path, a probe-servicer harness reproduces
the exact P2a acks: `window: hann` applies; `notawindow` is rejected with the striqt reason;
`fractional_overlap: 0.463` rounds to `467/1008`; `frequency_resolution: 7000` snaps nfft
1024→2048 with the 2016-grid note; `{"target": "nonsense"}` rejects with the known-target
list. **Verify [hardware]:** none needed beyond P2a's (no behavior change).

### P2b-2 — Spectrogram long tail: time_aperture through the freedom model
**Files:** `live/striqt_web_server.py`, `live/web/index.html`, `live/web/app.js`
**Changed:** Exposes the last striqt `Spectrogram` param the P2a panel didn't: `time_aperture`
(binned RMS averaging along the time axis, seconds; `none` = off). New `RadioConfig.time_aperture`
drives `make_analysis_spec`; tier-1 snaps a request to an integer multiple of the row hop period
`(1-overlap)·nfft/fs` (striqt's own constraint), capped at one frame, and reports the snap; when a
message moves the hop grid (nfft/overlap) under a standing aperture, the aperture is **re-snapped
to the new grid and reported** instead of letting the next live frame throw. Tier-2 scratch runs
now size their synthetic buffer so a configured aperture yields ≥1 averaged row (a 2-row buffer
would judge legal apertures on an empty result), and a new `probe_reset` descriptor hook clears
the stale aperture from the tier-2 working copy while nfft/overlap probe (it rides their hop grid;
probing them with the stale aperture attached would falsely reject them). Compute path: striqt
returns `rows//k` averaged rows for `k = round(aperture·fs/hop)`; `calibrated_spectrogram` now
fits to that honest count and ships `hop_size = hop·k`, so the client's `↕ ms`/meta time labels
stay exact with zero client changes. `/config` analysis block and the DAN Analysis panel gain the
field (`time aperture (s)`, `none | s`). Backstop: `time_aperture` joins `ANALYSIS_CFG_KEYS` /
`ANALYSIS_DEFAULTS`, so tier-3 revert covers it.

**Verify [demo]:** offline harness on the vendored striqt: `0.001` s at defaults snaps to
0.984375 ms (28 rows) with the hop-grid reason; a 20 ms frame computes (2, 20, 83) blocks with
hop_size 15120 → 19.69 ms label; changing overlap re-derives rows and keeps the aperture legal;
`none` clears; `banana` is rejected; a deliberately misaligned aperture forced into the compute
raises striqt's multiple-of-hop error (tier-3 catches it live). **Verify [hardware]:** with a
real signal, a 1 ms aperture visibly smooths the waterfall texture and reduces rows ~28×; the
time label still matches the Duration control.

### P2b-3 — PSD backend: power_spectral_density params + time_statistic (server)
**Files:** `live/striqt_web_server.py`
**Changed:** New `"psd"` backend runs striqt's real `power_spectral_density` (Welch method) and
ships one trace per configured statistic: blocks are `(channels, n_statistics, bins)` float32
dB, additive header keys `psd_stats` (the statistic behind each row) and `time_span_ms` (the
true integrated span) disclose the shape. The PSD gets its **own** `RadioConfig` param block
(`psd_window/…/psd_trim_stopband`, `psd_time_statistic`, defaults identical to the spectrogram
recipe + `("mean","max")`), so tuning the PSD view never disturbs the spectrogram recipe. A new
`"psd"` entry in `ANALYSIS_TARGETS` reuses `_tier1_freq_fields` verbatim against the `psd_`
keys (acks labeled `psd.<field>`); `time_statistic` parses "mean, 0.5, 0.95, max" style lists
(tier 1: structure + quantiles in [0,1]; unknown statistic NAMES go to tier 2, where
`scratch_validate_psd` runs the real measurement on a 2-row buffer and returns striqt's own
reason, e.g. the valid-name list). `/config` gains an `analysis_psd` block. Backend plumbing:
`CALIBRATED_GRID_BACKENDS` + `backend_overlap()` make `row_hop`/`max_live_rows`/
`samples_needed` honor the PSD block's overlap, so Duration owns the PSD's integrated span
hop-aware. Also fixed: tier-1 fraction snapping now always uses the aligned 28-multiple grid
for spectrogram/PSD targets (the pipelines execute there unconditionally) — under P2a a value
snapped to k/1024 while quicklook was displayed broke window_fill integrality when the
calibrated view returned.

**Verify [demo]:** offline harness: `time_statistic: "mean, 0.5, 0.95, 0.99, max"` applies and
a 20 ms frame computes (2, 5, 83) blocks with `psd_stats`/`time_span_ms` in the header and
sane trace ordering (p99 ≤ max, mean ≤ max at a tone); `"bogus"` is rejected with striqt's
valid-name list; quantile 1.5 rejected at tier 1; editing psd window/overlap leaves the
spectrogram block untouched and re-derives rows from the psd hop; `/config.analysis_psd`
mirrors it all. **Verify [hardware]:** percentile traces over a real bursty signal order
correctly (p50 < p95 < p99 < max) and the mean trace matches the old client-computed mean.

### P2b-4 — PSD view: server statistic traces drawn in the browser
**Files:** `live/web/app.js`, `live/web/style.css`
**Changed:** The Analysis dropdown's "PSD view" now selects the real `psd` backend (P2b-3)
instead of the old client-only waterfall-hide over calibrated. When frames carry `psd_stats`,
the client rebuilds the uPlot with **one series per (channel, statistic)** — labels like
`RX1 Mean / RX1 p95 / RX2 Max`, mean staying the blue family and max the red family, other
statistics cycling a distinct palette — and draws the server-computed traces directly (no
client-side mean/max recomputation). uPlot's clickable legend entries are the trace toggles,
so the drawn set always follows the REAL `time_statistic` list rather than the fixed
mean/max pair; the static Mean/Max swatch key is hidden in PSD mode (style.css). The meta
line labels the true `integration N ms (Mean/p95/…)` span from the header instead of the
hop-derived window; the peak marker follows the max-like trace (max if present, else the
last statistic); the band monitor integrates the mean (or first) statistic trace; Save PSD
CSV exports one column per (channel, statistic) with the statistic list and span in the
header comments. Switching analyses rebuilds the correct plot layout both ways
(`uplotKind` tracks it). Peak hold / min trace / RX1−RX2 diff operate on the client display
window and have no effect in PSD mode (the server traces are the statistics).

**Verify [demo]:** on a demo boot, a WS client switching to `backend: psd` with
`time_statistic: mean, 0.5, 0.95, 0.99, max` receives (2, 5, 83) frames with
`psd_stats`/`time_span_ms` headers, mean ≤ max and p50 ≤ p99 everywhere; a rejected `bogus`
statistic leaves the 5-trace stream running (ack carries striqt's valid-name list);
switching back to Spectrogram resumes 569-row calibrated frames (hop 540). In the browser:
PSD mode shows one legend entry per statistic and clicking an entry hides that trace.
**Verify [hardware]:** percentile traces over a real bursty band order sensibly and change
when the statistic list is edited from the Analysis panel.

### P2b-5 — Honest SSB: full param block + capture-rate retune (server)
**Files:** `live/striqt_web_server.py`, `live/web/app.js`
**Changed:** The SSB backend's hardcoded recipe becomes a validated `RadioConfig` block
(`ssb_subcarrier_spacing`, `ssb_sample_rate`, `ssb_discovery_periodicity`,
`ssb_frequency_offset`, `ssb_max_block_count`, `ssb_window`, `ssb_lo_bandstop`; defaults are
the old hardcodes) behind a new `"ssb"` `ANALYSIS_TARGETS` entry. **The audit's honesty item is
fixed by retuning:** `ssb_grid_compatible` is generalized (2·fs/scs must be an integer
28-multiple ⇔ fs a multiple of 14·scs) and, whenever a message leaves the SSB backend at an
incompatible rate (selecting SSB, changing SCS, or picking an off-grid rate), `update()`
retunes the capture rate to `ssb_compatible_rate()` — the nearest 14·scs multiple, preferring
the radio's 1.92 MHz LTE family (13.44 MS/s for all standard SCS) — and reports it in the ack's
`rounded` list; `ws_endpoint` now acks ANY message the freedom model adjusted, so even a bare
`{"backend":"ssb"}` surfaces the retune. If a rate is still off-grid (retune impossible),
`compute_blocks` keeps the existing explicit calibrated substitution via
`backend`/`backend_requested`; the old **silent** in-function fallback (which mislabeled
calibrated output as executed-SSB) is removed — no phantom SSB. `ssb_spectrogram` is rewritten
on an exact geometry (`ssb_geometry`: nfft = 2fs/scs, hop = one OFDM symbol, 28·scs/15e3
symbol rows per burst — always 2 ms; `ssb_block_samples`/`ssb_max_blocks`): frames carry whole
burst sets only (striqt's blockwise reshape requires it), rows = bursts × symbol_rows,
disclosed as `fft_nfft` = nfft, `bin_avg` = 2 (scs-wide integrated bins), `hop_size` = one
symbol; exact striqt frequency coords come from the `cellular_ssb_baseband_frequency` factory
(private-module import with symmetric-axis fallback — **vendored/installed divergence risk
flagged**). `row_hop`/`max_live_rows`/`samples_needed` gain burst-aware SSB branches so
Duration picks the burst count (20 ms → 1 burst, 40 ms → 2). Tier-1 rules (all snap & tell):
scs bounded so 14·scs ≤ 30.72 MS/s; SSB output rate ≤ sampled span; discovery ≥ one 2 ms burst
and ≤ ring; frequency_offset a multiple of scs AND |offset| + ssb_rate/2 ≤ fs/2 (both
constraints confirmed against striqt's own errors); max_block_count whole or none. Tier 2:
`scratch_validate_ssb` runs the real measurement on a one-burst buffer at the rate the retune
would arm. The display-side LO null is skipped at nonzero offsets (DC is no longer at the band
center; striqt's own lo_bandstop covers it). Client: the SSB dropdown option is no longer
disabled by a client-side grid guess — the server retunes and reports instead.
**[hardware] risk flag:** the retune arms 13.44 MS/s (7×1.92 MHz) — an LTE-family but
non-default rate; confirm the AIR8201B accepts it during the hardware window.

**Verify [demo]:** on a demo boot, `{"backend":"ssb"}` at 15.36 MS/s acks
`rounded sample_rate: 15.36e6 → 13.44e6 (…symbol grid…)` and streams TRUE SSB frames
(rows 56, bins 256, fft_nfft 896, bin_avg 2, hop 480, no backend_requested mismatch);
duration 40 ms yields 112 rows (2 bursts); live SCS 15 kHz reshapes to rows 28/bins 512/hop
960 with no retune (13.44 is a 210 kHz multiple); offset 2 MHz snaps to 2.01 MHz and computes;
99 MHz clamps to the span-fitting ±2.88 MHz; `notawindow` rejected with striqt's reason;
max_block_count caps the frame. **Verify [hardware]:** the radio arms 13.44 MS/s; with a live
n41 SSB the burst view shows the 4-column SSB symbol structure; discovery 20 ms lines bursts
up frame-to-frame; deliberately-bad params report and never kill the feed.

### P2b-6 — Per-analysis panels: the Analysis column follows the selected analysis
**Files:** `live/web/index.html`, `live/web/app.js`
**Changed:** The DAN-mode Analysis column (inside the pro-only `#settings-panel`; ARIC never
sees it) is now rendered dynamically from an `ANALYSIS_PANELS` descriptor table instead of a
fixed field list, and **switching the Analysis dropdown swaps which parameter set is
editable** — matching the intent that the config targets the shown analysis. Spectrogram
shows the P2a set + `time aperture (s)`; PSD shows the shared STFT fields + `time statistics`
("mean, 0.95, max" style); SSB shows `subcarrier spacing / SSB output rate / discovery period
/ frequency offset / max burst sets / window / LO bandstop`; Quicklook states it has no
analysis parameters (Apply hidden). Apply sends `{"analysis": {"target": <shown>, …}}` with
only the non-empty fields; each panel seeds from its own `/config` block (`analysis`,
`analysis_psd`, `analysis_ssb`) on render, on load, and after every ack (the cached config
re-seeds instantly on panel switches). The badge line names the target and its caveat (e.g.
the SSB retune). Free-text inputs remain by design — all legality still lives in the server's
freedom model.

**Verify [demo]:** in DAN mode the panel renders the spectrogram set by default; selecting
PSD/SSB/Quicklook swaps the fields (and badge) and a bare Apply on each target returns
`applied []`; editing `time statistics` changes which PSD traces draw; editing SSB
`subcarrier spacing` reshapes the burst view; all panels re-seed to the server's executed
values after a rounded ack. ARIC mode shows no panel. **Verify [hardware]:** none beyond the
P2b-2/3/5 server items (pure UI routing).

### P2b-7 — SSB-grid rates survive the LTE-list snap (no rearm churn)
**Files:** `live/striqt_web_server.py`
**Changed:** With the SSB backend at a retuned rate (13.44 MS/s), a bare Capture-form Apply
re-sent the server's own `sample_rate`, which the generic update loop snapped onto the LTE
list (→ 15.36 MS/s) only for the P2b-5 retune to snap it straight back — a net-zero change
that still marked the config dirty and re-armed the radio (ring clear + display blank) on
every bare Apply. The sample-rate snap now consults the message's effective backend/SCS: a
rate already on the SSB grid passes through untouched while SSB is (or becomes) the backend;
every other case still snaps to `RATES_HZ`, and off-grid rates chosen while in SSB still
retune with the reported ack.

**Verify [demo]:** offline harness: after selecting SSB (fs 13.44), a bare Apply of the
server-seeded capture values returns `applied []` (no rearm); choosing 7.68 MS/s while in SSB
applies-then-retunes with the reported `rounded sample_rate → 13.44e6`; on the calibrated
backend 13.44 still snaps to 15.36. **Verify [hardware]:** with SSB live, a bare Apply causes
no waterfall blank/rearm log line.
