/* GoldManager PWA service worker — push notifications only (no offline cache,
   so the app shell is never served stale; matches the dashboard's no-cache policy). */
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let data = { title: 'GoldManager', body: 'Update' };
  try { if (e.data) data = e.data.json(); }
  catch (x) { if (e.data) data = { title: 'GoldManager', body: e.data.text() }; }
  e.waitUntil(self.registration.showNotification(data.title || 'GoldManager', {
    body: data.body || '',
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png',
    tag: data.tag || 'goldmanager',
    renotify: true,
    data: { url: '/m/' },
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cl) => {
    for (const c of cl) { if (c.url.includes('/m') && 'focus' in c) return c.focus(); }
    if (self.clients.openWindow) return self.clients.openWindow('/m/');
  }));
});
