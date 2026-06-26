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

// Current frame metadata (updated on each frame)
let curCenter   = 1955e6;
let curFs       = 15.36e6;
let curNfft     = 1024;
let curRows     = 12;
let freqsMHz    = null;     // Float32Array(nfft)
let levels      = [-90, -10];

// Per-channel display buffers [rows_displayed × nfft], newest row at index 0
const wfBuf   = { 0: null, 1: null };
// Peak-hold and min-trace per channel (Float32Array of length nfft)
const holdBuf = { 0: null, 1: null };
const minBuf  = { 0: null, 1: null };
// Last raw PSD data (mean+max per channel) for exports and band monitor
const psdData = {
    mean: { 0: null, 1: null },
    max:  { 0: null, 1: null },
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
    if (!curNfft || !curFs) return;
    const depthRows = wfBuf[0] ? wfBuf[0].length / curNfft : curRows;
    const winMs     = (depthRows * curNfft / curFs * 1e3).toFixed(0);
    const mode      = replaceMode ? "flicker" : "waterfall";
    const scale     = autoColor ? "auto" : "manual";
    metaEl.textContent = (
        `LIVE | center ${(curCenter / 1e6).toFixed(3)} MHz | ` +
        `span ${(curFs / 1e6).toFixed(2)} MS/s | ` +
        `FFT ${curNfft} | ${mode} | window ${winMs} ms (${depthRows} rows) | ` +
        `scale ${scale} [${levels[0].toFixed(0)}, ${levels[1].toFixed(0)}] | ` +
        `${absRF ? "absolute RF" : "baseband"} | ${renderedFps.toFixed(0)} fps`
    );
}

// ---------------------------------------------------------------------------
// Frequency axis helpers
// ---------------------------------------------------------------------------

function buildFreqsMHz(center, fs, nfft, absoluteRF) {
    const f = new Float32Array(nfft);
    for (let i = 0; i < nfft; i++) {
        // fftshifted: bin 0 = most-negative freq, bin nfft/2 = DC
        const baseHz = ((i - nfft / 2) / nfft) * fs;
        f[i] = absoluteRF ? (center + baseHz) / 1e6 : baseHz / 1e6;
    }
    return f;
}

function rowsForWindow(fs, nfft, windowMs) {
    return Math.max(1, Math.min(Math.round(windowMs / 1000 * fs / nfft), 300));
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
        // Tell the server our initial window size
        sendControl({ rows: rowsForWindow(curFs, curNfft, windowMs) });
    };

    ws.onmessage = (e) => {
        if (!paused) onFrame(e.data);
    };

    ws.onclose = () => {
        setStatus("disconnected — reconnecting…", "warn");
        logMsg("WebSocket disconnected; retrying in 1.2 s", "WARN");
        setTimeout(connect, 1200);
    };

    ws.onerror = () => ws.close();
}

function sendControl(ctrl) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(ctrl));
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

    const { nfft, rows, channels, center, fs, gain, dtype, scale } = header;
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
    const tuningChanged = (
        nfft !== curNfft || center !== curCenter || fs !== curFs
    );
    if (tuningChanged) {
        curNfft   = nfft;
        curCenter = center;
        curFs     = fs;
        freqsMHz  = buildFreqsMHz(center, fs, nfft, absRF);
        // Clear hold/min on tuning change (freq-axis specific)
        holdBuf[0] = holdBuf[1] = null;
        minBuf[0]  = minBuf[1]  = null;
        initUplot(freqsMHz);
        resetBand(freqsMHz);
    }
    curRows = rows;

    // ── Render ────────────────────────────────────────────────────────────
    for (const ch of channels) {
        updateWaterfall(ch, blocks[ch], rows, nfft, center, fs);
    }
    updatePSD(channels, blocks, rows, nfft);
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
    return rowsForWindow(fs, nfft, windowMs);
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
        // Scroll mode: shift existing rows down, prepend new rows at [0]
        const newRows = Math.min(bLen / nfft, depth);
        const keep    = (depth - newRows) * nfft;
        if (keep > 0) buf.copyWithin(newRows * nfft, 0, keep);
        buf.set(block.subarray(0, newRows * nfft));
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

// ---------------------------------------------------------------------------
// PSD (uPlot)
// ---------------------------------------------------------------------------

