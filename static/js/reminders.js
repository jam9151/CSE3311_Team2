// Browser reminders for MediaFlow.
//
// Two layers. While a MediaFlow tab is open, ask the server once a minute
// what is about to start and show a notification for anything we haven't
// already announced. And, when the server has push keys configured, register
// the service worker and hand the server a push subscription, so the same
// reminders arrive with the tab closed (sent by manage.py send_push_reminders).
(function () {
  var script = document.currentScript;
  var url = script && script.dataset.url;
  var pushKey = script && script.dataset.pushKey;
  var swUrl = script && script.dataset.swUrl;
  var subscribeUrl = script && script.dataset.subscribeUrl;
  var csrf = script && script.dataset.csrf;
  var POLL_MS = 60 * 1000;
  var SEEN_KEY = "mediaflow-seen-reminders";
  var SEEN_LIMIT = 50;

  var supported = "Notification" in window;
  var pushSupported = supported && !!pushKey && "serviceWorker" in navigator && "PushManager" in window;

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

  // The applicationServerKey the browser wants is bytes, not the base64url
  // string the server hands out.
  function keyBytes(base64url) {
    var padding = "=".repeat((4 - (base64url.length % 4)) % 4);
    var base64 = (base64url + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(base64);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  // Register the worker, subscribe (or reuse the subscription this browser
  // already has), and tell the server. Resolves true when the server has it.
  function enablePush() {
    if (!pushSupported || Notification.permission !== "granted") return Promise.resolve(false);
    return navigator.serviceWorker
      .register(swUrl)
      .then(function (registration) {
        return registration.pushManager.getSubscription().then(function (existing) {
          return (
            existing ||
            registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(pushKey) })
          );
        });
      })
      .then(function (subscription) {
        return fetch(subscribeUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify(subscription.toJSON()),
        }).then(function (response) {
          return response.ok;
        });
      })
      .catch(function () {
        return false;
      });
  }

  // The settings card on My week shows permission state and the allow button.
  function setUpSettingsCard() {
    var status = document.querySelector("[data-notification-status]");
    var button = document.querySelector("[data-enable-notifications]");
    var note = document.querySelector("[data-push-note]");
    if (!status || !button) return;

    function describePush(enabled) {
      if (!note) return;
      if (enabled) {
        note.textContent = "Background reminders are on for this browser: they arrive even with MediaFlow closed.";
      } else if (pushSupported) {
        note.textContent = "Allow notifications and reminders will also arrive with MediaFlow closed.";
      } else {
        note.textContent = "Reminders appear while MediaFlow is open in a browser tab.";
      }
    }

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
      describePush(false);
      enablePush().then(describePush);
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
  // Keep the server's copy of this browser's subscription fresh on every page.
  enablePush();
  poll();
  setInterval(poll, POLL_MS);
})();
