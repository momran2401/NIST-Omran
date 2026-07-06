# Bug Report — `live/` Folder

> **HISTORICAL — verified fixed 2026-07-06, see `AUDIT_REPORT.md` §5.C; note S-5 was erroneous (`np.hanning` is not deprecated and the suggested `np.hann` does not exist). This document describes the pre-fix state; for the current backlog see `AUDIT_REPORT.md` and `FIXLOG.md`.**

**Repository:** NIST-Omran  
**Scope:** All files inside `live/`  
**Reviewer:** Claude (read-only inspection — no code was modified)  
**Date:** 2026-06-24

---

## Files Inspected

| # | File | Lines |
|---|------|-------|
| 1 | `striqt_frontend_TCP.py` | 937 |
| 2 | `striqt_server_TCP.py` | 637 |
| 3 | `striqt_standalone_terminal.py` | 1093 |
| 4 | `striqt_standalone.py` | 1674 |
| 5 | `pluto_standalone.py` | 1715 |

The `striqt/` subfolder (the NIST library) was read for architectural context but is not a bug-inspection target.

---

## Severity Scale

| Level | Meaning |
|-------|---------|
| **Critical** | Causes a hang or permanent crash in normal use |
| **High** | Causes incorrect data, a process-killing crash, or loss of measurement |
| **Medium** | Degrades behaviour in reachable edge cases or produces misleading output |
| **Low** | Bad practice, latent risk, or minor UX defect with no immediate consequence |

---

---

# 1. `striqt_frontend_TCP.py`

---

### Bug F-1 — QMutex not released on BaseException in `Receiver.run()`
**Lines:** 104–134  
**Severity:** Low (deadlock risk under uncommon conditions)

**What it is:** `self._lock.lock()` is called inside the outer `try:` block to atomically assign `self._sock`. If a `BaseException` subclass (`KeyboardInterrupt`, `SystemExit`, `MemoryError`) is raised between that `lock()` and the matching `unlock()` call four lines later, the outer `except Exception` block catches nothing and the GIL unwinds — but the lock was already taken. The outer `except Exception` then tries to call `self._lock.lock()` again (line 130), which deadlocks the Receiver thread because PyQt's default `QMutex` is non-reentrant.

**When it triggers:** The window is very small (only a few lines of code hold the lock) but any hard interrupt or memory pressure between lines 104–113 could trigger it.

**Suggested fix:** Wrap the `self._lock.lock()` / `self._lock.unlock()` pair with a `try/finally` block so `unlock()` is guaranteed. Better yet, replace with `QMutexLocker` (RAII guard), which PyQt provides for this pattern.

---

### Bug F-2 — `_save_csv` crashes with `AttributeError` if channel 0 buffer is absent
**Lines:** 854–868  
**Severity:** Low

**What it is:**
```python
b0 = self.buffers.get(0)
...
m0, x0 = b0.mean(axis=0), b0.max(axis=0)   # crashes if b0 is None
```
The function returns early if `self.nfft is None` (line 847), but `self.nfft` can be set by the very first frame before `self.buffers` contains channel 0 (e.g., if the server sends a header for an unexpected channel set). If `b0` is `None`, `b0.mean()` raises `AttributeError`.

**When it triggers:** User clicks "Save PSD CSV" during the brief window after the first frame sets `nfft` but before buffers are populated for channel 0, or if the server reports an unexpected channel list.

**Suggested fix:** Add `if b0 is None: return` (or show a status-bar warning) after `b0 = self.buffers.get(0)`.

---

### Bug F-3 — Band-power averaging is performed in dB domain (incorrect math)
**Lines:** 767–769 (also identical in `striqt_standalone.py:1495–1497` and `pluto_standalone.py:1535–1537`)  
**Severity:** Medium

**What it is:**
```python
lin = 10.0 ** (b.mean(axis=0) / 10.0)     # time-avg PSD, dB -> linear
band[ch] = 10.0 * np.log10(lin[mask].mean())
```
`b.mean(axis=0)` computes the arithmetic mean of the dB values across time rows. Averaging decibel values arithmetically is not the same as averaging power: the result underestimates high-power events and overestimates low-power baselines relative to the true time-average power. The correct sequence is: convert each row to linear power first, average in linear domain, then convert back.

The same flaw propagates to the RSRQ-like quality metric on line 769:
```python
qual[ch] = band[ch] - 10.0 * np.log10(lin.mean())
```
`lin` is already computed from the incorrect mean-in-dB, so the quality metric inherits the error.