const PSD_BG    = "#101418";
const PSD_FG    = "#8b949e";

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
                stroke: PSD_FG, ticks: { stroke: PSD_FG }, grid: { stroke: "#30363d" },
                font:   "11px Menlo,monospace",
            },
            {
                label:  "Power (dB)",
                stroke: PSD_FG, ticks: { stroke: PSD_FG }, grid: { stroke: "#30363d" },
                font:   "11px Menlo,monospace",
            },
        ],
        series: [
            {},   // x (freqs)
            { label: "RX1 Mean", stroke: "#50dcdc", width: 2, show: true  },
            { label: "RX1 Max",  stroke: "#f5d750", width: 2, show: true  },
            { label: "RX2 Mean", stroke: "#ff9646", width: 2, show: true  },
            { label: "RX2 Max",  stroke: "#eb78eb", width: 2, show: true  },
            { label: "RX1 Hold", stroke: "rgba(80,220,220,0.45)", width: 1,
              dash: [4, 4], show: false },
            { label: "RX2 Hold", stroke: "rgba(255,150,70,0.45)", width: 1,
              dash: [4, 4], show: false },
            { label: "RX1 Min",  stroke: "rgba(80,220,220,0.6)",  width: 1,
              dash: [2, 4], show: false },
            { label: "RX2 Min",  stroke: "rgba(255,150,70,0.6)",  width: 1,
              dash: [2, 4], show: false },
            { label: "RX1−RX2",  stroke: "#ebebeb", width: 2,    show: false },
        ],
        hooks: {
            draw: [drawPsdOverlays],
        },
    };

    const nfft   = freqs.length;
    const empty  = Array(9).fill(null);
    uplot = new uPlot(opts, [Array.from(freqs), ...empty], container);

    // Set up band dragging on the uPlot canvas
    setupBandDrag();
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
                m[f] += v;
                if (v > x[f]) x[f] = v;
                if (v < n[f]) n[f] = v;
            }
        }
        for (let f = 0; f < nfft; f++) m[f] /= depth;

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

    let s1m = mean[0] ? Array.from(mean[0]) : null;
    let s1x = max[0]  ? Array.from(max[0])  : null;
    let s2m = mean[1] ? Array.from(mean[1]) : null;
    let s2x = max[1]  ? Array.from(max[1])  : null;
    let h1  = (peakHold && holdBuf[0]) ? Array.from(holdBuf[0]) : null;
    let h2  = (peakHold && holdBuf[1]) ? Array.from(holdBuf[1]) : null;
    let n1  = (showMin  && minBuf[0])  ? Array.from(minBuf[0])  : null;
    let n2  = (showMin  && minBuf[1])  ? Array.from(minBuf[1])  : null;
    let dif = null;

    if (showDiff && s1m && s2m) {
        dif = s1m.map((v, i) => v - s2m[i]);
    }

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

    // Peak marker (strongest bin of RX1 max)
    if (peakMarker && s1x && !showDiff) {
        drawPeakMarker(s1x, freqArr);
    }

    // Fixed Y-span
    applyYspan();
}

// Peak marker: drawn each frame via uPlot's redraw hook
let peakMarkerData = null;
function drawPeakMarker(s1x, freqArr) {
    if (!s1x) { peakMarkerData = null; return; }
    let bestI = 0;
    for (let i = 1; i < s1x.length; i++) {
        if (s1x[i] > s1x[bestI]) bestI = i;
    }
    peakMarkerData = { freq: freqArr[bestI], power: s1x[bestI] };
}

