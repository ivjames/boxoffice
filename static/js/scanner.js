/*
 * Inline door-scanner Alpine component (scanning/templates/scanning/scan_home.html).
 *
 * Cross-browser is the whole point: BarcodeDetector (fast, native) only
 * ships in Chromium/Android -- iOS Safari, where door staff's iPhones live,
 * has no BarcodeDetector at all. So this component feature-detects it as an
 * *optional* fast path and always has a working fallback: draw each camera
 * frame to a <canvas> and decode it with vendored jsQR (static/js/jsQR.js,
 * pure JS, no deps) -- that path works everywhere getUserMedia does.
 *
 * On a decoded hit: parse the bare ticket code the QR encodes
 * (<token>.<sig> -- see orders/tokens.py), build /S/<token>/<sig>/ and fetch() it
 * with Accept: application/json (scanning/views.scan_redeem returns JSON
 * for that instead of a full HTML page) and render PASS/FAIL inline so
 * staff can immediately scan the next ticket. Debounced so a code sitting
 * in frame doesn't get resubmitted on every animation frame.
 *
 * getUserMedia requires a secure context (HTTPS) -- localhost is treated as
 * secure by browsers, so this works unmodified in dev (runserver on
 * localhost/127.0.0.1) and in prod (every tenant subdomain is behind TLS
 * per docs/ARCHITECTURE.md). On a non-secure context, or if the camera
 * permission is denied / no camera exists, the component reports a clear
 * message and staff fall back to the always-available manual token-entry
 * form below the scanner.
 */

