/**
 * app.js — striqt WebSocket live viewer
 *
 * Connects to /ws, receives binary spectrogram frames, renders two waterfall
 * canvases + an overlaid PSD chart (uPlot), and sends radio control messages
 * back to the server.
 *
 * Wire format (binary WebSocket message, server → browser):
 *   [4-byte LE uint32 : JSON header byte length]
 *   [JSON header bytes]
 *   [block-0 raw bytes]   rows×nfft float32-LE (or uint8 with "scale" header)
 *   [block-1 raw bytes]
 *
 * Control message (text JSON, browser → server):
 *   { center, sample_rate, gain, nfft, rows }   (any subset of these keys)
 */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let ws          = null;
let paused      = false;
let replaceMode = true;     // Boring Mode (replace) vs Cool Mode (scroll)
let absRF       = true;     // absolute RF freq vs baseband offset
let autoColor   = true;
let showDiff    = false;    // RX1−RX2 difference on PSD
let peakMarker  = true;
let peakHold    = false;
let showMin     = false;
let psdYspan    = null;     // null = auto; number = fixed dB span
let windowMs    = 20;
let analysisMode = "spectrogram";
let maxFps      = 15;       // client-side render-rate cap (LV-U1a)
let lastRender  = 0;        // performance.now() of the last rendered frame

// Current frame metadata (updated on each frame)
let curCenter   = 1955e6;
let curFs       = 15.36e6;
let radioNfft   = 1024;     // requested radio FFT size (from #nfft-sel); NEVER set from frame headers
let curBins     = 1024;     // bins in the current frame's blocks (from header "nfft")
let curRows     = 12;
let curBackend  = "calibrated";
let freqsMHz    = null;     // Float32Array(nfft)
let curF0       = null;     // header freqs_hz_f0 (true axis origin, Hz baseband)
let curStep     = null;     // header freqs_hz_step (true bin spacing, Hz)
let curFftNfft  = 1024;     // header fft_nfft (real FFT size behind the bin count)
let curBinAvg   = 1;        // header bin_avg (frequency-bin averaging factor)
let curHopSize  = null;     // header hop_size (samples of signal per display row, P2a-4)
let lastBackendWarn = null; // dedups the "SSB unavailable" status warning
let levels      = [-90, -10];
// PSD-backend state (P2b-4): server-computed statistic traces
let serverStats = null;     // header psd_stats — statistic behind each block row
let curSpanMs   = null;     // header time_span_ms — true integrated span
let uplotKind   = "std";    // which uPlot layout is built: "std" | "psd:<stats>"

// Per-channel display buffers [rows_displayed × nfft], newest row at index 0
const wfBuf   = { 0: null, 1: null };
// Peak-hold and min-trace per channel (Float32Array of length nfft)
const holdBuf = { 0: null, 1: null };
const minBuf  = { 0: null, 1: null };
// Last raw PSD data (mean+max per channel) for exports and band monitor
const psdData = {
    mean: { 0: null, 1: null },
    max:  { 0: null, 1: null },
    // PSD backend (P2b-4): server statistic traces
    // { stats: [...], traces: {0: [Float32Array per stat], 1: [...]} }
    server: null,
};

// FPS counter
let frameCount  = 0;
let lastFpsTime = performance.now();
let renderedFps = 0;

// Band selection (MHz) — draggable region over the PSD
let bandLo = null;
let bandHi = null;
let bandDrag = null;   // null | "lo" | "hi" | "body"

// uPlot instance
let uplot = null;

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

const logPre = document.getElementById("log-pre");
const MAX_LOG_LINES = 150;

function logMsg(msg, level = "INFO") {
    const ts   = new Date().toTimeString().slice(0, 8);
    const line = `[${ts}] ${level.padEnd(5)} ${msg}`;
    const lines = (logPre.textContent + "\n" + line).split("\n");
    if (lines.length > MAX_LOG_LINES) lines.splice(0, lines.length - MAX_LOG_LINES);
    logPre.textContent = lines.join("\n").trimStart();
    logPre.scrollTop   = logPre.scrollHeight;
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

const statusEl = document.getElementById("status-text");
const metaEl   = document.getElementById("meta-text");

function setStatus(text, cls = "") {
    statusEl.textContent = text;
    statusEl.className   = cls;
}

function updateMeta() {
    if (!curBins || !curFs) return;
    const depthRows = wfBuf[0] ? wfBuf[0].length / curBins : curRows;
    const winMs     = (depthRows * rowHopSamples() / curFs * 1e3).toFixed(0);
    const mode      = replaceMode ? "flicker" : "waterfall";
    const scale     = autoColor ? "auto" : "manual";
    const analysis  = curBackend;   // executed backend from the header (honest — LV-F2)
    // FFT label discloses radio size → real FFT size (bins × averaging) for the
    // calibrated/ssb averaged grid; plain radio size for the per-bin quicklook.
    const fftLabel  = curBackend === "quicklook"
        ? `${radioNfft}`
        : `${radioNfft}→${curFftNfft} (${curBins} bins × ${curBinAvg})`;
    // PSD backend: block rows are statistics, not time — label the true
    // integrated span from the header instead of the hop-derived window.
    const winLabel  = (serverStats && curSpanMs != null)
        ? `integration ${curSpanMs.toFixed(0)} ms (${serverStats.map(statLabel).join("/")})`
        : `window ${winMs} ms (${depthRows} rows)`;
    metaEl.textContent = (
        `LIVE | center ${(curCenter / 1e6).toFixed(3)} MHz | ` +
        `span ${(curFs / 1e6).toFixed(2)} MS/s | ` +
        `FFT ${fftLabel} | ${analysis} | ${mode} | ${winLabel} | ` +
        `scale ${scale} [${levels[0].toFixed(0)}, ${levels[1].toFixed(0)}] | ` +
        `${absRF ? "absolute RF" : "baseband"} | ${renderedFps.toFixed(0)} fps`
    );
    renderWfAxis();
}

// ---------------------------------------------------------------------------
// Frequency axis helpers
// ---------------------------------------------------------------------------

function buildFreqsMHz(center, fs, nfft, absoluteRF, f0, step) {
    const f = new Float32Array(nfft);
    if (f0 != null && step != null) {
        // Server-supplied true axis: correct for the calibrated DC-centered bin
        // groups (which drop edge bins) as well as the quicklook per-bin FFT.
        for (let i = 0; i < nfft; i++) {
            const baseHz = f0 + i * step;
            f[i] = absoluteRF ? (center + baseHz) / 1e6 : baseHz / 1e6;
        }
        return f;
    }
    for (let i = 0; i < nfft; i++) {
        // fftshifted fallback (old servers): bin 0 = most-negative, nfft/2 = DC
        const baseHz = ((i - nfft / 2) / nfft) * fs;
        f[i] = absoluteRF ? (center + baseHz) / 1e6 : baseHz / 1e6;
    }
    return f;
}

// Samples of signal one displayed STFT row spans. The server ships the exact
// value in the frame header (hop_size, P2a-4) — correct for any FFT size and
// fractional_overlap. Fallback for old headers: quicklook takes non-overlapping
// full-length FFTs (hop = nfft); calibrated/ssb use the default 13/28 overlap,
// so the hop is nfft·15/28.
function rowHopSamples() {
    if (curHopSize) return curHopSize;
    return Math.max(1, Math.round(radioNfft * (curBackend === "quicklook" ? 1 : 15 / 28)));
}

// Absolute ceiling on client-side display rows — matches the server's
// MAX_ROWS_ABS (P1-5). Protects browser render/memory; the old 300 clamp pinned
// every long duration to the same span and made the Duration control inert.
const CLIENT_MAX_ROWS = 4096;

// Rows the display window spans. windowMs of signal advances by rowHopSamples()
// per STFT row, so rows = windowMs·fs / hop. The cap is a generous safety
// ceiling (not a low clamp), so a longer duration honestly renders more rows —
// the meta/axis ms label reflects the actual rows shown.
function rowsForWindowMs(ms) {
    return Math.max(1, Math.min(Math.round(ms / 1000 * curFs / rowHopSamples()), CLIENT_MAX_ROWS));
}

// Send the time-axis control (P2a-4). Duration stays the single owner (P1-4):
// in replace (Boring) mode the SERVER derives rows hop-aware from a first-class
// capture.duration — so the JSON drives the radio honestly and the ↕ ms label
// (computed from header hop_size) matches exactly. In scroll (Cool) mode the
// client display depth follows windowMs and the server streams fixed 12-row
// frame chunks (an explicit rows control, which reclaims rows ownership).
function sendTimeControl() {
    if (replaceMode) sendControl({ capture: { duration: windowMs / 1000 } });
    else             sendControl({ rows: 12 });
}

// SSB is always selectable now (P2b-5): when the current rate is off the SSB
// symbol grid, the SERVER retunes the capture rate to the nearest compatible
// one and reports the change through the settings ack (handleAck shows it) —
// no silent fallback, no disabled option guessing at the server's grid.
function updateSsbOption() {
    const opt = document.querySelector('#analysis-sel option[value="ssb"]');
    if (!opt) return;
    opt.disabled = false;
    opt.title = "May retune the capture sample rate onto the SSB symbol grid (reported in the log)";
}

// ---------------------------------------------------------------------------
// WebSocket connection
// ---------------------------------------------------------------------------

function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
        setStatus("connected", "ok");
        logMsg("WebSocket connected");
        // Tell the server our initial time window (duration in replace mode,
        // fixed frame chunks in scroll mode — P2a-4)
        sendTimeControl();
    };

    ws.onmessage = (e) => {
        if (typeof e.data === "string") {
            try {
                const msg = JSON.parse(e.data);
                if (msg.message && msg.message !== "ping") logMsg(msg.message);
                if (msg.ack) {
                    handleAck(msg.ack);
                    // Re-sync forms + radioNfft with what the server actually
                    // runs (it may have rounded or rejected inputs) — P2a-5.
                    scheduleConfigRefresh();
                }
            } catch (_) {}
            return;
        }
        if (!paused) onFrame(e.data);
    };

    ws.onclose = (event) => {
        // Distinct close codes (LV-R3): 1008 = auth failed, 4001 = viewer slot busy.
        if (event && event.code === 1008) {
            setStatus("authentication failed — reload to log in", "error");
            logMsg("WebSocket closed: authentication failed (1008)", "ERROR");
            return;   // do NOT reconnect on an auth failure
        }
        if (event && event.code === 4001) {
            setStatus("another viewer is connected — retrying…", "warn");
            logMsg("Viewer slot busy (4001); retrying in 1.2 s", "WARN");
        } else {
            setStatus("disconnected — reconnecting…", "warn");
            logMsg("WebSocket disconnected; retrying in 1.2 s", "WARN");
        }
        setTimeout(connect, 1200);
    };

    ws.onerror = () => ws.close();
}

