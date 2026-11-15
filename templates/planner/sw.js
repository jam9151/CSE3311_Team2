// MediaFlow service worker. Only job: show background push reminders and
// bring the app forward when one is clicked. Served at /sw.js so its scope
// covers the whole site. No caching, no offline mode.
self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "MediaFlow", body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "MediaFlow", {
      body: data.body || "",
      tag: data.tag || undefined,
      data: { url: data.url || "/dashboard/" },
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/dashboard/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windows) {
      for (var i = 0; i < windows.length; i++) {
        if ("focus" in windows[i]) {
          windows[i].navigate(url);
          return windows[i].focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