**When it triggers:** Any time the Band monitor is active and the signal power varies across time rows (i.e., always during live operation with any signal).

**Suggested fix:** Replace the averaging with:
```python
lin = 10.0 ** (b / 10.0)          # all rows to linear
lin_mean = lin.mean(axis=0)        # true time-average in linear domain
band[ch] = 10.0 * np.log10(lin_mean[mask].mean())
qual[ch] = band[ch] - 10.0 * np.log10(lin_mean.mean())
```

---

### Bug F-4 — `on_frame` reads color levels from `self.images[0]` regardless of channel when `auto_scale=False`
**Lines:** 729–734  
**Severity:** Low

**What it is:**
```python
lv = self.images[0].getLevels()
```
When the user disables "Auto color", the code reads the histogram levels from channel 0's `ImageItem` only, ignoring any manual adjustments the user made to channel 1's histogram. If the user dragged channel 1's histogram to a different range, that range is silently discarded every frame.

**When it triggers:** User disables "Auto color" and adjusts the RX2 histogram independently.

**Suggested fix:** Either read levels from both images and use a union, or lock both histograms together and only read from one (make the behaviour explicit and documented).

---

---

# 2. `striqt_server_TCP.py`

---

### Bug S-1 — `Acquirer` thread has no error recovery; any stream error kills it permanently
**Lines:** 490–531  
**Severity:** High

**What it is:** The server's `Acquirer.run()` calls `self.source._read_stream(...)` with no exception handler around it. If `_read_stream` raises any exception (overflow, USB disconnect, timeout, `ReceiveStreamError`, `OSError`), the exception propagates out of the `while` loop, the `finally` block closes the source, and the acquirer thread **exits permanently**. The `serve()` function then calls `acquirer.latest()` in a tight loop every 20 ms forever, always receiving `(None, None)`, and the server never recovers without a manual restart.

Compare with `striqt_standalone.py` and `striqt_standalone_terminal.py`, which both wrap `_read_stream` in `except (ReceiveStreamError, OverflowError, OSError)` and call `recover_radio()`. The server omits this entirely.

**When it triggers:** Any hardware hiccup: USB glitch, buffer overflow, radio reset.

**Suggested fix:** Wrap the `_read_stream` call in the same `try/except (ReceiveStreamError, OverflowError, OSError)` + `recover_radio()` pattern used in the standalone scripts. This was intentionally implemented in the standalone versions and should be ported back to the server.

---

### Bug S-2 — `rearm()` does not refresh `read_size`, `tmp`, or `buffers`
**Lines:** 491–495 (initial setup), 463–476 (`rearm`), 490 (loop)  
**Severity:** High

**What it is:** When the viewer changes center frequency, sample rate, FFT size, or gain, `shared.update()` sets the dirty flag. The acquirer then calls `self.rearm(cfg)` — but after rearm, it continues using the stale `read_size`, `tmp`, and `buffers` variables that were set once at startup. If the stream MTU changes after the config change (e.g., after a sample-rate change), `buffers` are views into the old `tmp` array, which may no longer match the new stream geometry.

Compare with `striqt_standalone.py` line 667:
```python
self.rearm(cfg)
read_size, tmp, buffers = self._make_read_buffers()  # refreshes after rearm
```
The server `Acquirer.run()` has no equivalent refresh call after `rearm()`.

**When it triggers:** Any radio config change from the viewer (center, rate, gain, FFT size).

**Suggested fix:** Add `read_size, tmp, buffers = self._make_read_buffers()` after every successful `self.rearm(cfg)` call, matching the standalone implementation.

---

### Bug S-3 — `SharedConfig.update()` always marks `_dirty=True`, even when the value is unchanged
**Lines:** 92–107  
**Severity:** Medium

**What it is:**
```python
def update(self, update):
    with self._lock:
        for key, value in update.items():
            ...
            setattr(self._cfg, key, value)
        self._dirty = True   # set unconditionally
```
Even if the incoming value is identical to the current config (e.g., the viewer sends `{"gain": 0.0}` and gain is already `0.0`), `_dirty` is set to `True`. On the next acquirer loop iteration, the radio is fully re-armed (stream deactivated, re-opened, re-activated), causing a brief dropout in IQ data.

The standalone version avoids this with an explicit old-vs-new check:
```python
old = getattr(self._cfg, key)
if old == value:
    continue
```