function sendControl(ctrl) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(ctrl));
    }
}

// Surface the server's structured settings ack (P2a-2): what applied cleanly,
// what was rounded to a legal value ("invalid X → using Y"), what striqt
// rejected (last-good config kept). Rounded/rejected also land in the status
// line so the user sees it without watching the log.
function fmtAckValue(v) {
    if (typeof v === "number" && isFinite(v) && Math.abs(v) >= 1000) {
        return v.toLocaleString("en-US", { maximumFractionDigits: 1 });
    }
    return String(v);
}

function handleAck(ack) {
    const rounded  = ack.rounded  || [];
    const rejected = ack.rejected || [];
    for (const r of rounded) {
        logMsg(`invalid ${r.field}=${fmtAckValue(r.requested)} → using ${fmtAckValue(r.used)} (${r.reason})`, "WARN");
    }
    for (const r of rejected) {
        logMsg(`rejected ${r.field}=${fmtAckValue(r.requested)}: ${r.reason}`, "ERROR");
    }
    if (rejected.length) {
        setStatus(`rejected ${rejected.map((r) => r.field).join(", ")} — kept last-good config`, "error");
    } else if (rounded.length) {
        setStatus(`adjusted ${rounded.map((r) => r.field).join(", ")} to legal values`, "warn");
    }
}

// ---------------------------------------------------------------------------
// Frame parsing
// ---------------------------------------------------------------------------

function onFrame(data) {
    // ── Parse header ──────────────────────────────────────────────────────
    const dv      = new DataView(data);
    const hdrLen  = dv.getUint32(0, /*littleEndian=*/true);
    const hdrText = new TextDecoder().decode(new Uint8Array(data, 4, hdrLen));
    const header  = JSON.parse(hdrText);

    // Throttle to Max fps: skip the block parse + render for frames arriving faster
    // than 1000/maxFps. The header is already parsed; meta fps reflects the actual
    // render rate since the fps counter runs only on rendered frames (LV-U1a).
    const nowRender = performance.now();
    if (nowRender - lastRender < 1000 / maxFps) return;
    lastRender = nowRender;

    const { nfft, rows, channels, center, fs, gain, dtype, scale, backend,
            backend_requested, freqs_hz_f0, freqs_hz_step, fft_nfft, bin_avg,
            hop_size, psd_stats, time_span_ms } = header;
    let offset = 4 + hdrLen;

    // ── Parse blocks ──────────────────────────────────────────────────────
    const blocks = {};
    for (const ch of channels) {
        if (dtype === "uint8") {
            const nbytes = rows * nfft;
            const u8     = new Uint8Array(data, offset, nbytes);
            const f32    = new Float32Array(rows * nfft);
            const [vmin, vmax] = scale;
            const rng = vmax - vmin;
            for (let i = 0; i < nbytes; i++) f32[i] = vmin + (u8[i] / 255) * rng;
            blocks[ch] = f32;
            offset    += nbytes;
        } else {
            const nbytes = rows * nfft * 4;
            // slice() copies the bytes out of the message buffer
            blocks[ch] = new Float32Array(data.slice(offset, offset + nbytes));
            offset    += nbytes;
        }
    }

    // ── Update state when tuning changes ──────────────────────────────────
    const stepVal = (freqs_hz_step !== undefined && freqs_hz_step !== null) ? freqs_hz_step : null;
    const f0Val   = (freqs_hz_f0   !== undefined && freqs_hz_f0   !== null) ? freqs_hz_f0   : null;
    const tuningChanged = (
        nfft !== curBins || center !== curCenter || fs !== curFs || stepVal !== curStep
    );
    curBackend = backend || curBackend;
    curFftNfft = (fft_nfft !== undefined && fft_nfft !== null) ? fft_nfft : nfft;
    curBinAvg  = (bin_avg  !== undefined && bin_avg  !== null) ? bin_avg  : 1;
    curHopSize = (hop_size !== undefined && hop_size !== null) ? hop_size : null;
    serverStats = (curBackend === "psd" && psd_stats && psd_stats.length) ? psd_stats : null;
    curSpanMs   = (time_span_ms !== undefined && time_span_ms !== null) ? time_span_ms : null;

    // Honest backend reporting: warn once when the server had to substitute a
    // backend (e.g. SSB is unavailable at this sample rate) — LV-F2.
    if (backend_requested && backend && backend !== backend_requested) {
        const key = `${backend_requested}->${backend}`;
        if (lastBackendWarn !== key) {
            lastBackendWarn = key;
            setStatus(`${backend_requested.toUpperCase()} unavailable at this rate — showing ${backend}`, "warn");
            logMsg(`${backend_requested} unavailable at ${(fs / 1e6).toFixed(2)} MS/s — showing ${backend}`, "WARN");
        }
    } else if (lastBackendWarn !== null) {
        lastBackendWarn = null;
        setStatus("connected", "ok");
    }

    if (tuningChanged) {
        curBins   = nfft;
        curCenter = center;
        curFs     = fs;
        curF0     = f0Val;
        curStep   = stepVal;
        freqsMHz  = buildFreqsMHz(center, fs, nfft, absRF, curF0, curStep);
        updateSsbOption();
        // Clear hold/min on tuning change (freq-axis specific)
        holdBuf[0] = holdBuf[1] = null;
        minBuf[0]  = minBuf[1]  = null;
        uplotKind = null;   // force the renderer below to rebuild the right plot
        resetBand(freqsMHz);
    }
    curRows = rows;

    // ── Render ────────────────────────────────────────────────────────────
    if (serverStats) {
        // PSD backend (P2b-4): block rows are statistic traces, not time —
        // draw them directly; no waterfall to update.
        renderServerPsd(channels, blocks, rows, nfft);
    } else {
        psdData.server = null;
        if (uplotKind !== "std") initUplot(freqsMHz);
        for (const ch of channels) {
            updateWaterfall(ch, blocks[ch], rows, nfft, center, fs);
        }
        updatePSD(channels, blocks, rows, nfft);
    }
    updateBandMonitor(channels, blocks, rows, nfft);
    updateMeta();

    // ── FPS counter ───────────────────────────────────────────────────────
    frameCount++;
    const now = performance.now();
    if (now - lastFpsTime >= 1000) {
        renderedFps = frameCount / ((now - lastFpsTime) / 1000);
        frameCount  = 0;
        lastFpsTime = now;
    }
}

// ---------------------------------------------------------------------------
// Waterfall rendering
// ---------------------------------------------------------------------------

const wfCanvas = {
    0: document.getElementById("wf0"),
    1: document.getElementById("wf1"),
};
const wfCtx = {
    0: wfCanvas[0].getContext("2d"),
    1: wfCanvas[1].getContext("2d"),
};
const wfImageData = { 0: null, 1: null };

function computeDisplayDepth(rows, nfft, fs) {
    if (replaceMode) return rows;
    return rowsForWindowMs(windowMs);
}

