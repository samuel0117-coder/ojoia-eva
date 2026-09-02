const CACHE_NAME = 'ojoia-v14';

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
    const webpushNotif = (msg.webpush && msg.webpush.notification) || {};
    const data = msg.data || {};
    const title = notification.title || data.title || 'OjoIA';
    const body = notification.body || data.body || 'Nueva alerta detectada';
    const opts = {
      body: body,
      icon: '/img/icon-192.png',
      badge: '/img/icon-192.png',
      tag: data.tag || 'ojoia-alert',
      requireInteraction: true,
      data: { url: data.deeplink || data.url || notification.url || '/' },
    };
    // E1 (2026-09-01): botones de acción enviados por el backend
    // (✅ Correcta / 🚫 Falsa alarma) para alertas de vigilancia.
    if (webpushNotif.actions && webpushNotif.actions.length) {
      opts.actions = webpushNotif.actions;
    }
    if (webpushNotif.image) {
      opts.image = webpushNotif.image;
    }
    e.waitUntil(self.registration.showNotification(title, opts));
  } catch(err) { console.error('Push error:', err); }
});

// E1/E2: click en el cuerpo abre el evento; click en un botón de acción
// abre el deeplink con la acción prefijada — el SPA la procesa al cargar
// (envía el feedback al backend automáticamente y muestra confirmación).
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const base = (e.notification.data && e.notification.data.url) || '/';
  let target = base;
  if (e.action) {
    // action 'real'|'false' → deeplink ?action=real|false (además del
    // action=review que ya traiga el link)
    const sep = base.includes('?') ? '&' : '?';
    target = base + sep + 'feedback=' + e.action;
  }
  e.waitUntil(clients.matchAll({type:'window'}).then(clist => {
    for (let c of clist) { if (c.url === target && 'focus' in c) return c.focus(); }
    if (clients.openWindow) return clients.openWindow(target);
  }));
});
