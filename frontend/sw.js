const CACHE_NAME = 'ojoia-v7';

self.addEventListener('install', e => { 
    e.waitUntil(self.skipWaiting()); 
});
self.addEventListener('activate', e => { 
    e.waitUntil(
        caches.keys().then(keys => 
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});
self.addEventListener('push', e => {
  try {
    const payload = e.data?.json() || {};
    // FCM sends notification in payload.message.notification
    // and custom data in payload.message.data
    const msg = payload.message || payload;
    const notification = msg.notification || {};
    const data = msg.data || {};
    const title = notification.title || data.title || 'OjoIA';
    const body = notification.body || data.body || 'Nueva alerta detectada';
    const opts = {
      body: body,
      icon: '/img/icon-192.png',
      badge: '/img/icon-192.png',
      tag: data.tag || 'ojoia-alert',
      requireInteraction: true,
      data: { url: data.deeplink || data.url || notification.url || '/' }
    };
    e.waitUntil(self.registration.showNotification(title, opts));
  } catch(err) { console.error('Push error:', err); }
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type:'window'}).then(clist => {
    for (let c of clist) { if (c.url === e.notification.data.url && 'focus' in c) return c.focus(); }
    if (clients.openWindow) return clients.openWindow(e.notification.data.url || '/');
  }));
});