**When it triggers:** Any control message from the viewer, including the initial `send_control({"rows": ...})` sent on connection and the periodic control messages sent every 20 ms (though control messages are only sent on user interaction, not periodically — still, duplicated user actions trigger spurious rearms).

**Suggested fix:** Add the same `if old == value: continue` guard present in `striqt_standalone.py` `SharedConfig.update()`.

---

### Bug S-4 — Wire protocol desync if `calibrated_spectrogram` returns fewer rows than promised
**Lines:** 363–373 (server), 116–119 (client `Receiver.run`)  
**Severity:** High (protocol hang)

**What it is:** The server sends a header with `"rows": cfg.rows`. The client reads this and then calls `recvall(sock, rows * nfft * 4)` bytes for each channel. If `striqt.evaluate_spectrogram` returns a spectrogram with fewer time bins than `rows` (e.g., due to rounding in the `frequency_resolution` parameter), the defensive fixup at line 365:
```python
if spg.shape[1] != rows:
    spg = spg[:, -rows:, :]
```
does not zero-pad to `rows`; in NumPy, `array[-n:]` when `n > len(array)` simply returns all available rows. The block sent has fewer rows than the header declares. The client blocks in `recvall()` waiting for bytes that never arrive, causing a permanent hang until the TCP connection times out.

**When it triggers:** Only in the `calibrated` backend. Triggered if `round(sample_rate / (sample_rate / nfft))` != `nfft` due to floating-point rounding with certain sample rate / nfft combinations, causing striqt to allocate a slightly different STFT frame count.

**Suggested fix:** After the fixup slice, add an explicit zero-pad to guarantee the output has exactly `rows` time bins:
```python
if spg.shape[1] < rows:
    pad = np.full((spg.shape[0], rows - spg.shape[1], nfft), spg.min(), dtype=np.float32)
    spg = np.concatenate([pad, spg], axis=1)
```

---

### Bug S-5 — `np.hanning` is deprecated; use `np.hann`
**Line:** 298  
**Severity:** Low

**What it is:** `np.hanning` was deprecated in NumPy 1.25 and removed in NumPy 2.0. Code running on a machine with NumPy ≥ 2.0 will crash with `AttributeError: module 'numpy' has no attribute 'hanning'`.

**When it triggers:** On any machine with NumPy ≥ 2.0 using the `quicklook` backend.

**Suggested fix:** Replace `np.hanning(nfft)` with `np.hann(nfft)`.

---

---

# 3. `striqt_standalone_terminal.py`

---

### Bug T-1 — `quick_spectrogram` is off by ~30 dB relative to the server's quicklook mode
**Lines:** 367–368  
**Severity:** High

**What it is:**
```python
mag = np.abs(fft_blocks)
db = (20.0 * np.log10(np.maximum(mag, 1e-12))).astype(np.float32)
```
The terminal's "quick" path computes `20·log₁₀(|X[k]|)` — the amplitude spectrum in dB — with **no normalization**.

The server's `db_spectrogram` (used in quicklook mode) computes:
```python
power = (np.abs(spec) ** 2) / max(float(nfft), 1.0)
db = 10.0 * np.log10(power + 1e-20)
```
which is `10·log₁₀(|X[k]|² / N)` = `20·log₁₀(|X[k]|) − 10·log₁₀(N)`.

The difference is `10·log₁₀(nfft)`:
- nfft = 256 → ~24 dB offset
- nfft = 1024 → ~30 dB offset
- nfft = 4096 → ~36 dB offset

A user comparing readings between the terminal (SSH session) and the TCP server/viewer will observe a 24–36 dB discrepancy in the "quick" path, depending on nfft. This is a measurement-relevant error for a scientific instrument.

**When it triggers:** Any time `--backend quick` is used, which is available and documented.

**Suggested fix:** Apply the same normalization as the server: divide `power` by `nfft` before taking `log10`. Also apply the window-power correction: divide by `np.sum(window**2)` rather than `nfft` to match a proper PSD estimate.

---

### Bug T-2 — `handle_key` uses stale `cfg` when multiple keys arrive in the same frame
**Lines:** 855–903 (especially lines 881–902)  
**Severity:** Medium

