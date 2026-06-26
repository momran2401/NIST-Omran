/**
 * colormap.js — Viridis RGBA lookup table (256 entries × 4 bytes).
 *
 * Polynomial approximation of matplotlib's viridis:
 *   https://www.shadertoy.com/view/WlfXRN  (Íñigo Quílez, public domain)
 *
 * Exposes window.VIRIDIS_LUT: Uint8ClampedArray of length 1024 (256 × RGBA).
 * Usage:
 *   const t   = clamp01((dB - vmin) / (vmax - vmin));
 *   const idx = Math.round(t * 255) * 4;
 *   [R, G, B, A] = LUT.slice(idx, idx + 4);
 */
(function () {
    "use strict";

    function viridis(t) {
        // Polynomial coefficients for each channel (R, G, B)
        const c0 = [0.2777273272234177,   0.005407344544966578, 0.3340998053353061];
        const c1 = [0.1050930431085774,   1.404613529898575,    1.384590162594685];
        const c2 = [-0.3308618287255563,  0.214847559468213,    0.09509516302823659];
        const c3 = [-4.634230498983486,  -5.799100973351585,   -19.33244095627987];
        const c4 = [6.228269936347081,   14.17993336680509,    56.69055260068105];
        const c5 = [4.776384997670288,  -13.74514537774601,   -65.35303263337234];
        const c6 = [-5.435455855934631,   4.645852612178535,    26.3124352495832];
        return [
            c0[0] + t * (c1[0] + t * (c2[0] + t * (c3[0] + t * (c4[0] + t * (c5[0] + t * c6[0]))))),
            c0[1] + t * (c1[1] + t * (c2[1] + t * (c3[1] + t * (c4[1] + t * (c5[1] + t * c6[1]))))),
            c0[2] + t * (c1[2] + t * (c2[2] + t * (c3[2] + t * (c4[2] + t * (c5[2] + t * c6[2]))))),
        ];
    }

    const N   = 256;
    const LUT = new Uint8ClampedArray(N * 4);
    for (let i = 0; i < N; i++) {
        const [r, g, b] = viridis(i / (N - 1));
        LUT[i * 4]     = Math.round(Math.max(0, Math.min(1, r)) * 255);
        LUT[i * 4 + 1] = Math.round(Math.max(0, Math.min(1, g)) * 255);
        LUT[i * 4 + 2] = Math.round(Math.max(0, Math.min(1, b)) * 255);
        LUT[i * 4 + 3] = 255;
    }

    window.VIRIDIS_LUT = LUT;
})();
