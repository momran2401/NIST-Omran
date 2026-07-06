# Audit context — intent docs

Drop this file in the repo at `context/AUDIT_CONTEXT.md` before running the audit. It gives
Fable the *intended* behavior to measure reality against. How to use each section:

- **Dan's emails** → authoritative design decisions. When code disagrees with these, that's a
  fidelity finding.
- **Transcript** → feature intent and the "why" (less-grainy spectrogram, PSD line + analysis
  dropdown, GPS sync, single viewer, threading). Softer than the emails but reveals intent.
- **Architecture write-up** → the intended end-to-end data flow, function by function. Use it
  to check the code does what the design says.
- **Capture/sweep JSON** → the intended config/schema shape the settings editor renders.

Two of these (the transcript and the architecture write-up) are long and you already have them
verbatim — paste them into the marked slots below so everything lives in one file.

---

## 1. Dan's emails (design decisions)

Context: Dan shared a gist and a NiceGUI json-editor link as a starting point for a
schema-driven settings editor.

> It was a little challenging to find automatic GUI forms based on JSON schema (most tools
> seemed to make guesses based on JSON data structure instead). Here's one possibility for a
> json editor widget that fits in with a python web-based backend toolkit:
> https://nicegui.io/documentation/json_editor
> That's one of the things we can talk about today.

**From: Kuester, Dan (Fed) — Wed, Jul 1, 2026 — Re: Another gist link for discussions today**
(Written up so Aric can catch up later.)

Answers to Mustafa's questions:
- For now, it's for the **live viewer** — other capture parameters can be left for the backend.
- What to expose in the GUI: source configuration would be nice, but like the capture, some
  parameters can be skipped. For soapy, leave out **`receive_retries`, `adc_overload_limit`,
  `if_overload_limit`, `gapless`**.
- Suggest allowing the viewer to **upload a sweep JSON file** as input. Loading it and
  converting to a python dict seeds default values in the GUI and sets "lower-level"
  parameters that are hidden / not configured in the GUI. (A slimmed-down JSON is attached —
  see §4.)