**What it is:** `curses_main` passes the current `cfg` snapshot to `handle_key`. When `handle_key` processes a key that calls `shared.update()`, the shared config is updated immediately — but `cfg` inside `handle_key` is still the pre-update snapshot. If the user presses two keys in the same 33 ms frame (e.g., `-` twice to step gain down by 2 dB), both calls use the same stale `cfg.gain`:
```python
# press 1: cfg.gain = 0.0  -> shared.update({"gain": 0.0 - 1.0}) = -1.0
# press 2: cfg.gain = 0.0  -> shared.update({"gain": 0.0 - 1.0}) = -1.0 again
```
Only one step is applied instead of two. The rate adjustment (`1`/`2` keys) and FFT-size adjustment (`[`/`]` keys) have the same problem.

**When it triggers:** Any two same-direction key presses arriving within a single 33 ms frame, which is common with keyboard auto-repeat.

**Suggested fix:** After each `shared.update()` call inside `handle_key`, re-snapshot: `cfg = shared.snapshot()` so subsequent key handlers in the same frame see the updated value.

---

### Bug T-3 — `parse_args()` clamps `--nfft` to `[256, 4096]` but not to valid preset values
**Lines:** 1057  
**Severity:** Medium

**What it is:**
```python
nfft=clamp_int(args.nfft, min(NFFTS), max(NFFTS)),
```
`clamp_int` restricts nfft to `[256, 4096]` but accepts any integer in that range (e.g., `--nfft 300`). The spectrogram functions then use this arbitrary value directly. `analysis_specs.Spectrogram(frequency_resolution=sample_rate / nfft)` computes `nfft = round(sample_rate / frequency_resolution)` internally, and floating-point rounding may produce a different integer than the requested `nfft`. This can trigger the shape-mismatch guard and cause the `RuntimeError` at line 337–339.

Additionally, the keyboard `[`/`]` controls step through `NFFTS = [256, 512, 1024, 2048, 4096]`, while the startup value may not be in this list, causing `nearest_index` to map to the wrong adjacent preset.

**When it triggers:** Any user-supplied `--nfft` value that is valid (in range) but not one of the five presets.

**Suggested fix:** Snap the argument to the nearest value in `NFFTS`:
```python
nfft = min(NFFTS, key=lambda x: abs(x - args.nfft))
```

---

### Bug T-4 — `draw_screen` crashes if `peak_freq` is `None`
**Lines:** 803–806  
**Severity:** Low

**What it is:**
```python
if s1 and s2:
    ...
    add_line(stdscr, y,
        "RX1 peak "
        f"{s1['peak_freq'] / 1e6:10.3f} MHz ..."  # crashes if peak_freq is None
    )
```
`summarize_channel` returns `{"peak_freq": None, ...}` when `block.ndim != 2 or block.size == 0`. The guard `if s1 and s2` checks dict truthiness (always True), not whether individual values are None. Dividing `None / 1e6` raises `TypeError`.

**When it triggers:** If the spectrogram block for a channel has zero size — possible in edge cases during rapid config changes while the ring buffer is still draining.

**Suggested fix:** Check `if s1 and s2 and s1["peak_freq"] is not None and s2["peak_freq"] is not None:`.

---

---

# 4. `striqt_standalone.py`

---

### Bug A-1 — Acquirer thread crashes permanently when recovery fails during a dirty-config cycle
**Lines:** 665–679 and 686–703  
**Severity:** Critical

**What it is:** When the user changes a radio parameter (dirty flag set), the acquirer calls `rearm()`. If `rearm()` fails, it calls `recover_radio()`, which closes `self.source` (setting it to `None`) and tries to reopen. If `open_radio()` inside `recover_radio()` also fails, the outer `except Exception as recover_err:` block at line 697 catches the error, logs it, and sleeps 1 second — then hits `continue` at line 679, which goes back to the top of the loop.

On the next iteration, `dirty` is `False` (already cleared), `recover_requested` is `False`, and there is **no check for `self.source is None`** before the stream read:
```python
# ... (no guard for self.source is None)
count = read_size
got, _ = self.source._read_stream(...)   # AttributeError: 'NoneType' object has no attribute '_read_stream'
```
`AttributeError` is not in the `except (ReceiveStreamError, OverflowError, OSError)` handler at line 694, so it propagates out of the while loop, hits `finally: close_source(self.source)` (which handles `None` safely), and the acquirer thread **dies permanently**. The GUI continues running but `get_latest()` returns `None` forever and the display freezes.

Compare with `striqt_standalone_terminal.py` lines 557–569, which has an explicit guard:
```python
if recover_requested or self.source is None or buffers is None:
    try:
        read_size, tmp, buffers = self.recover_radio(...)
    ...
    continue
```
This guard is missing from `striqt_standalone.py`.