function updateWaterfall(ch, block, rows, nfft, center, fs) {
    const depth = computeDisplayDepth(rows, nfft, fs);
    const size  = depth * nfft;

    // Reallocate if dimensions changed
    if (!wfBuf[ch] || wfBuf[ch].length !== size) {
        wfBuf[ch]           = new Float32Array(size).fill(-150);
        wfImageData[ch]     = new ImageData(nfft, depth);
        wfCanvas[ch].width  = nfft;
        wfCanvas[ch].height = depth;
    }

    const buf  = wfBuf[ch];
    const bLen = block.length;   // rows × nfft samples in the new block

    if (replaceMode) {
        // Replace entire display buffer with the new frame
        buf.fill(-150);
        buf.set(block.subarray(0, Math.min(bLen, size)));
    } else {
        // Scroll mode: shift existing rows down, prepend new rows at [0].
        const newRows = Math.min(bLen / nfft, depth);
        const keep    = (depth - newRows) * nfft;
        if (keep > 0) buf.copyWithin(newRows * nfft, 0, keep);
        // Write the block's rows reversed: the block is oldest-first, but row 0 of a
        // downward-scrolling waterfall must be the newest row — otherwise each frame
        // band is internally time-reversed (zigzag on bursty signals) — LV-R7.
        for (let r = 0; r < newRows; r++) {
            buf.set(block.subarray((newRows - 1 - r) * nfft, (newRows - r) * nfft), r * nfft);
        }
    }

    // ── Auto color levels (5th / 99th percentile of a subsample) ──────────
    if (autoColor) {
        const step = Math.max(1, Math.floor(size / 2000));
        const samp = [];
        for (let i = 0; i < size; i += step) samp.push(buf[i]);
        samp.sort((a, b) => a - b);
        const vmin = samp[Math.floor(samp.length * 0.05)];
        const vmax = samp[Math.floor(samp.length * 0.99)];
        levels = [vmin, vmax - vmin < 5 ? vmin + 5 : vmax];
    }

    // ── Render buffer → ImageData via viridis LUT ─────────────────────────
    const imgData  = wfImageData[ch].data;
    const LUT      = window.VIRIDIS_LUT;
    const [vmin, vmax] = levels;
    const rng      = vmax - vmin || 1;

    for (let i = 0; i < size; i++) {
        const t  = Math.max(0, Math.min(1, (buf[i] - vmin) / rng));
        const li = Math.round(t * 255) * 4;
        imgData[i * 4]     = LUT[li];
        imgData[i * 4 + 1] = LUT[li + 1];
        imgData[i * 4 + 2] = LUT[li + 2];
        imgData[i * 4 + 3] = 255;
    }
    wfCtx[ch].putImageData(wfImageData[ch], 0, 0);
}

// Populate the waterfall frequency-axis overlays (LV-F7). Five evenly spaced
// ticks from the true axis (LV-F1) plus the current hop-aware window on the right.
function renderWfAxis() {
    if (!freqsMHz || !freqsMHz.length) return;
    const divs = document.querySelectorAll(".wf-freq-axis");
    if (!divs.length) return;
    const n = freqsMHz.length;
    let spans = "";
    for (let k = 0; k < 5; k++) {
        const i = Math.round((k / 4) * (n - 1));
        spans += `<span>${freqsMHz[i].toFixed(1)} MHz</span>`;
    }
    const depthRows = wfBuf[0] ? wfBuf[0].length / curBins : curRows;
    const winMs = (depthRows * rowHopSamples() / curFs * 1e3).toFixed(0);
    spans += `<span class="wf-axis-win">↕ ${winMs} ms</span>`;
    divs.forEach((d) => { d.innerHTML = spans; });
}

// ---------------------------------------------------------------------------
// PSD (uPlot)
// ---------------------------------------------------------------------------

const PSD_BG    = "#0e1726";
const PSD_FG    = "#8b97a8";

// PSD trace palette — mean traces are bluish, max traces are reddish (channels
// distinguished by light/dark shade of the same hue family).
const COL = {
    rx1Mean: "#4ea3ff",   // RX1 mean — azure
    rx2Mean: "#9ac8ff",   // RX2 mean — light blue
    rx1Max:  "#ff5252",   // RX1 max  — red
    rx2Max:  "#ff9a9a",   // RX2 max  — light red
    rx1Hold: "rgba(255,82,82,0.45)",
    rx2Hold: "rgba(255,154,154,0.45)",
    rx1Min:  "rgba(78,163,255,0.6)",
    rx2Min:  "rgba(154,200,255,0.6)",
    diff:    "#e6e9ef",
};

// PSD y-axis label depends on the backend: calibrated/ssb values are band-
// integrated over one averaged bin (~+8.5 dB vs per-bin); quicklook is per-bin.
function psdYLabel() {
    return curBackend === "quicklook"
        ? "Power (dB rel. FS / bin)"
        : "Integrated power (dB rel. FS)";
}

function initUplot(freqs) {
    const container = document.getElementById("psd-plot");
    container.innerHTML = "";  // clear previous instance

    const w = document.getElementById("psd-container").clientWidth || 900;

    const opts = {
        width:  w,
        height: 300,
        title:  "Power Spectral Density (RX1 + RX2)",
        background: PSD_BG,
        cursor: {
            show:  true,
            drag:  { x: false, y: false },
            focus: { prox: 32 },
        },
        legend: { show: true, live: false },
        scales: {
            x: { time: false },
            y: { auto: true },
        },
        axes: [
            {
                label:  "Frequency (MHz)",
                stroke: PSD_FG, ticks: { stroke: PSD_FG }, grid: { stroke: "#243042" },
                font:   "11px Menlo,monospace",
            },
            {
                label:  psdYLabel(),
                stroke: PSD_FG, ticks: { stroke: PSD_FG }, grid: { stroke: "#243042" },
                font:   "11px Menlo,monospace",
            },
        ],
        series: [
            {},   // x (freqs)
            { label: "RX1 Mean", stroke: COL.rx1Mean, width: 2, show: true  },
            { label: "RX1 Max",  stroke: COL.rx1Max,  width: 2, show: true  },
            { label: "RX2 Mean", stroke: COL.rx2Mean, width: 2, show: true  },
            { label: "RX2 Max",  stroke: COL.rx2Max,  width: 2, show: true  },
            { label: "RX1 Hold", stroke: COL.rx1Hold, width: 1,
              dash: [4, 4], show: false },
            { label: "RX2 Hold", stroke: COL.rx2Hold, width: 1,
              dash: [4, 4], show: false },
            { label: "RX1 Min",  stroke: COL.rx1Min,  width: 1,
              dash: [2, 4], show: false },
            { label: "RX2 Min",  stroke: COL.rx2Min,  width: 1,
              dash: [2, 4], show: false },
            { label: "RX1−RX2",  stroke: COL.diff,    width: 2,    show: false },
        ],
        hooks: {
            draw: [drawPsdOverlays],
        },
    };

    const nfft   = freqs.length;
    // Each y-series must be an array the same length as the x-axis. uPlot reads
    // data[i].length on every series, so a bare null throws at construction —
    // initialize with all-null arrays (rendered as gaps) until the first frame.
    const empty  = Array.from({ length: 9 }, () => new Array(nfft).fill(null));
    uplot = new uPlot(opts, [Array.from(freqs), ...empty], container);
    uplotKind = "std";

    // Preserve the crosshair toggle across re-inits (a retune rebuilds the plot,
    // which would otherwise silently reset the cursor to "on") — LV-R9a.
    const crossChk = document.getElementById("cross-chk");
    if (crossChk) uplot.cursor.show = crossChk.checked;

    // Set up band dragging on the uPlot canvas
    setupBandDrag();
}

// ---------------------------------------------------------------------------
// PSD backend — server statistic traces (P2b-4)
// ---------------------------------------------------------------------------
//
// With backend "psd" the server runs striqt's power_spectral_density and each
// block row is one time_statistic trace (header psd_stats names them). The
// plot is rebuilt with one series per (channel, statistic); uPlot's clickable
// legend entries are the trace toggles, so the drawn set always reflects the
// REAL statistic list instead of a fixed mean/max pair.

function statLabel(stat) {
    const q = parseFloat(stat);
    if (isFinite(q) && String(q) === String(stat).trim()) {
        return "p" + (q * 100).toFixed(q * 100 % 1 ? 1 : 0);
    }
    const s = String(stat);
    return s.charAt(0).toUpperCase() + s.slice(1);
}

// Trace colors: mean stays the blue family, max the red family (matching the
// classic pair); other statistics cycle a distinct palette. Index 0 = RX1
// (saturated), 1 = RX2 (light shade of the same hue).
const STAT_COLS = {
    mean: ["#4ea3ff", "#9ac8ff"],
    max:  ["#ff5252", "#ff9a9a"],
    peak: ["#ff5252", "#ff9a9a"],
    min:  ["#7986cb", "#c5cae9"],
};
const QUANT_COLS = [
    ["#ffb74d", "#ffe0b2"],   // orange
    ["#ba68c8", "#e1bee7"],   // violet
    ["#4db6ac", "#b2dfdb"],   // teal
    ["#f06292", "#f8bbd0"],   // pink
    ["#dce775", "#f0f4c3"],   // lime
    ["#90a4ae", "#cfd8dc"],   // blue-grey
];

function statColors(stats) {
    let cycle = 0;
    return stats.map((s) => {
        const named = STAT_COLS[String(s).toLowerCase()];
        if (named) return named;
        return QUANT_COLS[cycle++ % QUANT_COLS.length];
    });
}

