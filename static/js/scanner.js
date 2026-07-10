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
 * On a decoded hit: parse the ticket URL the QR encodes
 * (HTTPS://<host>/S/<token>/<sig>/ -- see orders/tokens.py), fetch() it
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
        stream: null,
        useBarcodeDetector: typeof window.BarcodeDetector !== "undefined",
        detector: null,
        rafId: null,

        async start() {
            this.status = "starting";
            this.errorMessage = "";

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
                    let text = null;
                    if (this.useBarcodeDetector && this.detector) {
                        try {
                            const codes = await this.detector.detect(video);
                            if (codes.length) text = codes[0].rawValue;
                        } catch (e) {
                            // Transient decode error -- just try the next frame.
                        }
                    } else {
                        text = this.decodeWithJsQR(video);
                    }
                    if (text) this.handleCode(text);
                }
            }

            this.rafId = requestAnimationFrame(() => this.loop());
        },

        decodeWithJsQR(video) {
            if (typeof window.jsQR !== "function") return null;
            const canvas = this.$refs.canvas;
            const vw = video.videoWidth;
            const vh = video.videoHeight;
            if (!vw || !vh) return null;

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
            return code ? code.data : null;
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
                let url;
                try {
                    url = new URL(decodedText, window.location.origin);
                } catch (e) {
                    this.recordResult({
                        ok: false,
                        reason: "invalid_code",
                        message: "Scanned code isn't a ticket link.",
                        ticket: null,
                    });
                    return;
                }
                if (!/^\/S\//.test(url.pathname)) {
                    this.recordResult({
                        ok: false,
                        reason: "invalid_code",
                        message: "Scanned code isn't a ticket link.",
                        ticket: null,
                    });
                    return;
                }

                let resp;
                try {
                    resp = await fetch(url.pathname + url.search, {
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

        recordResult(data) {
            this.lastResult = data;
            if (data.ok) {
                this.tally.pass += 1;
            } else {
                this.tally.fail += 1;
            }
        },
    };
}
