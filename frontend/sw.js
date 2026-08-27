// SW minimalista: solo push notifications, NO cachea assets.
// Razón: el SW anterior cacheaba app-2026.js / chat-2026.js y cuando había
// errores de sintaxis los servía cacheados. Ahora el SW es "passthrough" para
// todo lo que no sea push — el servidor siempre sirve la versión más reciente.
const CACHE_VERSION = 'ojoia-passthrough-v2';

self.addEventListener('install', e => {
    e.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', e => {
    e.waitUntil(
        // Borra TODOS los caches viejos (no solo el de este CACHE_NAME)
        // porque el SW anterior pudo haber creado caches con otros nombres.
        caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

// No fetch handler = el SW NO intercepta ningún fetch. Las requests van
// siempre al servidor, evitando caches de archivos corruptos o viejos.

// Push notification handling (solo lo que necesitamos)
self.addEventListener('push', e => {
    try {
        const payload = e.data?.json() || {};
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
        for (let c of clist) {
            if (c.url === e.notification.data.url && 'focus' in c) return c.focus();
        }
        if (clients.openWindow) return clients.openWindow(e.notification.data.url || '/');
    }));
});