**When it triggers:** Radio hardware failure or USB disconnect during a radio config change (e.g., user changes center frequency while the USB connection is briefly unstable).

**Suggested fix:** Add the `self.source is None` guard at the top of the non-dirty branch, identical to the terminal version:
```python
if self.source is None or buffers is None:
    try:
        read_size, tmp, buffers = self.recover_radio(cfg, "source lost")
    except Exception as recover_err:
        ...
        time.sleep(1.0)
    continue
```

---

### Bug A-2 — `print()` called while holding `self._lock` in `SharedConfig.update()`
**Lines:** 169–176  
**Severity:** Low

**What it is:**
```python
with self._lock:
    ...
    for key, old, value, label in changes:
        print(f"[config] changed ({label}): ...")   # I/O inside lock
    self._changes.extend(changes)
    self._dirty = True
    return changes
```
Python's `print()` acquires the internal `sys.stdout` lock. If another thread also holds `sys.stdout`'s lock while waiting on `self._lock`, a deadlock can occur. More practically, doing blocking I/O inside a mutex prolongs the critical section and increases latency for any other thread waiting on the lock.

**When it triggers:** Any config change from the GUI thread while the acquirer thread is simultaneously logging its own status.

**Suggested fix:** Move `print()` outside the `with self._lock:` block, or collect the strings inside the lock and print them after releasing it.

---

### Bug A-3 — `_save_csv` crashes with `AttributeError` if channel 0 buffer is absent
**Lines:** 1589–1596  
**Severity:** Low

**What it is:** Same as Bug F-2. `b0 = self.buffers.get(0)` may return `None` in edge cases, and the following `b0.mean(axis=0)` crashes.

**When it triggers:** Same edge case as F-2.

**Suggested fix:** Same as F-2 — guard with `if b0 is None: return`.

---

---

# 5. `pluto_standalone.py`

---

### Bug P-1 — `Air8201BSourceSpec` with incorrect `master_clock_rate` used for PlutoSDR
**Lines:** 344–356  
**Severity:** High

**What it is:**
```python
def make_source():
    source_spec = Air8201BSourceSpec(
        master_clock_rate=MASTER_CLOCK_RATE,   # = 125e6 Hz
        ...
    )
    source = PlutoSource(source_spec)
    source.setup()
    return source
```
`Air8201BSourceSpec` is the spec type for the Deepwave AIR8201B hardware. It is passed here to `PlutoSource`, which overrides the driver to `'plutosdr'`. However, `master_clock_rate=125e6` (125 MHz) is the AIR8201B's clock, not the PlutoSDR's. The PlutoSDR's reference oscillator is nominally 40 MHz with a sample clock of up to 61.44 MS/s derived from a 245.76 MHz TCXO. If the striqt/SoapySDR layer propagates `master_clock_rate` into an API call that sets the PlutoSDR's reference clock or validates sample rate divisibility, it will configure the hardware incorrectly, causing wrong frequency mapping, incorrect sample rates, or outright initialization failure.

**When it triggers:** Every startup of `pluto_standalone.py`.

**Suggested fix:** Create a `PlutoSourceSpec` or a generic `SoapyCapture`-compatible spec with PlutoSDR-appropriate parameters, or at minimum remove `master_clock_rate` from the spec and let the PlutoSDR driver use its own default.

---

### Bug P-2 — RX2 spectrogram panel is built and remains visible in single-channel mode
**Lines:** 958–976 (`_build_ui`), 1041–1047 (`_suppress_rx2`)  
**Severity:** Medium

**What it is:** `_build_ui()` always constructs both the RX1 and RX2 spectrogram plots and `ImageItem` objects (ch=0 and ch=1). `_suppress_rx2()` is called immediately after and hides the two RX2 **PSD curves** and disables the diff checkbox, but it does **not** hide or disable `self.specplots[1]` (the RX2 spectrogram panel) or `self.images[1]`.

In `on_frame`, `chans = header["channels"] = list(CHANNELS) = [0]`, so only channel 0 data is written to `self.buffers` and `self.images`. `self.images[1]` is never updated. `_apply_geometry()` iterates only over `self.buffers` keys (just `{0}`), so `self.images[1]` also has no axis/rect applied to it. The RX2 spectrogram panel is displayed in the GUI as an empty, unlabelled black rectangle taking up half the spectrogram area.

