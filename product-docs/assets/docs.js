/* Digital Brain product documentation - the shell every page shares.
   Draws the top bar, the page list, "on this page", previous/next, the theme
   and motion switches, and the search box. Pages hold only their content. */
(function () {
  "use strict";

  var NAV = [
    { group: "Start", items: [
      ["index.html", "Start here"],
      ["find.html", "Where do I find…"],
      ["glossary.html", "Glossary"],
    ] },
    { group: "For everyone", audience: "everyone", items: [
      ["guide/what-is.html", "What Digital Brain is"],
      ["guide/features.html", "What each feature does for you"],
      ["guide/screens.html", "Using each screen"],
      ["guide/roles-and-access.html", "Roles, access and sign-in"],
      ["guide/ai-and-cost.html", "AI, cost and your data"],
      ["guide/faq.html", "Questions people ask"],
    ] },
    { group: "Technical", audience: "tech", items: [
      ["tech/architecture.html", "Architecture"],
      ["tech/technology.html", "Technology choices and why"],
      ["tech/components.html", "Components and modules"],
      ["tech/data-model.html", "Data model"],
      ["tech/knowledge-lifecycle.html", "Knowledge lifecycle"],
      ["tech/answering.html", "Answering and citations"],
      ["tech/ai-gateway.html", "AI gateway and cost control"],
      ["tech/security.html", "Security and HIPAA safeguards"],
      ["tech/tenancy.html", "Tenancy and row security"],
      ["tech/workers.html", "Workers, lanes and jobs"],
      ["tech/observability.html", "Observability and SLOs"],
      ["tech/apis.html", "APIs, MCP and SCIM"],
      ["tech/testing.html", "Testing gates and scorecard"],
      ["tech/configuration.html", "Configuration reference"],
    ] },
    { group: "Deployment", audience: "tech", items: [
      ["deploy/index.html", "Deployment strategies"],
      ["deploy/local.html", "Local development"],
      ["deploy/compose.html", "Docker Compose: VPS, scaled, observability"],
      ["deploy/cloud.html", "AWS, Azure and Google Cloud"],
      ["deploy/operations.html", "Secrets, TLS, upgrades, backups"],
    ] },
  ];

  var body = document.body;
  var root = body.getAttribute("data-root") || "";
  var here = body.getAttribute("data-page") || "index.html";

  function el(tag, attrs, html) {
    var node = document.createElement(tag);
    for (var key in attrs || {}) node.setAttribute(key, attrs[key]);
    if (html != null) node.innerHTML = html;
    return node;
  }
  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* storage blocked: the page still works */ }
    return null;
  }

  // ---------------------------------------------------------- theme and motion
  var theme = store("db-docs-theme");
  if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var motionOff = store("db-docs-motion") === "off" || reduce;
  // A sequenced diagram: <svg data-cycle="10"> with parts marked data-at="0.3"
  // (a fraction of the cycle). Each part fades in at its moment and every part
  // fades out together at the end, then the story starts again. Without script
  // every part simply shows.
  // A connector drawn outside a timed group would show before the boxes it joins.
  // Each one (split per subpath, so every segment gets its own arrow) appears once
  // the boxes at both of its ends have: the smallest timed group around each end.
  function timeConnectors(svg) {
    var groups = [];
    var timed = svg.querySelectorAll("g[data-at]");
    for (var g = 0; g < timed.length; g++) {
      var box = timed[g].getBBox();
      groups.push({ at: timed[g].getAttribute("data-at"), box: box, area: box.width * box.height });
    }
    var holder = function (point) {
      var best = null;
      for (var k = 0; k < groups.length; k++) {
        var b = groups[k].box;
        if (point.x >= b.x - 8 && point.x <= b.x + b.width + 8 && point.y >= b.y - 8 && point.y <= b.y + b.height + 8 &&
            (!best || groups[k].area < best.area)) best = groups[k];
      }
      return best;
    };
    var edges = svg.querySelectorAll("path.edge");
    for (var e = 0; e < edges.length; e++) {
      var piece = edges[e];
      if (piece.closest("[data-at]") || piece.closest("defs")) continue;
      var length = piece.getTotalLength();
      var to = holder(piece.getPointAtLength(length)), from = holder(piece.getPointAtLength(0));
      var at = Math.max(to ? parseFloat(to.at) : -1, from ? parseFloat(from.at) : -1);
      if (at >= 0) piece.setAttribute("data-at", String(at));
    }
  }
  // An SVG marker marks only a path's last vertex, so "M a H b M c H d" would draw
  // one arrowhead for two connectors. Each subpath becomes its own path.
  function splitEdges(svg) {
    var edges = svg.querySelectorAll("path.edge[marker-end]");
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      var subpaths = edge.getAttribute("d").split(/(?=M)/);
      if (subpaths.length < 2) continue;
      for (var s = 0; s < subpaths.length; s++) {
        var piece = edge.cloneNode(false);
        piece.setAttribute("d", subpaths[s].trim());
        edge.parentNode.insertBefore(piece, edge);
      }
      edge.parentNode.removeChild(edge);
    }
  }
  function sequence() {
    var all = document.querySelectorAll("main svg");
    for (var a = 0; a < all.length; a++) splitEdges(all[a]);
    var svgs = document.querySelectorAll("svg[data-cycle]");
    for (var i = 0; i < svgs.length; i++) {
      try { timeConnectors(svgs[i]); } catch (err) { /* a diagram still draws, untimed */ }
      var cycle = parseFloat(svgs[i].getAttribute("data-cycle"));
      var parts = svgs[i].querySelectorAll("[data-at]");
      for (var j = 0; j < parts.length; j++) {
        var at = Math.min(0.84, parseFloat(parts[j].getAttribute("data-at")));
        var anim = document.createElementNS("http://www.w3.org/2000/svg", "animate");
        anim.setAttribute("attributeName", "opacity");
        anim.setAttribute("values", "0;0;1;1;0");
        anim.setAttribute("keyTimes", "0;" + at.toFixed(3) + ";" + (at + 0.04).toFixed(3) + ";0.92;1");
        anim.setAttribute("dur", cycle + "s");
        anim.setAttribute("repeatCount", "indefinite");
        parts[j].appendChild(anim);
      }
    }
  }
  function applyMotion() {
    body.classList.toggle("no-motion", motionOff);
    var svgs = document.querySelectorAll("svg");
    for (var i = 0; i < svgs.length; i++) {
      if (!svgs[i].pauseAnimations) continue;
      if (motionOff) {
        // Hold a sequenced diagram at the moment everything is showing.
        var cycle = parseFloat(svgs[i].getAttribute("data-cycle"));
        if (cycle) svgs[i].setCurrentTime(cycle * 0.9);
        svgs[i].pauseAnimations();
      } else svgs[i].unpauseAnimations();
    }
  }

  var ICON = {
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>',
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    play: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>',
    logo: '<svg viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="var(--accent)"/><circle cx="10" cy="11" r="3" fill="var(--accent-ink)"/><circle cx="22" cy="10" r="3" fill="var(--accent-ink)"/><circle cx="16" cy="22" r="3.4" fill="var(--accent-ink)"/><path d="M10 11L22 10M10 11L16 22M22 10L16 22" stroke="var(--accent-ink)" stroke-width="1.6"/></svg>',
  };

  // ---------------------------------------------------------- top bar
  var current = null;
  NAV.forEach(function (g) { g.items.forEach(function (it) { if (it[0] === here) current = g; }); });
  var audience = current && current.audience;
  var top = el("header", { class: "topbar" });
  top.innerHTML =
    '<a class="brand" href="' + root + 'index.html">' + ICON.logo + ' Digital Brain <small>Product documentation</small></a>' +
    '<nav class="audience" aria-label="Audience">' +
    '<a href="' + root + 'guide/what-is.html" aria-current="' + (audience === "everyone") + '">For everyone</a>' +
    '<a href="' + root + 'tech/architecture.html" aria-current="' + (audience === "tech" && here.indexOf("deploy/") !== 0) + '">Technical</a>' +
    '<a href="' + root + 'deploy/index.html" aria-current="' + (here.indexOf("deploy/") === 0) + '">Deployment</a>' +
    '</nav><span class="spacer"></span>' +
    '<form class="search-box" role="search" action="' + root + 'find.html">' + ICON.search +
    '<input name="q" type="search" placeholder="Search any question…  ( / )" aria-label="Search the documentation"></form>' +
    '<button class="icon-btn" type="button" data-motion title="Pause or play the diagrams\' motion"></button>' +
    '<button class="icon-btn" type="button" data-theme-toggle title="Switch light or dark">' + ICON.sun + '</button>';
  body.insertBefore(top, body.firstChild);

  var motionButton = top.querySelector("[data-motion]");
  function drawMotionButton() {
    motionButton.innerHTML = (motionOff ? ICON.play + " Motion" : ICON.pause + " Motion");
    motionButton.setAttribute("aria-pressed", String(!motionOff));
  }
  motionButton.addEventListener("click", function () {
    motionOff = !motionOff;
    store("db-docs-motion", motionOff ? "off" : null);
    drawMotionButton(); applyMotion();
  });
  top.querySelector("[data-theme-toggle]").addEventListener("click", function () {
    var now = document.documentElement.getAttribute("data-theme");
    if (!now) now = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    var next = now === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    store("db-docs-theme", next);
  });
  document.addEventListener("keydown", function (event) {
    var target = event.target.tagName;
    if (event.key === "/" && target !== "INPUT" && target !== "TEXTAREA") {
      event.preventDefault();
      var input = document.querySelector(".big-search input") || top.querySelector(".search-box input");
      if (input) input.focus();
    }
  });

  // ---------------------------------------------------------- page list
  var main = document.querySelector("main");
  var layout = el("div", { class: "layout" });
  var side = el("nav", { class: "sidebar", "aria-label": "All pages" });
  var flat = [];
  NAV.forEach(function (g) {
    side.appendChild(el("h5", {}, g.group));
    g.items.forEach(function (it) {
      flat.push(it);
      var a = el("a", { href: root + it[0] }, it[1]);
      if (it[0] === here) a.setAttribute("aria-current", "page");
      side.appendChild(a);
    });
  });
  main.classList.add("content");
  main.parentNode.insertBefore(layout, main);
  layout.appendChild(side);
  layout.appendChild(main);

  // ---------------------------------------------------------- on this page
  var heads = main.querySelectorAll("h2[id], h2");
  if (heads.length > 1) {
    var toc = el("aside", { class: "toc", "aria-label": "On this page" });
    toc.appendChild(el("h6", {}, "On this page"));
    var links = [];
    for (var i = 0; i < heads.length; i++) {
      var h = heads[i];
      if (!h.id) h.id = h.textContent.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      var a = el("a", { href: "#" + h.id }, h.textContent);
      toc.appendChild(a); links.push([h, a]);
    }
    layout.appendChild(toc);
    var mark = function () {
      var best = links[0];
      for (var j = 0; j < links.length; j++) if (links[j][0].getBoundingClientRect().top < 120) best = links[j];
      links.forEach(function (pair) { pair[1].classList.toggle("active", pair === best); });
    };
    window.addEventListener("scroll", mark, { passive: true }); mark();
  }

  // ---------------------------------------------------------- previous / next
  var at = -1;
  flat.forEach(function (it, n) { if (it[0] === here) at = n; });
  if (at >= 0 && !body.hasAttribute("data-no-pager")) {
    var pager = el("nav", { class: "pager", "aria-label": "Previous and next page" });
    if (at > 0) pager.appendChild(el("a", { class: "prev", href: root + flat[at - 1][0] }, "<small>Previous</small>" + flat[at - 1][1]));
    else pager.appendChild(el("span"));
    if (at < flat.length - 1) pager.appendChild(el("a", { class: "next", href: root + flat[at + 1][0] }, "<small>Next</small>" + flat[at + 1][1]));
    main.appendChild(pager);
  }
  var foot = el("footer", { class: "site" },
    "Digital Brain product documentation. Written from the code on <code>main</code>; where a page and the code disagree, the code is right. " +
    "Works offline: open <code>product-docs/index.html</code> in any browser.");
  main.appendChild(foot);

  drawMotionButton();
  sequence();
  applyMotion();
  window.addEventListener("load", applyMotion);

  // ---------------------------------------------------------- search page
  var big = document.querySelector("[data-search]");
  if (big && window.DOCS_INDEX) {
    var input = big.querySelector("input");
    var list = document.querySelector("[data-results]");
    var count = document.querySelector("[data-count]");
    var filter = "all";
    var params = new URLSearchParams(window.location.search);
    input.value = params.get("q") || "";
    var buttons = document.querySelectorAll("[data-filter]");
    for (var b = 0; b < buttons.length; b++) {
      buttons[b].addEventListener("click", function () {
        filter = this.getAttribute("data-filter");
        for (var k = 0; k < buttons.length; k++) buttons[k].setAttribute("aria-pressed", String(buttons[k] === this));
        draw();
      });
    }
    var words = function (text) { return (text.toLowerCase().match(/[a-z0-9§.]+/g) || []).filter(function (w) { return w.length > 1; }); };
    var draw = function () {
      var query = words(input.value);
      var scored = [];
      window.DOCS_INDEX.forEach(function (entry) {
        if (filter !== "all" && entry.for !== filter && entry.for !== "both") return;
        var hay = (entry.q + " " + entry.a + " " + (entry.k || "")).toLowerCase();
        var title = entry.q.toLowerCase();
        var score = 0;
        for (var w = 0; w < query.length; w++) {
          var term = query[w];
          var stem = term.length > 4 ? term.slice(0, term.length - 1) : term;
          if (hay.indexOf(stem) < 0) { score = -1; break; }
          score += title.indexOf(stem) >= 0 ? 3 : 1;
        }
        if (score >= 0) scored.push([score, entry]);
      });
      scored.sort(function (x, y) { return y[0] - x[0]; });
      list.innerHTML = "";
      scored.forEach(function (pair) {
        var entry = pair[1];
        var li = el("li");
        li.innerHTML = '<span class="where">' + (entry.for === "tech" ? "Technical" : entry.for === "everyone" ? "For everyone" : "Everyone and technical") + " · " + entry.u.split("#")[0] + "</span><br>" +
          '<a href="' + entry.u + '">' + entry.q + "</a><p>" + entry.a + "</p>";
        list.appendChild(li);
      });
      count.textContent = scored.length + (scored.length === 1 ? " answer" : " answers");
    };
    input.addEventListener("input", function () {
      var url = new URL(window.location.href);
      if (input.value) url.searchParams.set("q", input.value); else url.searchParams.delete("q");
      try { window.history.replaceState(null, "", url); } catch (e) { /* file: urls */ }
      draw();
    });
    draw();
  }
})();