// uPlot draw hook — overlays: peak marker, band selection
function drawPsdOverlays(u) {
    const ctx = u.ctx;
    ctx.save();

    // ── Peak marker ──────────────────────────────────────────────────────
    if (peakMarker && peakMarkerData && !showDiff) {
        const { freq, power } = peakMarkerData;
        const px = u.valToPos(freq,  "x", true);
        const py = u.valToPos(power, "y", true);
        if (px && py) {
            ctx.beginPath();
            ctx.arc(px, py, 5, 0, 2 * Math.PI);
            ctx.fillStyle   = "#f5d750";
            ctx.strokeStyle = "#000";
            ctx.lineWidth   = 1;
            ctx.fill();
            ctx.stroke();
            ctx.fillStyle = "#f5d750";
            ctx.font      = "bold 11px Menlo,monospace";
            ctx.fillText(`${freq.toFixed(3)} MHz  ${power.toFixed(1)} dB`, px + 8, py - 5);
        }
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
        uplot.scales.y.auto = true;
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
        uplot.scales.y.auto = false;
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

    // Gather in-band frequency indices
    const mask = [];
    for (let i = 0; i < nfft; i++) {
        if (freqsMHz[i] >= lo && freqsMHz[i] <= hi) mask.push(i);
    }
    if (mask.length === 0) {
        bandMonitorEl.textContent = `Band ${lo.toFixed(3)}–${hi.toFixed(3)} MHz: no bins`;
        return;
    }

    const band = {}, qual = {};
    for (const ch of [0, 1]) {
        const buf = wfBuf[ch];
        if (!buf) continue;
        const depth = buf.length / nfft;

        // Correct linear-domain averaging (avoids the dB-averaging error)
        let sumInBand = 0, sumAll = 0;
        for (let r = 0; r < depth; r++) {
            const off = r * nfft;
            for (let i = 0; i < nfft; i++) {
                const lin = Math.pow(10, buf[off + i] / 10);
                sumAll += lin;
                if (mask.includes(i)) sumInBand += lin;
            }
        }
        const linBand = sumInBand / (mask.length * depth);
        const linAll  = sumAll    / (nfft        * depth);
        band[ch] = 10 * Math.log10(Math.max(linBand, 1e-20));
        qual[ch] = 10 * Math.log10(Math.max(linBand, 1e-20))
                 - 10 * Math.log10(Math.max(linAll,  1e-20));
    }

    const segs = [`Band ${lo.toFixed(3)}–${hi.toFixed(3)} MHz (${mask.length} bins)`];
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

    over.addEventListener("mousedown", (e) => {
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
        e.preventDefault();
    });

    window.addEventListener("mousemove", (e) => {
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

    window.addEventListener("mouseup", () => {
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
    if (!freqsMHz || !psdData.mean[0]) {
        logMsg("No PSD data yet — try again after the first frame", "WARN");
        return;
    }
    const nfft = freqsMHz.length;
    const rows = ["freq_mhz,rx1_mean_db,rx1_max_db,rx2_mean_db,rx2_max_db"];
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
    ctx.fillStyle = "#101418";
    ctx.fillRect(0, 0, W, H);
    ctx.drawImage(c0, 0, 0);
    ctx.drawImage(c1, c0.width, 0);
    if (psdCanvas) ctx.drawImage(psdCanvas, 0, Math.max(c0.height, c1.height));

    // Settings caption
    const ts  = new Date().toLocaleString();
    const cap = `${ts}  center ${(curCenter / 1e6).toFixed(3)} MHz  span ${(curFs / 1e6).toFixed(2)} MS/s  FFT ${curNfft}`;
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

function rowsForCurrentSettings() {
    return rowsForWindow(curFs, curNfft, windowMs);
}

document.getElementById("center-btn").addEventListener("click", () => {
    const mhz = parseFloat(document.getElementById("center-mhz").value);
    if (!isNaN(mhz)) sendControl({ center: mhz * 1e6 });
});
document.getElementById("center-mhz").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("center-btn").click();
});

document.getElementById("rate-sel").addEventListener("change", (e) => {
    const fs   = parseFloat(e.target.value) * 1e6;
    const ctrl = { sample_rate: fs };
    if (replaceMode) ctrl.rows = rowsForWindow(fs, curNfft, windowMs);
    sendControl(ctrl);
});

document.getElementById("gain-btn").addEventListener("click", () => {
    const g = parseFloat(document.getElementById("gain").value);
    if (!isNaN(g)) sendControl({ gain: g });
});
document.getElementById("gain").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("gain-btn").click();
});

document.getElementById("nfft-sel").addEventListener("change", (e) => {
    const nfft = parseInt(e.target.value, 10);
    const ctrl = { nfft };
    if (replaceMode) ctrl.rows = rowsForWindow(curFs, nfft, windowMs);
    sendControl(ctrl);
});

document.getElementById("tune-btn").addEventListener("click", () => {
    if (bandLo === null || bandHi === null || !absRF) return;
    const lo = Math.min(bandLo, bandHi);
    const hi = Math.max(bandLo, bandHi);
    const newCenter = ((lo + hi) / 2) * 1e6;
    document.getElementById("center-mhz").value = (newCenter / 1e6).toFixed(3);
    sendControl({ center: newCenter });
    logMsg(`Tuned to selection center: ${(newCenter / 1e6).toFixed(3)} MHz`);
});

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
    const rows = replaceMode ? rowsForCurrentSettings() : 12;
    sendControl({ rows });
});

document.getElementById("win-sel").addEventListener("change", (e) => {
    windowMs = parseInt(e.target.value, 10);
    if (replaceMode) sendControl({ rows: rowsForCurrentSettings() });
});

document.getElementById("auto-color").addEventListener("change", (e) => {
    autoColor = e.target.checked;
});

document.getElementById("abs-rf").addEventListener("change", (e) => {
    absRF    = e.target.checked;
    freqsMHz = buildFreqsMHz(curCenter, curFs, curNfft, absRF);
    if (uplot && freqsMHz) initUplot(freqsMHz);
    resetBand(freqsMHz);
});

document.getElementById("reset-btn").addEventListener("click", () => {
    if (uplot) {
        uplot.scales.x.auto = true;
        uplot.scales.y.auto = true;
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
        uplot.scales.y.auto = true;
    }
});

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

// Default band to middle 10% of span (reset once we have freq data)
bandLo = -curFs / 1e6 * 0.05;
bandHi =  curFs / 1e6 * 0.05;

// Init PSD with placeholder data so layout is in place
freqsMHz = buildFreqsMHz(curCenter, curFs, curNfft, absRF);
initUplot(freqsMHz);

connect();
logMsg("App initialised. Connecting to server…");
