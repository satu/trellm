// iOS PWA standalone-only pull-to-refresh.
//
// On Android Chrome and any normal browser tab, the OS already provides
// pull-to-refresh. iOS Safari does too. The single environment that loses
// it is "Add to Home Screen" on iOS, which strips Safari chrome — the
// dashboard then has no way to refresh short of force-closing the app.
//
// `navigator.standalone === true` is iOS-only and is exactly that case,
// so we gate the entire hack on it.

(function () {
    "use strict";

    var nav = window.navigator;
    if (!nav || nav.standalone !== true) {
        return;
    }

    var THRESHOLD = 70;     // distance (px) past which a release triggers refresh
    var MAX_PULL = 110;     // max distance the indicator travels
    var DAMPING = 0.55;     // resistive feel — actual travel is dy * damping

    var startY = 0;
    var pulling = false;
    var dy = 0;

    var indicator = document.createElement("div");
    indicator.id = "ptr-indicator";
    indicator.setAttribute("aria-hidden", "true");
    indicator.style.cssText = [
        "position:fixed",
        "top:0",
        "left:50%",
        "transform:translate(-50%,-100%)",
        "background:#161b22",
        "color:#c9d1d9",
        "border:1px solid #30363d",
        "border-top:none",
        "border-radius:0 0 10px 10px",
        "padding:10px 18px",
        "font:500 13px -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif",
        "z-index:99999",
        "pointer-events:none",
        "transition:transform 220ms ease-out",
        "will-change:transform"
    ].join(";");
    indicator.textContent = "Pull to refresh";

    function attachIndicator() {
        if (document.body) {
            document.body.appendChild(indicator);
        } else {
            document.addEventListener("DOMContentLoaded", function () {
                document.body.appendChild(indicator);
            });
        }
    }
    attachIndicator();

    function setIndicator(travel, ready, refreshing) {
        // travel is 0..MAX_PULL — translate the indicator down from -100% (hidden)
        // toward 0% (sitting at the very top). At MAX_PULL we want it fully visible.
        var pct = Math.min(travel / MAX_PULL, 1);
        var ty = -100 + pct * 100;
        indicator.style.transform = "translate(-50%," + ty.toFixed(2) + "%)";
        if (refreshing) {
            indicator.textContent = "Refreshing…";
        } else if (ready) {
            indicator.textContent = "Release to refresh";
        } else {
            indicator.textContent = "Pull to refresh";
        }
    }

    function reset(animated) {
        if (!animated) {
            indicator.style.transition = "none";
        }
        indicator.style.transform = "translate(-50%,-100%)";
        if (!animated) {
            // Force reflow then re-enable transition.
            void indicator.offsetWidth;
            indicator.style.transition = "transform 220ms ease-out";
        }
        startY = 0;
        dy = 0;
        pulling = false;
    }

    document.addEventListener("touchstart", function (e) {
        if (window.scrollY > 0) {
            return;
        }
        if (e.touches.length !== 1) {
            return;
        }
        startY = e.touches[0].clientY;
        pulling = true;
        dy = 0;
    }, { passive: true });

    document.addEventListener("touchmove", function (e) {
        if (!pulling) {
            return;
        }
        if (window.scrollY > 0) {
            // The user scrolled within the page — abort.
            reset(false);
            return;
        }
        var delta = e.touches[0].clientY - startY;
        if (delta <= 0) {
            // Pulling upward — let the page scroll normally.
            return;
        }
        dy = Math.min(delta * DAMPING, MAX_PULL);
        setIndicator(dy, dy >= THRESHOLD, false);
    }, { passive: true });

    document.addEventListener("touchend", function () {
        if (!pulling) {
            return;
        }
        if (dy >= THRESHOLD) {
            setIndicator(MAX_PULL, false, true);
            // Slight delay so the "Refreshing…" state is visible to the user.
            setTimeout(function () { window.location.reload(); }, 150);
        } else {
            reset(true);
        }
        pulling = false;
        dy = 0;
    }, { passive: true });

    document.addEventListener("touchcancel", function () {
        reset(true);
    }, { passive: true });
})();
