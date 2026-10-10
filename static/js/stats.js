// Hover and keyboard tooltips for the charts on the progress page.
//
// Anything with data-tip (the label) and data-tip-value (the number) gets one.
// The tooltip only repeats what the table view and the legend already say, so
// the page still works without this script.
(function () {
  var tip = document.getElementById("chart-tip");
  var valueSlot = tip && tip.querySelector("[data-tip-value-slot]");
  var labelSlot = tip && tip.querySelector("[data-tip-label-slot]");
  if (!tip || !valueSlot || !labelSlot) return;

  var MARGIN = 8;

  function show(target, pointerX) {
    // textContent, never innerHTML: these strings come from the page.
    valueSlot.textContent = target.getAttribute("data-tip-value");
    labelSlot.textContent = target.getAttribute("data-tip");
    tip.hidden = false;

    var box = target.getBoundingClientRect();
    var width = tip.offsetWidth;
    var height = tip.offsetHeight;
    var centre = pointerX === undefined ? box.left + box.width / 2 : pointerX;

    var left = Math.min(Math.max(centre - width / 2, MARGIN), window.innerWidth - width - MARGIN);
    // Above the mark, or below it if there's no room up there.
    var top = box.top - height - MARGIN;
    if (top < MARGIN) top = box.bottom + MARGIN;

    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  function hide() {
    tip.hidden = true;
  }

  document.querySelectorAll("[data-tip]").forEach(function (target) {
    target.addEventListener("pointerenter", function (event) {
      show(target, event.clientX);
    });
    target.addEventListener("pointermove", function (event) {
      show(target, event.clientX);
    });
    target.addEventListener("pointerleave", hide);
    target.addEventListener("focus", function () {
      show(target);
    });
    target.addEventListener("blur", hide);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") hide();
  });
  window.addEventListener("scroll", hide, { passive: true });
})();
