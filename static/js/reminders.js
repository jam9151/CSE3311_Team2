// Browser reminders for MediaFlow.
//
// While a MediaFlow tab is open, ask the server once a minute what is about to
// start and show a notification for anything we haven't already announced.
// This is not true background push: nothing fires once the tab is closed.
// That would need a service worker, VAPID keys and a scheduler on the server.
(function () {
  var script = document.currentScript;
  var url = script && script.dataset.url;
  var POLL_MS = 60 * 1000;
  var SEEN_KEY = "mediaflow-seen-reminders";
  var SEEN_LIMIT = 50;

  var supported = "Notification" in window;

  function readSeen() {
    try {
      return JSON.parse(localStorage.getItem(SEEN_KEY)) || [];
    } catch (e) {
      return [];
    }
  }

  function writeSeen(keys) {
    try {
      localStorage.setItem(SEEN_KEY, JSON.stringify(keys.slice(-SEEN_LIMIT)));
    } catch (e) {
      // Private mode or storage full: worst case is a repeat notification.
    }
  }

  function poll() {
    if (!url || !supported || Notification.permission !== "granted") return;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data || !data.enabled) return;
        var seen = readSeen();
        data.reminders.forEach(function (reminder) {
          if (seen.indexOf(reminder.key) !== -1) return;
          new Notification(reminder.title, { body: reminder.body, tag: reminder.key });
          seen.push(reminder.key);
        });
        writeSeen(seen);
      })
      .catch(function () {
        // Offline or server restarting. Try again next tick.
      });
  }

  // The settings card on My week shows permission state and the allow button.
  function setUpSettingsCard() {
    var status = document.querySelector("[data-notification-status]");
    var button = document.querySelector("[data-enable-notifications]");
    if (!status || !button) return;

    function render() {
      button.hidden = true;
      if (!supported) {
        status.textContent = "This browser doesn't support notifications.";
      } else if (Notification.permission === "granted") {
        status.textContent = "Browser notifications are allowed.";
      } else if (Notification.permission === "denied") {
        status.textContent = "Notifications are blocked. Change this in your browser's site settings.";
      } else {
        status.textContent = "Browser notifications aren't allowed yet.";
        button.hidden = false;
      }
    }

    button.addEventListener("click", function () {
      Notification.requestPermission().then(function () {
        render();
        poll();
      });
    });
    render();
  }

  setUpSettingsCard();
  poll();
  setInterval(poll, POLL_MS);
})();