function initUplotPsdStats(freqs, stats) {
    const container = document.getElementById("psd-plot");
    container.innerHTML = "";
    const w = document.getElementById("psd-container").clientWidth || 900;
    const cols = statColors(stats);

    const series = [{}];
    for (const ch of [0, 1]) {
        stats.forEach((s, i) => {
            series.push({
                label:  `RX${ch + 1} ${statLabel(s)}`,
                stroke: cols[i][ch],
                width:  2,
                show:   true,
            });
        });
    }

    const opts = {
        width:  w,
        height: 300,
        title:  "Power Spectral Density — striqt statistics (RX1 + RX2)",
        background: PSD_BG,
        cursor: {
            show:  true,
            drag:  { x: false, y: false },
            focus: { prox: 32 },
        },
        legend: { show: true, live: false },
        scales: { x: { time: false }, y: { auto: true } },
        axes: [
            {
                label:  "Frequency (MHz)",
                stroke: PSD_FG, ticks: { stroke: PSD_FG }, grid: { stroke: "#243042" },
                font:   "11px Menlo,monospace",
            },
            {
                label:  psdYLabel(),
                stroke: PSD_FG, ticks: { stroke: PSD_FG }, grid: { stroke: "#243042" },
                font:   "11px Menlo,monospace",
            },
        ],
        series,
        hooks: { draw: [drawPsdOverlays] },
    };

    const nfft  = freqs.length;
    const empty = Array.from({ length: series.length - 1 },
                             () => new Array(nfft).fill(null));
    uplot = new uPlot(opts, [Array.from(freqs), ...empty], container);
    uplotKind = "psd:" + stats.join(",");

    const crossChk = document.getElementById("cross-chk");
    if (crossChk) uplot.cursor.show = crossChk.checked;
    setupBandDrag();
}

function renderServerPsd(channels, blocks, rows, nfft) {
    if (!freqsMHz || !serverStats) return;
    const stats = serverStats;
    const kind  = "psd:" + stats.join(",");
    if (!uplot || uplotKind !== kind) initUplotPsdStats(freqsMHz, stats);

    const nStats  = Math.min(stats.length, rows);
    const freqArr = Array.from(freqsMHz);
    const gaps    = new Array(nfft).fill(null);
    const data    = [freqArr];
    const traces  = { 0: [], 1: [] };
    for (const ch of [0, 1]) {
        const block = blocks[ch];
        for (let s = 0; s < stats.length; s++) {
            if (!block || s >= nStats) {
                data.push(gaps);
                traces[ch].push(null);
                continue;
            }
            const tr = block.subarray(s * nfft, (s + 1) * nfft);
            traces[ch].push(tr);
            data.push(Array.from(tr));
        }
    }
    uplot.setData(data);
    psdData.server = { stats, traces };

    // Peak markers from the most peak-like trace (max if present, else the
    // last statistic), respecting the existing Peak marker checkbox.
    if (peakMarker) {
        let idx = stats.findIndex((s) => String(s).toLowerCase() === "max");
        if (idx < 0) idx = stats.length - 1;
        peakMarkerData = {
            rx1: bestBin(traces[0][idx], freqArr),
            rx2: bestBin(traces[1][idx], freqArr),
        };
    }
    applyYspan();
}

function psdSeries(channels, blocks, rows, nfft) {
    /**
     * Compute mean and max PSD curves from the current display buffers
     * (not just the latest frame), so the PSD reflects the same window
     * that's shown in the waterfall.
     */
    const mean = {}, max = {}, min = {}, diff = null;

    for (const ch of [0, 1]) {
        const buf = wfBuf[ch];
        if (!buf) continue;
        const depth = buf.length / nfft;
        const m = new Float32Array(nfft);
        const x = new Float32Array(nfft).fill(-Infinity);
        const n = new Float32Array(nfft).fill(Infinity);

        for (let r = 0; r < depth; r++) {
            const off = r * nfft;
            for (let f = 0; f < nfft; f++) {
                const v = buf[off + f];
                m[f] += Math.pow(10, v / 10);   // accumulate LINEAR power (LV-F3)
                if (v > x[f]) x[f] = v;
                if (v < n[f]) n[f] = v;
            }
        }
        // Convert the linear-power mean back to dB. Averaging dB directly
        // underreports the time-averaged power of fluctuating signals; this
        // mirrors the band monitor's (correct) linear convention.
        for (let f = 0; f < nfft; f++) m[f] = 10 * Math.log10(Math.max(m[f] / depth, 1e-20));

        mean[ch] = m;
        max[ch]  = x;
        min[ch]  = n;

        // Cache for band monitor + exports
        psdData.mean[ch] = m;
        psdData.max[ch]  = x;
    }

    return { mean, max, min };
}

function updatePSD(channels, blocks, rows, nfft) {
    if (!uplot || !freqsMHz) return;

    const { mean, max, min } = psdSeries(channels, blocks, rows, nfft);

    // Update peak hold
    for (const ch of [0, 1]) {
        if (!max[ch]) continue;
        if (peakHold) {
            if (!holdBuf[ch] || holdBuf[ch].length !== nfft) {
                holdBuf[ch] = new Float32Array(max[ch]);
            } else {
                for (let i = 0; i < nfft; i++) {
                    if (max[ch][i] > holdBuf[ch][i]) holdBuf[ch][i] = max[ch][i];
                }
            }
        }
        if (showMin) {
            if (!minBuf[ch] || minBuf[ch].length !== nfft) {
                minBuf[ch] = new Float32Array(min[ch]);
            } else {
                for (let i = 0; i < nfft; i++) {
                    if (min[ch][i] < minBuf[ch][i]) minBuf[ch][i] = min[ch][i];
                }
            }
        }
    }

    const freqArr = Array.from(freqsMHz);

    // Never hand uPlot a bare null series — it reads data[i].length. Any series
    // that is toggled off or not yet available becomes a length-nfft array of
    // nulls, which uPlot renders as gaps (drawing nothing).
    const gaps = new Array(nfft).fill(null);
    let s1m = mean[0] ? Array.from(mean[0]) : gaps;
    let s1x = max[0]  ? Array.from(max[0])  : gaps;
    let s2m = mean[1] ? Array.from(mean[1]) : gaps;
    let s2x = max[1]  ? Array.from(max[1])  : gaps;
    let h1  = (peakHold && holdBuf[0]) ? Array.from(holdBuf[0]) : gaps;
    let h2  = (peakHold && holdBuf[1]) ? Array.from(holdBuf[1]) : gaps;
    let n1  = (showMin  && minBuf[0])  ? Array.from(minBuf[0])  : gaps;
    let n2  = (showMin  && minBuf[1])  ? Array.from(minBuf[1])  : gaps;
    let dif = (showDiff && mean[0] && mean[1])
            ? Array.from(mean[0]).map((v, i) => v - mean[1][i]) : gaps;

    // Series order: freqs, mean0, max0, mean1, max1, hold0, hold1, min0, min1, diff
    uplot.setData([freqArr, s1m, s1x, s2m, s2x, h1, h2, n1, n2, dif]);

    // Show/hide series
    const vis = [
        true,
        !showDiff, !showDiff,     // mean/max RX1
        !showDiff, !showDiff,     // mean/max RX2
        peakHold && !showDiff,
        peakHold && !showDiff,
        showMin  && !showDiff,
        showMin  && !showDiff,
        showDiff,                  // diff
    ];
    vis.forEach((v, i) => { if (i > 0) uplot.setSeries(i, { show: v }); });

    // Peak markers (strongest bin per visible channel) — LV-U1b
    if (peakMarker && !showDiff) {
        drawPeakMarker(s1x, s2x, freqArr);
    }

    // Fixed Y-span
    applyYspan();
}

// Peak markers: computed here, drawn each frame via uPlot's redraw hook. One per
// channel (RX1/RX2 max traces), so the label is no longer RX1-only (LV-U1b).
let peakMarkerData = null;
function bestBin(arr, freqArr) {
    if (!arr) return null;
    let bestI = 0;
    for (let i = 1; i < arr.length; i++) {
        if (arr[i] !== null && (arr[bestI] === null || arr[i] > arr[bestI])) bestI = i;
    }
    const v = arr[bestI];
    return (v === null || v === undefined) ? null : { freq: freqArr[bestI], power: v };
}
function drawPeakMarker(s1x, s2x, freqArr) {
    peakMarkerData = { rx1: bestBin(s1x, freqArr), rx2: bestBin(s2x, freqArr) };
}

