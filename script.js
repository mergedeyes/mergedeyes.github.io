/* Small, dependency-free enhancements. Everything degrades gracefully
   if JS is disabled — the page is fully readable without it.

   Shared as-is by index.html and every generated /projects/*.html page
   (they reuse the same <header class="rail"> markup), so code here has
   to tolerate nav links whose href isn't a same-page anchor - on a
   project page they're rewritten to "../index.html#section" instead of
   "#section", since they need to navigate back to index.html first. */

(function () {
  "use strict";

  var rail = document.getElementById("rail");
  var toggle = document.getElementById("menuToggle");
  var nav = document.getElementById("nav");
  var links = Array.prototype.slice.call(nav.querySelectorAll("a"));

  /* ── Mobile menu ── */
  function closeMenu() {
    rail.classList.remove("nav-open");
    toggle.setAttribute("aria-expanded", "false");
  }
  toggle.addEventListener("click", function () {
    var open = rail.classList.toggle("nav-open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  // Close after picking a section on mobile.
  links.forEach(function (a) {
    a.addEventListener("click", closeMenu);
  });

  /* ── Active section highlight ──
     Only same-page anchors (href starting with "#") can be resolved with
     querySelector; a relative path like "../index.html#work" is not a
     valid CSS selector and querySelector would throw on it, which would
     kill the rest of this script. On project pages every nav href is
     the relative kind, so `sections` just ends up empty and this becomes
     a no-op there — nothing to highlight against, which is correct. */
  var sections = links
    .map(function (a) {
      var href = a.getAttribute("href");
      return href && href.charAt(0) === "#" ? document.querySelector(href) : null;
    })
    .filter(Boolean);

  function setActiveHref(href) {
    links.forEach(function (a) {
      a.classList.toggle("is-active", a.getAttribute("href") === href);
    });
  }

  /* True once the page can't scroll any further down - i.e. the actual
     bottom, not just "Contact is on screen". */
  function isAtPageBottom() {
    return window.innerHeight + window.scrollY
      >= document.documentElement.scrollHeight - 2;
  }

  if ("IntersectionObserver" in window && sections.length) {
    var lastHref = "#" + sections[sections.length - 1].id;

    /* The rootMargin band below sits around the vertical middle of the
       viewport, which works well while scrolling through normal-height
       sections - but the last section (Contact) is short, and once the
       page is scrolled all the way to the bottom, Contact + the footer
       don't add up to enough height to ever push that middle band down
       onto Contact. It never intersects, so the observer keeps reporting
       whatever section intersected last (About Me) as still active. The
       isAtPageBottom() check has to live INSIDE this callback, not in a
       separate scroll listener racing against it - a separate listener
       can win the race for a moment, but this callback fires right after
       and, unaware anything changed, reports About Me as active again. */
    var spy = new IntersectionObserver(function (entries) {
      if (isAtPageBottom()) { setActiveHref(lastHref); return; }
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        setActiveHref("#" + entry.target.id);
      });
    }, { rootMargin: "-45% 0px -50% 0px", threshold: 0 });

    sections.forEach(function (s) { spy.observe(s); });

    window.addEventListener("scroll", function () {
      if (isAtPageBottom()) setActiveHref(lastHref);
    }, { passive: true });
  }

  /* ── Scroll reveal ── */
  var revealEls = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  if (reduce || !("IntersectionObserver" in window)) {
    revealEls.forEach(function (el) { el.classList.add("in"); });
  } else {
    var revealer = new IntersectionObserver(function (entries, obs) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          obs.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.1 });
    revealEls.forEach(function (el) { revealer.observe(el); });
  }

  /* ── Clickable project cards ──
     The whole card navigates to its data-href (the generated /projects
     page). A click that starts on an actual link — the title, Repository,
     or Live/demo — is left alone so that link's own target/behavior wins
     instead of also triggering a card-wide navigation. */
  var projectCards = Array.prototype.slice.call(document.querySelectorAll(".card[data-href]"));
  projectCards.forEach(function (card) {
    card.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      window.location.href = card.dataset.href;
    });
  });

  /* ── Footer year ── */
  var yearEl = document.getElementById("year");
  if (yearEl) yearEl.textContent = new Date().getFullYear();
})();
