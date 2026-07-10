/*
 * WCAG contrast helper for seat lettering. A seat's fill color is chosen by
 * staff (a section's color in the chart editor, a pricing zone's color in the
 * live maps/exports) and can be anything from near-black to bright yellow, so
 * the row/number drawn ON the seat can't use a fixed ink color -- white text
 * vanishes on a pale seat, black text vanishes on a dark one. `textColor()`
 * picks black or white per seat by whichever yields the higher WCAG 2.x
 * contrast ratio against the seat's actual fill.
 *
 * This is the CLIENT half of a two-language contract, mirrored EXACTLY by
 * events/seat_contrast.py (same luminance formula, same black-or-white tie
 * rule, same sRGB mix) so a seat's ink color is identical in the browser
 * (editor + online maps) and in the server-rendered PNG/PDF export. If you
 * change one side, change the other; events/test_seat_contrast.py hard-codes
 * the expected pick for representative colors both sides must agree on.
 *
 * Pure math -- no DOM, no Alpine, no fetch.
 */
(function () {
    const BLACK = "#000000";
    const WHITE = "#ffffff";

    // Parse "#rgb", "#rrggbb", or "rgb()/rgba()" into [r, g, b] (0-255), or
    // null if it can't (e.g. a CSS var that didn't resolve, or empty input).
    function parseColor(input) {
        if (!input) return null;
        let s = String(input).trim();
        if (s[0] === "#") {
            s = s.slice(1);
            if (s.length === 3) {
                s = s.split("").map((c) => c + c).join("");
            }
            if (s.length !== 6) return null;
            const r = parseInt(s.slice(0, 2), 16);
            const g = parseInt(s.slice(2, 4), 16);
            const b = parseInt(s.slice(4, 6), 16);
            if ([r, g, b].some((n) => Number.isNaN(n))) return null;
            return [r, g, b];
        }
        const m = s.match(/rgba?\(([^)]+)\)/i);
        if (m) {
            const parts = m[1].split(/[,\s/]+/).map((p) => parseFloat(p)).filter((n) => !Number.isNaN(n));
            if (parts.length >= 3) return [parts[0], parts[1], parts[2]];
        }
        return null;
    }

    // Per-channel sRGB -> linear, then the WCAG relative-luminance weighting.
    function channelLuminance(c) {
        const cs = c / 255;
        return cs <= 0.03928 ? cs / 12.92 : Math.pow((cs + 0.055) / 1.055, 2.4);
    }

    function relativeLuminance(rgb) {
        return (
            0.2126 * channelLuminance(rgb[0]) +
            0.7152 * channelLuminance(rgb[1]) +
            0.0722 * channelLuminance(rgb[2])
        );
    }

    // WCAG contrast ratio between two relative luminances.
    function contrastRatio(l1, l2) {
        const hi = Math.max(l1, l2);
        const lo = Math.min(l1, l2);
        return (hi + 0.05) / (lo + 0.05);
    }

    // Black or white -- whichever has the higher WCAG contrast ratio against
    // `bg`. On an exact tie (a mid-gray seat), and on unparseable input, fall
    // to black, the safer default on the light neutral fills unpriced seats
    // usually take.
    function textColor(bg) {
        const rgb = parseColor(bg);
        if (!rgb) return BLACK;
        const L = relativeLuminance(rgb);
        const onWhite = contrastRatio(L, 1.0); // white ink (luminance 1)
        const onBlack = contrastRatio(L, 0.0); // black ink (luminance 0)
        return onWhite > onBlack ? WHITE : BLACK;
    }

    // Weighted sRGB mix, the integer-channel approximation of CSS
    // `color-mix(in srgb, a weightA%, b)`. `weightA` is a 0..1 fraction of
    // `a`. Used so the online map can compute the concrete fill of a zoned-
    // but-unselected seat (a 20% zone tint over white) to contrast against.
    function mix(a, b, weightA) {
        const ca = parseColor(a);
        const cb = parseColor(b);
        if (!ca || !cb) return a;
        const w = Math.max(0, Math.min(1, weightA));
        const chan = (i) => Math.round(ca[i] * w + cb[i] * (1 - w));
        return (
            "#" +
            [chan(0), chan(1), chan(2)].map((n) => n.toString(16).padStart(2, "0")).join("")
        );
    }

    window.SeatContrast = { parseColor, relativeLuminance, contrastRatio, textColor, mix };
})();