// uPlot draw hook — overlays: peak marker, band selection
function drawPsdOverlays(u) {
    const ctx = u.ctx;
    ctx.save();

    // ── Peak markers (one per visible channel) ────────────────────────────
    if (peakMarker && peakMarkerData && !showDiff) {
        const drawOne = (pm, color, tag) => {
            if (!pm) return;
            const px = u.valToPos(pm.freq,  "x", true);
            const py = u.valToPos(pm.power, "y", true);
            if (!px || !py) return;
            ctx.beginPath();
            ctx.arc(px, py, 5, 0, 2 * Math.PI);
            ctx.fillStyle   = color;
            ctx.strokeStyle = "#000";
            ctx.lineWidth   = 1;
            ctx.fill();
            ctx.stroke();
            ctx.fillStyle = color;
            ctx.font      = "bold 11px Menlo,monospace";
            ctx.fillText(`${tag} ${pm.freq.toFixed(3)} MHz  ${pm.power.toFixed(1)} dB`, px + 8, py - 5);
        };
        drawOne(peakMarkerData.rx1, COL.rx1Max, "RX1");
        drawOne(peakMarkerData.rx2, COL.rx2Max, "RX2");
    }

    // ── Band selection region ─────────────────────────────────────────────
    if (bandLo !== null && bandHi !== null) {
        const lo = Math.min(bandLo, bandHi);
        const hi = Math.max(bandLo, bandHi);
        const lx = u.valToPos(lo, "x", true);
        const rx = u.valToPos(hi, "x", true);
        const yt = u.bbox.top;
        const yh = u.bbox.height;
        if (lx !== null && rx !== null) {
            ctx.fillStyle   = "rgba(120,255,160,0.10)";
            ctx.strokeStyle = "rgba(120,255,160,0.85)";
            ctx.lineWidth   = 2;
            ctx.fillRect(lx, yt, rx - lx, yh);
            ctx.strokeRect(lx, yt, rx - lx, yh);
        }
    }

    ctx.restore();
}

function applyYspan() {
    if (!uplot) return;
    if (psdYspan === null) {
        // uPlot normalizes scale.auto into a function (fnOrSelf) at construction
        // and *calls* it as auto(self, resetScales) on every rescale. Assigning a
        // bare boolean here makes uPlot throw "e.auto is not a function" on the
        // next draw and the plot never paints — so always assign a function.
        uplot.scales.y.auto = () => true;
        return;
    }
    // Find highest displayed value across all visible curves and lock a span
    let peak = null;
    const d  = uplot.data;
    for (let s = 1; s < d.length; s++) {
        if (!d[s] || !uplot.series[s].show) continue;
        for (const v of d[s]) {
            if (v !== null && (peak === null || v > peak)) peak = v;
        }
    }
    if (peak !== null) {
        const head = psdYspan * 0.05;
        uplot.scales.y.auto = () => false;
        uplot.setScale("y", { min: peak - psdYspan + head, max: peak + head });
    }
}

// ---------------------------------------------------------------------------
// Band monitor
// ---------------------------------------------------------------------------

const bandMonitorEl = document.getElementById("band-monitor");

function updateBandMonitor(channels, blocks, rows, nfft) {
    if (!freqsMHz || bandLo === null || bandHi === null) {
        bandMonitorEl.textContent = "Band monitor: --";
        return;
    }
    const lo = Math.min(bandLo, bandHi);
    const hi = Math.max(bandLo, bandHi);

    // freqsMHz is sorted ascending — find the in-band index range once, instead of
    // an O(rows·nfft·bins) mask.includes() scan that froze at nfft 4096 (LV-R6).
    let loIdx = 0;
    while (loIdx < nfft && freqsMHz[loIdx] < lo) loIdx++;
    let hiIdx = nfft - 1;
    while (hiIdx >= 0 && freqsMHz[hiIdx] > hi) hiIdx--;
    if (loIdx > hiIdx) {
        bandMonitorEl.textContent = `Band ${lo.toFixed(3)}–${hi.toFixed(3)} MHz: no bins`;
        return;
    }
    const nBins = hiIdx - loIdx + 1;

    const band = {}, qual = {};
    for (const ch of [0, 1]) {
        // PSD backend (P2b-4): no waterfall window — integrate the mean trace
        // (or the first statistic) instead of the display buffer.
        let buf = null, depth = 0;
        if (psdData.server) {
            const stats = psdData.server.stats;
            let idx = stats.findIndex((s) => String(s).toLowerCase() === "mean");
            if (idx < 0) idx = 0;
            buf = psdData.server.traces[ch] ? psdData.server.traces[ch][idx] : null;
            depth = 1;
        } else {
            buf = wfBuf[ch];
            depth = buf ? buf.length / nfft : 0;
        }
        if (!buf) continue;

        // Correct linear-domain averaging (avoids the dB-averaging error)
        let sumInBand = 0, sumAll = 0;
        for (let r = 0; r < depth; r++) {
            const off = r * nfft;
            for (let i = 0; i < nfft; i++) {
                const lin = Math.pow(10, buf[off + i] / 10);
                sumAll += lin;
                if (i >= loIdx && i <= hiIdx) sumInBand += lin;
            }
        }
        const linBand = sumInBand / (nBins * depth);
        const linAll  = sumAll    / (nfft        * depth);
        band[ch] = 10 * Math.log10(Math.max(linBand, 1e-20));
        qual[ch] = 10 * Math.log10(Math.max(linBand, 1e-20))
                 - 10 * Math.log10(Math.max(linAll,  1e-20));
    }

    const segs = [`Band ${lo.toFixed(3)}–${hi.toFixed(3)} MHz (${nBins} bins)`];
    if (band[0] !== undefined) segs.push(`RX1 ${band[0].toFixed(1)} dB`);
    if (band[1] !== undefined) segs.push(`RX2 ${band[1].toFixed(1)} dB`);
    if (band[0] !== undefined && band[1] !== undefined) {
        segs.push(`Δ ${(band[0] - band[1]).toFixed(1)} dB`);
        segs.push(`Q RX1 ${qual[0] >= 0 ? "+" : ""}${qual[0].toFixed(1)} RX2 ${qual[1] >= 0 ? "+" : ""}${qual[1].toFixed(1)} dB`);
    }
    bandMonitorEl.textContent = segs.join("   |   ");
}

// ---------------------------------------------------------------------------
// Band selection drag (on the uPlot canvas)
// ---------------------------------------------------------------------------

function resetBand(freqs) {
    if (!freqs) return;
    const lo = freqs[Math.floor(freqs.length * 0.45)];
    const hi = freqs[Math.floor(freqs.length * 0.55)];
    bandLo = Math.min(lo, hi);
    bandHi = Math.max(lo, hi);
    if (uplot) uplot.redraw();
}

function setupBandDrag() {
    if (!uplot) return;
    const over = uplot.over;   // the event-capture div over the uPlot canvas

    let dragStart = null;
    let origLo, origHi;

    // Pointer events fire for mouse, touch and pen — touch-action:none keeps a
    // drag on a phone from scrolling the page instead of moving the band.
    over.style.touchAction = "none";

    function freqAtX(clientX) {
        const rect = over.getBoundingClientRect();
        const px   = clientX - rect.left;
        return uplot.posToVal(px, "x");
    }

    function hitTest(freq) {
        if (bandLo === null) return null;
        const lo = Math.min(bandLo, bandHi);
        const hi = Math.max(bandLo, bandHi);
        const tol = (hi - lo) * 0.12 + 0.05;   // MHz tolerance for handle grab
        if (Math.abs(freq - lo) < tol) return "lo";
        if (Math.abs(freq - hi) < tol) return "hi";
        if (freq > lo && freq < hi)    return "body";
        return null;
    }

    over.style.cursor = "crosshair";

    over.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        const f = freqAtX(e.clientX);
        if (f === null) return;
        const hit = hitTest(f);
        if (hit) {
            // Drag existing band
            dragStart = f;
            bandDrag  = hit;
            origLo    = bandLo;
            origHi    = bandHi;
            over.style.cursor = hit === "body" ? "grab" : "ew-resize";
        } else {
            // Draw new band
            bandLo = f;
            bandHi = f;
            bandDrag = "new";
            dragStart = f;
        }
        try { over.setPointerCapture(e.pointerId); } catch (_) {}
        e.preventDefault();
    });

    window.addEventListener("pointermove", (e) => {
        if (!bandDrag) return;
        const f = freqAtX(e.clientX);
        if (f === null) return;
        const delta = f - dragStart;
        if (bandDrag === "lo")   { bandLo = origLo + delta; }
        else if (bandDrag === "hi")  { bandHi = origHi + delta; }
        else if (bandDrag === "body"){ bandLo = origLo + delta; bandHi = origHi + delta; }
        else if (bandDrag === "new") { bandHi = f; }
        if (uplot) uplot.redraw();
    });

    window.addEventListener("pointerup", () => {
        if (bandDrag) {
            bandDrag = null;
            over.style.cursor = "crosshair";
        }
    });
}

// ---------------------------------------------------------------------------
// Export helpers
// ---------------------------------------------------------------------------

