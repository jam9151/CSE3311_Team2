// Theme picker in the footer: System (follow the device), Light or Dark.
//
// The choice lives in localStorage and is applied before the first paint by
// a one-line script in <head>, so this file only has to keep the buttons in
// step and react to clicks. "System" removes the override and lets the
// prefers-color-scheme rules in the stylesheet decide.
(function () {
  var KEY = "mediaflow-theme";
  var root = document.documentElement;
  var buttons = document.querySelectorAll("[data-theme-choice]");
  if (!buttons.length) return;

  function current() {
    try {
      var saved = localStorage.getItem(KEY);
      return saved === "light" || saved === "dark" ? saved : "system";
    } catch (e) {
      return "system";
    }
  }

  function apply(choice) {
    if (choice === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", choice);
    }
    try {
      if (choice === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch (e) {
      // Private mode: the choice lasts for this page only.
    }
    buttons.forEach(function (button) {
      var on = button.dataset.themeChoice === choice;
      button.setAttribute("aria-pressed", on ? "true" : "false");
      button.classList.toggle("btn-secondary", on);
      button.classList.toggle("btn-ghost", !on);
    });
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      apply(button.dataset.themeChoice);
    });
  });
  apply(current());
})();