- Configuration is for the **currently shown analysis**. The file includes examples (ignore
  the `pss_sync` one — that's just used to enable triggering). There's a line that runs the
  spectrogram with **symbol alignment and averaging**; of its args it's safe to leave
  `as_xarray=False` constant to get simpler numpy arrays.
- On NiceGUI: "Seems like we'll wait for the future to play with nicegui on the radio."
- On source settings: they must be set **exactly once on opening** the radio. Changing them
  means **re-connecting** the radio. To avoid that problem, **limit to one viewer connection
  per radio**. The capture settings should then be changeable more dynamically (assuming one
  connection per radio).
- To Aric: idea to hook up a **GPS antenna** at his house to get nicely synced spectrograms.

**From: Omran, Mustafa — Tue, Jun 30, 2026 — his four questions to Dan**
1. Is this mainly for tuning the live viewer, or for authoring/validating capture (and
   eventually sweep) config files for recordings?
2. Just the capture, or also the source spec and the full sweep (loops, sink, etc.)?
3. Current NiceGUI needs Python 3.10+, but the DeepWave is on 3.9 — is this meant to run on
   the radio, or as a desktop tool? (If on the radio and matching the current dashboard,
   building the form into the existing UI from the same schema sidesteps that.)
4. If it ever applies settings to the live radio, that has to go through the viewer rather
   than a separate capture, because of the single-holder rule?

Mustafa's own summary of the striqt schema path: striqt's `json_schema()` hands back a
"rulebook" for a capture spec (types, valid ranges, allowed options, required fields), so the
editor gets validation basically for free from striqt's own definitions. Plan: a small
read-only endpoint that serves that schema, and a labeled form rendered in the existing
dashboard — no new heavy dependencies, inheriting the existing login and deployment.

---

## 2. Dan/Mustafa transcript (feature intent)

<!-- PASTE THE FULL "Dan & Mustafa Conversation" TRANSCRIPT HERE.
     Key intent points it contains, so you know what you're looking for:
     - The striqt spectrogram function takes slightly different args and is "less grainy";
       it averages into bins in groups of ~12 (tied to how the cellular waveforms assemble).
     - GPS antenna → clean, time-synced spectrograms (SSB start times every 20 ms vs UTC).
     - Dan likes the PSD line plot under the spectrogram; wants a dropdown/button to switch
       which analysis is shown; a checkbox menu to pick spectrogram/PSD/etc per port.
     - Restrict to ONE connection at a time (simplifies the UI person's life).
     - Threading: simultaneous acquisition + compute (don't stall the drain) — the "next step."
-->

(paste transcript here)

---

## 3. Web architecture write-up (intended end-to-end data flow)

<!-- PASTE THE FULL "How the Live RF Radio Viewer Works — End to End" DOC HERE.
     It defines the intended behavior of: the SDR/IQ, FFT/spectrogram/PSD, calibrated vs
     quicklook, striqt.sensor/.analysis roles, the Acquirer/Computer/broadcaster threads,
     the ring buffer + staleness/retune-clear, serialize_frame + quantize, the /ws control
     channel, static file serving, Basic Auth middleware, the Cloudflare tunnel, and the
     browser rendering (waterfalls, uPlot PSD, controls). Use it as the spec the code must match.
-->

(paste architecture write-up here)

---

## 4. Sample capture/sweep JSON (intended schema/config shape)

```json
{
  "sensor_binding": "air8201b",
  "source": {
    "master_clock_rate": 125000000.0,
    "trigger_strobe": 0.02,
    "signal_trigger": "cellular_5g_pss_sync",
    "array_backend": "cupy",
    "calibration": null,
    "time_source": "external",
    "time_sync_at": "acquire",
    "clock_source": "internal",
    "receive_retries": 3,
    "adc_overload_limit": -3.0,
    "if_overload_limit": -14.0,
    "gapless": false
  },
  "captures": [{
    "duration": 0.1,
    "sample_rate": 107520000.0,
    "analysis_bandwidth": 100000000.0,
    "port": [0, 1],
    "lo_shift": "none",
    "host_resample": true,
    "backend_sample_rate": null,
    "adjust_analysis": {
      "frequency_offset": "-40.08e6",
      "guard_bandwidths": ["875e3", "575e3"],
      "frame_slots": "dddsuudddddddsuudddd",
      "special_symbols": "ddddddfffffffu"
    },
    "center_frequency": 3750000000.0,
    "gain": -10.0
  }],
  "analysis": {
    "cellular_5g_pss_sync": {"subcarrier_spacing": 30000.0, "sample_rate": 7680000.0, "discovery_periodicity": 0.02, "frequency_offset": 0.0, "shared_spectrum": false, "delay": 0.0, "symbol_indexes": "c", "max_lag_symbols": null, "window_fill": 1.0, "per_port": true, "max_beams": 4},
    "cellular_5g_ssb_spectrogram": {"subcarrier_spacing": 30000.0, "sample_rate": 7680000.0, "discovery_periodicity": 0.02, "frequency_offset": 0.0, "max_block_count": null, "window": "blackmanharris", "lo_bandstop": 120000.0},
    "spectrogram": {"window": ["kaiser", 11.88], "frequency_resolution": 15000.0, "fractional_overlap": "13/28", "window_fill": "15/28", "integration_bandwidth": 360000.0, "trim_stopband": true, "lo_bandstop": 120000.0, "time_aperture": null},
    "power_spectral_density": {"window": ["kaiser", 11.88], "frequency_resolution": 15000.0, "fractional_overlap": "13/28", "window_fill": "15/28", "integration_bandwidth": 360000.0, "trim_stopband": true, "lo_bandstop": 120000.0, "time_statistic": ["mean", 0.5, 0.9, 0.95, 0.99, "max"]}
  },
  "extensions": {"sink": "striqt.sensor.sinks.NoSink"},
  "description": {}
}
```