function savePsdCsv() {
    // PSD backend (P2b-4): export the server statistic traces, one column per
    // (channel, statistic).
    if (psdData.server && freqsMHz) {
        const { stats, traces } = psdData.server;
        const nfft = freqsMHz.length;
        const cols = [];
        for (const ch of [0, 1]) {
            for (const s of stats) cols.push(`rx${ch + 1}_${statLabel(s).toLowerCase()}_db`);
        }
        const rows = [
            `# backend=psd`,
            `# fft_nfft=${curFftNfft}`,
            `# bin_avg=${curBinAvg}`,
            `# time_statistic=${stats.join(";")}`,
            `# integration_span_ms=${curSpanMs != null ? curSpanMs.toFixed(3) : ""}`,
            "freq_mhz," + cols.join(","),
        ];
        for (let i = 0; i < nfft; i++) {
            const vals = [];
            for (const ch of [0, 1]) {
                for (let s = 0; s < stats.length; s++) {
                    const tr = traces[ch] ? traces[ch][s] : null;
                    vals.push(tr ? tr[i].toFixed(3) : "");
                }
            }
            rows.push(`${freqsMHz[i].toFixed(6)},${vals.join(",")}`);
        }
        const blob = new Blob([rows.join("\n")], { type: "text/csv" });
        const a    = document.createElement("a");
        a.href     = URL.createObjectURL(blob);
        a.download = `live_psd_stats_${Date.now()}.csv`;
        a.click();
        logMsg("PSD statistics CSV saved");
        return;
    }
    if (!freqsMHz || !psdData.mean[0]) {
        logMsg("No PSD data yet — try again after the first frame", "WARN");
        return;
    }
    const nfft = freqsMHz.length;
    const rows = [
        `# backend=${curBackend}`,
        `# fft_nfft=${curFftNfft}`,
        `# bin_avg=${curBinAvg}`,
        `# units=dB (uncalibrated, ${curBackend === "quicklook" ? "per-bin" : "band-integrated"})`,
        "freq_mhz,rx1_mean_db,rx1_max_db,rx2_mean_db,rx2_max_db",
    ];
    for (let i = 0; i < nfft; i++) {
        const m0 = psdData.mean[0] ? psdData.mean[0][i].toFixed(3) : "";
        const x0 = psdData.max[0]  ? psdData.max[0][i].toFixed(3)  : "";
        const m1 = psdData.mean[1] ? psdData.mean[1][i].toFixed(3) : "";
        const x1 = psdData.max[1]  ? psdData.max[1][i].toFixed(3)  : "";
        rows.push(`${freqsMHz[i].toFixed(6)},${m0},${x0},${m1},${x1}`);
    }
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const a    = document.createElement("a");
    a.href     = URL.createObjectURL(blob);
    a.download = `live_psd_${Date.now()}.csv`;
    a.click();
    logMsg("PSD CSV saved");
}

function exportPng() {
    // Composite the two waterfalls side by side, then the PSD below
    const c0 = wfCanvas[0], c1 = wfCanvas[1];
    const psdCanvas = uplot ? uplot.root.querySelector("canvas") : null;

    const W  = Math.max((c0.width + c1.width), psdCanvas ? psdCanvas.width : 0);
    const H  = Math.max(c0.height, c1.height) + (psdCanvas ? psdCanvas.height : 0) + 30;
    const out = document.createElement("canvas");
    out.width  = W;
    out.height = H;
    const ctx = out.getContext("2d");
    ctx.fillStyle = "#0e1726";
    ctx.fillRect(0, 0, W, H);
    ctx.drawImage(c0, 0, 0);
    ctx.drawImage(c1, c0.width, 0);
    if (psdCanvas) ctx.drawImage(psdCanvas, 0, Math.max(c0.height, c1.height));

    // Settings caption
    const ts  = new Date().toLocaleString();
    const capDepth = wfBuf[0] ? wfBuf[0].length / curBins : curRows;
    const capWinMs = (capDepth * rowHopSamples() / curFs * 1e3).toFixed(0);
    const capFft   = curBackend === "quicklook" ? `${radioNfft}` : `${radioNfft}→${curFftNfft}`;
    const cap = `${ts}  center ${(curCenter / 1e6).toFixed(3)} MHz  span ${(curFs / 1e6).toFixed(2)} MS/s  FFT ${capFft}  window ${capWinMs} ms`;
    ctx.fillStyle = "#d0d0d0";
    ctx.font      = "11px Menlo,monospace";
    ctx.fillText(cap, 10, H - 8);

    const a    = document.createElement("a");
    a.href     = out.toDataURL("image/png");
    a.download = `live_view_${Date.now()}.png`;
    a.click();
    logMsg("PNG exported");
}

// ---------------------------------------------------------------------------
// Resize handling
// ---------------------------------------------------------------------------

const resizeObserver = new ResizeObserver(() => {
    if (!uplot || !freqsMHz) return;
    const w = document.getElementById("psd-container").clientWidth;
    uplot.setSize({ width: w, height: 300 });
});
resizeObserver.observe(document.getElementById("psd-container"));

// ---------------------------------------------------------------------------
// Control wiring
// ---------------------------------------------------------------------------

function applyAnalysisMode() {
    document.body.classList.toggle("analysis-psd", analysisMode === "psd");
    document.body.classList.toggle("analysis-ssb", analysisMode === "ssb");
    // "PSD view" now selects the real striqt power_spectral_density backend
    // (P2b-4) — server-computed statistic traces — instead of the old client-only
    // waterfall-hide over the calibrated backend.
    const backend = analysisMode === "ssb" ? "ssb"
                  : analysisMode === "quicklook" ? "quicklook"
                  : analysisMode === "psd" ? "psd"
                  : "calibrated";
    wfBuf[0] = wfBuf[1] = null;
    holdBuf[0] = holdBuf[1] = null;
    minBuf[0] = minBuf[1] = null;
    sendControl({ backend });
    // Swap the Analysis panel to the selected analysis' parameter set (P2b-6).
    if (typeof renderAnalysisPanel === "function") renderAnalysisPanel();
    updateMeta();
}

// Center / span (sample_rate) / gain are set from the schema Capture Settings
// form now (they map to live radio params in SharedConfig.update) — the old
// "Radio (AIR-T)" bar and its handlers were removed in P1-3. FFT keeps a home
// as a static select in the Capture panel, wired below.
document.getElementById("nfft-sel").addEventListener("change", (e) => {
    const nfft = parseInt(e.target.value, 10);
    radioNfft = nfft;   // updated here and by the /config re-sync (P2a-5)
    // No client-side rows math: the server re-derives rows hop-aware from the
    // stored first-class duration whenever nfft changes (P2a-4).
    sendControl({ nfft });
});

// (P1-3) "Tune to band" was removed with the Radio bar. The PSD band-drag
// selection stays — it still drives the band monitor; only the tune action is
// gone.

const pauseBtn = document.getElementById("pause-btn");
pauseBtn.addEventListener("click", () => {
    paused = !paused;
    pauseBtn.textContent = paused ? "Resume" : "Pause";
    pauseBtn.classList.toggle("active", paused);
});

document.getElementById("mode-sel").addEventListener("change", (e) => {
    replaceMode = e.target.value === "replace";
    // Clear display buffers so mode switch starts clean
    wfBuf[0] = wfBuf[1] = null;
    sendTimeControl();
});

document.getElementById("analysis-sel").addEventListener("change", (e) => {
    analysisMode = e.target.value;
    applyAnalysisMode();
});

// Duration control (P1-4/P2a-4) — the single owner of the time axis. Presets
// are available in both modes; the "custom…" option + number box are DAN-only.
// The value is in ms; it drives `windowMs`. In replace (Boring) mode it is sent
// as a first-class `capture.duration` and the SERVER derives rows hop-aware; in
// scroll (Cool) mode the client display depth follows `windowMs` via
// computeDisplayDepth.
const durSel         = document.getElementById("dur-sel");
const durCustomLabel = document.getElementById("dur-custom-label");
const durCustom      = document.getElementById("dur-custom");

function applyDuration() {
    const proMode = document.body.classList.contains("mode-pro");
    let ms;
    if (durSel.value === "custom" && proMode) {
        durCustomLabel.style.display = "";
        ms = parseFloat(durCustom.value);
    } else {
        durCustomLabel.style.display = "none";
        ms = parseFloat(durSel.value);   // NaN if "custom" reached outside DAN
    }
    if (!isFinite(ms) || ms <= 0) return;
    windowMs = ms;
    // Replace mode: ship the duration itself — the server owns the hop-aware
    // duration→rows mapping (P2a-4). Scroll mode: the display depth follows
    // windowMs client-side; the server keeps streaming fixed 12-row chunks.
    if (replaceMode) sendControl({ capture: { duration: windowMs / 1000 } });
    updateMeta();
}

durSel.addEventListener("change", applyDuration);
durCustom.addEventListener("change", applyDuration);
durCustom.addEventListener("input", applyDuration);

document.getElementById("fps-sel").addEventListener("change", (e) => {
    maxFps = parseFloat(e.target.value) || 15;   // client-side render cap (LV-U1a)
});

document.getElementById("auto-color").addEventListener("change", (e) => {
    autoColor = e.target.checked;
});

document.getElementById("lo-null").addEventListener("change", (e) => {
    sendControl({ lo_null: e.target.checked });   // server-side DC-null toggle (LV-F8)
});

