/* BOXO.SHOW landing — auto-rotating white-label storefront preview.
 *
 * Cycles the "Your brand, not ours" preview through three example venues,
 * swapping the theme (data-theme) and ticket-card copy every 5s with a
 * short cross-fade. Pauses on hover/focus. Under prefers-reduced-motion the
 * timer is never started — the first example is shown statically (spec:
 * States / Animation). Progressive enhancement: with this script absent the
 * markup already shows the first example (The Regal Theatre), no rotation —
 * an acceptable degraded state, not a broken one.
 */
(function () {
  var panel = document.getElementById('boxo-preview-panel');
  if (!panel) return;

  var examples = [
    { theme: 'regal', name: 'The Regal Theatre', subdomain: 'regaltheatre.boxo.show', show: 'Hamlet', venue: 'Fri & Sat · 7:30 PM · from $24', price: 'From $24' },
    { theme: 'blackbox', name: 'Blackbox Collective', subdomain: 'blackboxcollective.boxo.show', show: 'Fences', venue: 'Thu–Sun · 8:00 PM · from $18', price: 'From $18' },
    { theme: 'empire', name: 'Empire Players', subdomain: 'empireplayers.boxo.show', show: 'Cabaret', venue: 'Sat & Sun · 2:00 PM · from $32', price: 'From $32' }
  ];

  var nameEl = document.getElementById('boxo-preview-venue-name');
  var subdomainEl = document.getElementById('boxo-preview-subdomain');
  var showEl = document.getElementById('boxo-preview-show');
  var venueEl = document.getElementById('boxo-preview-venue');
  var priceEl = document.getElementById('boxo-preview-price');
  var dots = Array.prototype.slice.call(panel.parentNode.querySelectorAll('.example-progress span'));
  var fadeTargets = [nameEl, showEl, venueEl, priceEl];

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var i = 0, paused = false;
  var DURATION = 5000;

  function render(idx) {
    var ex = examples[idx];
    panel.setAttribute('data-theme', ex.theme);
    panel.setAttribute('aria-label', 'Rotating example storefront: ' + ex.name);
    if (nameEl) nameEl.textContent = ex.name;
    if (subdomainEl) subdomainEl.textContent = ex.subdomain;
    if (showEl) showEl.textContent = ex.show;
    if (venueEl) venueEl.textContent = ex.venue;
    if (priceEl) priceEl.textContent = ex.price;
    dots.forEach(function (d, di) {
      d.classList.toggle('active', di === idx);
      d.classList.toggle('done', di < idx);
    });
  }

  function advance() {
    if (paused) return;
    i = (i + 1) % examples.length;
    if (reduceMotion) {
      render(i);
      return;
    }
    fadeTargets.forEach(function (el) { if (el) el.classList.add('fade-swap'); });
    setTimeout(function () {
      render(i);
      fadeTargets.forEach(function (el) { if (el) el.classList.remove('fade-swap'); });
    }, 220);
  }

  render(0);

  if (!reduceMotion) {
    setInterval(advance, DURATION);
    var label = panel.parentNode.querySelector('.example-label');
    [panel, label].forEach(function (el) {
      if (!el) return;
      el.addEventListener('mouseenter', function () { paused = true; });
      el.addEventListener('mouseleave', function () { paused = false; });
      el.addEventListener('focusin', function () { paused = true; });
      el.addEventListener('focusout', function () { paused = false; });
    });
  }
})();