**When it triggers:** Every run of `pluto_standalone.py` with the default single-channel config.

**Suggested fix:** In `_suppress_rx2()`, also hide the RX2 spectrogram panel and its histogram:
```python
self.specplots[1].setVisible(False)
self.images[1].setVisible(False)
# remove or hide the associated HistogramLUTItem for ch=1
```

---

### Bug P-3 — Acquirer thread crashes permanently when recovery fails during dirty cycle (inherited)
**Lines:** 681–705 and 712–729  
**Severity:** Critical

**What it is:** Identical to Bug A-1 in `striqt_standalone.py`. The code is a direct copy and carries the same missing `self.source is None` guard. After a failed recovery during a dirty-config cycle, `self.source` is `None` and the next `self.source._read_stream(...)` call raises `AttributeError`, which kills the acquirer thread permanently.

**When it triggers:** Same conditions as A-1.

**Suggested fix:** Same as A-1 — add `if self.source is None or buffers is None:` guard before the stream read.

---

### Bug P-4 — `print()` called inside lock in `SharedConfig.update()` (inherited)
**Lines:** 193–199  
**Severity:** Low

**What it is:** Identical to Bug A-2. Copy-pasted from `striqt_standalone.py`.

**Suggested fix:** Same as A-2.

---

### Bug P-5 — `_save_csv` crashes with `AttributeError` if `b0` is None (inherited)
**Lines:** 1629–1636  
**Severity:** Low

**What it is:** Identical to Bug A-3. Copy-pasted from `striqt_standalone.py`.

**Suggested fix:** Same as A-3.

---

---

# Summary Table

| ID | File | Severity | Description |
|----|------|----------|-------------|
| F-1 | `striqt_frontend_TCP.py` | Low | QMutex not protected by try/finally → potential deadlock on BaseException |
| F-2 | `striqt_frontend_TCP.py` | Low | `_save_csv` crashes if channel-0 buffer is None |
| F-3 | `striqt_frontend_TCP.py` | Medium | Band-power average computed in dB domain (should be linear) |
| F-4 | `striqt_frontend_TCP.py` | Low | Manual color levels always read from channel 0, ignoring channel 1 |
| S-1 | `striqt_server_TCP.py` | **High** | Acquirer thread dies permanently on any stream error (no recovery path) |
| S-2 | `striqt_server_TCP.py` | **High** | `rearm()` leaves stale read_size/tmp/buffers after config change |
| S-3 | `striqt_server_TCP.py` | Medium | `SharedConfig.update()` marks dirty on no-change, causing spurious rearms |
| S-4 | `striqt_server_TCP.py` | **High** | Wire-protocol hang if calibrated spectrogram returns fewer rows than promised |
| S-5 | `striqt_server_TCP.py` | Low | `np.hanning` deprecated / removed in NumPy ≥ 2.0 |
| T-1 | `striqt_standalone_terminal.py` | **High** | `quick_spectrogram` off by 10·log₁₀(nfft) ≈ 24–36 dB vs server quicklook |
| T-2 | `striqt_standalone_terminal.py` | Medium | `handle_key` uses stale cfg; rapid key presses miss steps |
| T-3 | `striqt_standalone_terminal.py` | Medium | `--nfft` accepts non-preset values, may cause shape-mismatch crash |
| T-4 | `striqt_standalone_terminal.py` | Low | `draw_screen` crashes (`TypeError`) if `peak_freq` is None |
| A-1 | `striqt_standalone.py` | **Critical** | Acquirer thread crashes permanently when dirty-recovery fails (source is None) |
| A-2 | `striqt_standalone.py` | Low | `print()` inside lock in `SharedConfig.update()` |
| A-3 | `striqt_standalone.py` | Low | `_save_csv` crashes if channel-0 buffer is None |
| P-1 | `pluto_standalone.py` | **High** | `Air8201BSourceSpec` / wrong `master_clock_rate` (125 MHz) used for PlutoSDR |
| P-2 | `pluto_standalone.py` | Medium | RX2 spectrogram panel remains visible/active in single-channel mode |
| P-3 | `pluto_standalone.py` | **Critical** | Same acquirer-crash-on-dirty-recovery bug as A-1 (copy-pasted) |
| P-4 | `pluto_standalone.py` | Low | Same print-inside-lock as A-2 (copy-pasted) |
| P-5 | `pluto_standalone.py` | Low | Same `_save_csv` crash as A-3 (copy-pasted) |
