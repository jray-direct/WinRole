(function () {
  var nav = document.querySelector(".side-nav nav");
  if (!nav) return;
  var indicator = nav.querySelector(".nav-indicator");
  var links = Array.prototype.slice.call(nav.querySelectorAll(":scope > a"));
  var current = links.filter(function (a) { return a.getAttribute("aria-current") === "page"; })[0];
  if (!indicator || !current) return;

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var root = document.documentElement;

  function place(link, animate) {
    if (!animate) indicator.style.transition = "none";
    indicator.style.width = link.offsetWidth + "px";
    indicator.style.height = link.offsetHeight + "px";
    indicator.style.transform = "translate(" + link.offsetLeft + "px, " + link.offsetTop + "px)";
    if (!animate) {
      indicator.getBoundingClientRect();
      indicator.style.transition = "";
    }
  }

  var data = null;
  try { data = JSON.parse(sessionStorage.getItem("js-nav") || "null"); } catch (e) {}
  sessionStorage.removeItem("js-nav");
  var from = data && (Date.now() - data.t) < 2000 ? links[data.from] : null;
  if (from === current) from = null;

  place(from || current, false);
  nav.classList.add("is-ready");
  if (from && !reduce) {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { place(current, true); });
    });
    indicator.addEventListener("transitionend", function (ev) {
      if (ev.propertyName !== "transform") return;
      place(current, false);
    });
  }

  var main = document.querySelector("main");
  if (main && root.hasAttribute("data-nav-enter")) {
    var clearEnter = function (ev) {
      if (ev && ev.target !== main) return;
      root.removeAttribute("data-nav-enter");
    };
    main.addEventListener("animationend", clearEnter);
    window.setTimeout(function () { root.removeAttribute("data-nav-enter"); }, 700);
  }

  window.addEventListener("resize", function () { place(current, false); });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      if (!from) place(current, false);
    });
  }

  var leaving = false;
  links.forEach(function (a) {
    a.addEventListener("click", function (e) {
      if (leaving || e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if (a.target && a.target !== "_self") return;
      var to = links.indexOf(a);
      var fromIndex = links.indexOf(current);
      if (to < 0 || to === fromIndex) return;
      var dir = to > fromIndex ? 1 : -1;
      try {
        sessionStorage.setItem("js-nav", JSON.stringify({ from: fromIndex, dir: dir, t: Date.now() }));
      } catch (err) {}
      if (reduce || !main) return;
      e.preventDefault();
      leaving = true;
      root.setAttribute("data-nav-leave", dir > 0 ? "down" : "up");
      var done = false;
      var go = function (ev) {
        if (ev && ev.target !== main) return;
        if (done) return;
        done = true;
        window.location.href = a.href;
      };
      main.addEventListener("animationend", go);
      window.setTimeout(go, 420);
    });
  });
})();