document.getElementById("abs-rf").addEventListener("change", (e) => {
    absRF    = e.target.checked;
    freqsMHz = buildFreqsMHz(curCenter, curFs, curBins, absRF, curF0, curStep);
    if (uplot && freqsMHz) initUplot(freqsMHz);
    resetBand(freqsMHz);
    renderWfAxis();
});

document.getElementById("reset-btn").addEventListener("click", () => {
    if (uplot) {
        uplot.scales.x.auto = () => true;
        uplot.scales.y.auto = () => true;
    }
});

document.getElementById("csv-btn").addEventListener("click", savePsdCsv);
document.getElementById("png-btn").addEventListener("click", exportPng);

document.getElementById("diff-chk").addEventListener("change", (e) => {
    showDiff = e.target.checked;
});

document.getElementById("peak-chk").addEventListener("change", (e) => {
    peakMarker = e.target.checked;
    if (!peakMarker) peakMarkerData = null;
});

document.getElementById("hold-chk").addEventListener("change", (e) => {
    peakHold = e.target.checked;
    if (!peakHold) { holdBuf[0] = holdBuf[1] = null; }
});

document.getElementById("clear-hold-btn").addEventListener("click", () => {
    holdBuf[0] = holdBuf[1] = null;
    logMsg("Peak hold cleared");
});

document.getElementById("min-chk").addEventListener("change", (e) => {
    showMin = e.target.checked;
    if (!showMin) { minBuf[0] = minBuf[1] = null; }
});

document.getElementById("cross-chk").addEventListener("change", (e) => {
    if (uplot) uplot.cursor.show = e.target.checked;
});

document.getElementById("yspan-sel").addEventListener("change", (e) => {
    psdYspan = e.target.value === "auto" ? null : parseFloat(e.target.value);
    if (psdYspan === null && uplot) {
        uplot.scales.y.auto = () => true;
    }
});

// ---------------------------------------------------------------------------
// Schema-driven settings editor
// ---------------------------------------------------------------------------

const SOURCE_SKIP = new Set(["receive_retries", "adc_overload_limit", "if_overload_limit", "gapless"]);
// `port` is intentionally excluded — it is fixed at both RX ports server-side
// (make_capture) because the two-waterfall UI depends on it (P1-2). The four
// analysis knobs are now wired through to the radio on the next re-arm.
// `duration` is intentionally excluded — the Display "Duration (ms)" control is
// the single owner of the time axis (P1-4). Keeping it here too would let two
// controls fight over `rows` (the old Window-vs-duration bug).
const captureFields = [
    "center_frequency", "sample_rate", "gain", "analysis_bandwidth",
    "lo_shift", "host_resample", "backend_sample_rate",
];
const sourceFields = [
    "master_clock_rate", "trigger_strobe", "signal_trigger", "array_backend",
    "calibration", "time_source", "time_sync_at", "clock_source",
];
let schemaDoc = null;
let hiddenSweepSettings = {};

function schemaDefs() {
    return schemaDoc && (schemaDoc.$defs || schemaDoc.definitions) || {};
}

function resolveSchema(schema) {
    if (!schema || !schema.$ref) return schema || {};
    const name = schema.$ref.split("/").pop();
    return schemaDefs()[name] || schema;
}

function scalarSchema(schema) {
    schema = resolveSchema(schema);
    if (schema.anyOf) {
        return resolveSchema(schema.anyOf.find((item) => item.type !== "null") || schema.anyOf[0]);
    }
    return schema;
}

function defaultFor(schema, fallback = "") {
    if (!schema) return fallback;
    if (Object.prototype.hasOwnProperty.call(schema, "default")) return schema.default;
    return fallback;
}

function makeField(group, name, schema, value) {
    const spec = scalarSchema(schema);
    const label = document.createElement("label");
    label.textContent = name.replaceAll("_", " ");

    let input;
    if (spec.enum) {
        input = document.createElement("select");
        for (const opt of spec.enum) {
            const option = document.createElement("option");
            option.value = opt;
            option.textContent = String(opt);
            input.appendChild(option);
        }
    } else if (spec.type === "boolean") {
        input = document.createElement("input");
        input.type = "checkbox";
    } else {
        input = document.createElement("input");
        input.type = spec.type === "integer" || spec.type === "number" ? "number" : "text";
        if (spec.type === "integer") input.step = "1";
        if (spec.type === "number") input.step = "any";
        if (typeof spec.minimum === "number") input.min = spec.minimum;
        if (typeof spec.maximum === "number") input.max = spec.maximum;
        if (typeof spec.exclusiveMinimum === "number") input.min = spec.exclusiveMinimum;
    }

    input.dataset.group = group;
    input.dataset.field = name;
    input.dataset.type = spec.type || "";
    setFieldValue(input, value ?? defaultFor(spec));
    label.appendChild(input);
    return label;
}

function setFieldValue(input, value) {
    if (value === null || value === undefined) value = "";
    if (Array.isArray(value)) value = value.join(",");
    if (input.type === "checkbox") {
        input.checked = Boolean(value);
    } else {
        input.value = String(value);
    }
}

function readFieldValue(input) {
    if (input.type === "checkbox") return input.checked;
    const raw = input.value.trim();
    if (raw === "") return null;
    if (input.dataset.type === "integer") return parseInt(raw, 10);
    if (input.dataset.type === "number") return parseFloat(raw);
    if (raw.includes(",") && input.dataset.field === "port") {
        return raw.split(",").map((item) => parseInt(item.trim(), 10)).filter((item) => !Number.isNaN(item));
    }
    return raw;
}

function renderSettings(schema, seed = {}) {
    schemaDoc = schema;
    hiddenSweepSettings = seed;
    const defs = schemaDefs();
    const sweep = defs.air8201b || resolveSchema(schema);
    const source = resolveSchema(sweep.properties.source);
    const capture = resolveSchema((sweep.properties.captures || {}).items);
    const sourceValues = seed.source || {};
    const captureValues = (seed.captures && seed.captures[0]) || {};

    const captureForm = document.getElementById("capture-settings-form");
    const sourceForm = document.getElementById("source-settings-form");
    captureForm.textContent = "";
    sourceForm.textContent = "";

    for (const name of captureFields) {
        if (capture.properties && capture.properties[name]) {
            captureForm.appendChild(makeField("capture", name, capture.properties[name], captureValues[name]));
        }
    }
    for (const name of sourceFields) {
        if (!SOURCE_SKIP.has(name) && source.properties && source.properties[name]) {
            sourceForm.appendChild(makeField("source", name, source.properties[name], sourceValues[name]));
        }
    }
}

function collectSettings() {
    const payload = { capture: {}, source: {} };
    document.querySelectorAll("#settings-editor input, #settings-editor select").forEach((input) => {
        payload[input.dataset.group][input.dataset.field] = readFieldValue(input);
    });
    return payload;
}

// ---------------------------------------------------------------------------
// Server-config seeding (P2a-5)
// ---------------------------------------------------------------------------
//
// Forms seed from the server's CURRENT config (/config), not the striqt schema
// defaults, so a bare Apply re-sends exactly what the server already runs — no
// silent flips of untouched fields (e.g. schema host_resample=true vs server
// false). Also the re-sync path after every settings/analysis ack, which keeps
// radioNfft and the panel values honest when the server rounds an input.

async function fetchConfig() {
    const resp = await fetch("/config", { cache: "no-store" });
    if (!resp.ok) throw new Error(`config HTTP ${resp.status}`);
    return resp.json();
}

function seedStaticControls(config) {
    const cap = (config && config.capture) || {};
    if (cap.nfft) {
        radioNfft = cap.nfft;   // /config re-sync — the other radioNfft updater
        const sel = document.getElementById("nfft-sel");
        if (sel) sel.value = String(cap.nfft);
    }
    if (cap.duration) {
        const ms = cap.duration * 1000;
        windowMs = ms;
        const preset = Array.from(durSel.options)
            .map((o) => o.value)
            .find((v) => parseFloat(v) === ms);
        if (preset) {
            durSel.value = preset;
            durCustomLabel.style.display = "none";
        } else {
            durSel.value = "custom";
            durCustom.value = String(ms);
            if (document.body.classList.contains("mode-pro")) {
                durCustomLabel.style.display = "";
            }
        }
    }
}

function seedCaptureForm(config) {
    const cap = (config && config.capture) || {};
    document.querySelectorAll("#capture-settings-form input, #capture-settings-form select")
        .forEach((input) => {
            const name = input.dataset.field;
            if (name in cap) setFieldValue(input, cap[name]);
        });
}

let configRefreshTimer = null;
function scheduleConfigRefresh() {
    if (configRefreshTimer) return;
    configRefreshTimer = setTimeout(async () => {
        configRefreshTimer = null;
        try {
            const config = await fetchConfig();
            seedStaticControls(config);
            seedCaptureForm(config);
            if (typeof seedAnalysisForm === "function") seedAnalysisForm(config);
        } catch (_) { /* transient — next ack retries */ }
    }, 250);
}

