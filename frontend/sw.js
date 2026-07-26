const CACHE_NAME = 'ojoia-v8';

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
  const data = e.notification.data || {};
  const targetUrl = data.url || '/';
  e.waitUntil(clients.matchAll({type:'window', includeUncontrolled: true}).then(clist => {
    // Si hay ventana abierta de nuestro origen → focus + delegar el deep-link al cliente
    // (el cliente cambia su hash para que _handleInitialRoute abra el modal del evento).
    for (let c of clist) {
      try {
        const url = new URL(c.url);
        if (url.origin === self.location.origin) {
          c.postMessage({ type: 'ojoia-event', url: targetUrl });
          return c.focus();
        }
      } catch (_) {}
    }
    // Si no hay ventana abierta, abrir directo con el deep-link completo (#cameras?event=...).
    // NO limpiar el ?event= : el routing del frontend necesita ese parámetro.
    const openTarget = targetUrl || self.registration.scope;
    if (clients.openWindow) return clients.openWindow(openTarget);
  }));
});