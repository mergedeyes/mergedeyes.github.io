/* Small, dependency-free enhancements. Everything degrades gracefully
   if JS is disabled — the page is fully readable without it. */

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

  /* ── Active section highlight ── */
  var sections = links
    .map(function (a) { return document.querySelector(a.getAttribute("href")); })
    .filter(Boolean);

  if ("IntersectionObserver" in window) {
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var id = entry.target.id;
        links.forEach(function (a) {
          a.classList.toggle("is-active", a.getAttribute("href") === "#" + id);
        });
      });
    }, { rootMargin: "-45% 0px -50% 0px", threshold: 0 });

    sections.forEach(function (s) { spy.observe(s); });
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