function qrScanner() {
    return {
        status: "idle", // idle | starting | scanning | error
        errorMessage: "",
        lastResult: null,
        tally: { pass: 0, fail: 0 },
        busy: false,
        lastCode: null,
        lastCodeAt: 0,
        lastAttemptAt: 0,
        // `multiple` is true while more than one QR is visible (or was, within
        // the last MULTI_CLEAR_MS), which we refuse to scan. Detection flickers
        // -- a multi-code scene decodes just one code on the odd frame -- so we
        // hold off scanning a lone code until the extras have stayed gone for
        // MULTI_CLEAR_MS. This is a single-code check, not a camera-stability
        // settle: a code shown on its own scans on the frame it decodes.
        multiple: false,
        MULTI_CLEAR_MS: 600,
        lastMultipleAt: 0,
        // One QR code shows exactly 3 finder patterns; seeing this many or
        // more on a frame means a second code is in view (jsQR path only --
        // see decodeWithJsQR). 4 = "any evidence of a second code refuses".
        MULTI_PATTERN_MIN: 4,
        stream: null,
        useBarcodeDetector: typeof window.BarcodeDetector !== "undefined",
        detector: null,
        rafId: null,
        // Feedback state (see feedback()): `flash` drives the full-screen
        // color wash; `soundOn` gates the audio cue (persisted so a staffer
        // working a quiet room stays muted across reloads).
        soundOn: true,
        flash: null, // null | "pass" | "used" | "fail"
        flashTimer: null,
        audioCtx: null,

        async start() {
            this.status = "starting";
            this.errorMessage = "";

            try {
                this.soundOn = localStorage.getItem("scannerSound") !== "off";
            } catch (e) {
                // Private-mode / disabled storage -- default to sound on.
            }
            // Browsers won't let an AudioContext make noise until a user
            // gesture. start() runs from x-init (no gesture), so arm a
            // one-shot unlock on the first tap/keypress anywhere on the page.
            this.installAudioUnlock();

            if (!window.isSecureContext) {
                this.status = "error";
                this.errorMessage = "Camera scanning requires HTTPS. Use manual code entry below.";
                return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                this.status = "error";
                this.errorMessage = "This browser doesn't support camera access. Use manual code entry below.";
                return;
            }

            try {
                this.stream = await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: { ideal: "environment" },
                        width: { ideal: 1280 },
                        height: { ideal: 720 },
                    },
                    audio: false,
                });
            } catch (err) {
                this.status = "error";
                this.errorMessage = this.describeCameraError(err);
                return;
            }

            const video = this.$refs.video;
            video.srcObject = this.stream;
            try {
                await video.play();
            } catch (err) {
                // Autoplay can reject if the user hasn't interacted yet; the
                // "Start scanning" button click that got us here counts as
                // interaction on every browser we support, but guard anyway.
            }

            if (this.useBarcodeDetector) {
                try {
                    this.detector = new BarcodeDetector({ formats: ["qr_code"] });
                } catch (e) {
                    this.useBarcodeDetector = false;
                }
            }

            this.status = "scanning";
            this.loop();
        },

        stop() {
            if (this.rafId) cancelAnimationFrame(this.rafId);
            this.rafId = null;
            if (this.stream) {
                this.stream.getTracks().forEach((track) => track.stop());
                this.stream = null;
            }
            this.status = "idle";
        },

        describeCameraError(err) {
            const name = err && err.name;
            if (name === "NotAllowedError" || name === "PermissionDeniedError") {
                return "Camera permission was denied. Allow camera access and reload, or use manual code entry below.";
            }
            if (name === "NotFoundError" || name === "DevicesNotFoundError") {
                return "No camera was found on this device. Use manual code entry below.";
            }
            if (name === "NotReadableError") {
                return "The camera is already in use by another app. Use manual code entry below.";
            }
            return "Couldn't access the camera. Use manual code entry below.";
        },

        async loop() {
            if (this.status !== "scanning") return;

            const now = performance.now();
            // Cap decode attempts (~8/s) -- the <video> preview itself stays
            // smooth regardless; this just throttles the CPU-heavy part.
            if (now - this.lastAttemptAt > 120) {
                this.lastAttemptAt = now;
                const video = this.$refs.video;
                if (video.readyState >= video.HAVE_CURRENT_DATA && video.videoWidth) {
                    let result = null; // { codes, multiple } for this frame, or null if we skipped
                    if (this.useBarcodeDetector && this.detector) {
                        try {
                            const found = await this.detector.detect(video);
                            const codes = found.map((c) => c.rawValue);
                            result = { codes, multiple: codes.length > 1 };
                        } catch (e) {
                            // Transient decode error -- just try the next frame.
                        }
                    } else {
                        result = this.decodeWithJsQR(video);
                    }
                    if (result) this.considerCodes(result);
                }
            }

            this.rafId = requestAnimationFrame(() => this.loop());
        },

        decodeWithJsQR(video) {
            // Returns { codes, multiple } like the BarcodeDetector path.
            //
            // jsQR decodes at most ONE code per call, and a second ticket in
            // frame often won't fully decode anyway (small, angled, or moiré
            // from a code shown on a monitor) -- so any "decode it again"
            // scheme misses real multiples. Instead we DETECT extra codes
            // without decoding them: every QR has three finder squares
            // (1:1:3:1:1 pattern), which stay visible long after the code
            // itself is too degraded to read. countQrFinderPatterns
            // (static/js/qr-multi.js) counts distinct finder centers on the
            // same frame: 3 belong to one code, 4+ means another code is in
            // view and the frame is ambiguous.
            if (typeof window.jsQR !== "function") return { codes: [], multiple: false };
            const canvas = this.$refs.canvas;
            const vw = video.videoWidth;
            const vh = video.videoHeight;
            if (!vw || !vh) return { codes: [], multiple: false };

            // Downscale before decoding -- jsQR's cost scales with pixel
            // count, and QR codes fill a printed/e-ticket frame generously
            // enough that a 480px-max-dimension image decodes fine while
            // being much cheaper per attempt on a phone.
            const maxDim = 480;
            const scale = Math.min(1, maxDim / Math.max(vw, vh));
            const w = Math.max(1, Math.round(vw * scale));
            const h = Math.max(1, Math.round(vh * scale));
            canvas.width = w;
            canvas.height = h;

            const ctx = canvas.getContext("2d", { willReadFrequently: true });
            ctx.drawImage(video, 0, 0, w, h);
            const imageData = ctx.getImageData(0, 0, w, h);

            const code = window.jsQR(imageData.data, w, h, { inversionAttempts: "dontInvert" });

            // Count finder patterns on the SAME frame. This runs even when
            // nothing decoded, so a screenful of codes raises the "show one at
            // a time" hint before any of them is readable.
            let multiple = false;
            if (typeof window.countQrFinderPatterns === "function") {
                const patterns = window.countQrFinderPatterns(imageData.data, w, h);
                multiple = patterns >= this.MULTI_PATTERN_MIN;
            }

            return { codes: code ? [code.data] : [], multiple };
        },

        considerCodes(result) {
            // Refuse to scan when more than one QR is in frame. With several
            // tickets fanned out we can't tell which one the staffer means, so
            // we scan none of them and wait for a single code to be isolated.
            // (BarcodeDetector reports multiples natively; the jsQR fallback
            // detects them by counting finder patterns -- see decodeWithJsQR.
            // Either way `result.multiple` can be true even when fewer than
            // two codes actually DECODED this frame.)
            const codes = result.codes;
            const now = performance.now();
            if (result.multiple || codes.length > 1) {
                this.multiple = true;
                this.lastMultipleAt = now;
                return;
            }

            if (codes.length === 0) {
                // Empty frame -- clear the hint, but only once the just-seen
                // extras have been gone long enough that this isn't a flicker.
                if (now - this.lastMultipleAt >= this.MULTI_CLEAR_MS) this.multiple = false;
                return;
            }

            // Exactly one code decoded this frame. If we saw multiple codes
            // within the last MULTI_CLEAR_MS, treat this as the flicker of a
            // still-crowded frame: keep refusing (and keep the hint up) until
            // the extras have truly cleared, so a fanned-out stack can't slip a
            // scan through on the odd single-code frame.
            if (now - this.lastMultipleAt < this.MULTI_CLEAR_MS) {
                this.multiple = true;
                return;
            }

            // A genuinely isolated code: redeem it right away -- no
            // camera-stability settle. handleCode's busy/4s debounce keeps a
            // code sitting in frame from re-submitting.
            this.multiple = false;
            this.handleCode(codes[0]);
        },

        handleCode(text) {
            const now = Date.now();
            if (this.busy) return;
            if (text === this.lastCode && now - this.lastCodeAt < 4000) return; // debounce repeats
            this.lastCode = text;
            this.lastCodeAt = now;
            this.redeem(text);
        },

        async redeem(decodedText) {
            this.busy = true;
            try {
                // The QR encodes a bare "<token>.<sig>" code (see orders/tokens.py),
                // not a URL. Validate the shape, split on ".", and build the redeem
                // path ourselves. A random non-ticket QR won't match and is rejected.
                // Token is uppercase alphanumeric; sig is base32 -- both are covered
                // by [A-Z0-9] here (shape guard only; the server does the real check).
                const m = /^([A-Z0-9]+)\.([A-Z0-9]+)$/.exec(decodedText.trim().toUpperCase());
                if (!m) {
                    this.recordResult({
                        ok: false,
                        reason: "invalid_code",
                        message: "Scanned code isn't a ticket code.",
                        ticket: null,
                    });
                    return;
                }
                const [, token, sig] = m;
                const path = `/S/${token}/${sig}/`;

                let resp;
                try {
                    resp = await fetch(path, {
                        headers: { Accept: "application/json" },
                        credentials: "same-origin",
                    });
                } catch (e) {
                    this.recordResult({
                        ok: false,
                        reason: "network_error",
                        message: "Network error — check connectivity and try again.",
                        ticket: null,
                    });
                    return;
                }

                let data;
                try {
                    data = await resp.json();
                } catch (e) {
                    this.recordResult({
                        ok: false,
                        reason: "session_expired",
                        message: "Unexpected response — your session may have expired. Reload the page.",
                        ticket: null,
                    });
                    return;
                }
                this.recordResult(data);
            } finally {
                this.busy = false;
            }
        },

        async submitManual(evt) {
            // Manual token entry, redeemed in-page so staff stay on the scan
            // screen instead of navigating to the full result page. POST the
            // form (token + CSRF) to /scan/ with Accept: application/json;
            // scanning.views.scan_home signs the token server-side and returns
            // the same ScanResult JSON the camera loop renders.
            const form = evt.target;
            const body = new FormData(form);
            const token = (body.get("token") || "").toString().trim();
            if (!token || this.busy) return;

            this.busy = true;
            try {
                let resp;
                try {
                    resp = await fetch(form.action, {
                        method: "POST",
                        body,
                        headers: { Accept: "application/json" },
                        credentials: "same-origin",
                    });
                } catch (e) {
                    this.recordResult({
                        ok: false,
                        reason: "network_error",
                        message: "Network error — check connectivity and try again.",
                        ticket: null,
                    });
                    return;
                }

                let data;
                try {
                    data = await resp.json();
                } catch (e) {
                    this.recordResult({
                        ok: false,
                        reason: "session_expired",
                        message: "Unexpected response — your session may have expired. Reload the page.",
                        ticket: null,
                    });
                    return;
                }
                this.recordResult(data);
                // Clear + refocus so staff can key the next code straight in.
                form.reset();
                if (this.$refs.manualInput) this.$refs.manualInput.focus();
            } finally {
                this.busy = false;
            }
        },

        recordResult(data) {
            // Three visible outcomes, not two: a valid admit (green), a QR
            // that scanned fine but shouldn't admit yet (amber -- either
            // already redeemed or scanned outside the showtime window, both
            // distinct from a fake so staff can make a judgment call), and
            // everything else (red -- bad signature, unknown code, void,
            // non-ticket QR, network/session error).
            const amber = data.reason === "already_used" || data.reason === "wrong_time";
            const category = data.ok ? "pass" : amber ? "used" : "fail";
            data.category = category;
            this.lastResult = data;
            if (data.ok) {
                this.tally.pass += 1;
            } else {
                this.tally.fail += 1;
            }
            this.feedback(category);
            // The verdict card sits below the manual entry, clear of the live
            // camera, so it no longer auto-hides -- it stays until the next scan
            // replaces it or staff tap it to dismiss (dismissResult).
        },

        dismissResult() {
            this.lastResult = null;
        },

        headlineFor(data) {
            if (!data) return "";
            if (data.category === "pass") return "PASS";
            if (data.reason === "wrong_time") return "WRONG TIME";
            if (data.category === "used") return "ALREADY SCANNED";
            return "FAIL";
        },

        feedback(category) {
            // Full-screen color wash: on a phone held at arm's length the tiny
            // inline banner is easy to miss, so the whole viewport flashes.
            this.flash = null; // reset so a repeat of the same category re-triggers the CSS animation
            this.$nextTick(() => {
                this.flash = category;
                if (this.flashTimer) clearTimeout(this.flashTimer);
                this.flashTimer = setTimeout(() => {
                    this.flash = null;
                }, 1200);
            });

            if (category === "pass") {
                // Bright rising two-note "accept" chirp -- loud enough to carry
                // over a busy door.
                this.playTones([
                    { freq: 880, dur: 0.09, vol: 0.55 },
                    { freq: 1319, dur: 0.15, vol: 0.55 },
                ]);
                this.vibrate(50);
            } else if (category === "used") {
                // Neutral double blip -- a warning, not an alarm.
                this.playTones([
                    { freq: 620, dur: 0.11, type: "triangle", vol: 0.55 },
                    { freq: 620, dur: 0.11, type: "triangle", vol: 0.55, gap: 0.05 },
                ]);
                this.vibrate([40, 60, 40]);
            } else {
                // Low buzz for a reject.
                this.playTones([
                    { freq: 200, dur: 0.18, type: "sawtooth", vol: 0.6 },
                    { freq: 150, dur: 0.24, type: "sawtooth", vol: 0.6 },
                ]);
                this.vibrate([90, 60, 90]);
            }
        },

        toggleSound() {
            this.soundOn = !this.soundOn;
            try {
                localStorage.setItem("scannerSound", this.soundOn ? "on" : "off");
            } catch (e) {
                // Storage unavailable -- toggle still works for this session.
            }
            if (this.soundOn) this.ensureAudio();
        },

        installAudioUnlock() {
            const unlock = () => this.ensureAudio();
            document.addEventListener("pointerdown", unlock, { once: true });
            document.addEventListener("keydown", unlock, { once: true });
        },

        ensureAudio() {
            try {
                if (!this.audioCtx) {
                    const Ctx = window.AudioContext || window.webkitAudioContext;
                    if (!Ctx) return null;
                    this.audioCtx = new Ctx();
                }
                if (this.audioCtx.state === "suspended") this.audioCtx.resume();
                return this.audioCtx;
            } catch (e) {
                return null;
            }
        },

        playTones(tones) {
            if (!this.soundOn) return;
            const ctx = this.ensureAudio();
            if (!ctx) return;
            try {
                let t = ctx.currentTime;
                for (const tone of tones) {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.type = tone.type || "sine";
                    osc.frequency.value = tone.freq;
                    // Quick attack + exponential release; ramp to a tiny
                    // non-zero floor because exponentialRampToValueAtTime(0) is
                    // illegal.
                    gain.gain.setValueAtTime(0.0001, t);
                    gain.gain.exponentialRampToValueAtTime(tone.vol || 0.45, t + 0.012);
                    gain.gain.exponentialRampToValueAtTime(0.0001, t + tone.dur);
                    osc.connect(gain).connect(ctx.destination);
                    osc.start(t);
                    osc.stop(t + tone.dur);
                    t += tone.dur + (tone.gap || 0);
                }
            } catch (e) {
                // Never let an audio hiccup interrupt scanning.
            }
        },

        vibrate(pattern) {
            try {
                if (navigator.vibrate) navigator.vibrate(pattern);
            } catch (e) {
                // Vibration is best-effort (unsupported on desktop / iOS).
            }
        },
    };
}
