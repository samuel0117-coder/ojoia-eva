// firebase-messaging-sw.js — requerido por Firebase Messaging para web push.
// Firebase registra su propio SW en esta ruta POR DEFECTO; el enfoque
// estándar: importScripts del SDK compat y delegar el resto a /sw.js
// (que ya maneja push + notificationclick con soporte FCM v1 payload).
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js');

firebase.initializeApp({
    apiKey: "AIzaSyAtlS7rikClpJBVHM46gPvN4HL_CYyRxP0",
    authDomain: "ojoia-67216.firebaseapp.com",
    projectId: "ojoia-67216",
    storageBucket: "ojoia-67216.firebasestorage.app",
    messagingSenderId: "490868607747",
    appId: "1:490868607747:web:f722468d4f3493deb8f736",
    measurementId: "G-KX5V3B6547"
});

const messaging = firebase.messaging();
messaging.onBackgroundMessage((payload) => {
    // Cuando la PWA está en background, mostramos la notificación
    // con el mismo formato que /sw.js usa para push directo.
    const n = payload.notification || {};
    const d = payload.data || {};
    const title = n.title || d.title || 'OjoIA';
    const body = n.body || d.body || 'Nueva alerta detectada';
    self.registration.showNotification(title, {
        body: body,
        icon: '/img/icon-192.png',
        badge: '/img/icon-192.png',
        tag: d.tag || 'ojoia-alert',
        requireInteraction: true,
        data: { url: d.deeplink || d.url || '/' }
    });
});