async function loadSchema(seed = null) {
    const resp = await fetch("/schema", { cache: "no-store" });
    if (!resp.ok) throw new Error(`schema HTTP ${resp.status}`);
    const schema = await resp.json();
    let effSeed = seed;
    if (!effSeed) {
        try {
            const config = await fetchConfig();
            effSeed = { captures: [config.capture || {}], source: {} };
            seedStaticControls(config);
            if (typeof seedAnalysisForm === "function") seedAnalysisForm(config);
        } catch (err) {
            logMsg(`Config load failed (${err.message}); using schema defaults`, "WARN");
            effSeed = {};
        }
    }
    renderSettings(schema, effSeed);
}

document.getElementById("settings-apply").addEventListener("click", () => {
    // Merge the hidden lower-level params from an uploaded sweep JSON under the
    // visible form values (form wins), so uploading a sweep actually seeds them
    // instead of being silently dropped (LV-F6).
    const form = collectSettings();
    const hiddenCapture = (hiddenSweepSettings.captures && hiddenSweepSettings.captures[0]) || {};
    const hiddenSource  = hiddenSweepSettings.source || {};
    const payload = {
        capture: { ...hiddenCapture, ...form.capture },
        source:  { ...hiddenSource,  ...form.source  },
    };
    sendControl(payload);
    logMsg("Settings sent");
});

document.getElementById("settings-upload").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
        const seed = JSON.parse(await file.text());
        await loadSchema(seed);
        logMsg("Settings JSON loaded");
    } catch (err) {
        logMsg(`Settings JSON failed: ${err.message}`, "ERROR");
    }
});

// ---------------------------------------------------------------------------
// Analysis panel (P2a-6, per-analysis P2b-6) — DAN-mode editors for the striqt
// analysis params. The rendered field set follows the Analysis dropdown
// (spectrogram / PSD / SSB), so the config always targets the shown analysis.
// ---------------------------------------------------------------------------
//
// Free-text by design (the freedom model): values are sent raw as
// {"analysis": {"target": …, …}} and the SERVER snaps knowable constraints
// ("invalid X → using Y" via handleAck) or lets striqt scratch-validate the
// rest before the live stream sees anything. Fields seed from /config and
// re-seed after every ack, so the panel always shows what the server runs.

const SHARED_FREQ_FIELDS = [
    { key: "window", label: "window", ph: "kaiser, 11.88",
      title: "scipy get_window spec: a name (hann, blackmanharris, …) or name, parameter (kaiser, 11.88)" },
    { key: "frequency_resolution", label: "frequency resolution (Hz)", ph: "15238.1",
      title: "Hz per FFT bin — the other view of FFT size; snaps to the nearest legal FFT size" },
    { key: "fractional_overlap", label: "fractional overlap", ph: "13/28",
      title: "fraction of each FFT window shared with its neighbor, e.g. 13/28 or 0.46; snaps to k/nfft" },
    { key: "window_fill", label: "window fill", ph: "15/28",
      title: "fraction of the window filled by the taper (rest zeroed), e.g. 15/28; snaps to k/nfft" },
    { key: "integration_bandwidth", label: "integration bandwidth (Hz)", ph: "auto | none | Hz",
      title: "RMS frequency-bin averaging width: auto (tracks FFT size), none, or Hz (snaps to a multiple of the resolution)" },
    { key: "lo_bandstop", label: "LO bandstop (Hz)", ph: "none | Hz",
      title: "width nulled at DC by striqt: none, or Hz" },
];
const TRIM_FIELD = {
    key: "trim_stopband", label: "trim stopband", checkbox: true,
    title: "trim the frequency axis to the capture analysis_bandwidth (needs a finite analysis_bandwidth)",
};

const ANALYSIS_PANELS = {
    spectrogram: {
        target: "spectrogram", configKey: "analysis",
        badge: "calibrated spectrogram — validated before going live",
        fields: [
            ...SHARED_FREQ_FIELDS,
            { key: "time_aperture", label: "time aperture (s)", ph: "none | s",
              title: "binned RMS averaging along the time axis: none, or seconds (snaps to a multiple of the row hop)" },
            TRIM_FIELD,
        ],
    },
    psd: {
        target: "psd", configKey: "analysis_psd",
        badge: "striqt power_spectral_density — one trace per statistic",
        fields: [
            ...SHARED_FREQ_FIELDS,
            { key: "time_statistic", label: "time statistics", ph: "mean, 0.95, max",
              title: "statistics evaluated along the time axis — names (mean/max/min/rms/median) and/or quantiles in [0,1]; one PSD trace each" },
            TRIM_FIELD,
        ],
    },
    ssb: {
        target: "ssb", configKey: "analysis_ssb",
        badge: "5G SSB burst view — may retune the capture rate onto the symbol grid",
        fields: [
            { key: "subcarrier_spacing", label: "subcarrier spacing (Hz)", ph: "30000",
              title: "3GPP SCS (15000/30000/60000 …); selecting SSB retunes the capture rate onto the 14·scs grid (reported)" },
            { key: "sample_rate", label: "SSB output rate (S/s)", ph: "7680000",
              title: "output rate of the recentered SSB band; cannot exceed the sampled span" },
            { key: "discovery_periodicity", label: "discovery period (s)", ph: "0.02",
              title: "time between synchronization bursts; ≥ one 2 ms burst set and one period must fit the IQ ring" },
            { key: "frequency_offset", label: "frequency offset (Hz)", ph: "0",
              title: "SSB center offset from the capture center; snaps to the subcarrier grid and must keep the band in the span" },
            { key: "max_block_count", label: "max burst sets", ph: "none | count",
              title: "cap on synchronization bursts evaluated per frame, or none" },
            { key: "window", label: "window", ph: "blackmanharris",
              title: "scipy get_window spec for the SSB STFT" },
            { key: "lo_bandstop", label: "LO bandstop (Hz)", ph: "none | Hz",
              title: "width nulled at DC by striqt: none, or Hz" },
        ],
    },
    quicklook: {
        target: null, configKey: null,
        badge: "raw per-bin FFT — no analysis parameters",
        fields: [],
    },
};

let lastConfig = null;      // latest /config payload — seeds panel switches
let renderedPanel = null;   // key into ANALYSIS_PANELS currently in the DOM

function analysisFieldValue(v) {
    if (v === null || v === undefined) return "none";
    if (Array.isArray(v)) return v.join(", ");   // ["kaiser", 11.88] → "kaiser, 11.88"
    return String(v);
}

function renderAnalysisPanel() {
    const key = ANALYSIS_PANELS[analysisMode] ? analysisMode : "spectrogram";
    const panel = ANALYSIS_PANELS[key];
    const form  = document.getElementById("analysis-form");
    const badge = document.getElementById("analysis-badge");
    const apply = document.getElementById("analysis-apply");
    if (!form) return;
    renderedPanel = key;
    if (badge) badge.textContent = panel.badge;
    if (apply) apply.style.display = panel.fields.length ? "" : "none";
    form.textContent = "";
    for (const f of panel.fields) {
        const label = document.createElement("label");
        if (f.title) label.title = f.title;
        const input = document.createElement("input");
        input.dataset.key = f.key;
        if (f.checkbox) {
            label.className = "check";
            input.type = "checkbox";
            label.appendChild(input);
            label.appendChild(document.createTextNode(" " + f.label));
        } else {
            label.textContent = f.label;
            input.type = "text";
            input.placeholder = f.ph || "";
            label.appendChild(input);
        }
        form.appendChild(label);
    }
    seedAnalysisForm(lastConfig);
}

function seedAnalysisForm(config) {
    if (config) lastConfig = config;
    const panel = ANALYSIS_PANELS[renderedPanel];
    if (!panel || !panel.configKey || !lastConfig) return;
    const an = lastConfig[panel.configKey] || {};
    document.querySelectorAll("#analysis-form input").forEach((el) => {
        const key = el.dataset.key;
        if (!(key in an)) return;
        if (el.type === "checkbox") el.checked = Boolean(an[key]);
        else el.value = analysisFieldValue(an[key]);
    });
}

document.getElementById("analysis-apply").addEventListener("click", () => {
    const panel = ANALYSIS_PANELS[renderedPanel];
    if (!panel || !panel.target) return;
    const analysis = { target: panel.target };
    document.querySelectorAll("#analysis-form input").forEach((el) => {
        if (el.type === "checkbox") {
            analysis[el.dataset.key] = el.checked;
        } else if (el.value.trim() !== "") {   // cleared fields are not sent
            analysis[el.dataset.key] = el.value.trim();
        }
    });
    sendControl({ analysis });
    logMsg(`Analysis settings sent (${panel.target})`);
});

renderAnalysisPanel();

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

// Default band to middle 10% of span (reset once we have freq data)
bandLo = -curFs / 1e6 * 0.05;
bandHi =  curFs / 1e6 * 0.05;

// Init PSD with placeholder data so layout is in place
freqsMHz = buildFreqsMHz(curCenter, curFs, curBins, absRF, curF0, curStep);
initUplot(freqsMHz);
updateSsbOption();

connect();
loadSchema().catch((err) => logMsg(`Schema load failed: ${err.message}`, "ERROR"));
logMsg("App initialised. Connecting to server…");
