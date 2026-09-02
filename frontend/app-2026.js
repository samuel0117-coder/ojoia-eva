// ============================================================
// OjoIA - App v12 Production Ready (BUILD 20260822-a)
// api.ojoia.com.do | Firebase: ojoia-67216
// Análisis: detección encuentra objetos -> Eva revisa el área
// Fix cache-busting: este build fuerza invalidación de cache de Cloudflare
// que tenía Content-Encoding: br incorrecto.
// ============================================================

const firebaseConfig = {
    apiKey: "AIzaSyAtlS7rikClpJBVHM46gPvN4HL_CYyRxP0",
    authDomain: "ojoia-67216.firebaseapp.com",
    projectId: "ojoia-67216",
    storageBucket: "ojoia-67216.firebasestorage.app",
    messagingSenderId: "490868607747",
    appId: "1:490868607747:web:f722468d4f3493deb8f736",
    measurementId: "G-KX5V3B6547"
};
firebase.initializeApp(firebaseConfig);

// P0 (Sección #9): apiFetch ahora detecta 401 y dispara un evento global.
// Antes: un 401 respondía {"detail":"..."} (JSON válido) y `await r.json()` no throw,
// pero la UI mostraba "vacío" en lugar de "sesión expirada".
// Ahora: en 401, limpiamos token + userId y disparamos `ojoia:auth-expired` para que
// App._handleAuthExpired redirija a login. La promise rechaza para que el caller
// no siga procesando data corrupta/undefined.
//
// 2026-08-26: dedup de GETs idénticos en vuelo. Cuando dos partes de la app piden
// el mismo recurso simultáneamente (p.ej. /api/cameras al abrir el home Y al
// verificar el contador de alertas), evitamos 2 requests al backend. Solo aplica
// a GETs sin body, y los POSTs/PUTs/DELETEs pasan siempre (pueden no ser idempotentes).
const _getInflight = new Map();
function apiFetch(url, opts = {}) {
    const headers = { ...opts.headers };
    if (opts.body && typeof opts.body === 'string') {
        try { JSON.parse(opts.body); headers['Content-Type'] = 'application/json'; } catch(e) {}
    }
    if (!headers['Content-Type']) headers['Content-Type'] = 'application/json';
    // Add Authorization header if access token exists
    if (App.accessToken) {
        headers['Authorization'] = 'Bearer ' + App.accessToken;
    }
    const method = (opts.method || 'GET').toUpperCase();
    // Dedup solo para GETs sin body: misma URL+método dentro de la misma ventana
    // de inflight = misma Promise. Reduce latencia y carga del backend sin cambiar
    // comportamiento (la response se entrega a todos los awaiters por separado).
    if (method === 'GET' && !opts.body) {
        const key = url;
        const existing = _getInflight.get(key);
        if (existing) return existing;
    }
    const p = fetch(url, { mode: 'cors', ...opts, headers }).then(r => {
        if (r.status === 401) {
            // Sesión expirada o token inválido. Limpia y notifica.
            try {
                sessionStorage.removeItem('ojoia_token');
                localStorage.removeItem('ojoia_token');
                if (typeof App !== 'undefined') {
                    App.accessToken = null;
                    App.userId = null;
                }
                // Dispara evento; App._handleAuthExpired lo escucha
                window.dispatchEvent(new CustomEvent('ojoia:auth-expired', {
                    detail: { url, status: 401 }
                }));
            } catch (e) { console.warn('[apiFetch] 401 cleanup error:', e); }
            throw new Error('Sesión expirada (401)');
        }
        return r;
    });
    if (method === 'GET' && !opts.body) {
        _getInflight.set(url, p);
        // Limpia del map apenas termina (resuelta o rechazada) — un solo turno
        p.finally(() => _getInflight.delete(url));
    }
    return p;
}

const App = {
    // P0 (Fuga #7.1): helper para escapar valores que van dentro de atributos HTML
    // y onclick inline. Si el backend devuelve un event_id / camera_id / profile.name
    // con ' o " o <script>, sin este escape tenemos RCE via XSS.
    _escAttr(s) {
        return String(s == null ? '' : s).replace(/&/g, '&').replace(/</g, '<').replace(/>/g, '>')
            .replace(/"/g, '"').replace(/'/g, '\'');
    },
    userId: null,
    accessToken: null,
    API: '',
    page: 'home',
    _polls: {},
    _authStarted: false,
    _loginMode: 'login',
    _evaSession: null,
    _evaCamId: null,
    _evaReady: false,
    _pendingNotificationEventId: '',
    _pendingNotificationCameraId: '',
    _viewerCamId: null,
    _apiReady: false,
    _homeFrameInFlight: false,
    _homeLastYoloFetch: 0,
    _homeYoloPollMs: 2000,
    _homeLastDetections: [],
    _homeWatermarkText: '',

    init() {
        const h = window.location.hostname;
        if (h === '10.0.0.44' || h === 'localhost' || h === '') {
            this.API = 'http://10.0.0.44:8005';
            this._apiReady = true;
            this._startAuth();
            return;
        }
        this._fetchServerUrl();
    },

    async _fetchServerUrl() {
        try {
            const r = await fetch('https://api.ojoia.com.do/health', {
                mode: 'cors',
                signal: AbortSignal.timeout(3000)
            });
            this.API = r.ok ? 'https://api.ojoia.com.do' : this.API || 'https://api.ojoia.com.do';
        } catch(e) {
            this.API = 'https://api.ojoia.com.do';
        }
        if (!this._apiReady) { this._apiReady = true; }
        if (!this._authStarted) { this._authStarted = true; this._startAuth(); }
    },

    async _waitForAPI(timeoutMs = 8000) {
        if (this._apiReady && this.API) return this.API;
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            if (this._apiReady && this.API) return this.API;
            await new Promise(r => setTimeout(r, 200));
        }
        if (!this.API) this.API = 'https://api.ojoia.com.do';
        return this.API;
    },

    _startAuth() {
        firebase.auth().onAuthStateChanged(async u => {
            if (u) {
                await this._waitForAPI();
                // Restore access_token de sessionStorage o localStorage.
                // sessionStorage: se borra al cerrar pestaña (más seguro)
                // localStorage: persiste entre sesiones (UX: no re-login cada vez)
                const storedToken = sessionStorage.getItem('ojoia_token') || localStorage.getItem('ojoia_token');
                if (storedToken) {
                    this.accessToken = storedToken;
                }
                const token = await u.getIdToken();
                const apiUrl = await this._waitForAPI();
                const r = await fetch(apiUrl + '/auth/firebase/verify', {
                    method: 'POST', mode: 'cors',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id_token: token, email: u.email, name: u.displayName || '' })
                });
                const d = await r.json();
                if (d.success) {
                    this.userId = d.user_id;
                    this.accessToken = d.access_token;
                    // Persistir en ambos storages: sessionStorage (sesión actual)
                    // y localStorage (persiste entre sesiones del navegador)
                    localStorage.setItem('ojoia_uid', this.userId);
                    sessionStorage.setItem('ojoia_token', this.accessToken);
                    localStorage.setItem('ojoia_token', this.accessToken);
                    this._showApp();
                } else {
                    // Usuario Firebase OK pero sin registro completo en backend
                    firebase.auth().signOut();
                    this._showLogin();
                    this.setLoginMode('register');
                    this._err('Tu cuenta no está completa. Regístrate para empezar.');
                }
            } else {
                this._showLogin();
            }
        });
        ['login-email','login-pw','login-pw2','reg-name','reg-business'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('keypress', e => { if (e.key === 'Enter') this.doLogin(); });
        });
        // P0 (Sección #9): listener global para 401 desde apiFetch.
        // Dispara una sola redirección a login (evitar storms si hay N requests en paralelo).
        this._authExpiredHandled = false;
        window.addEventListener('ojoia:auth-expired', () => {
            if (this._authExpiredHandled) return;
            this._authExpiredHandled = true;
            this._handleAuthExpired();
        });
    },

    // P0 (Sección #9): cuando apiFetch detecta 401, ejecuta esto.
    // Limpia estado, muestra mensaje, y redirige a login.
    _handleAuthExpired() {
        console.warn('[App] Sesión expirada — redirigiendo a login');
        // Cierra el modal de chat por si está abierto
        try { if (typeof EvaChat !== 'undefined' && EvaChat._teardown) EvaChat._teardown(); } catch(e) {}
        // Limpia el token de sessionStorage. Tambien limpia localStorage
        // por si quedo un token legacy de versiones anteriores (migración
        // segura — si no existe, removeItem no hace nada).
        try { sessionStorage.removeItem('ojoia_token'); localStorage.removeItem('ojoia_token'); } catch(e) {}
        // Detén todos los polls — si la sesión expiró, cada poll disparará 401
        // y desperdiciará requests hasta el redirect. Mejor cortar de raíz.
        try { this._clearAllPolls(); } catch(e) {}
        // Firebase signOut para que onAuthStateChanged dispare de nuevo con usuario limpio
        try { if (firebase && firebase.auth && firebase.auth().currentUser) firebase.auth().signOut(); } catch(e) {}
        this.accessToken = null;
        this.userId = null;
        // Reset flag después de un rato para permitir re-handling
        setTimeout(() => { this._authExpiredHandled = false; }, 5000);
        // Redirige a login (la UI de login se mostrará via onAuthStateChanged)
        this._showLogin();
        this._err('Tu sesión expiró. Por favor inicia sesión de nuevo.');
    },

    _showLogin() {
        document.getElementById('screen-login').style.display = 'flex';
        document.getElementById('screen-app').style.display = 'none';
        this.setLoginMode('login');
    },

    setLoginMode(mode) {
        this._loginMode = mode;
        const isReg = mode === 'register';
        document.getElementById('tab-switch-login').classList.toggle('active', !isReg);
        document.getElementById('tab-switch-register').classList.toggle('active', isReg);
        document.getElementById('reg-fields').style.display = isReg ? 'block' : 'none';
        const pw2group = document.getElementById('pw2-group');
        if (pw2group) pw2group.style.display = isReg ? 'block' : 'none';
        const consentGrp = document.getElementById('consent-group');
        if (consentGrp) consentGrp.style.display = isReg ? 'block' : 'none';
        document.getElementById('btn-auth').textContent = isReg ? 'Crear cuenta' : 'Entrar';
        document.getElementById('auth-hint').textContent = isReg
            ? 'Eva configurará tu primera cámara después.'
            : '¿Olvidaste tu contraseña? Contacta al administrador.';
        this._clearErr();
    },

    async doLogin() {
        const email = document.getElementById('login-email').value.trim();
        const pw = document.getElementById('login-pw').value;
        if (!email || !pw) { this._err('Completa todos los campos'); return; }

        const btn = document.getElementById('btn-auth');
        btn.disabled = true;
        btn.textContent = '...';
        this._clearErr();

        if (this._loginMode === 'register') {
            const name = document.getElementById('reg-name').value.trim();
            const biz = document.getElementById('reg-business').value.trim();
            const bizType = document.getElementById('reg-biztype').value;
            const pw2 = document.getElementById('login-pw2').value;
            if (!name) { this._err('📝 Escribe tu nombre para continuar'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
            if (!biz) { this._err('🏢 Escribe el nombre de tu negocio'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
            if (pw.length < 6) { this._err('🔒 La contraseña debe tener al menos 6 caracteres'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
            if (pw !== pw2) { this._err('🔒 Las contraseñas no coinciden'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
            const consentEl = document.getElementById('reg-consent');
            if (consentEl && !consentEl.checked) { this._err('✅ Debes aceptar las políticas de uso para crear tu cuenta'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
            btn.textContent = 'Creando cuenta...';
            try {
                const cred = await firebase.auth().createUserWithEmailAndPassword(email, pw);
                await cred.user.updateProfile({ displayName: name });
                const ok = await this._verifyFB(cred.user, {
                    name, business_name: biz, email, business_type: bizType || 'other',
                    schedule_open: '08:00', schedule_close: '20:00',
                    consent_network_scan: !!(document.getElementById('reg-consent') || {}).checked
                });
                if (!ok) {
                    btn.disabled = false;
                    btn.textContent = 'Crear cuenta';
                }
            } catch(e) {
                btn.disabled = false;
                btn.textContent = 'Crear cuenta';
                this._err(this._fbErr(e));
            }
        } else {
            // ── LOGIN ──
            if (pw.length < 6) { this._err('Contraseña muy corta'); btn.disabled = false; btn.textContent = 'Entrar'; return; }
            btn.textContent = 'Entrando...';
            try {
                const cred = await firebase.auth().signInWithEmailAndPassword(email, pw);
                await this._waitForAPI();
                const token = await cred.user.getIdToken();
                const apiUrl = await this._waitForAPI();
                const r = await fetch(apiUrl + '/auth/firebase/verify', {
                    method: 'POST', mode: 'cors',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id_token: token, email: cred.user.email, name: cred.user.displayName || '' })
                });
                const d = await r.json();
                if (d.success) {
                    this.userId = d.user_id;
                    this.accessToken = d.access_token;
                    // Persistir en ambos storages: sessionStorage (sesión actual)
                    // y localStorage (persiste entre sesiones del navegador)
                    localStorage.setItem('ojoia_uid', this.userId);
                    sessionStorage.setItem('ojoia_token', this.accessToken);
                    localStorage.setItem('ojoia_token', this.accessToken);
                    this._showApp();
                } else {
                    // Usuario no registrado en backend — cambiar a registro
                    firebase.auth().signOut();
                    this.setLoginMode('register');
                    this._err('⚠️ Usuario no registrado. Completa tus datos para crear tu cuenta.');
                    btn.disabled = false;
                    btn.textContent = 'Crear cuenta';
                }
            } catch(e) {
                btn.disabled = false;
                btn.textContent = 'Entrar';
                const code = e.code || '';
                if (code === 'auth/invalid-credential' || code === 'auth/wrong-password') {
                    this._err('⚠️ Contraseña incorrecta. Intenta de nuevo.');
                } else if (code === 'auth/user-not-found') {
                    this._err('⚠️ Este correo no está registrado.');
                } else if (code === 'auth/too-many-requests') {
                    this._err('⚠️ Demasiados intentos. Espera un momento.');
                } else {
                    this._err(this._fbErr(e));
                }
            }
        }
    },

    async _verifyFB(user, extra = {}) {
        const token = await user.getIdToken();
        const body = { id_token: token, email: user.email, name: user.displayName || '', ...extra };
        const apiUrl = await this._waitForAPI();
        const r = await fetch(apiUrl + '/auth/firebase/verify', {
            method: 'POST', mode: 'cors',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const d = await r.json();
        if (d.success) {
            this.userId = d.user_id;
            this.accessToken = d.access_token;
            // Persistir en ambos storages: sessionStorage (sesión actual)
            // y localStorage (persiste entre sesiones del navegador)
            localStorage.setItem('ojoia_uid', this.userId);
            sessionStorage.setItem('ojoia_token', this.accessToken);
            localStorage.setItem('ojoia_token', this.accessToken);
            this._showApp();
            return d;
        } else {
            this._err(d.message || d.error || 'Error de autenticación');
            return null;
        }
    },

    _showApp() {
        document.getElementById('screen-login').style.display = 'none';
        document.getElementById('screen-app').style.display = 'flex';
        this._initPush();
        this._handleInitialRoute();
        // E2: feedback del push (botón ✅/🚫) se envía con userId listo.
        if (this._pendingAutoFeedback) {
            setTimeout(() => this._processPendingFeedback(), 1200);
        }
    },

    logout() {
        this._clearAllPolls();
        // P0 (Fuga #7.2): limpiar token de sessionStorage Y localStorage
        // (migra tokens legacy que estaban en localStorage)
        localStorage.removeItem('ojoia_uid');
        sessionStorage.removeItem('ojoia_token');
        localStorage.removeItem('ojoia_token');
        this.userId = null;
        this.accessToken = null;
        firebase.auth().signOut();
        this._showLogin();
    },

    _fbErr(e) {
        const m = {
            'auth/user-not-found': 'Este correo no está registrado. Crea una cuenta.',
            'auth/wrong-password': 'Contraseña incorrecta',
            'auth/invalid-credential': 'Credenciales incorrectas. Verifica tu email y contraseña.',
            'auth/email-already-in-use': 'Este correo ya tiene una cuenta. Inicia sesión.',
            'auth/weak-password': 'Contraseña muy corta (mín. 6 caracteres)',
            'auth/invalid-email': 'El formato del correo no es válido',
            'auth/too-many-requests': 'Demasiados intentos. Espera un momento.',
            'auth/network-request-failed': 'Error de conexión. Verifica tu internet.',
            'auth/user-disabled': 'Esta cuenta ha sido desactivada.',
        };
        return m[e.code] || e.message || 'Error desconocido';
    },

    _err(msg) {
        const el = document.getElementById('login-err');
        el.textContent = msg;
        el.style.display = 'block';
        // Si es mensaje de registro requerido, estilo más visible
        if (msg.includes('no registrado') || msg.includes('Regístrate')) {
            el.style.background = 'rgba(255,69,58,0.15)';
            el.style.color = '#ff453a';
            el.style.padding = '10px 14px';
            el.style.borderRadius = '8px';
            el.style.border = '1px solid rgba(255,69,58,0.3)';
        } else {
            el.style.background = '';
            el.style.color = '';
            el.style.padding = '';
            el.style.borderRadius = '';
            el.style.border = '';
        }
    },
    _clearErr() {
        const el = document.getElementById('login-err');
        if (el) {
            el.textContent = '';
            el.style.display = 'none';
            el.style.background = '';
            el.style.color = '';
            el.style.padding = '';
            el.style.borderRadius = '';
            el.style.border = '';
        }
    },

    go(page, eventId) {
        if (this.page !== page) this._clearAllPolls();
        this.page = page;
        const c = document.getElementById('app-content');
        c.className = `page ${page}-page`;
c.style.display = '';
        c.style.overflow = '';
        c.style.padding = '';
        c.style.flex = '';
        c.style.height = '';
        c.style.minHeight = '';
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
        ({ home: () => this._pageHome(c), cameras: () => this._pageHome(c), eva: () => this._pageEva(c), events: () => this._pageEvents(c, eventId), settings: () => this._pageSettings(c) })[page]?.();
    },

    _clearAllPolls() { Object.values(this._polls).forEach(id => clearInterval(id)); this._polls = {}; if (this._configViewerPoll) { clearInterval(this._configViewerPoll); this._configViewerPoll = null; }
        // P0 (Bug #2): reset de caches per-camara. Sin esto, al volver a Home
        // despues de navegar a otra pagina, _homeStreamStarted[key] sigue en
        // true y el stream MJPEG NO se reinicia => imagen en negro silenciosa.
        // Tambien limpiamos _homeFrameInFlight para evitar deadlock de fetch.
        this._homeStreamStarted = {};
        this._homeFrameInFlight = {};
        // Nota: NO limpiamos _homeLastDetectionsByCam ni _homeWatermarkTextByCam
        // porque el primer frame de la Nueva carga usara el cache anterior
        // mientras llega uno fresco — mejor UX que placeholder vacio.
    },
    _resetScrollContent(c) {
        if (!c) return;
        c.style.display = 'block';
        c.style.overflow = 'auto';
        c.style.overflowY = 'auto';
        c.style.overflowX = 'hidden';
        c.style.padding = '16px';
        c.style.paddingBottom = '96px';
        c.style.flex = '1 1 auto';
        c.style.height = '';
        c.style.minHeight = '0';
    },
    _resetFlexContent(c) {
        if (!c) return;
        c.style.display = 'flex';
        c.style.overflow = 'hidden';
        c.style.overflowY = 'hidden';
        c.style.padding = '0';
        c.style.paddingBottom = '0';
        c.style.flex = 'none';
        c.style.flexDirection = 'column';
        c.style.height = 'calc(100dvh - 108px)';
        c.style.minHeight = '0';
    },
    _removeStaleEvaChat() {
        const stale = document.getElementById('eva-chat-container');
        if (stale) stale.remove();
    },
    _isCurrentPage(page) { return this.page === page && document.getElementById('app-content'); },
    _handleInitialRoute() {
        const hash = decodeURIComponent(window.location.hash || '').replace(/^#/, '');
        const [pageName, query = ''] = hash.split('?');
        const params = new URLSearchParams(query);
        const eventId = params.get('event') || params.get('alert') || '';
        const cameraId = params.get('camera') || params.get('cam') || '';
        // E2 (Fase E): el usuario tocó un botón del push (✅/🚫) → el SW
        // abre el deeplink con feedback=real|false. Al cargar, enviamos el
        // feedback automáticamente y mostramos confirmación en el evento.
        const feedbackAction = params.get('feedback') || params.get('action');
        if (eventId && feedbackAction && (pageName === 'cameras' || pageName === 'events' || pageName === 'eva')) {
            this._pendingAutoFeedback = { eventId, cameraId, isReal: feedbackAction === 'real' };
        }
        if (eventId && pageName === 'eva') {
            this._pendingNotificationEventId = eventId;
            this._pendingNotificationCameraId = cameraId;
            this.go('eva');
            return;
        }
        if (eventId && pageName === 'events') {
            this.go('events');
            setTimeout(() => this._openEvent(eventId), 250);
            return;
        }
        if (cameraId && pageName === 'live') {
            this._openCameraLive(cameraId);
            return;
        }
        this.go('eva');
    },

    // E2: procesar el feedback del push una vez listo el userId (lo llama
    // el init después de auth). Registra la respuesta y avisa al usuario.
    async _processPendingFeedback() {
        const pf = this._pendingAutoFeedback;
        if (!pf || !this.userId) return;
        this._pendingAutoFeedback = null;
        try {
            const r = await apiFetch(`${this.API}/api/chat/eva/feedback`, {
                method: 'POST',
                body: JSON.stringify({
                    user_id: this.userId,
                    event_id: pf.eventId,
                    is_real: pf.isReal,
                    notes: pf.isReal ? '' : 'Marcado como falsa alarma desde la notificación push',
                })
            });
            const d = await r.json();
            if (d.success) {
                this._toast('', pf.isReal
                    ? '✅ Gracias — alerta confirmada como correcta'
                    : '🚫 Falsa alarma registrada. Eva aprenderá de esto.', 'success');
                if (typeof this._refreshEvents === 'function') this._refreshEvents();
            } else {
                this._toast('', 'No pude registrar tu respuesta: ' + (d.error || ''), 'danger');
            }
        } catch (e) {
            console.warn('[feedback] falló envío:', e);
            this._toast('', 'No pude registrar tu respuesta (sin conexión)', 'danger');
        }
    },

    _poll(key, fn, ms) {
        if (this._polls[key]) clearInterval(this._polls[key]);
        fn();
        this._polls[key] = setInterval(() => {
            if (document.hidden || this.page !== key.split('_')[0]) { clearInterval(this._polls[key]); delete this._polls[key]; return; }
            fn();
        }, ms);
    },

    async _initPush() {
        // RD-5 (2026-09-01): registro de push REPARADO.
        // Bugs anteriores: (1) getToken sin vapidKey → Firebase 10 lo rechaza
        // y el catch lo tragaba EN SILENCIO → nunca se registraba el token
        // (por eso no pedía permiso al entrar). (2) sw registrado en /sw.js
        // pero Firebase busca /firebase-messaging-sw.js. (3) catch mudo.
        try {
            if (!('Notification' in window)) return;
            const perm = await Notification.requestPermission();
            if (perm !== 'granted') {
                console.warn('[push] permiso denegado:', perm);
                return;
            }
            if (!('serviceWorker' in navigator) || !firebase.messaging) return;
            const reg = await navigator.serviceWorker.register('/firebase-messaging-sw.js');
            await navigator.serviceWorker.register('/sw.js'); // push directo + click
            const msg = firebase.messaging();
            const opts = { serviceWorkerRegistration: reg };
            // vapidKey: el backend la sirve (configurable sin redeploy);
            // sin ella Firebase 10.x falla silenciosamente en web.
            try {
                const cfg = await (await fetch(this.API + '/api/push-config')).json();
                if (cfg && cfg.vapid_key) opts.vapidKey = cfg.vapid_key;
            } catch (e) { /* sin config: getToken intentará sin vapid */ }
            const token = await msg.getToken(opts);
            if (!token) { console.warn('[push] getToken sin token'); return; }
            const r = await apiFetch(this.API + '/api/fcm/register', { method: 'POST', body: JSON.stringify({ user_id: this.userId, fcm_token: token }) });
            console.log('[push] token registrado:', (r && r.ok) ? 'OK' : r);
            msg.onMessage(p => this._toast(p.notification?.title || 'OjoIA', p.notification?.body || '', 'danger'));
        } catch (e) {
            // RD-5: NUNCA mudo — si el push falla, tiene que verse.
            console.warn('[push] init falló:', e && e.message);
        }
    },

    _toast(title, msg, type = 'info') {
        const t = document.createElement('div');
        const colors = { danger: '#ff453a', warning: '#ffd60a', success: '#30d158', info: '#0a84ff' };
        t.style.cssText = `position:fixed;top:60px;left:16px;right:16px;background:#1c1c1e;border-left:3px solid ${colors[type]};border-radius:12px;padding:14px 16px;z-index:9999;box-shadow:0 8px 32px rgba(0,0,0,0.5);animation:slideDown .3s ease`;
        t.innerHTML = `<div style="font-weight:600;font-size:.9rem;margin-bottom:3px">${title}</div><div style="font-size:.82rem;color:#aeaeb2">${msg}</div>`;
        document.body.appendChild(t);
        setTimeout(() => { t.style.animation = 'fadeOut .3s ease forwards'; setTimeout(() => t.remove(), 300); }, 6000);
    },

    // ── HOME ─────────────────────────────────────────────────
    async _pageHome(c) {
        this._resetScrollContent(c);
        c.innerHTML = this._skeleton();
        try {
            const [camsR, evtsR, profileR] = await Promise.all([
                apiFetch(`${this.API}/api/cameras?user_id=${this.userId || 'default'}`),
                apiFetch(`${this.API}/api/user/events?user_id=${this.userId || 'default'}&limit=1`),
                apiFetch(`${this.API}/api/user/profile?user_id=${this.userId || 'default'}`)
            ]);
            const cams = (await camsR.json()).cameras || [];
            const evts = (await evtsR.json()).events || [];
            const profile = await profileR.json();
            if (!this._isCurrentPage('home') && !this._isCurrentPage('cameras')) return;

            this._homeCams = cams;
            this._homeViewCount = Number(localStorage.getItem('ojoia_home_view_count') || 1);
            this._homeViewCount = [1, 2, 4, 8, 16].includes(this._homeViewCount) ? this._homeViewCount : 1;

            const on = cams.filter(x => x.active).length;
            const lastEvt = evts[0];
            const heroText = on > 0 
                ? `✅ ${on} de ${cams.length} cámaras activas` 
                : cams.length > 0 
                    ? `⚠️ ${cams.length} cámaras sin conexión` 
                    : '📹 Sin cámaras';
            const heroClass = on > 0 ? 'ok' : 'off';

            let lastAlertHTML = '';
            if (lastEvt) {
                const isViolation = lastEvt.qwen?.violation || lastEvt.event_type === 'violation';
                const isAttention = (lastEvt.attention_hits && lastEvt.attention_hits.length > 0) || lastEvt.event_type === 'attention';
                const isSentinel = lastEvt.event_type === 'sentinel' || (lastEvt.qwen_json?.after_hours && lastEvt.qwen_json?.importancia === 'alta');
                if (isViolation || isAttention || isSentinel) {
                    const ts = lastEvt.timestamp ? new Date(lastEvt.timestamp * 1000).toLocaleTimeString('es-ES', {hour:'2-digit', minute:'2-digit', hour12:true}) : '--';
                    const evtDesc = lastEvt.description || lastEvt.summary || (lastEvt.qwen_json?.summary || '');
                    const evtHits = lastEvt.attention_hits || [];
                    const alertColor = isSentinel ? 'var(--warning, #f5a623)' : 'var(--danger)';
                    const alertIcon = isSentinel ? '🛡️' : '📷';
                    const alertTitle = isSentinel ? 'FUERA DE HORARIO — Se detectó presencia' : (isAttention ? '🔍 Observación relevante' : '🚨 Alerta');
                    lastAlertHTML = `<div class="last-alert" onclick="App._openEvent('${this._escAttr(lastEvt.event_id)}')" style="background:${isSentinel ? 'rgba(245,166,35,0.08)' : 'rgba(255,59,48,0.06)'};border:1px solid ${alertColor};padding:14px 16px;border-radius:12px;margin-bottom:12px;cursor:pointer">
                        <div style="font-size:.78rem;color:${alertColor};font-weight:700;margin-bottom:6px">${alertIcon} ${alertTitle} — ${ts}</div>
                        <div style="font-size:.92rem;line-height:1.4;margin-bottom:6px">${evtDesc.substring(0, 180) || 'Se detectó actividad en la zona'}</div>
                        ${evtHits.length ? `<div style="font-size:.78rem;color:var(--text-secondary);margin-bottom:6px">🔍 ${evtHits.slice(0, 2).join(', ')}</div>` : ''}
                        <div style="display:flex;gap:8px;margin-top:8px">
                            <button class="btn btn-sm" onclick="event.stopPropagation();App._openEvent('${this._escAttr(lastEvt.event_id)}')" style="background:var(--accent);color:#fff;border:none;padding:6px 14px;border-radius:8px;font-size:.82rem">Ver detalle</button>
                            <button class="btn btn-sm" onclick="event.stopPropagation();App._dismissEvent('${this._escAttr(lastEvt.event_id)}');App.go('home')" style="background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);padding:6px 14px;border-radius:8px;font-size:.82rem">✓ Falsa alarma</button>
                        </div>
                    </div>`;
                }
            }

            const defaultCam = cams.find(c => c.active) || cams[0] || null;
            const defaultCamId = defaultCam ? defaultCam.camera_id : '';
            this._homeActiveCamId = defaultCamId;

            let isVigilante = false;
            if (defaultCam && defaultCam.schedule) {
                const now = new Date();
                const curMin = now.getHours() * 60 + now.getMinutes();
                const openParts = (defaultCam.schedule.open || '07:00').split(':');
                const closeParts = (defaultCam.schedule.close || '19:00').split(':');
                const openMin = parseInt(openParts[0]) * 60 + parseInt(openParts[1]);
                const closeMin = parseInt(closeParts[0]) * 60 + parseInt(closeParts[1]);
                const graceMin = (defaultCam.vigilance && defaultCam.vigilance.grace_minutes) || 15;
                const vigilanteStart = closeMin + graceMin;
                isVigilante = (curMin < openMin || curMin >= vigilanteStart);
            }
            const vigilanteBadge = isVigilante
                ? '<span class="badge" style="background:var(--danger);color:#fff;margin-left:8px;font-size:.7rem;padding:3px 8px;border-radius:12px;">🛡️ CENTINELA</span>'
                : '<span class="badge" style="background:var(--success);color:#fff;margin-left:8px;font-size:.7rem;padding:3px 8px;border-radius:12px;">● NORMAL</span>';

            const viewCams = this._getHomeViewCams(cams);
            const cols = this._homeGridColumns(this._homeViewCount);
            const frameInterval = this._homeFrameInterval();
            const viewButtons = [1, 2, 4, 8, 16].map(n => {
                const selected = this._homeViewCount === n;
                return `<button style="min-width:34px;padding:6px 10px;border-radius:20px;border:1px solid var(--border);background:${selected ? 'var(--accent)' : 'var(--bg-tertiary)'};color:${selected ? '#fff' : 'var(--text-secondary)'};font-size:.78rem;cursor:pointer" onclick="App._setHomeViewCount(${n})">${n}</button>`;
            }).join('');
            const gridHtml = viewCams.length > 0 ? viewCams.map((cam, i) => {
                const status = cam.active ? 'En vivo' : 'Offline';
                const statusColor = cam.active ? 'var(--success)' : 'var(--danger)';
                const shortId = cam.camera_id.substring(0, 8);
                return `
                    <div class="home-cam-tile" data-home-cam-id="${cam.camera_id}">
                        <div class="home-cam-header">
                            <div>
                                <div style="font-weight:600">${cam.name || `ojo-${shortId}`}</div>
                                <div class="meta">${cam.zone || 'sin zona'} · ${shortId}</div>
                            </div>
                            <span class="home-cam-status" data-home-status="${cam.camera_id}" style="color:${statusColor}">${status}</span>
                        </div>
                        <div id="home-frame-${i}" class="home-frame-wrap"><div class="ojo-placeholder">Esperando imagen...</div></div>
                        <button class="btn btn-sm btn-outline" style="width:100%;margin-top:8px" onclick="event.stopPropagation();App._saveRecentClip('${this._escAttr(cam.camera_id)}')">Guardar 45 min</button>
                    </div>`;
            }).join('') : '<div style="grid-column:1/-1"><div class="empty-state"><div class="empty-icon">📷</div><div class="empty-title">Sin cámaras</div><p>Agrega una cámara desde Eva para verla aquí.</p></div></div>';

            c.innerHTML = `
                <div class="home-hero">
                    <div class="hero-status ${heroClass}">${heroText}</div>
                    <div style="margin-top:8px;">${vigilanteBadge}</div>
                </div>
                ${lastAlertHTML}

                <div class="card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:12px">
                        <div>
                            <div style="font-weight:600">📷 Cámaras en vivo</div>
                            <div class="meta">${viewCams.length} mostrando de ${cams.length} · refresco ${frameInterval / 1000}s</div>
                        </div>
                        <div class="mini-button-row">${viewButtons}</div>
                    </div>
                    <div id="home-camera-grid" style="display:grid;grid-template-columns:repeat(${cols},1fr);gap:12px">
                        ${gridHtml}
                    </div>
                </div>

                <div class="card">
                    <div class="card-title">📊 Hoy</div>
                    <div class="stats-row">
                        <div class="stat"><div class="stat-val" id="stat-events">—</div><div class="stat-lbl">Eventos</div></div>
                        <div class="stat"><div class="stat-val danger" id="stat-alerts">—</div><div class="stat-lbl">Alertas</div></div>
                        <div class="stat"><div class="stat-val ok" id="stat-cams">${on}</div><div class="stat-lbl">Activas</div></div>
                    </div>
                </div>`;

            this._fetchStats();
            if (defaultCam) {
                this._loadCamVigilance(defaultCam);
                this._fetchFullCamConfig(defaultCamId);
            }
            setTimeout(() => {
                this._poll('home_frames', () => this._fetchHomeFrames(), frameInterval);
                this._poll('home_stats', () => this._fetchStats(), 30000);
                this._poll('home_cams', () => this._refreshCamStatus(), 15000);
            }, 500);
        } catch(e) {
            if (!this._isCurrentPage('home') && !this._isCurrentPage('cameras')) return;
            c.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><div class="empty-title">Sin conexión</div><p>Verifica que el servidor esté activo</p><button class="btn btn-sm" onclick="App.go('home')" style="margin-top:12px">Reintentar</button></div>`;
        }
    },

    async _fetchFullCamConfig(camId) {
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}?user_id=${this.userId}`);
            const d = await r.json();
            const cam = d.camera || d || {};
            if (this._homeActiveCamId === camId) {
                this._loadCamVigilance(cam);
            }
            const idx = this._homeCams.findIndex(c => c.camera_id === camId);
            if (idx >= 0) this._homeCams[idx] = { ...this._homeCams[idx], ...cam };
        } catch(e) { console.warn('[App] _updateHomeCam silent fail:', e); }
    },

    _getHomeViewCams(cams = this._homeCams) {
        return [...(cams || [])]
            .sort((a, b) => Number(b.active || false) - Number(a.active || false))
            .slice(0, this._homeViewCount);
    },

    _homeGridColumns(count) {
        if (count >= 16) return 4;
        if (count >= 4) return 2;
        return 1;
    },

    _homeFrameInterval() {
        if (this._homeViewCount >= 8) return 1000;
        if (this._homeViewCount >= 4) return 750;
        if (this._homeViewCount === 2) return 600;
        return 500;
    },

    _openCameraLive(camId) {
        this._homeActiveCamId = camId;
        this._homeViewCount = 1;
        this.go('home');
    },

    _setHomeViewCount(count) {
        this._homeViewCount = Number(count);
        localStorage.setItem('ojoia_home_view_count', String(this._homeViewCount));
        this._pageHome(document.getElementById('app-content'));
    },

    async _refreshCamStatus() {
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const data = await r.json();
            const cams = data.cameras || [];
            this._homeCams = cams;
            const on = cams.filter(x => x.active).length;
            // Update hero status
            const heroEl = document.querySelector('.home-hero .hero-status');
            if (heroEl) {
                heroEl.textContent = on > 0 ? `✅ ${on} de ${cams.length} cámaras activas` : cams.length > 0 ? `⚠️ ${cams.length} cámaras sin conexión` : '📹 Sin cámaras';
                heroEl.className = `hero-status ${on > 0 ? 'ok' : 'off'}`;
            }
            // Update camera tiles
            cams.forEach(cam => {
                const statusEl = document.querySelector(`[data-home-status="${cam.camera_id}"]`);
                if (statusEl) {
                    statusEl.textContent = cam.active ? 'En vivo' : 'Offline';
                    statusEl.style.color = cam.active ? 'var(--success)' : 'var(--danger)';
                }
            });
            // Update stats active count
            const statCams = document.getElementById('stat-cams');
            if (statCams) statCams.textContent = on;
            // Update badge
            const badge = document.querySelector('.card .badge');
            if (badge && badge.textContent.includes('/')) badge.textContent = `${on}/${cams.length}`;
            // Update live view status if active camera is among these
            if (this._homeActiveCamId) {
                const activeCam = cams.find(c => c.camera_id === this._homeActiveCamId);
                const statusEl = document.getElementById('home-live-status');
                if (statusEl && activeCam) {
                    statusEl.textContent = activeCam.active ? '● En vivo' : '○ Offline';
                    statusEl.className = `badge ${activeCam.active ? 'badge-ok' : 'badge-alert'}`;
                }
            }
        } catch(e) { console.warn('[App] home status silent fail:', e); }
    },

    _homeCams: [],
    _homeActiveCamId: null,
    _homeViewCount: Number(localStorage.getItem('ojoia_home_view_count') || 1),
    _homeFrameInFlight: {},
    _homeLastDetectionsByCam: {},
    _homeWatermarkTextByCam: {},
    _homeLastYoloFetchByCam: {},
    _homeStreamStarted: {},  // {camId: true} — MJPEG stream ya iniciado
    _homeLastFrameTs: {},     // {camId: ts} — último onload del stream (watchdog)
    _homeWatchdogInterval: null,
    _gridSettingsCamId: null,

    _fetchHomeFrames() {
        const cams = this._getHomeViewCams();
        cams.forEach((cam, i) => {
            const camId = cam.camera_id;
            const targetId = `home-frame-${i}`;
            const key = `${targetId}:${camId}`;
            // Watchdog: si el último frame llegó hace >STREAM_STALE_MS, reiniciar
            const lastTs = this._homeLastFrameTs[key] || 0;
            const streamStale = lastTs > 0 && (Date.now() - lastTs) > 15000;
            // Iniciar MJPEG stream solo si no está activo o si está estancado
            if (!this._homeStreamStarted[key] || streamStale) {
                this._homeStreamStarted[key] = true;
                this._homeLastFrameTs[key] = Date.now();
                this._fetchFrameForCam(camId, targetId);
            } else {
                // Stream ya activo, solo actualizar YOLO metadata
                this._refreshYoloOnly(camId, targetId);
            }
        });
    },

    async _refreshYoloOnly(camId, targetId) {
        const lastYoloFetch = this._homeLastYoloFetchByCam[camId] || 0;
        if (Date.now() - lastYoloFetch >= this._homeYoloPollMs) {
            this._homeLastYoloFetchByCam[camId] = Date.now();
            const el = document.getElementById(targetId);
            if (!el) return;
            const camIdShort = camId.substring(0, 8);
            const zone = (this._homeCams && this._homeCams.find) ? (this._homeCams.find(c=>c.camera_id===camId)?.zone || '—') : '—';
            const nowText = new Date().toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
            this._fetchYoloMetadata(camId, el, nowText, camIdShort, zone, targetId);
        }
    },

    async _switchHomeCamera(camId, el) {
        if (!camId) return;
        
        // Update selected visual
        document.querySelectorAll('.cam-card-selector').forEach(c => c.classList.remove('selected'));
        if (el) el.classList.add('selected');

        // Update active cam
        this._homeActiveCamId = camId;

        // Find cam data from cache
        let cam = this._homeCams.find(c => c.camera_id === camId);
        
        // Update live view header
        const nameEl = document.getElementById('home-live-cam-name');
        const statusEl = document.getElementById('home-live-status');
        if (nameEl) nameEl.textContent = cam ? (cam.name || cam.camera_id) : camId;
        if (statusEl) {
            const active = cam?.active;
            statusEl.textContent = active ? '● En vivo' : '○ Offline';
            statusEl.className = `badge ${active ? 'badge-ok' : 'badge-alert'}`;
        }

        // Load vigilance from cached data first
        if (cam) {
            this._loadCamVigilance(cam);
        }

        // Fetch full config in background (non-blocking)
        this._fetchFullCamConfig(camId);

        // Refresh live frame for this camera
        this._fetchFrameForCam(camId);
    },

    _translateYoloClass(cls) {
        const map = {
            'person': 'persona',
            'bicycle': 'bicicleta',
            'car': 'carro',
            'motorcycle': 'moto',
            'airplane': 'avión',
            'bus': 'guagua',
            'train': 'tren',
            'truck': 'camión',
            'boat': 'barco',
            'traffic light': 'semáforo',
            'fire hydrant': 'hidrante',
            'stop sign': 'señal stop',
            'parking meter': 'parquímetro',
            'bench': 'banca',
            'bird': 'pájaro',
            'cat': 'gato',
            'dog': 'perro',
            'horse': 'caballo',
            'sheep': 'oveja',
            'cow': 'vaca',
            'elephant': 'elefante',
            'bear': 'oso',
            'zebra': 'cebra',
            'giraffe': 'jirafa',
            'backpack': 'mochila',
            'umbrella': 'sombrilla',
            'handbag': 'bolso',
            'tie': 'corbata',
            'suitcase': 'maleta',
            'frisbee': 'frisbee',
            'skis': 'esquís',
            'snowboard': 'tabla nieve',
            'sports ball': 'pelota',
            'kite': 'chichigua',
            'baseball bat': 'bate',
            'baseball glove': 'guante',
            'skateboard': 'skate',
            'surfboard': 'tabla surf',
            'tennis racket': 'raqueta',
            'bottle': 'botella',
            'wine glass': 'copa',
            'cup': 'vaso',
            'fork': 'tenedor',
            'knife': 'cuchillo',
            'spoon': 'cuchara',
            'bowl': 'plato',
            'banana': 'guineo',
            'apple': 'manzana',
            'sandwich': 'sándwich',
            'orange': 'chinola',
            'broccoli': 'brócoli',
            'carrot': 'zanahoria',
            'hot dog': 'hot dog',
            'pizza': 'pizza',
            'donut': 'donut',
            'cake': 'pastel',
            'chair': 'silla',
            'couch': 'sofá',
            'potted plant': 'planta',
            'bed': 'cama',
            'dining table': 'mesa',
            'toilet': 'inodoro',
            'tv': 'TV',
            'laptop': 'laptop',
            'mouse': 'mouse',
            'remote': 'control',
            'keyboard': 'teclado',
            'cell phone': 'celular',
            'microwave': 'microondas',
            'oven': 'horno',
            'toaster': 'tostadora',
            'sink': 'fregadero',
            'refrigerator': 'refrigerador',
            'book': 'libro',
            'clock': 'reloj',
            'vase': 'florero',
            'scissors': 'tijeras',
            'teddy bear': 'peluche',
            'hair drier': 'secadora',
            'toothbrush': 'cepillo'
        };
        return map[cls] || cls || 'objeto';
    },

    _ensureLiveFrameDom(camId, rawUrl, watermark, onImgLoad, onImgError, targetId = 'live-wrap') {
        const el = document.getElementById(targetId);
        if (!el) return null;
        let imgEl = el.querySelector('img.live-img');
        let canvasEl = el.querySelector('canvas.yolo-canvas');
        if (!imgEl) {
            el.innerHTML = `
                <div style="position:relative;width:100%;max-width:720px;margin:0 auto;background:#1a1a1a;border-radius:8px;overflow:hidden">
                    <img src="${rawUrl}" class="live-img" decoding="async" style="width:100%;height:auto;aspect-ratio:1/1;object-fit:contain;display:block" onerror="this.parentElement.innerHTML='<div class=\\'ojo-placeholder\\'>📡 Sin señal</div>'">
                    <canvas class="yolo-canvas" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></canvas>
                </div>
                <div class="ai-row">
                    <div class="ai-card"><div class="ai-label">Detección</div><div class="ai-val">—</div></div>
                    <div class="ai-card"><div class="ai-label">Hora</div><div class="ai-val" style="font-size:.8rem">—</div>
                </div>`;
            imgEl = el.querySelector('img.live-img');
            canvasEl = el.querySelector('canvas.yolo-canvas');
            imgEl.onload = onImgLoad;
            imgEl.onerror = onImgError;
        } else {
            imgEl.onload = onImgLoad;
            imgEl.onerror = onImgError;
            imgEl.src = rawUrl;
        }
        return { el, imgEl, canvasEl };
    },

    _drawYoloBoxes(camId, detections, watermarkText = this._homeWatermarkText, targetId = 'live-wrap') {
        const el = document.getElementById(targetId);
        if (!el) return;
        const imgEl = el.querySelector('img.live-img');
        const canvasEl = el.querySelector('canvas.yolo-canvas');
        if (!imgEl || !canvasEl || !imgEl.naturalWidth || !imgEl.naturalHeight) return;
        const ctx = canvasEl.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const cw = imgEl.clientWidth || imgEl.naturalWidth;
        const ch = imgEl.clientHeight || imgEl.naturalHeight;
        canvasEl.width = Math.max(1, Math.floor(cw * dpr));
        canvasEl.height = Math.max(1, Math.floor(ch * dpr));
        canvasEl.style.width = `${cw}px`;
        canvasEl.style.height = `${ch}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, cw, ch);

        const scale = Math.min(cw / imgEl.naturalWidth, ch / imgEl.naturalHeight);
        const drawW = imgEl.naturalWidth * scale;
        const drawH = imgEl.naturalHeight * scale;
        const offsetX = (cw - drawW) / 2;
        const offsetY = (ch - drawH) / 2;
        const sx = drawW / imgEl.naturalWidth;
        const sy = drawH / imgEl.naturalHeight;

        if (watermarkText) {
            const fontSize = Math.max(11, Math.min(drawW, drawH) * 0.035);
            const padX = 7;
            const padY = 4;
            const th = fontSize + padY * 2;
            ctx.font = `${fontSize}px monospace`;
            const tw = Math.min(ctx.measureText(watermarkText).width + padX * 2, drawW - 12);
            const x = offsetX + 6;
            const y = offsetY + drawH - th - 6;
            ctx.fillStyle = 'rgba(0,0,0,0.68)';
            ctx.fillRect(x, y, tw, th);
            ctx.fillStyle = '#fff';
            ctx.fillText(watermarkText, x + padX, y + padY + fontSize);
        }

        if (!Array.isArray(detections) || detections.length === 0) return;
        detections.slice(0, 12).forEach(d => {
            const bbox = d.bbox || [];
            if (!bbox || bbox.length < 4) return;
            const [x1, y1, x2, y2] = bbox;
            const conf = Number(d.confidence || 0);
            const color = conf >= 0.55 ? '#30d158' : conf >= 0.35 ? '#ffd60a' : '#ff9f0a';
            ctx.strokeStyle = color;
            ctx.lineWidth = Math.max(2, Math.min(drawW, drawH) * 0.006);
            ctx.strokeRect(offsetX + x1 * sx, offsetY + y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
            const label = `${this._translateYoloClass(d.class || 'obj')} ${(conf * 100).toFixed(0)}%`;
            ctx.font = `${Math.max(11, Math.min(drawW, drawH) * 0.025)}px monospace`;
            const tw = ctx.measureText(label).width + 8;
            const th = 18;
            const lx = Math.max(offsetX, offsetX + x1 * sx);
            const ly = Math.max(offsetY, offsetY + y1 * sy - th - 2);
            ctx.fillStyle = 'rgba(0,0,0,0.65)';
            ctx.fillRect(lx, ly, tw, th);
            ctx.fillStyle = '#fff';
            ctx.fillText(label, lx + 4, ly + 13);
            // ── YOLO Pose keypoints: dibujar la silueta de la persona ──
            if (d.class && d.class.toLowerCase() === 'person' && d.keypoints) {
                const kps = Array.isArray(d.keypoints) ? d.keypoints : [];
                const pose = d.pose || {};
                const visibleKps = kps.filter(kp => {
                    if (!kp || kp.length < 2) return false;
                    const [kx, ky] = kp;
                    return kx > 0 && ky > 0;
                });
                if (visibleKps.length > 0) {
                    const kpColor = (pose.visible || 0) >= 6 ? '#00e5ff' : '#ff6d00';
                    const kpRadius = Math.max(2, Math.min(drawW, drawH) * 0.005);
                    const lineWidth = Math.max(1.5, Math.min(drawW, drawH) * 0.004);
                    // Dibujar conexiones del esqueleto (COCO 17 keypoints)
                    const skeleton = [
                        [0,1],[0,2],[1,3],[2,4],
                        [5,6],[5,7],[7,9],[6,8],[8,10],
                        [5,11],[6,12],[11,12],
                        [11,13],[13,15],[12,14],[14,16]
                    ];
                    ctx.strokeStyle = kpColor;
                    ctx.lineWidth = lineWidth;
                    skeleton.forEach(([a, b]) => {
                        if (a < visibleKps.length && b < visibleKps.length) {
                            const [ax, ay] = visibleKps[a];
                            const [bx, by] = visibleKps[b];
                            if (ax > 0 && ay > 0 && bx > 0 && by > 0) {
                                ctx.beginPath();
                                ctx.moveTo(offsetX + ax * sx, offsetY + ay * sy);
                                ctx.lineTo(offsetX + bx * sx, offsetY + by * sy);
                                ctx.stroke();
                            }
                        }
                    });
                    // Dibujar puntos (keypoints)
                    visibleKps.forEach(kp => {
                        const [kx, ky] = kp;
                        const px = offsetX + kx * sx;
                        const py = offsetY + ky * sy;
                        ctx.fillStyle = kpColor;
                        ctx.beginPath();
                        ctx.arc(px, py, kpRadius, 0, Math.PI * 2);
                        ctx.fill();
                        ctx.strokeStyle = '#000';
                        ctx.lineWidth = 1;
                        ctx.stroke();
                    });
                }
            }
        });
    },

    _fetchYoloMetadata(camId, el, nowText, camIdShort, zone, targetId = 'live-wrap') {
        apiFetch(`${this.API}/frames/latest?camera_id=${camId}&user_id=${this.userId || 'default'}`)
            .then(r => r.json())
            .then(d => {
                const yolo = d.yolo || {};
                const detections = Array.isArray(yolo.detections) ? yolo.detections : [];
                this._homeLastDetectionsByCam[camId] = detections;
                const yoloText = yolo.count != null ? `${yolo.count} 👁` : '—';
                let ts_str = '';
                if (yolo.timestamp) {
                    const dt = new Date(yolo.timestamp * 1000);
                    ts_str = dt.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
                }
                const aiAge = yolo.timestamp ? Math.max(0, Math.round(Date.now()/1000 - yolo.timestamp)) : null;
                const yoloLabel = aiAge != null ? `${yoloText} · AI ${aiAge}s` : yoloText;
                const yoloEl = el.querySelector('.ai-card:first-child .ai-val');
                const horaEl = el.querySelector('.ai-card:last-child .ai-val');
                if (yoloEl) yoloEl.textContent = yoloLabel;
                if (horaEl) horaEl.textContent = ts_str || nowText;
                const dateText = new Date().toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit',year:'2-digit'});
                const watermark = `OJO-${camIdShort} | ${dateText} ${ts_str || nowText} | ${zone}${aiAge != null ? ` | AI ${aiAge}s` : ''}`;
                this._homeWatermarkTextByCam[camId] = watermark;
                this._drawYoloBoxes(camId, detections, watermark, targetId);
            })
            .catch(() => {});
    },

    async _fetchFrameForCam(camId, targetId = 'live-wrap') {
        const key = `${targetId}:${camId}`;
        if (this._homeFrameInFlight[key]) return;
        this._homeFrameInFlight[key] = true;
        const clearInFlight = () => { this._homeFrameInFlight[key] = false; };
        try {
            const el = document.getElementById(targetId);
            if (!el) { clearInFlight(); return; }
            const uid = this.userId || 'default';
            const camIdShort = camId.substring(0, 8);
            const zone = (this._homeCams && this._homeCams.find) ? (this._homeCams.find(c=>c.camera_id===camId)?.zone || '—') : '—';
            const nowText = new Date().toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
            const dateText = new Date().toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit',year:'2-digit'});
            const watermark = `OJO-${camIdShort} | ${dateText} ${nowText} | ${zone}`;
            this._homeWatermarkTextByCam[camId] = watermark;

            // MJPEG stream en lugar de polling JPEG
            const streamUrl = `${this.API}/cameras/${camId}/stream?user_id=${uid}&fps=5`;
            const onImgLoad = () => {
                this._homeLastFrameTs[key] = Date.now();
                clearInFlight();
                this._drawYoloBoxes(camId, this._homeLastDetectionsByCam[camId] || [], this._homeWatermarkTextByCam[camId] || watermark, targetId);
            };
            const onImgError = () => {
                // Si el stream se rompe, forzar reinicio en el siguiente tick
                this._homeStreamStarted[key] = false;
                clearInFlight();
            };
            const dom = this._ensureLiveFrameDom(camId, streamUrl, watermark, onImgLoad, onImgError, targetId);
            if (!dom) { clearInFlight(); return; }
            const { imgEl } = dom;
            imgEl.decoding = 'async';
            imgEl.loading = 'eager';

            // YOLO metadata polling (cada 2s, independiente del stream)
            const lastYoloFetch = this._homeLastYoloFetchByCam[camId] || 0;
            const shouldFetchYolo = Date.now() - lastYoloFetch >= this._homeYoloPollMs;
            if (shouldFetchYolo) {
                this._homeLastYoloFetchByCam[camId] = Date.now();
                this._fetchYoloMetadata(camId, el, nowText, camIdShort, zone, targetId);
            }
        } catch(e) {
            clearInFlight();
        }
    },

    _loadCamVigilance(cam) {
        return;
    },

    async _fetchFrame(targetId) {
        const camId = this._homeActiveCamId || '';
        if (camId) {
            await this._fetchFrameForCam(camId);
            return;
        }
        // Fallback: no camera selected
        const el = document.getElementById(targetId);
        if (el) el.innerHTML = '<div class="ojo-placeholder">Selecciona una cámara</div>';
    },

    async _fetchGrid(targetId) {
        try {
            const uid2 = this.userId || 'default';
            const r = await apiFetch(`${this.API}/grid/latest?partial=1&camera_id=${this._homeActiveCamId || ''}&user_id=${uid2}`);
            const d = await r.json();
            const el = document.getElementById(targetId);
            const badgeEl = document.getElementById('grid-badge');
            const progressEl = document.getElementById('grid-progress');
            if (!el) return;
            if (d.grid_b64) {
                const frames = d.frames_used || 0;
                const size = d.grid_size || 16;
                const pct = Math.min(100, Math.round((frames / size) * 100));
                if (badgeEl) { badgeEl.textContent = `${frames}/${size}`; badgeEl.className = `badge ${frames >= size ? 'badge-alert' : 'badge-ok'}`; }
                if (progressEl) { progressEl.style.width = `${pct}%`; progressEl.style.transition = 'width 0.5s ease'; }
                // Grid con transición suave
                let gridImg = el.querySelector('img.grid-img');
                if (gridImg) {
                    gridImg.style.opacity = '0.5';
                    gridImg.src = 'data:image/jpeg;base64,' + d.grid_b64;
                    setTimeout(function() { gridImg.style.opacity = '1'; }, 200);
                } else {
                    el.innerHTML = `
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                            <span class="meta">${frames}/${size} frames${d.partial ? ' (parcial)' : ''}</span>
                            <span class="badge ${frames >= size ? 'badge-alert' : 'badge-ok'}">${frames >= size ? '✓ ÁREA LISTA' : `${pct}%`}</span>
                        </div>
                        <div style="background:#1a1a1a;border-radius:8px;overflow:hidden">
                            <img class="grid-img" src="data:image/jpeg;base64,${d.grid_b64}" style="width:100%;max-width:720px;aspect-ratio:1/1;object-fit:contain;display:block;transition:opacity 0.3s ease">
                        </div>`;
                }
            } else {
                if (badgeEl) { badgeEl.textContent = `0/${d.grid_size || 16}`; badgeEl.className = 'badge badge-ok'; }
                if (progressEl) { progressEl.style.width = '0%'; progressEl.style.transition = 'width 0.5s ease'; }
                el.innerHTML = '<p class="meta" style="padding:8px 0">Detección encuentra objetos → Eva revisa el área</p>';
            }
        } catch(e) { console.warn('[App] overview meta silent fail:', e); }
    },

    async _fetchStats() {
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${uid}`);
            const d = await r.json();
            const cams = d.cameras || [];
            const active = cams.filter(c => c.active).length;
            const se = document.getElementById('stat-events');
            const sa = document.getElementById('stat-alerts');
            const sc = document.getElementById('stat-cams');
            // Total events and alerts from metrics
            let totalEvents = 0;
            let totalAlerts = 0;
            for (const cam of cams) {
                const m = cam.metrics || {};
                totalEvents += m.today_events ?? m.total_events ?? 0;
                totalAlerts += m.today_alerts ?? m.total_alerts ?? 0;
            }
            if (se) se.textContent = totalEvents;
            if (sa) sa.textContent = totalAlerts;
            if (sc) sc.textContent = active;
        } catch(e) { console.warn('[App] counters silent fail:', e); }
    },

    // ── CAMERAS ──────────────────────────────────────────────
    async _pageCameras(c) {
        this._resetScrollContent(c);
        c.innerHTML = this._skeleton();
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const cams = (await r.json()).cameras || [];
            if (!this._isCurrentPage('cameras')) return;

            if (cams.length === 0) {
                c.innerHTML = `<div class="empty-state">
                    <div class="empty-icon">📷</div>
                    <div class="empty-title">Sin ojos</div>
                    <p>Instala tu primera cámara con Eva</p>
                    <button class="btn" onclick="App.newCamera()" style="margin-top:16px">+ Instalar cámara con Eva</button>
                </div>`;
            } else {
                let html = '';
                cams.forEach(cam => {
                    const lastSeen = cam.last_seen ? this._relTime(cam.last_seen) : 'Sin datos';
                    const events = cam.metrics?.total_events || 0;
                    const alerts = cam.metrics?.total_alerts || 0;
                    const fp = cam.metrics?.total_false_positives || 0;
                    const rulesCount = cam.metrics?.rules_count || (cam.rules?.length || 0);
                    html += `<div class="ojo-card">
                        <div class="ojo-card-header" style="cursor:default">
                            <div class="ojo-thumb" id="thumb-${cam.camera_id}"><span style="font-size:1.5rem">📷</span></div>
                            <div style="flex:1">
                                <div style="font-weight:600">${cam.name}</div>
                                <div class="meta">${cam.zone || ''} · ${cam.active ? '🟢 Online' : '⚫ Offline'} · ${lastSeen}</div>
                                <div class="meta" style="margin-top:2px;font-size:0.75rem;color:var(--text-secondary)">
                                    📊 ${events} eventos · ${alerts} alertas${fp ? ` · ⚠️ ${fp} falsas alarmas` : ''}
                                </div>
                                <div class="meta" style="font-size:0.75rem;color:var(--text-secondary)">
                                    📏 ${rulesCount} comportamientos activos${cam.metrics?.needs_review ? ' · 🔧 necesita revisión' : ''}
                                </div>
                            </div>
                        </div>
                        <div class="ojo-card-actions" style="justify-content:flex-end">
                                    <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();App._openCameraTimeline('${this._escAttr(cam.camera_id)}')">Ver últimos 45 min</button>
                                    <button class="btn btn-sm" onclick="event.stopPropagation();App._saveRecentClip('${this._escAttr(cam.camera_id)}')">Guardar 45 min</button>
                                    <button class="btn btn-sm" onclick="event.stopPropagation();App._openVigilanceSettings('${this._escAttr(cam.camera_id)}')">🛡️ Ajustar protección</button>
                            <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();App._openCameraConfig('${this._escAttr(cam.camera_id)}')">⚙️ Ajustes de cámara</button>
                            <button class="btn-ghost btn-sm" style="color:var(--danger)" onclick="App.deleteCamera('${this._escAttr(cam.camera_id)}','${this._escAttr(cam.name)}')">🗑️</button>
                        </div>
                    </div>`;
                });
                html += `<button class="btn" style="margin-top:8px" onclick="App.newCamera()">+ Instalar otra cámara con Eva</button>`;
                c.innerHTML = html;
                cams.forEach(cam => cam.active && this._loadThumb(cam.camera_id));
            }
        } catch(e) {
            if (!this._isCurrentPage('cameras')) return;
            c.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><p>Error de conexión</p></div>`;
        }
    },

    // ── EVA CHAT ─────────────────────────────────────────────────
    async _pageEva(c) {
        c.innerHTML = '';
        c.style.cssText = 'display:flex;flex-direction:column;overflow:hidden;padding:0;flex:none;height:calc(100dvh - 108px);min-height:0;';
        if (this.userId && typeof EvaChat !== 'undefined') {
            EvaChat.init(this.userId, firebase.auth().currentUser?.displayName || firebase.auth().currentUser?.email || '');
            if (this._pendingNotificationEventId && typeof EvaChat.showAlertEvent === 'function') {
                setTimeout(() => EvaChat.showAlertEvent(this._pendingNotificationEventId), 150);
                this._pendingNotificationEventId = '';
                this._pendingNotificationCameraId = '';
            }
        } else {
            c.innerHTML = '<div style="display:flex;flex-direction:column;height:100%;background:#000">' +
                '<div style="flex:1;display:flex;align-items:center;justify-content:center"><div style="text-align:center;color:#fff"><div style="font-size:3rem;margin-bottom:16px">🤖</div><div>' + (this.userId ? 'Cargando Eva...' : 'Inicia sesión para hablar con Eva') + '</div></div></div>' +
                '</div>';
        }
},

    // ── EVA CHAT INPUT (delegado a EvaChat v5, único flujo) ──────
    async _minimalEvaSend() {
        const input = document.getElementById('eva-input');
        if (input && typeof EvaChat !== 'undefined') {
            const msg = input.value.trim();
            if (msg) EvaChat.sendMessage();
            return;
        }
    },

    async _loadThumb(camId) {
        try {
                const r = await apiFetch(`${this.API}/frames/latest?camera_id=${camId}&user_id=${this.userId}`);
            const d = await r.json();
            const el = document.getElementById(`thumb-${camId}`);
            if (el && d.success && d.image_b64) {
                el.innerHTML = `<img src="data:image/jpeg;base64,${d.image_b64}" style="width:100%;height:100%;object-fit:cover;border-radius:8px">`;
            }
        } catch(e) { console.warn('[App] timeline thumb silent fail:', e); }
    },

    async _openCameraTimeline(camId) {
        const c = document.getElementById('app-content');
        c.style.padding = '0';
        c.style.overflow = 'auto';
        c.innerHTML = `<div style="padding:16px;max-width:900px;margin:auto">
            <button class="btn btn-ghost" onclick="App.go('cameras')">← Volver a cámaras</button>
            <div class="card" style="margin-top:12px">
                <div style="font-size:1.25rem;font-weight:700">Historial de últimos 45 minutos</div>
                <div class="meta">Cargando imágenes recientes...</div>
            </div>
        </div>`;
        this._timelineFrames = this._timelineFrames || {};
        this._timelineIndex = this._timelineIndex || {};
        try {
            const framesReq = await apiFetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}/recent-frames?user_id=${encodeURIComponent(this.userId)}&limit=1000&minutes=45`);
            const framesData = await framesReq.json();
            const frames = framesData.frames || [];
            this._timelineFrames[camId] = frames;
            this._timelineIndex[camId] = Math.max(0, frames.length - 1);
            const camReq = await apiFetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}?user_id=${encodeURIComponent(this.userId)}`);
            const camData = await camReq.json();
            if (!this._isCurrentPage('cameras')) return;
            const cam = camData.camera || {};
            c.innerHTML = `
                <div style="padding:16px;max-width:900px;margin:auto">
                    <button class="btn btn-ghost" onclick="App.go('cameras')">← Volver a cámaras</button>
                    <div class="card" style="margin-top:12px">
                        <div style="display:flex;gap:12px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap">
                            <div>
                                <div style="font-size:1.35rem;font-weight:800">${cam.name || camId}</div>
                                <div class="meta">${cam.zone || 'Sin zona'} · ${frames.length} imágenes guardadas · 45 minutos</div>
                            </div>
                            <button class="btn" id="save-clip-btn" onclick="App._saveRecentClip('${this._escAttr(camId)}')">Guardar últimos 45 min</button>
                        </div>
                        <div style="margin-top:16px;text-align:center">
                            <img id="timeline-img" style="width:100%;max-height:420px;object-fit:contain;background:#000;border-radius:14px" alt="Imagen reciente">
                        </div>
                        <div id="timeline-status" class="meta" style="margin-top:10px;text-align:center">—</div>
                        <div style="display:flex;gap:8px;align-items:center;margin-top:14px">
                            <button class="btn btn-sm" onclick="App._moveTimeline('${this._escAttr(camId)}', -1)">◀ Atrás</button>
                            <input id="timeline-range" type="range" min="0" max="${Math.max(0, frames.length - 1)}" value="${Math.max(0, frames.length - 1)}" style="flex:1" oninput="App._showTimelineFrame('${this._escAttr(camId)}', this.value)">
                            <button class="btn btn-sm" onclick="App._moveTimeline('${this._escAttr(camId)}', 1)">Adelante ▶</button>
                        </div>
                        <div class="meta" style="margin-top:10px">
                            Usa Atrás/Adelante para revisar lo que pasó antes o después del momento actual.
                        </div>
                    </div>
                </div>`;
            await this._showTimelineFrame(camId, this._timelineIndex[camId]);
        } catch(e) {
            if (!this._isCurrentPage('cameras')) return;
            c.innerHTML = `<div style="padding:16px;max-width:900px;margin:auto"><button class="btn btn-ghost" onclick="App.go('cameras')">← Volver</button><div class="empty-state" style="margin-top:12px"><div class="empty-icon">📡</div><p>No hay imágenes recientes</p></div></div>`;
        }
    },

    async _showTimelineFrame(camId, rawIndex) {
        const index = Math.max(0, parseInt(rawIndex || '0', 10));
        let frames = this._timelineFrames?.[camId] || [];
        if (!frames.length) {
            const r = await apiFetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}/recent-frames?user_id=${encodeURIComponent(this.userId)}&limit=1000&minutes=45`);
            const d = await r.json();
            frames = d.frames || [];
            this._timelineFrames[camId] = frames;
        }
        if (!frames.length) return;
        const safeIndex = Math.min(index, frames.length - 1);
        this._timelineIndex[camId] = safeIndex;
        const frame = frames[safeIndex];
        const img = document.getElementById('timeline-img');
        const status = document.getElementById('timeline-status');
        const range = document.getElementById('timeline-range');
        if (img) img.src = `${this.API}/api/cameras/${encodeURIComponent(camId)}/recent-frame/${safeIndex}?user_id=${encodeURIComponent(this.userId)}&minutes=45&_=${Date.now()}`;
        if (range) range.value = safeIndex;
        if (status && frame) status.textContent = `${safeIndex + 1}/${frames.length} · ${frame.datetime || ''}`;
    },

    _moveTimeline(camId, delta) {
        const frames = this._timelineFrames?.[camId] || [];
        if (!frames.length) return;
        const current = this._timelineIndex?.[camId] || 0;
        const next = Math.max(0, Math.min(frames.length - 1, current + delta));
        this._showTimelineFrame(camId, next);
    },

    async _saveRecentClip(camId) {
        const btn = document.getElementById('save-clip-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Guardando...';
        }
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}/save-recent-clip?user_id=${encodeURIComponent(this.userId)}&minutes=45`, { method: 'POST' });
            const d = await r.json();
            if (!r.ok || !d.success) throw new Error(d.error || 'No se pudo guardar la evidencia');
            alert(`Evidencia guardada: ${d.clip_id}`);
        } catch(e) {
            alert(e.message || 'No se pudo guardar la evidencia');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Guardar últimos 45 min';
            }
        }
    },

    // ── CAMERA CONFIG (ajustes de cámara via ESP32 local API) ──
    async _openCameraConfig(camId) {
        const c = document.getElementById('app-content');
        this._configReturnPage = this.page === 'settings' ? 'settings' : 'cameras';
        c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:2rem;margin-bottom:16px;">⏳</div><div style="font-weight:600;">Cargando configuración...</div></div>';
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const d = await r.json();
            const cam = (d.cameras || []).find(x => x.camera_id === camId);
            if (!cam) throw new Error('Camera not found');
            cam.cooldown_min = cam.cooldown_min || 5;
            const fs = cam.active ? 'Online' : 'Offline';
            const returnPage = this._configReturnPage === 'settings' ? 'settings' : 'cameras';
            if (this.page !== 'settings' && this.page !== 'cameras' && this.page !== 'home') return;

            this._configRotation = 0;

            c.innerHTML = `
                <div class="camera-config-page">
                    <div class="config-hero card">
                        <div class="config-hero-main">
                            <div class="config-hero-icon">⚙️</div>
                            <div style="min-width:0">
                                <div class="config-hero-title">${cam.name}</div>
                                <div class="config-hero-sub">${cam.zone || 'Sin zona'} · ${cam.camera_id}</div>
                            </div>
                        </div>
                        <span class="status-pill ${cam.active ? 'online' : 'offline'}">${fs}</span>
                    </div>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">ESP32</div>
                                <div class="section-title">📷 Vista en vivo</div>
                            </div>
                            <span class="live-dot ${cam.active ? 'online' : 'offline'}"></span>
                        </div>
                        <div class="live-frame">
                            <img id="cfg-live-img" src="${this.API}/frames/latest-raw.jpg?camera_id=${camId}&user_id=${this.userId}&_=${Date.now()}" decoding="async" onerror="this.parentElement.innerHTML='<div class=ojo-placeholder>📡 Sin señal</div>'">
                            <div id="cfg-watermark" class="live-watermark">OJO-${camId.substring(0,8)}</div>
                        </div>
                        <div class="meta-line"><span>Refresco automático</span><span>1s</span></div>
                    </section>

                    <section class="config-section" id="zones-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Zonas</div>
                                <div class="section-title">🗺️ Zonas de interés (ROI)</div>
                            </div>
                            <button class="btn btn-sm btn-primary" onclick="App._openZoneDrawer('${this._escAttr(camId)}')">➕ Gestionar zonas</button>
                        </div>
                        <div id="zones-list-${camId}" class="zones-list">
                            <div class="zones-empty">Sin zonas. Haz clic en "Gestionar zonas" para añadir.</div>
                        </div>
                    </section>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Ajustes de imagen</div>
                                <div class="section-title">🔆 Brillo y contraste</div>
                            </div>
                        </div>
                        <div class="config-grid two">
                            <div class="control-card">
                                <div class="control-label-row"><span>🔆 Brillo</span><strong id="cfg_brightness_val" class="value-pill">0</strong></div>
                                <input class="range-control" id="cfg_brightness" type="range" min="-100" max="100" value="0" oninput="App._updateImageFilter('${this._escAttr(camId)}')">>
                                <div class="range-labels"><span>Oscuro</span><span>Brillante</span></div>
                            </div>
                            <div class="control-card">
                                <div class="control-label-row"><span>🎚️ Contraste</span><strong id="cfg_contrast_val" class="value-pill">0</strong></div>
                                <input class="range-control" id="cfg_contrast" type="range" min="-100" max="100" value="0" oninput="App._updateImageFilter('${this._escAttr(camId)}')">>
                                <div class="range-labels"><span>Bajo</span><span>Alto</span></div>
                            </div>
                        </div>
                        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
                        <button class="btn config-action" data-config-cmd="brightness" onclick="App._sendCamCmd('${this._escAttr(camId)}','brightness',document.getElementById('cfg_brightness').value,this)">💾 Aplicar</button>
                        <button class="btn config-action" onclick="App._resetBrightnessContrast('${this._escAttr(camId)}')">↩️ Restablecer valores</button>
                        </div>
                    </section>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Orientación</div>
                                <div class="section-title">↻ Rotación</div>
                            </div>
                        </div>
                        <div class="segmented four">
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="0" onclick="App._sendCamCmd('${this._escAttr(camId)}','rotation',0,this)">0°</button>
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="1" onclick="App._sendCamCmd('${this._escAttr(camId)}','rotation',1,this)">90°</button>
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="2" onclick="App._sendCamCmd('${this._escAttr(camId)}','rotation',2,this)">180°</button>
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="3" onclick="App._sendCamCmd('${this._escAttr(camId)}','rotation',3,this)">270°</button>
                        </div>
                        <p class="meta" style="margin:8px 0 0;text-align:center;">0°/180° giran la imagen; 90°/270° ajustan orientación por espejo.</p>
                    </section>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Calidad JPEG</div>
                                <div class="section-title">📐 Calidad</div>
                            </div>
                        </div>
                        <div class="segmented">
                            <button class="btn-ghost" data-config-cmd="quality" data-config-value="8" onclick="App._sendCamCmd('${this._escAttr(camId)}','quality',8,this)">Baja</button>
                            <button class="btn-ghost" data-config-cmd="quality" data-config-value="12" onclick="App._sendCamCmd('${this._escAttr(camId)}','quality',12,this)">Media</button>
                            <button class="btn-ghost" data-config-cmd="quality" data-config-value="6" onclick="App._sendCamCmd('${this._escAttr(camId)}','quality',6,this)">Alta</button>
                        </div>
                    </section>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Frecuencia de frames</div>
                                <div class="section-title">⚡ Velocidad de envío</div>
                            </div>
                        </div>
                        <div class="segmented four">
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="200" onclick="App._sendCamCmd('${this._escAttr(camId)}','fps',200,this)">5 fps</button>
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="500" onclick="App._sendCamCmd('${this._escAttr(camId)}','fps',500,this)">2 fps</button>
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="1000" onclick="App._sendCamCmd('${this._escAttr(camId)}','fps',1000,this)">1 fps</button>
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="2000" onclick="App._sendCamCmd('${this._escAttr(camId)}','fps',2000,this)">0.5 fps</button>
                        </div>
                        <p class="meta" style="margin:8px 0 0;text-align:center;">2 fps recomendado para vivo fluido. Más fps = más ancho de banda.</p>
                    </section>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">LED flash</div>
                                <div class="section-title">💡 Iluminación</div>
                            </div>
                        </div>
                        <div class="segmented">
                            <button class="btn-ghost" data-config-cmd="led" data-config-value="1" onclick="App._sendCamCmd('${this._escAttr(camId)}','led',1,this)">💡 On</button>
                            <button class="btn-ghost" data-config-cmd="led" data-config-value="0" onclick="App._sendCamCmd('${this._escAttr(camId)}','led',0,this)">🌙 Off</button>
                            <button class="btn-ghost" data-config-cmd="led_auto" data-config-value="1" onclick="App._sendCamCmd('${this._escAttr(camId)}','led_auto',1,this)">⚡ Auto</button>
                        </div>
                    </section>

                    <section class="config-section">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Alertas</div>
                                <div class="section-title">⏱️ Cooldown entre alertas</div>
                            </div>
                        </div>
                        <p class="meta" style="margin-bottom:12px;">Tiempo mínimo entre notificaciones de la misma alerta</p>
                        <input class="range-control cooldown-range" id="cfg_cooldown_min" type="range" min="5" max="60" value="${cam.cooldown_min}" oninput="App._updateCooldownLabel()">
                        <div class="cooldown-value"><strong id="cfg_cooldown_val">${cam.cooldown_min}</strong><span>min</span></div>
                        <button class="btn config-action" onclick="App._saveCooldown('${this._escAttr(camId)}',this)">💾 Guardar cooldown</button>
                    </section>

                    <section class="config-actions">
                        <button class="btn" onclick="App._sendCamCmd('${this._escAttr(camId)}','snapshot',0,this)">📸 Snapshot</button>
                        <button class="btn btn-outline" onclick="App._exportVideo('${this._escAttr(camId)}', 45, this)">🎬 Guardar video (45 min)</button>
                        <button class="btn btn-outline" onclick="App._openVigilanceSettings('${this._escAttr(camId)}')">🛡️ Ajustar protección</button>
                    </section>

                    <button class="btn btn-ghost" onclick="App.go('${returnPage}')">← Volver</button>
                    </div>`;

            // Iniciar polling del viewer
            this._startConfigViewerPoll(camId);
        } catch(e) {
            if (this.page !== 'settings' && this.page !== 'cameras' && this.page !== 'home') return;
            c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:3rem;margin-bottom:16px;">❌</div><div style="font-weight:600;margin-bottom:8px;">Error cargando cámara</div><button class="btn" style="margin-top:16px" onclick="App._openCameraConfig(\''+this._escAttr(camId)+'\')">Reintentar</button></div>';
        }

        // Aplicar valores por defecto si la cámara es nueva (nunca configurada)
        this._applyCamDefaults(camId, cam);
    },

    async _applyCamDefaults(camId, cam) {
        // Solo aplicar si la cámara nunca ha sido configurada (first_seen == last_announce o no tiene interval_ms)
        const isNew = cam.first_seen && cam.last_announce && (cam.first_seen === cam.last_announce || !cam.interval_ms);
        if (!isNew) return;

        console.log(`[CAM_CONFIG] Aplicando defaults a ${camId}`);
        try {
            // Valores por defecto recomendados:
            // quality: 10 (balance tamaño/calidad, ~15-25KB por frame)
            // interval_ms: 500 (2fps, fluido para vivo)
            await apiFetch(`${this.API}/cameras/${camId}/cmd?user_id=${this.userId}`, {
                method: 'POST',
                body: JSON.stringify({quality: 10, interval_ms: 500})
            });
            console.log(`[CAM_CONFIG] Defaults aplicados: quality=10, interval_ms=500`);
        } catch(e) {
            console.warn('[CAM_CONFIG] No se pudieron aplicar defaults:', e);
        }
    },

    async _openVigilanceSettings(camId) {
        const c = document.getElementById('app-content');
        c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:2rem;margin-bottom:16px;">🛡️</div><div style="font-weight:600;margin-bottom:8px;">Cargando protección...</div></div>';
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/vigilance?user_id=${this.userId}`);
            const d = await r.json();
            const v = d.vigilance || {};
            const n = v.normal_mode || {};
            const s = v.sentinel_mode || {};
            const lines = arr => Array.isArray(arr) ? arr.join('\n') : '';
            c.innerHTML = `
                <div style="display:flex;flex-direction:column;height:100%;overflow-y:auto;padding:16px;">
                    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">
                        <span style="font-size:2rem;">🛡️</span>
                        <div style="flex:1">
                            <div style="font-weight:600;font-size:1.1rem;">Ajustes de protección</div>
                            <div style="color:var(--text-secondary);font-size:.85rem;">Modo actual: ${d.mode || 'normal'} · ${camId}</div>
                        </div>
                    </div>

                    <div class="card">
                        <div class="card-title">Modo y horario</div>
                        <label class="settings-label">Horario apertura</label>
                        <input id="vig-open" value="${v.schedule_open || (d.schedule && d.schedule.open) || '08:00'}" style="width:100%;padding:10px;margin-bottom:8px;">
                        <label class="settings-label">Horario cierre</label>
                        <input id="vig-close" value="${v.schedule_close || (d.schedule && d.schedule.close) || '22:00'}" style="width:100%;padding:10px;margin-bottom:12px;">
                        <label style="display:flex;gap:8px;align-items:center;font-size:.9rem;">
                            <input id="vig-sentinel-enabled" type="checkbox" ${s.enabled !== false ? 'checked' : ''}>
                            Activar modo centinela fuera de horario
                        </label>
                        <label class="settings-label">Preocupación principal</label>
                        <textarea id="vig-concern" style="width:100%;min-height:60px;padding:10px;margin-bottom:8px;">${v.concern || ''}</textarea>
                    </div>

                    <div class="card">
                        <div class="card-title">Configuración normal</div>
                        <label class="settings-label">Sensibilidad</label>
                        <select id="vig-sensitivity" style="width:100%;padding:10px;margin-bottom:8px;">
                            <option value="baja" ${n.sensitivity === 'baja' ? 'selected' : ''}>Baja</option>
                            <option value="media" ${n.sensitivity === 'media' ? 'selected' : ''}>Media</option>
                            <option value="alta" ${n.sensitivity === 'alta' ? 'selected' : ''}>Alta</option>
                            <option value="critica" ${n.sensitivity === 'critica' ? 'selected' : ''}>Crítica</option>
                        </select>
                        <label class="settings-label">Nunca debe pasar...</label>
                        <textarea id="vig-forbidden" style="width:100%;min-height:70px;padding:10px;margin-bottom:8px;">${v.forbidden_events || ''}</textarea>
                        <label class="settings-label">Estado normal esperado</label>
                        <textarea id="vig-normal-state" style="width:100%;min-height:70px;padding:10px;margin-bottom:8px;">${v.normal_state || n.normal_state || ''}</textarea>
                        <label class="settings-label">Personas autorizadas</label>
                        <textarea id="vig-authorized" style="width:100%;min-height:60px;padding:10px;margin-bottom:8px;">${v.authorized_people || n.authorized_people || ''}</textarea>
                        <label class="settings-label">Objetos importantes</label>
                        <textarea id="vig-important" style="width:100%;min-height:60px;padding:10px;margin-bottom:8px;">${v.important_objects || n.important_objects || ''}</textarea>
                        <label class="settings-label">Alertar si... (una por línea)</label>
                        <textarea id="vig-alerts" style="width:100%;min-height:130px;padding:10px;margin-bottom:8px;">${lines(v.alert_behaviors || n.alert_behaviors)}</textarea>
                        <label class="settings-label">No alertar por... (una por línea)</label>
                        <textarea id="vig-ignore" style="width:100%;min-height:130px;padding:10px;margin-bottom:8px;">${lines(v.ignore_behaviors || n.ignore_behaviors)}</textarea>
                    </div>

                    <div class="card">
                        <div class="card-title">Prompt generado, solo lectura</div>
                        <textarea id="vig-prompt-preview" readonly style="width:100%;min-height:180px;padding:10px;color:var(--text-secondary);">${d.system_prompt || ''}</textarea>
                    </div>
                    <div id="vig-test-result" style="margin-top:12px;"></div>

                    <button class="btn" style="width:100%;margin-bottom:8px;" onclick="App._saveVigilanceSettings('${this._escAttr(camId)}')">💾 Guardar y regenerar prompt</button>
                    <button class="btn btn-outline" style="width:100%;margin-bottom:8px;" onclick="App._regenerateVigilancePrompt('${this._escAttr(camId)}')">🔄 Regenerar prompt</button>
                    <button class="btn btn-outline" style="width:100%;margin-bottom:8px;" onclick="App._testVigilancePrompt('${this._escAttr(camId)}')">🧪 Probar con última imagen</button>
                    <button class="btn btn-outline" style="width:100%;" onclick="App._editVigilanceWithEva('${this._escAttr(camId)}')">🤖 Editar con Eva</button>
                    <button class="btn btn-ghost" style="width:100%;" onclick="App.go('settings')">← Volver</button>
                </div>`;
        } catch(e) {
            c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:3rem;margin-bottom:16px;">❌</div><div style="font-weight:600;">Error cargando protección</div></div>';
        }
    },

    async _saveVigilanceSettings(camId) {
        const payload = {
            schedule: {
                open: document.getElementById('vig-open').value.trim() || '08:00',
                close: document.getElementById('vig-close').value.trim() || '22:00'
            },
            vigilance: {
                enabled: document.getElementById('vig-sentinel-enabled').checked,
                concern: document.getElementById('vig-concern').value.trim(),
                forbidden_events: document.getElementById('vig-forbidden').value.trim(),
                normal_mode: {
                    sensitivity: document.getElementById('vig-sensitivity').value,
                    normal_state: document.getElementById('vig-normal-state').value.trim(),
                    authorized_people: document.getElementById('vig-authorized').value.trim(),
                    important_objects: document.getElementById('vig-important').value.trim(),
                    alert_behaviors: document.getElementById('vig-alerts').value.split('\n').map(x => x.trim()).filter(Boolean),
                    ignore_behaviors: document.getElementById('vig-ignore').value.split('\n').map(x => x.trim()).filter(Boolean)
                },
                sentinel_mode: {
                    enabled: document.getElementById('vig-sentinel-enabled').checked
                }
            }
        };
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/vigilance?user_id=${this.userId}`, { method: 'PUT', body: JSON.stringify(payload) });
            const d = await r.json();
            if (d.success) this._toast('Protección guardada', `Modo actual: ${d.mode}`, 'success');
            else throw new Error(d.error || 'Error guardando');
        } catch(e) {
            this._toast('Error', e.message || 'No se pudo guardar', 'danger');
        }
    },

    async _regenerateVigilancePrompt(camId) {
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/vigilance?user_id=${this.userId}`, { method: 'PUT', body: JSON.stringify({}) });
            const d = await r.json();
            const preview = document.getElementById('vig-prompt-preview');
            if (preview) preview.value = d.system_prompt || '';
            this._toast('Prompt regenerado', `Modo actual: ${d.mode}`, 'success');
        } catch(e) {
            this._toast('Error', 'No se pudo regenerar el prompt', 'danger');
        }
    },

    async _testVigilancePrompt(camId) {
        const c = document.getElementById('app-content');
        c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:2rem;margin-bottom:16px;">🧪</div><div style="font-weight:600;">Probando con última imagen...</div></div>';
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/vigilance/test?user_id=${this.userId}`, { method: 'POST', body: JSON.stringify({}) });
            const d = await r.json();
            const preview = document.getElementById('vig-prompt-preview');
            if (preview && d.prompt) preview.value = d.prompt;
            const result = d.success
                ? `<div class="card" style="margin-top:12px;">
                    <div class="card-title">Resultado</div>
                    <div><b>Modo:</b> ${d.mode || '—'}</div>
                    <div><b>Alerta:</b> ${d.violation ? 'Sí' : 'No'}</div>
                    <div><b>Importancia:</b> ${d.importance || '—'}</div>
                    <div style="white-space:pre-wrap;margin-top:8px;">${(d.summary || d.message || '').replace(/</g,'&lt;')}</div>
                   </div>`
                : `<div class="card" style="margin-top:12px;"><div class="card-title">Error</div><div>${(d.error || 'No se pudo probar').replace(/</g,'&lt;')}</div></div>`;
            this._openVigilanceSettings(camId);
            setTimeout(() => {
                const out = document.getElementById('vig-test-result');
                if (out) out.innerHTML = result;
                this._toast(d.success ? 'Prueba completada' : 'Prueba fallida', d.success ? 'Revisa el resultado' : 'Sin imagen disponible', d.success ? 'success' : 'danger');
            }, 0);
        } catch(e) {
            this._openVigilanceSettings(camId);
            this._toast('Error', 'No se pudo probar el último frame', 'danger');
        }
    },

    _editVigilanceWithEva(camId) {
        if (typeof EvaChat !== 'undefined' && EvaChat.open) {
            EvaChat.open();
            EvaChat.sendUserMessage(`Quiero editar la protección de la cámara ${camId}.`);
            return;
        }
        this.go('eva');
        setTimeout(() => {
            if (typeof EvaChat !== 'undefined' && EvaChat.sendUserMessage) {
                EvaChat.sendUserMessage(`Quiero editar la protección de la cámara ${camId}.`);
            }
        }, 500);
    },

    // MJPEG stream en config viewer
    _configViewerPoll: null,
    _configRotation: 0,
    _configReturnPage: 'cameras',
    _configStreamStarted: false,
    _startConfigViewerPoll(camId) {
        if (this._configViewerPoll) clearInterval(this._configViewerPoll);
        this._configStreamStarted = false;
        const startStream = () => {
            const img = document.getElementById('cfg-live-img');
            if (!img) return;
            if (!this._configStreamStarted) {
                this._configStreamStarted = true;
                img.src = `${this.API}/cameras/${camId}/stream?user_id=${this.userId}&fps=5`;
            }
        };
        startStream();
        // Watermark update cada 5s (el stream maneja los frames)
        this._configViewerPoll = setInterval(() => {
            const wm = document.getElementById('cfg-watermark');
            if (wm) {
                const now = new Date();
                const dateText = now.toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit',year:'2-digit'});
                const ts_str = now.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
                wm.textContent = `OJO-${camId.substring(0,8)} | ${dateText} ${ts_str}`;
            }
        }, 5000);
    },

    // Aplicar filtros de brillo/contraste a la imagen del viewer
    _updateImageFilter(camId) {
        const b = document.getElementById('cfg_brightness')?.value || '0';
        const c = document.getElementById('cfg_contrast')?.value || '0';
        const bv = document.getElementById('cfg_brightness_val');
        const cv = document.getElementById('cfg_contrast_val');
        if (bv) bv.textContent = b;
        if (cv) cv.textContent = c;
        const img = document.getElementById('cfg-live-img');
        if (img) {
            img.style.filter = `brightness(${100 + parseInt(b)}%) contrast(${100 + parseInt(c)}%)`;
        }
    },

    _resetBrightnessContrast(camId) {
        const bInput = document.getElementById('cfg_brightness');
        const cInput = document.getElementById('cfg_contrast');
        if (bInput) bInput.value = '0';
        if (cInput) cInput.value = '0';
        this._updateImageFilter(camId);
        this._sendCamCmd(camId, 'brightness', '0', null);
    },

    _updateCooldownLabel() {
        const el = document.getElementById('cfg_cooldown_min');
        const val = document.getElementById('cfg_cooldown_val');
        if (el && val) val.textContent = el.value;
    },

    _mapCamLevel(value) {
        const n = parseInt(value) || 0;
        return Math.max(-2, Math.min(2, Math.round(n / 50)));
    },

    _setConfigButtonBusy(btn, busy) {
        if (!btn) return;
        if (busy) {
            btn.dataset.originalText = btn.textContent;
            btn.dataset.originalDisabled = btn.disabled ? '1' : '0';
            btn.disabled = true;
        } else {
            if (btn.dataset.originalText) btn.textContent = btn.dataset.originalText;
            btn.disabled = btn.dataset.originalDisabled === '1';
        }
    },

    _updateConfigButtonStates(cmd, val) {
        document.querySelectorAll('[data-config-cmd]').forEach(btn => {
            const active = btn.dataset.configCmd === cmd && String(btn.dataset.configValue) === String(val);
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        if (cmd === 'rotation') {
            const rot = this._configRotation || 0;
            const btn = document.querySelector('[data-config-cmd="rotation"]');
            if (btn) {
                btn.dataset.configValue = String(rot);
                btn.textContent = `↻ Rotación ${rot * 90}°`;
            }
        }
    },

    async _sendCamCmd(camId, cmd, val, btn) {
        this._setConfigButtonBusy(btn, true);
        try {
            let body = {};
            if (cmd === 'rotation') {
                const next = val === 'next' ? ((this._configRotation || 0) + 1) % 4 : Number(val);
                const rot = Number.isFinite(next) ? next : 0;
                this._configRotation = rot;
                body = {
                    h_mirror: rot === 1 || rot === 2,
                    v_flip: rot === 2 || rot === 3
                };
            } else if (cmd === 'quality') {
                body = {quality: val};
            } else if (cmd === 'interval_ms' || cmd === 'fps') {
                body = {interval_ms: val};
            } else if (cmd === 'led') {
                body = {led_auto: false, led_on: val ? true : false, led_bright: val ? 255 : 0};
            } else if (cmd === 'led_auto') {
                body = {led_auto: true};
            } else if (cmd === 'brightness') {
                body = {
                    brightness: this._mapCamLevel(document.getElementById('cfg_brightness')?.value || 0),
                    contrast: this._mapCamLevel(document.getElementById('cfg_contrast')?.value || 0)
                };
            } else if (cmd === 'snapshot') {
                const imgUrl = `${this.API}/frames/latest-raw.jpg?camera_id=${camId}&user_id=${this.userId}&_=${Date.now()}`;
                window.open(imgUrl, '_blank');
                return;
            } else {
                this._toast('', 'Comando no soportado', 'warning');
                return;
            }
            console.log('SEND CMD:', camId, cmd, val, body);
            const r = await apiFetch(`${this.API}/cameras/${camId}/cmd?user_id=${this.userId}`, {
                method: 'POST',
                body: JSON.stringify(body)
            });
            console.log('RESPONSE:', r.status, r.ok);
            const d = await r.json();
            console.log('DATA:', d);
            if (!d.ok) {
                if (d.detail?.includes('offline') || d.detail?.includes('503')) {
                    this._toast('', 'Cámara offline — comando en cola', 'warning');
                } else {
                    this._toast('', d.detail || d.error || 'Error enviando comando', 'danger');
                }
                return;
            }
            if (d.queued) {
                this._toast('', 'Comando enviado: la cámara lo aplicará al conectar', 'warning');
            }
            if (cmd === 'led') {
                this._updateConfigButtonStates(cmd, val ? 1 : 0);
                this._toast('', 'LED ' + (val ? 'encendido 💡' : 'apagado 🌙'), 'success');
            } else if (cmd === 'led_auto') {
                this._updateConfigButtonStates(cmd, 1);
                this._toast('', 'LED automático ⚡', 'success');
            } else if (cmd === 'quality') {
                this._updateConfigButtonStates(cmd, val);
                const labels = {6:'Alta', 12:'Media', 8:'Baja', 10:'Media-Baja'};
                this._toast('', 'Calidad: ' + (labels[val] || val), 'success');
            } else if (cmd === 'rotation') {
                this._updateConfigButtonStates(cmd, this._configRotation);
                this._toast('', 'Rotación: ' + (this._configRotation * 90) + '°', 'success');
            } else if (cmd === 'brightness') {
                this._toast('', `Brillo/contraste aplicado 🔆 (${body.brightness}/${body.contrast})`, 'success');
            } else if (cmd === 'interval_ms' || cmd === 'fps') {
                const fps = Math.round(1000 / val);
                this._updateConfigButtonStates('fps', val);
                this._toast('', `Velocidad: ${fps} fps (${val}ms)`, 'success');
            }
        } catch(e) {
            console.error('SEND CMD ERROR:', e);
            this._toast('', 'Error de red — reintenta', 'danger');
        } finally {
            this._setConfigButtonBusy(btn, false);
        }
    },

    async _exportVideo(camId, minutes = 45, btn = null) {
        if (btn) this._setConfigButtonBusy(btn, true);
        this._toast('', `Generando video de últimos ${minutes} min...`, 'info');
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/export-video?user_id=${this.userId}&minutes=${minutes}`, {
                method: 'POST'
            });
            const d = await r.json();
            if (d.success) {
                const mins = Math.round(d.duration_seconds / 60);
                this._toast('', `Video listo: ${d.frames_used} frames, ${mins} min`, 'success');
                // Abrir descarga
                const downloadUrl = `${this.API}/api/cameras/${camId}/download-video?user_id=${this.userId}&file=${encodeURIComponent(d.video_url.split('file=')[1] || '')}`;
                window.open(downloadUrl, '_blank');
            } else {
                this._toast('', d.detail || d.error || 'Error generando video', 'danger');
            }
        } catch(e) {
            console.error('EXPORT VIDEO ERROR:', e);
            this._toast('', 'Error generando video', 'danger');
        } finally {
            if (btn) this._setConfigButtonBusy(btn, false);
        }
    },

    // ── EVENTS ───────────────────────────────────────────────
    async _pageEvents(c, openEventId) {
        this._resetScrollContent(c);
        c.innerHTML = `<div class="filters">
            <button class="filter-btn active" onclick="App._filterEvents(this,'recent')">🕒 Últimas 24h</button>
            <button class="filter-btn" onclick="App._filterEvents(this,'alerts')">🚨 Alertas</button>
            <button class="filter-btn" onclick="App._filterEvents(this,'all')">📋 Todos</button>
            <select id="filter-cam" onchange="App._filterByCam(this.value)" style="margin-left:auto;padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-secondary);color:var(--text);font-size:0.75rem;">
                <option value="all">Todas las cámaras</option>
            </select>
        </div><div id="events-list">${this._skeleton()}</div>`;
        this._eventFilterCam = 'all';
        this._lastEventTs = 0;
        this._loadEvents('recent');
        // Poll for new events every 10 seconds (smart - only if changed)
        if (this._polls.events) clearInterval(this._polls.events);
        this._polls.events = setInterval(() => {
            if (document.getElementById('events-list')) {
                this._pollNewEvents();
            } else {
                clearInterval(this._polls.events);
            }
        }, 10000);
        // Load camera list for filter
        this._loadCamFilter();
        if (openEventId) {
            setTimeout(() => this._openEvent(openEventId), 500);
        }
    },

    async _loadCamFilter() {
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const cams = (await r.json()).cameras || [];
            const sel = document.getElementById('filter-cam');
            if (sel) {
                sel.innerHTML = '<option value="all">Todas las cámaras</option>';
                cams.forEach(cam => {
                    sel.innerHTML += `<option value="${cam.camera_id}">${cam.name}</option>`;
                });
            }
        } catch(e) { console.warn('[App] events filter select silent fail:', e); }
    },

    _filterByCam(camId) {
        this._eventFilterCam = camId;
        this._loadEvents(this._currentEventFilter || 'today');
    },

    async _pollNewEvents() {
        // Smart poll: check if there are new events, only reload if changed
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=recent&limit=1`);
            const evts = (await r.json()).events || [];
            if (evts.length > 0 && evts[0].timestamp > this._lastEventTs) {
                this._loadEvents(this._currentEventFilter || 'today');
            }
        } catch(e) { console.warn('[App] events diff silent fail:', e); }
    },

    _filterEvents(btn, filter) {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._currentEventFilter = filter;
        this._loadEvents(filter);
    },

    async _loadEvents(filter) {
        const el = document.getElementById('events-list');
        if (!el) return;
        el.innerHTML = this._skeleton();
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=${filter}&limit=50`);
            let evts = (await r.json()).events || [];
            
            // Filtrar por cámara si seleccionó una
            if (this._eventFilterCam && this._eventFilterCam !== 'all') {
                evts = evts.filter(e => e.camera_id === this._eventFilterCam);
            }

            if (!evts.length) {
                el.innerHTML = `<div class="empty-state" style="padding:40px 0">
                    <div style="font-size:2.5rem;margin-bottom:12px">👁</div>
                    <div class="empty-title">Todo tranquilo</div>
                    <p>No hay eventos recientes${filter === 'alerts' ? ' de alerta' : ''} para este filtro</p>
                </div>`;
                return;
            }

            // Guardar el último timestamp para smart polling
            if (evts.length > 0) {
                this._lastEventTs = evts[0].timestamp || 0;
            }

            const moreHtml = evts.length >= 50
                ? `<button class="btn btn-outline" onclick="App._loadMoreEvents('${filter}')" style="margin:12px 0 24px">Ver más análisis recientes</button>`
                : '';
            el.innerHTML = evts.map(evt => this._eventRowHtml(evt)).join('') + moreHtml;

            // Cargar miniaturas de eventos
            for (const evt of evts) {
                if (evt.thumb_url) {
                    const el2 = document.getElementById(`evthumb-${evt.event_id}`);
                    if (el2) {
                        el2.style.background = '#222';
                        el2.innerHTML = `<img src="${evt.thumb_url}" loading="lazy" decoding="async" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="this.style.display='none'" />`;
                    }
                }
            }
        } catch(e) {
            el.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><p>Error cargando eventos</p></div>`;
        }
    },

    async _loadMoreEvents(filter) {
        const el = document.getElementById('events-list');
        if (!el) return;
        el.innerHTML = this._skeleton();
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=${filter}&limit=100`);
            const data = await r.json();
            let evts = data.events || [];
            if (this._eventFilterCam && this._eventFilterCam !== 'all') {
                evts = evts.filter(e => e.camera_id === this._eventFilterCam);
            }
            if (!evts.length) {
                el.innerHTML = `<div class="empty-state" style="padding:40px 0"><div style="font-size:2.5rem;margin-bottom:12px">👁</div><div class="empty-title">Todo tranquilo</div><p>No hay más análisis para este filtro</p></div>`;
                return;
            }
            this._lastEventTs = evts[0].timestamp || 0;
            el.innerHTML = evts.map(evt => this._eventRowHtml(evt)).join('') +
                (evts.length >= 100 ? `<button class="btn btn-outline" onclick="App._loadMoreEvents('${filter}')" style="margin:12px 0 24px">Ver más análisis</button>` : '');
        } catch(e) {
            el.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><p>Error cargando eventos</p></div>`;
        }
    },

    _cleanEventDescription(desc) {
        if (!desc) return '';
        let text = String(desc).trim();
        if (!text) return '';
        try {
            const parsed = JSON.parse(text);
            if (parsed && typeof parsed === 'object') text = parsed.summary || parsed.description || text;
        } catch(e) { console.warn('[App] event desc JSON.parse silent fail:', e); }
        return text
            .replace(/- If ALL checks NO[\s\S]*/m, '')
            .replace(/No violation detected[\s\S]*/i, 'Sin actividad sospechosa')
            .replace(/The employee's hands[\s\S]*/i, 'Sin actividad sospechosa')
            .replace(/The provided (images|frames)[\s\S]*/i, 'Sin actividad sospechosa')
            .replace(/Error analizando[\s\S]*/i, 'Sin actividad sospechosa')
            .trim() || 'Actividad normal';
    },

    _eventRowHtml(evt) {
        const violation = evt.qwen?.violation;
        const level = violation ? 'alert' : 'ok';
        const label = violation ? '🚨 Análisis' : '✅ Normal';
        const ts = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleString('es-ES', {hour:'2-digit',minute:'2-digit',month:'short',day:'numeric',hour12:true}) : '--';
        const camName = evt.camera_name || evt.camera_id || 'Sin nombre';
        const camZone = evt.metadata?.zone || '';
        const qa = evt.qwen_analysis || {};
        const vision = qa.vision || {};
        const ruleChecks = qa.rule_checks || {};
        const searchTags = qa.search_tags || [];
        let desc = this._cleanEventDescription(qa.summary || qa.description || evt.qwen?.description || evt.description || (violation ? 'Actividad sospechosa' : 'Actividad normal'));
        if (!desc || desc.length < 3) desc = violation ? 'Actividad sospechosa' : 'Actividad normal';
        // Enriquecer descripción con datos de visión
        const visionParts = [];
        if (vision.persons && Array.isArray(vision.persons)) {
            vision.persons.forEach(p => {
                const parts = [];
                if (p.location) parts.push(`en ${p.location}`);
                if (p.clothing) { let c = Array.isArray(p.clothing) ? p.clothing.join(', ') : (typeof p.clothing === 'string' ? p.clothing : ''); if (c) parts.push(`con ${c}`); }
                if (p.actions && p.actions.length) parts.push(p.actions.join(', '));
                if (parts.length) visionParts.push(`Persona ${parts.join(' ')}`);
            });
        }
        if (vision.objects && vision.objects.length) visionParts.push(`Objetos: ${vision.objects.join(', ')}`);
        if (vision.scene) visionParts.push(vision.scene);
        if (vision.cliente && vision.cliente.presente) {
            let cDesc = vision.cliente.descripcion || '';
            if (vision.cliente.accion) cDesc += ` — ${vision.cliente.accion}`;
            if (cDesc) visionParts.push(`Cliente: ${cDesc}`);
        }
        if (vision.empleado && vision.empleado.presente) {
            let eDesc = vision.empleado.descripcion || '';
            if (vision.empleado.accion) eDesc += ` — ${vision.empleado.accion}`;
            if (eDesc) visionParts.push(`Empleado: ${eDesc}`);
        }
        const meaningfulTags = searchTags.filter(t => t !== 'visible');
        if (meaningfulTags.length) visionParts.push(`Tags: ${meaningfulTags.join(', ')}`);
        const enrichedDesc = visionParts.length ? visionParts.join('. ') : desc;
            const yolo = evt.yolo || {};
            const yoloCount = (evt.yolo?.count ?? evt.yolo_count ?? 0) || (Array.isArray(yolo.detections) ? yolo.detections.length : 0);
        const icon = violation ? '🚨' : '✓';
        const thumbHtml = evt.thumb_url
            ? `<img src="${evt.thumb_url}" loading="lazy" decoding="async" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="this.style.display='none';this.parentElement.innerHTML='<span style=\'font-size:1.3rem\'>${icon}</span>'" />`
            : `<span style="font-size:1.3rem;">${icon}</span>`;
        const evtTime = evt.datetime || ts;
        return `<div class="event-row ${violation ? 'event-alert' : ''}" onclick="App._openEvent('${this._escAttr(evt.event_id)}')">
            <div class="event-thumb" id="evthumb-${evt.event_id}" style="background:#222;display:flex;align-items:center;justify-content:center;overflow:hidden;border-radius:10px;flex-shrink:0;width:80px;height:60px">
                ${thumbHtml}
            </div>
            <div class="event-info" style="flex:1;min-width:0">
                <div class="event-title">${camName}${camZone ? ' · ' + camZone : ''}</div>
                <div class="meta">${evtTime} · Detección: ${yoloCount} objeto(s)</div>
                <div class="meta event-desc" style="margin-top:2px;font-size:0.78rem;color:var(--text-secondary);white-space:normal;line-height:1.35">${enrichedDesc}</div>
            </div>
            <span class="badge badge-${level}" style="flex-shrink:0">${label}</span>
        </div>`;
    },

    async _openEvent(eventId) {
        if (!eventId) return;
        // un solo modal: quitar el anterior (evita reproductores apilados)
        this._stopEventPlayback();
        document.getElementById('event-modal')?.remove();
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/events/${eventId}?user_id=${uid}`);
            const d = await r.json();
            if (!d || d.error) return;
            const violation = d.qwen?.violation || d.event_type === 'violation';
            const isAttention = (d.attention_hits && d.attention_hits.length > 0) || d.event_type === 'attention';
            const isSentinel = d.event_type === 'sentinel' || (d.qwen_json?.after_hours && d.qwen_json?.importancia === 'alta');
            const attentionHits = d.attention_hits || [];
            const modal = document.createElement('div');
            modal.id = 'event-modal';
            modal.style.cssText = 'position:fixed;inset:0;z-index:500;background:#000;display:flex;flex-direction:column;overflow-y:auto';

            const header = document.createElement('div');
            header.style.cssText = 'display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--bg-secondary);position:sticky;top:0;z-index:1';
            const closeBtn = document.createElement('button');
            closeBtn.innerHTML = '✕';
            closeBtn.style.cssText = 'background:none;border:none;color:var(--text-secondary);font-size:1.3rem;cursor:pointer;margin-right:12px';
            closeBtn.onclick = () => { this._stopEventPlayback(); modal.remove(); };
            const title = document.createElement('span');
            title.style.fontWeight = '600';
            title.textContent = d.camera_name || 'Evento';
            header.appendChild(closeBtn);
            header.appendChild(title);
            if (violation || isAttention || isSentinel) {
                const badge = document.createElement('span');
                badge.className = 'badge badge-alert';
                badge.style.marginLeft = 'auto';
                badge.textContent = isSentinel ? '🛡️ Fuera de horario' : (isAttention ? '🔍 Observación' : '🚨 Alerta');
                header.appendChild(badge);
            }

            const content = document.createElement('div');
            content.style.padding = '16px';

            // ── REPRODUCTOR UNIFICADO (2026-09-02): UN solo visor con autoplay.
            // Antes: 3 bloques duplicados (imagen grande frame_b64 + carrusel
            // manual + grid 4×4). Ahora: video mp4 si existe, o carrusel de
            // frames que SE REPRODUCE solo (2 fps), con ▶/⏸ y al terminar
            // pasa automáticamente al siguiente evento de la lista.
            const frames = Array.isArray(d.frames) ? d.frames : [];
            if (d.video_file) {
                const videoCard = document.createElement('div');
                videoCard.className = 'card';
                const videoTitle = document.createElement('div');
                videoTitle.className = 'card-title';
                videoTitle.textContent = '🎞️ Video del evento';
                const video = document.createElement('video');
                video.controls = true;
                video.autoplay = true;
                video.muted = true;      // autoplay permitido por políticas del navegador
                video.playsInline = true;
                video.src = `${this.API}/api/events/${eventId}/video.mp4?user_id=${uid}`;
                video.style.cssText = 'width:100%;border-radius:8px;display:block;background:#000';
                video.onended = () => this._onEventPlaybackEnd(eventId);
                videoCard.appendChild(videoTitle);
                videoCard.appendChild(video);
                content.appendChild(videoCard);
            } else if (frames.length > 1) {
                const hasNext = this._hasNextEvent(eventId);
                const frameCard = document.createElement('div');
                frameCard.className = 'card';
                const frameTitle = document.createElement('div');
                frameTitle.className = 'card-title';
                frameTitle.textContent = '🎞️ Reproducción del evento';
                const frameImg = document.createElement('img');
                frameImg.id = `event-frame-img-${eventId}`;
                frameImg.style.cssText = 'width:100%;border-radius:8px;display:block;background:#000';
                const frameStatus = document.createElement('div');
                frameStatus.id = `event-frame-status-${eventId}`;
                frameStatus.className = 'meta';
                frameStatus.style.textAlign = 'center';
                if (hasNext) frameStatus.textContent = '0/' + frames.length + ' · al terminar ➡️ siguiente evento';
                const frameControls = document.createElement('div');
                frameControls.style.cssText = 'display:flex;gap:8px;align-items:center;margin-top:10px';
                frameControls.innerHTML = `
                    <button class="btn btn-sm" id="event-play-btn-${this._escAttr(eventId)}" onclick="App._toggleEventPlayback('${this._escAttr(eventId)}', ${frames.length})" style="min-width:44px">⏸</button>
                    <input id="event-frame-range-${eventId}" type="range" min="0" max="${frames.length - 1}" value="0" style="flex:1" oninput="App._showEventFrame('${this._escAttr(eventId)}', ${frames.length}, this.value)">
                    <button class="btn btn-sm" onclick="App._moveEventFrame('${this._escAttr(eventId)}', ${frames.length}, -1)">◀</button>
                    <button class="btn btn-sm" onclick="App._moveEventFrame('${this._escAttr(eventId)}', ${frames.length}, 1)">▶</button>`;
                frameCard.appendChild(frameTitle);
                frameCard.appendChild(frameImg);
                frameCard.appendChild(frameStatus);
                frameCard.appendChild(frameControls);
                content.appendChild(frameCard);
                this._eventFrameIndex = this._eventFrameIndex || {};
                this._eventFrameIndex[eventId] = 0;
                this._showEventFrame(eventId, frames.length, 0);
                // AUTOPLAY: reproducir como video (2 fps)
                this._startEventPlayback(eventId, frames.length);
            } else if (d.frame_b64) {
                const img = document.createElement('img');
                img.src = `data:image/jpeg;base64,${d.frame_b64}`;
                img.style.cssText = 'width:100%;border-radius:10px;display:block;margin-bottom:16px';
                content.appendChild(img);
            }
            if (d.camera_id) {
                const liveBtn = document.createElement('button');
                liveBtn.className = 'btn';
                liveBtn.style.cssText = 'width:100%;margin:12px 0 16px';
                liveBtn.textContent = '📹 Ver cámara en vivo';
                liveBtn.onclick = () => { this._stopEventPlayback(); modal.remove(); this._openCameraLive(d.camera_id); };
                content.appendChild(liveBtn);
            }

            const card = document.createElement('div');
            card.className = 'card';
            const cardTitle = document.createElement('div');
            cardTitle.className = 'card-title';
            cardTitle.textContent = '🤖 Análisis de Eva';
            const qa = d.qwen_analysis || {};
            const qjson = d.qwen_json || {};
            const desc = this._cleanEventDescription(qa.summary || qa.description || qjson.summary || qjson.description || d.qwen?.description || d.description);
            if (desc) {
                const p = document.createElement('p');
                p.style.cssText = 'font-size:.9rem;margin-bottom:8px;line-height:1.45;white-space:pre-wrap';
                p.textContent = desc;
                card.appendChild(p);
            }
            if (attentionHits.length) {
                const hitsDiv = document.createElement('div');
                hitsDiv.style.cssText = 'background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:10px 14px;margin-bottom:12px';
                hitsDiv.innerHTML = `<div style="font-size:.78rem;color:var(--warning,#f5a623);font-weight:600;margin-bottom:4px">🔍 Observaciones detectadas:</div><div style="font-size:.85rem;line-height:1.4">${attentionHits.map(h => `• ${h}`).join('<br>')}</div>`;
                card.appendChild(hitsDiv);
            }
            if (isSentinel) {
                const sentinelDiv = document.createElement('div');
                sentinelDiv.style.cssText = 'background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:10px 14px;margin-bottom:12px';
                sentinelDiv.innerHTML = `<div style="font-size:.82rem;color:var(--warning,#f5a623);font-weight:600">🛡️ Modo centinela — Se detectó presencia fuera del horario de trabajo</div>`;
                card.appendChild(sentinelDiv);
            }
            const aiRow = document.createElement('div');
            aiRow.className = 'ai-row';
                const yoloCount = (d.yolo?.count ?? 0) || (Array.isArray(d.yolo?.detections) ? d.yolo.detections.length : 0);
                aiRow.innerHTML = `<div class="ai-card"><div class="ai-label">👁 Detección</div><div class="ai-val">${yoloCount} obj.</div></div><div class="ai-card"><div class="ai-label">👥 Personas</div><div class="ai-val">${d.persons ?? d.qwen_analysis?.persons ?? '—'}</div></div><div class="ai-card"><div class="ai-label">🧠 Eva</div><div class="ai-val">${isSentinel ? '🛡️ Centinela' : (isAttention || violation ? '🔍 Observación' : '✅ Normal')}</div></div>`;
            card.appendChild(aiRow);
            content.appendChild(card);
            
            const btnRow = document.createElement('div');
            btnRow.style.cssText = 'display:flex;gap:8px;margin-top:8px';
            const dismissBtn = document.createElement('button');
            dismissBtn.className = 'btn';
            dismissBtn.style.cssText = 'flex:1;background:var(--bg-tertiary);color:var(--text-secondary)';
            dismissBtn.innerHTML = '✓ Falsa alarma';
            dismissBtn.onclick = () => { this._stopEventPlayback(); this._dismissEvent(eventId); modal.remove(); };
            btnRow.appendChild(dismissBtn);
            if (violation || isAttention) {
                const confirmBtn = document.createElement('button');
                confirmBtn.className = 'btn';
                confirmBtn.style.cssText = 'flex:1;background:var(--accent)';
                confirmBtn.innerHTML = isAttention ? '🏷️ Marcar como falta real' : '⚠️ Confirmar alerta';
                confirmBtn.onclick = () => { this._stopEventPlayback(); this._confirmThreat(eventId); modal.remove(); };
                btnRow.appendChild(confirmBtn);
            }

            modal.appendChild(header);
            content.appendChild(btnRow);
            modal.appendChild(content);
            document.body.appendChild(modal);
        } catch(e) { console.warn('[App] event modal silent fail:', e); }
    },

    // ── Reproducción del evento (2026-09-02) ──────────────────────────────
    // El carrusel de frames corre SOLO a 2 fps (como el video del grid).
    // Al llegar al último frame → siguiente evento de la lista actual.
    _startEventPlayback(eventId, total, fps = 2) {
        this._stopEventPlayback();
        this._eventPlayback = { eventId, total, playing: true };
        const btn = document.getElementById(`event-play-btn-${CSS.escape(eventId)}`);
        if (btn) btn.textContent = '⏸';
        this._eventPlayback.timer = setInterval(() => {
            if (!this._eventPlayback || this._eventPlayback.eventId !== eventId) return;
            const idx = (this._eventFrameIndex?.[eventId] ?? 0) + 1;
            if (idx >= total) { this._onEventPlaybackEnd(eventId); return; }
            this._showEventFrame(eventId, total, idx);
        }, Math.round(1000 / fps));
    },

    _toggleEventPlayback(eventId, total) {
        if (this._eventPlayback && this._eventPlayback.playing) {
            this._stopEventPlayback();  // deja el índice actual (pausa)
            const btn = document.getElementById(`event-play-btn-${CSS.escape(eventId)}`);
            if (btn) btn.textContent = '▶';
        } else {
            // reanudar (o empezar si venía en pausa en el último frame → reinicia)
            const idx = this._eventFrameIndex?.[eventId] ?? 0;
            const from = idx >= total - 1 ? 0 : idx;
            this._startEventPlayback(eventId, total);
            if (from === 0) this._showEventFrame(eventId, total, 0);
        }
    },

    _stopEventPlayback() {
        if (this._eventPlayback?.timer) clearInterval(this._eventPlayback.timer);
        this._eventPlayback = null;
    },

    _onEventPlaybackEnd(eventId) {
        this._stopEventPlayback();
        const next = this._getNextEventId(eventId);
        if (next) {
            // pequeño respiro para que se vea que terminó, luego siguiente
            setTimeout(() => this._openEvent(next), 700);
        } else {
            const status = document.getElementById(`event-frame-status-${eventId}`);
            if (status) status.textContent = 'Fin de la lista de eventos ✓';
        }
    },

    // Lista actual de eventos visibles en la pestaña (para saber el siguiente)
    _visibleEventIds() {
        return Array.from(document.querySelectorAll('#events-list .event-row'))
            .map(el => el.getAttribute('onclick') || '')
            .map(s => (s.match(/_openEvent\('([^']+)'\)/) || [])[1])
            .filter(Boolean);
    },

    _hasNextEvent(eventId) {
        const ids = this._visibleEventIds();
        return ids.length > 1 && ids.indexOf(eventId) < ids.length - 1;
    },

    _getNextEventId(eventId) {
        const ids = this._visibleEventIds();
        const i = ids.indexOf(eventId);
        return (i >= 0 && i < ids.length - 1) ? ids[i + 1] : null;
    },

    _showEventFrame(eventId, total, rawIndex) {
        const index = Math.max(0, Math.min(total - 1, parseInt(rawIndex || '0', 10)));
        const uid = this.userId || 'default';
        this._eventFrameIndex = this._eventFrameIndex || {};
        this._eventFrameIndex[eventId] = index;
        const img = document.getElementById(`event-frame-img-${eventId}`);
        const status = document.getElementById(`event-frame-status-${eventId}`);
        const range = document.getElementById(`event-frame-range-${eventId}`);
        if (img) img.src = `${this.API}/api/events/${eventId}/frame/${index}?user_id=${uid}&_=${Date.now()}`;
        if (range) range.value = index;
        const hasNext = this._hasNextEvent(eventId);
        if (status) status.textContent = hasNext
            ? `${index + 1}/${total} · al terminar ➡️ siguiente evento`
            : `${index + 1}/${total}`;
    },

    _moveEventFrame(eventId, total, delta) {
        const wasPlaying = this._eventPlayback?.playing;
        if (wasPlaying) this._stopEventPlayback();
        const current = this._eventFrameIndex?.[eventId] || 0;
        this._showEventFrame(eventId, total, Math.max(0, Math.min(total - 1, current + delta)));
        const btn = document.getElementById(`event-play-btn-${CSS.escape(eventId)}`);
        if (btn) btn.textContent = '▶';
    },

    async _dismissEvent(id) {
        try {
            const uid = this.userId || 'default';
            await apiFetch(`${this.API}/api/event/${id}/dismiss`, { method: 'POST', body: JSON.stringify({ user_id: uid }) });
            this._toast('', 'Evento marcado como falsa alarma', 'success');
        } catch(e) { console.warn('[App] dismissEvent silent fail:', e); }
    },

    async _confirmThreat(id) {
        try {
            const uid = this.userId || 'default';
            await apiFetch(`${this.API}/api/event/${id}/confirm`, { method: 'POST', body: JSON.stringify({ user_id: uid }) });
            this._toast('', '¡Alerta confirmada! Gracias por la confirmación', 'danger');
        } catch(e) { console.warn('[App] confirmEvent silent fail:', e); }
    },

    // ── SETTINGS ─────────────────────────────────────────────
    async _pageSettings(c) {
        this._resetScrollContent(c);
        let profile = {};
        let cams = [];
        try { 
            const r = await apiFetch(`${this.API}/api/user/profile?user_id=${this.userId}`);
            profile = await r.json();
        } catch(e) { console.warn('[App] profile fetch silent fail:', e); }
        try {
            const r2 = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            cams = (await r2.json()).cameras || [];
            this._homeCams = cams;
        } catch(e) { console.warn('[App] cameras fetch silent fail:', e); }
        if (!this._isCurrentPage('settings')) return;
        
        const plan = profile.plan || 'Fundador';
        const active = profile.status === 'active';
        const activeCams = cams.filter(cam => cam.active).length;
        const alerts = cams.reduce((sum, cam) => sum + (cam.metrics?.total_alerts || 0), 0);
        const userName = profile.name || profile.business_name || 'Usuario';
        const userInitial = userName.trim().charAt(0).toUpperCase() || 'U';

        const cameraRows = cams.length > 0 ? cams.map(cam => {
            const zone = cam.zone || 'sin zona';
            const events = cam.metrics?.total_events || 0;
            const alertsCam = cam.metrics?.total_alerts || 0;
            const rules = cam.rules?.length || 0;
            const lastSeen = cam.last_frame ? this._relTime(cam.last_frame) : 'Sin datos';
            return `
                <button class="ios-row" onclick="App._openCameraConfig('${this._escAttr(cam.camera_id)}')">
                    <span class="ios-icon">📷</span>
                    <div class="ios-row-main">
                        <div class="ios-row-title">Ajustes de cámara</div>
                        <div class="ios-row-sub">${cam.name || cam.camera_id} · ${zone} · ${events} eventos · ${alertsCam} alertas · ${rules} reglas</div>
                        <div class="ios-row-sub">${lastSeen}</div>
                    </div>
                    <span class="ios-value ${cam.active ? 'ios-value-ok' : 'ios-value-danger'}">${cam.active ? 'Online' : 'Offline'}</span>
                    <span class="ios-chevron">›</span>
                </button>`;
        }).join('') : `
            <div class="ios-empty">
                <div class="empty-icon">📷</div>
                <div class="empty-title">Sin cámaras</div>
                <p class="meta">Configura tu primera cámara desde Eva.</p>
                <button class="btn" onclick="App.newCamera()">+ Instalar cámara con Eva</button>
            </div>`;

        const vigilanceRows = cams.length > 0 ? cams.map(cam => `
            <button class="ios-row" onclick="App._openVigilanceSettings('${this._escAttr(cam.camera_id)}')">
                <span class="ios-icon">🛡️</span>
                <div class="ios-row-main">
                    <div class="ios-row-title">${cam.name || cam.camera_id}</div>
                    <div class="ios-row-sub">${cam.zone || 'sin zona'} · ${cam.vigilance?.enabled ? 'protección activa' : 'protección apagada'}</div>
                </div>
                <span class="ios-chevron">›</span>
            </button>`).join('') : `
            <div class="ios-empty compact">
                <div class="empty-title">Sin protección configurada</div>
                <p class="meta">Agrega una cámara para editar sus reglas de protección.</p>
            </div>`;

        c.innerHTML = `
            <div class="settings-page">
                <div class="settings-hero">
                    <div class="settings-avatar">${userInitial}</div>
                    <div>
                        <div class="settings-hero-title">Ajustes</div>
                        <div class="settings-hero-sub">${userName}</div>
                    </div>
                </div>

                <div class="settings-stats">
                    <div><strong>${cams.length}</strong><span>Cámaras</span></div>
                    <div><strong>${activeCams}</strong><span>Online</span></div>
                    <div><strong>${alerts}</strong><span>Alertas</span></div>
                </div>

                <div class="ios-group">
                    <div class="ios-group-title">Cuenta</div>
                    <button class="ios-row" onclick="App._showSubscription()">
                        <span class="ios-icon">👤</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">Mi cuenta</div>
                            <div class="ios-row-sub">Plan, horario y datos del negocio</div>
                        </div>
                        <span class="ios-value ${active ? 'ios-value-ok' : 'ios-value-danger'}">${plan}</span>
                        <span class="ios-chevron">›</span>
                    </button>
                </div>

                <div class="ios-group">
                    <div class="ios-group-title">Cámaras</div>
                    ${cameraRows}
                    <button class="ios-row" onclick="App.newCamera()">
                        <span class="ios-icon">📷</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">Instalar cámara nueva con Eva</div>
                            <div class="ios-row-sub">Eva guía la instalación paso a paso</div>
                        </div>
                        <span class="ios-chevron">›</span>
                    </button>
                </div>

                <div class="ios-group">
                    <div class="ios-group-title">Protección</div>
                    ${vigilanceRows}
                </div>

                <div class="ios-group">
                    <div class="ios-group-title">Detección</div>
                    <button class="ios-row" onclick="App._openGridSettings()">
                        <span class="ios-icon">🔲</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">Área de análisis</div>
                            <div class="ios-row-sub">Cuántas imágenes revisa Eva por cámara</div>
                        </div>
                        <span class="ios-chevron">›</span>
                    </button>
                </div>

                <div class="ios-group">
                    <div class="ios-group-title">Eva</div>
                    <button class="ios-row" onclick="App._clearEvaChat()">
                        <span class="ios-icon">🧹</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">Limpiar conversación de Eva</div>
                            <div class="ios-row-sub">Borra el chat actual para que el saludo vuelva a aparecer limpio</div>
                        </div>
                        <span class="ios-value">Acción segura</span>
                    </button>
                </div>

                <div class="ios-group">
                    <div class="ios-group-title">Sistema</div>
                    <button class="ios-row" onclick="App._showApiConfig()">
                        <span class="ios-icon">🌐</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">URL del servidor</div>
                            <div class="ios-row-sub">${this.API}</div>
                        </div>
                        <span class="ios-chevron">›</span>
                    </button>
                    ${!this._isPWAInstalled() ? `
                    <button class="ios-row" onclick="App._installApp()">
                        <span class="ios-icon">📱</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">Instalar app</div>
                            <div class="ios-row-sub">Acceso rápido desde la pantalla de inicio</div>
                        </div>
                        <span class="ios-chevron">›</span>
                    </button>` : ''}
                </div>

                <div class="ios-group danger-group">
                    <button class="ios-row danger-row" onclick="App.logout()">
                        <span class="ios-icon">🚪</span>
                        <div class="ios-row-main">
                            <div class="ios-row-title">Cerrar sesión</div>
                            <div class="ios-row-sub">Salir de esta cuenta en este dispositivo</div>
                        </div>
                    </button>
                </div>
            </div>`;
    },

    _openGridSettings() {
        const c = document.getElementById('app-content');
        const cams = this._homeCams.length ? this._homeCams : [];
        const firstCam = cams[0]?.camera_id || '';
        this._gridSettingsCamId = this._gridSettingsCamId || firstCam;
        c.innerHTML = `
            <div style="display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--bg-secondary)">
                <div style="font-size:1.8rem">🔲</div>
                <div>
                    <div style="font-weight:600">Área de análisis</div>
                    <div class="meta">Configura cuántas imágenes revisa Eva por cámara</div>
                </div>
                <button class="modal-close" style="margin-left:auto" onclick="App.go('settings')">✕</button>
            </div>
            <div style="padding:16px">
                <div class="settings-section">
                    <div class="section-lbl">Cámara</div>
                    ${cams.length ? `<select id="settings-grid-camera" style="width:100%;padding:12px;border-radius:10px;border:1px solid var(--border);background:var(--bg-secondary);color:var(--text-primary);font-size:1rem;outline:none" onchange="App._setGridSettingsCamera(this.value)">
                        ${cams.map(cam => `<option value="${cam.camera_id}" ${cam.camera_id === this._gridSettingsCamId ? 'selected' : ''}>${cam.name || cam.camera_id} · ${cam.zone || 'sin zona'}</option>`).join('')}
                    </select>` : '<div style="padding:12px;color:var(--text-secondary);text-align:center">Sin cámaras configuradas</div>'}
                </div>
                <div class="settings-section">
                    <div class="section-lbl">Tamaño del área</div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap">
                        ${[8, 12, 16].map(size => `<button class="btn" style="width:auto;flex:1;min-width:90px" onclick="App._setCameraGridSize('${this._gridSettingsCamId || ''}', ${size})">${size} frames</button>`).join('')}
                    </div>
                    <p class="meta">Área más pequeña = análisis más rápido. Área más grande = más contexto visual.</p>
                </div>
                <div class="settings-section">
                    <div class="section-lbl">Vista previa</div>
                    <div id="settings-grid-preview" style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:12px">
                        <p class="meta">Cargando grid...</p>
                    </div>
                </div>
            </div>`;
        if (this._gridSettingsCamId) this._fetchSettingsGrid();
    },

    _setGridSettingsCamera(camId) {
        this._gridSettingsCamId = camId;
        this._fetchSettingsGrid();
    },

    async _setCameraGridSize(camId, size) {
        if (!camId) return;
        try {
            await apiFetch(`${this.API}/api/cameras/${camId}/grid-size?user_id=${this.userId}`, {
                method: 'PUT',
                body: JSON.stringify({ grid_size: size })
            });
            this._toast('Área actualizada', `Eva usará ${size} imágenes para esta cámara`, 'success');
            this._fetchSettingsGrid();
        } catch(e) {
            this._toast('Error', 'No se pudo actualizar el tamaño del área', 'danger');
        }
    },

    async _fetchSettingsGrid() {
        const camId = this._gridSettingsCamId || '';
        const el = document.getElementById('settings-grid-preview');
        if (!camId || !el) return;
        try {
            const r = await apiFetch(`${this.API}/grid/latest?partial=1&camera_id=${camId}&user_id=${this.userId}`);
            const d = await r.json();
            const frames = d.frames_used || 0;
            const size = d.grid_size || 16;
            const pct = Math.min(100, Math.round((frames / size) * 100));
            el.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <span class="meta">${frames}/${size} frames${d.partial ? ' (parcial)' : ''}</span>
                    <span class="badge ${frames >= size ? 'badge-alert' : 'badge-ok'}">${frames >= size ? '✓ ÁREA LISTA' : `${pct}%`}</span>
                </div>
                <div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>
                ${d.grid_b64 ? `<div style="background:#1a1a1a;border-radius:8px;overflow:hidden;margin-top:10px"><img src="data:image/jpeg;base64,${d.grid_b64}" style="width:100%;aspect-ratio:1/1;object-fit:contain;display:block"></div>` : '<p class="meta" style="padding:12px 0">Detección encuentra objetos → Eva revisa el área</p>'}
            `;
        } catch(e) {
            el.innerHTML = '<p class="meta">No se pudo cargar el área</p>';
        }
    },

    _showSubscription() {
        (async () => {
            let profile = {};
            let status = {};
            try {
                const [pr, sr] = await Promise.all([
                    apiFetch(`${this.API}/api/user/profile?user_id=${this.userId}`),
                    apiFetch(`${this.API}/api/user/status?user_id=${this.userId}`)
                ]);
                profile = await pr.json();
                status = await sr.json();
            } catch(e) { console.warn('[App] status fetch silent fail:', e); }

            const s = status || {};
            const planName = s.plan_name || s.plan || 'free';
            const daysLeft = s.days_left !== undefined ? s.days_left : '—';
            const planEnd = s.plan_end ? new Date(s.plan_end * 1000).toLocaleDateString('es-ES') : '—';
            const isExpired = s.status === 'expired' || s.status === 'suspended';
            const isWarning = s.status === 'warning' || s.status === 'grace' || s.expiring_soon;
            const isTrial = s.status === 'trial';
            const statusColor = isExpired ? 'var(--danger)' : isWarning ? 'var(--warning)' : 'var(--success)';
            const statusBg = isExpired ? 'rgba(255,69,58,.08)' : isWarning ? 'rgba(255,214,10,.08)' : 'rgba(48,209,88,.08)';
            const statusLabel = isExpired ? '🔴 Vencido' : isWarning ? '🟡 Por vencer' : isTrial ? '🟣 Prueba' : '🟢 Activo';
            const priceMonthly = s.billing ? s.billing.price_monthly : 0;
            const currency = s.billing ? s.billing.currency : 'USD';
            const limits = s.limits || {};
            const features = s.features || {};
            const bizTypeLabels = {
                retail: 'Colmado/Tienda', pharmacy: 'Farmacia', restaurant: 'Restaurante/Bar',
                agriculture: 'Finca/Agricultura', warehouse: 'Almacen/Bodega', office: 'Oficina',
                service: 'Taller/Servicio', home: 'Casa', other: 'Otro'
            };
            const schedule = profile.schedule || {};

            const modal = document.createElement('div');
            modal.style.cssText = 'position:fixed;inset:0;z-index:500;background:#000;display:flex;flex-direction:column';

            const header = document.createElement('div');
            header.style.cssText = 'display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--bg-secondary)';
            const closeBtn = document.createElement('button');
            closeBtn.textContent = '✕';
            closeBtn.style.cssText = 'background:none;border:none;color:var(--text-secondary);font-size:1.3rem;cursor:pointer;margin-right:12px';
            closeBtn.onclick = () => modal.remove();
            const title = document.createElement('span');
            title.style.fontWeight = '600';
            title.textContent = 'Mi cuenta';
            header.appendChild(closeBtn);
            header.appendChild(title);

            const content = document.createElement('div');
            content.style.cssText = 'flex:1;overflow-y:auto;padding:16px';

            const concernsLabel = (profile.main_concerns && profile.main_concerns.length > 0)
                ? profile.main_concerns.join(', ') : 'No especificado';
            const empCountLabel = { '1': '1 empleado', '10': '10 empleados', '100': '+100 empleados' }[profile.employee_count] || profile.employee_count || '-';
            const formatHour = (h24) => { const h = parseInt(h24); if (h === 0) return '12 AM'; if (h === 12) return '12 PM'; return (h > 12 ? h - 12 : h) + (h >= 12 ? ' PM' : ' AM'); };
            const scheduleDisplay = `${formatHour((schedule.open || '07:00').split(':')[0] + ':00')} a ${formatHour((schedule.close || '19:00').split(':')[0] + ':00')}`;

            // Payment history
            let paymentHistoryHTML = '';
            if (status.payments && status.payments.length > 0) {
                paymentHistoryHTML = '<div class="card" style="margin-top:12px"><div class="card-title">Historial de pagos</div>';
                status.payments.slice(-5).reverse().forEach(p => {
                    const pIcon = p.status === 'confirmed' ? '✅' : p.status === 'pending' ? '⏳' : '❌';
                    const pDate = p.created_at ? new Date(p.created_at * 1000).toLocaleDateString('es-ES') : '—';
                    paymentHistoryHTML += `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.85rem">
                        <span>${pIcon} <strong>$${p.amount || 0}</strong> ${p.method || ''}</span>
                        <span style="color:var(--text-secondary)">${pDate}</span>
                    </div>`;
                });
                paymentHistoryHTML += '</div>';
            }

            content.innerHTML = `
                <div class="card">
                    <div class="card-title">Datos del registro</div>
                    <div class="settings-row" onclick="App._editField('name', '${profile.name || ''}')"><span class="s-icon">👤</span><span style="flex:1">${profile.name || '-'}</span></div>
                    <div class="settings-row" onclick="App._editField('business_name', '${profile.business_name || ''}')"><span class="s-icon">🏢</span><span style="flex:1">${profile.business_name || '-'}</span></div>
                    <div class="settings-row" onclick="App._editField('business_type', '${profile.business_type || ''}', true, ['retail','pharmacy','restaurant','agriculture','warehouse','office','service','other'])"><span class="s-icon">📋</span><span style="flex:1">${bizTypeLabels[profile.business_type] || profile.business_type || '-'}</span></div>
                    <div class="settings-row" onclick="App._editField('employee_count', '${profile.employee_count || '1'}', true, ['1','10','100'])"><span class="s-icon">👥</span><span style="flex:1">${empCountLabel}</span></div>
                    <div class="settings-row" onclick="App._editSchedule()"><span class="s-icon">🕐</span><span style="flex:1">${scheduleDisplay}</span></div>
                    <div class="settings-row" onclick="App._editField('phone', '${profile.phone || ''}')"><span class="s-icon">📱</span><span style="flex:1">${profile.phone || 'No registrado'}</span></div>
                    <div class="settings-row"><span class="s-icon">🌐</span><span style="flex:1">${profile.email || '-'}</span></div>
                </div>

                <div class="card" style="text-align:center;padding:20px;background:${statusBg};border-color:${statusColor}">
                    <div style="font-size:1.3rem;font-weight:700;margin-bottom:4px">${statusLabel}</div>
                    <div style="font-size:1.5rem;font-weight:800">${planName}</div>
                    <div style="font-size:2rem;font-weight:800;color:var(--accent);margin:8px 0">${priceMonthly > 0 ? '$' + priceMonthly + '<span style="font-size:1rem;font-weight:400;color:var(--text-secondary)">/mes</span>' : 'Gratis'}</div>
                    <div class="meta">${daysLeft !== '—' ? daysLeft + ' días restantes · Vence: ' + planEnd : 'Sin vencimiento'}</div>
                    <div style="margin-top:12px;font-size:0.85rem;color:var(--text-secondary)">
                        📷 ${limits.camera_count || 0}/${limits.max_cameras || 1} cámaras ·
                        💾 ${limits.used_mb > 1024 ? (limits.used_mb/1024).toFixed(1) + ' GB' : (limits.used_mb || 0) + ' MB'}/${limits.max_storage_gb || 5} GB
                        ${features.grid_detection ? ' · 🔲 Grid' : ''}
                        ${features.multi_zone ? ' · 🌐 Multi-zona' : ''}
                    </div>
                    ${isExpired ? '<div style="margin-top:12px;color:var(--danger);font-weight:600">⚠️ Tu plan venció. Renueva para seguir usando OjoIA.</div>' : ''}
                    ${s.grace_days_left ? '<div style="margin-top:12px;color:var(--warning);font-weight:600">⏰ Período de gracia: ' + s.grace_days_left + ' días restantes</div>' : ''}
                    ${s.reason ? '<div style="margin-top:8px;color:var(--danger);font-size:0.85rem">' + s.reason + '</div>' : ''}
                </div>

                <div class="card">
                    <div class="card-title">Pagar por transferencia</div>
                    <p class="meta" style="margin-bottom:12px">Transfiere ${priceMonthly > 0 ? '$' + priceMonthly : 'el monto de tu plan'} a:</p>
                    <div style="background:var(--bg-tertiary);border-radius:8px;padding:12px;font-size:.9rem;line-height:1.8">
                        <strong>Banco:</strong> BanReservas<br><strong>Cuenta:</strong> Solicitar al administrador<br><strong>Nombre:</strong> OjoIA SRL
                    </div>
                    <div style="margin-top:12px">
                        <label style="display:block;font-size:.85rem;color:var(--text-secondary);margin-bottom:8px">Subir comprobante:</label>
                        <input type="file" id="receipt-file" accept="image/*,.pdf" style="width:100%;padding:8px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary)">
                        <button class="btn" style="margin-top:10px" onclick="App._uploadReceipt()">📤 Enviar comprobante</button>
                    </div>
                </div>
                ${paymentHistoryHTML}
                <p class="meta" style="text-align:center;padding:8px">Tu suscripción se activa en menos de 24 horas después de verificar el pago.</p>
            </div>`;

            modal.appendChild(header);
            modal.appendChild(content);
            document.body.appendChild(modal);
        })();
    },

    async _editField(field, current, isSelect=false, options=null) {
        let val;
        if (isSelect) {
            val = prompt(`Selecciona ${field === 'business_type' ? 'tipo de negocio' : field}:`, current);
            if (val !== null && !options.includes(val)) return;
        } else {
            val = prompt(`Edita ${field}:`, current);
        }
        if (val === null) return;
        const payload = { user_id: this.userId };
        payload[field] = val;
        try {
            const r = await apiFetch(`${this.API}/api/user/profile`, { method: 'POST', body: JSON.stringify(payload) });
            const d = await r.json();
            if (d.success) { this._toast('Actualizado', '', 'success'); App._showSubscription(); }
            else this._toast('Error', d.error || 'Intenta de nuevo', 'danger');
        } catch(e) { this._toast('Error', 'No se pudo guardar', 'danger'); }
    },

    async _editSchedule() {
        const open = prompt('Hora apertura (HH:MM):', '07:00');
        if (open === null) return;
        const close = prompt('Hora cierre (HH:MM):', '19:00');
        if (close === null) return;
        try {
            const r = await apiFetch(`${this.API}/api/user/profile`, {
                method: 'POST',
                body: JSON.stringify({ user_id: this.userId, schedule_open: open, schedule_close: close })
            });
            const d = await r.json();
            if (d.success) { this._toast('Actualizado', '', 'success'); App._showSubscription(); }
            else this._toast('Error', d.error || 'Intenta de nuevo', 'danger');
        } catch(e) { this._toast('Error', 'No se pudo guardar', 'danger'); }
    },

    async _uploadReceipt() {
        const file = document.getElementById('receipt-file')?.files[0];
        if (!file) { this._toast('', 'Selecciona una imagen primero', 'warning'); return; }
        try {
            const fd = new FormData();
            fd.append('receipt', file);
            fd.append('user_id', this.userId);
            fd.append('amount', '');
            fd.append('method', 'transfer');
            fd.append('reference', '');
            fd.append('notes', 'Comprobante subido desde app');
            const r = await fetch(`${this.API}/api/payment/upload`, { method: 'POST', body: fd });
            const d = await r.json();
            if (d.success) this._toast('¡Comprobante recibido!', 'Lo revisaremos en menos de 24 horas. ID: ' + d.payment_id, 'success');
            else this._toast('Error', d.error || 'Intenta de nuevo', 'danger');
        } catch(e) { this._toast('Error', 'No se pudo enviar el comprobante', 'danger'); }
    },

    _clearEvaChat() {
        const ok = confirm('¿Quieres limpiar la conversación de Eva?\n\nEsto borrará el chat actual y hará que Eva vuelva a mostrar el saludo inicial limpio la próxima vez que entres.');
        if (!ok) return;
        if (typeof EvaChat !== 'undefined' && EvaChat.clearChat) {
            EvaChat.clearChat(false);
        } else {
            localStorage.removeItem(`eva_history_${this.userId}`);
            localStorage.removeItem(`eva_session_${this.userId}`);
            this.go('eva');
        }
        this._toast('Eva limpia', 'La conversación se reinició', 'success');
    },

    _showApiConfig() {
        const cur = this.API;
        const url = prompt('URL del servidor:', cur);
        if (url !== null && url.trim()) {
            this.API = url.trim();
            localStorage.setItem('ojoia_api_url', this.API);
            this._toast('URL actualizada', this.API, 'success');
            this._pageSettings(document.getElementById('app-content'));
        }
    },

    // ── EVA: Asistente de configuración de cámara ────────────────
    
    newCamera() {
        this._evaCamId = '';
        this._evaSession = '';
        this._evaMode = 'new';
        this._evaMsgs = [];
        this._startEvaChat();
    },
    
    openEva(camId) {
        if (camId) {
            this._evaCamId = camId;
            this._evaMode = 'edit';
            this._runAutoConfig(camId);
        } else {
            this.newCamera();
        }
    },

    async _startEvaChat() {
        const c = document.getElementById('app-content');
        
        // Verificar que el usuario esté logueado
        if (!this.userId) {
            c.innerHTML = 
                '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                    '<div style="font-size:3rem;margin-bottom:16px;">🔒</div>' +
                    '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Inicia sesión primero</div>' +
                    '<div style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:24px;">Necesitas iniciar sesión para configurar una cámara</div>' +
                    '<button class="btn" onclick="App.go(\'login\')">Iniciar sesión</button>' +
                '</div>';
            return;
        }
        
        c.innerHTML = this._renderEvaChatScreen('Conectando con Eva...');
        
        try {
            const r = await fetch(`${this.API}/config/chat`, {
                method: 'POST',
                mode: 'cors',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: this.userId,
                    message: '__new_camera__',
                    session_id: '',
                    cam_id: '',
                    include_frame: false
                })
            });
            const d = await r.json();
            this._evaSession = d.session_id || '';
            this._evaMsgs = d.messages || [{ role: 'assistant', content: d.response || d.message || '¡Hola! Soy Eva. Voy a ayudarte a configurar tu nueva cámara. ¿Empezamos?' }];
            this._renderEvaChat();
        } catch(e) {
            c.innerHTML = 
                '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                    '<div style="font-size:3rem;margin-bottom:16px;">❌</div>' +
                    '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Error de conexión</div>' +
                    '<button class="btn" style="margin-top:16px" onclick="App.newCamera()">Reintentar</button>' +
                    '<button class="btn btn-ghost" style="margin-top:8px" onclick="App.go(\'cameras\')">Volver</button>' +
                '</div>';
        }
    },

    _renderEvaChatScreen(status) {
        return '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
            '<div style="font-size:3rem;margin-bottom:16px;">🤖</div>' +
            '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">' + status + '</div>' +
            '<div class="skeleton" style="width:200px;height:200px;margin-top:24px;border-radius:12px;"></div>' +
        '</div>';
    },

    _renderEvaChat() {
        const c = document.getElementById('app-content');
        
        let chatHtml = '';
        const msgs = this._evaMsgs;
        
        for (let i = 0; i < msgs.length; i++) {
            const msg = msgs[i];
            const isUser = msg.role === 'user';
            
            const bg = isUser ? 'var(--bg-tertiary)' : 'var(--bg-secondary)';
            const align = isUser ? 'flex-end' : 'flex-start';
            const borderRadius = isUser ? '12px 12px 4px 12px' : '12px 12px 12px 4px';
            const text = (msg.content || '').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
            let formatted = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\*(.+?)\*/g, '<em>$1</em>');
            
            // Mostrar imagen si el mensaje tiene image_url o image_b64
            let imgHtml = '';
            if (msg.image_url && msg.image_url.length > 10) {
                imgHtml = '<div style="margin-top:10px;margin-bottom:6px;"><img src="' + msg.image_url + '" style="width:100%;max-height:300px;object-fit:contain;border-radius:8px;background:#0a0a0a;cursor:pointer;" onclick="this.style.maxHeight=this.style.maxHeight===\'300px\'?\'none\':\'300px\'" title="Toca para ampliar"></div>';
            }
            if (msg.image_b64 && msg.image_b64.length > 10) {
                imgHtml = '<div style="margin-top:10px;margin-bottom:6px;"><img src="data:image/jpeg;base64,' + msg.image_b64 + '" style="width:100%;max-height:300px;object-fit:contain;border-radius:8px;background:#0a0a0a;cursor:pointer;" onclick="this.style.maxHeight=this.style.maxHeight===\'300px\'?\'none\':\'300px\'" title="Toca para ampliar"></div>';
            }
            
            chatHtml += '<div style="display:flex;justify-content:' + align + ';margin-bottom:10px;">' +
                '<div style="max-width:85%;background:' + bg + ';border-radius:' + borderRadius + ';padding:12px 16px;font-size:0.92rem;line-height:1.5;">' +
                formatted + imgHtml + '</div></div>';
        }
        
        let quickReplies = '';
        const lastMsg = msgs[msgs.length - 1];
        if (lastMsg && lastMsg.buttons) {
            quickReplies = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">';
            lastMsg.buttons.forEach(btn => {
                quickReplies += '<button class="btn btn-sm" style="border-radius:20px;" onclick="App._evaSendMessage(this.getAttribute(\'data-msg\'))" data-msg="' + (btn.value || btn.label) + '">' + btn.label + '</button>';
            });
            quickReplies += '</div>';
        }
        
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;height:100%;min-height:0;">' +
                '<div style="flex-shrink:0;display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);">' +
                    '<div style="font-size:1.8rem;">🤖</div>' +
                    '<div><div style="font-weight:600;font-size:1rem;">Eva</div><div style="font-size:0.75rem;color:var(--text-secondary);">Asistente de seguridad</div></div>' +
                '</div>' +
                '<div id="eva-chat-msgs" style="flex:1;overflow-y:auto;overflow-x:hidden;padding:12px 16px;min-height:0;">' + chatHtml + '</div>' +
                '<div style="flex-shrink:0;padding:12px 16px;border-top:1px solid var(--border);background:var(--bg);">' +
                    quickReplies +
                    '<div style="display:flex;gap:8px;">' +
                        '<input id="eva-input" placeholder="Escribe aquí..." style="flex:1;padding:12px 16px;border:1px solid var(--border);border-radius:24px;font-size:0.95rem;background:var(--bg-secondary);color:var(--text-primary);outline:none;" onkeydown="if(event.key===\'Enter\'){App._evaSendInput()}">' +
                        '<button class="btn" style="border-radius:50%;width:44px;height:44px;padding:0;display:flex;align-items:center;justify-content:center;" onclick="App._evaSendInput()">➤</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
        
        const chatDiv = document.getElementById('eva-chat-msgs');
        if (chatDiv) {
            chatDiv.classList.add('scroll-smooth');
            chatDiv.style.scrollBehavior = 'smooth';
            setTimeout(() => {
                chatDiv.scrollTop = chatDiv.scrollHeight;
            }, 300);
        }
        setTimeout(() => {
            const input = document.getElementById('eva-input');
            if (input) input.focus();
        }, 100);
    },

    async _evaSendInput() {
        const input = document.getElementById('eva-input');
        const msg = input?.value?.trim();
        if (!msg) return;
        input.value = '';
        await this._evaSendMessage(msg);
    },

    async _evaSendMessage(msg) {
        this._evaMsgs.push({ role: 'user', content: msg });
        this._renderEvaChat();
        
        try {
            const r = await fetch(`${this.API}/config/chat`, {
                method: 'POST',
                mode: 'cors',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: this.userId,
                    message: msg,
                    session_id: this._evaSession,
                    cam_id: this._evaCamId || '',
                    include_frame: true
                })
            });
            if (!r.ok) {
                throw new Error('HTTP ' + r.status);
            }
            const d = await r.json();
            this._evaSession = d.session_id || this._evaSession;
            if (d.messages) {
                this._evaMsgs = d.messages;
            } else {
                this._evaMsgs.push({ role: 'assistant', content: d.response || d.message || '...' });
            }
            this._renderEvaChat();
        } catch(e) {
            console.error('EVA CHAT ERROR:', e);
            this._evaMsgs.push({ role: 'assistant', content: 'Error de conexión. Intenta de nuevo.' });
            this._renderEvaChat();
        }
    },

    async _runAutoConfig(camId) {
        const c = document.getElementById('app-content');
        this._evaCamId = camId || '';
        this._evaMode = 'edit';
        
        c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
            '<div style="font-size:3rem;margin-bottom:16px;">🤖</div>' +
            '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Eva está analizando tu cámara...</div>' +
            '<div style="color:var(--text-secondary);font-size:0.9rem;">Esto toma unos segundos</div>' +
            '<div class="skeleton" style="width:200px;height:200px;margin-top:24px;border-radius:12px;"></div>' +
        '</div>';
        
        try {
            const r = await apiFetch(`${this.API}/config/auto_config`, {
                method: 'POST',
                body: JSON.stringify({ user_id: this.userId, camera_id: camId || '' })
            });
            const d = await r.json();
            
            if (!d.ready) {
                this._showManualCameraConfig(camId);
                return;
            }
            
            this._showEvaConfig(d);
        } catch(e) {
            c.innerHTML = 
                '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                    '<div style="font-size:3rem;margin-bottom:16px;">❌</div>' +
                    '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Error de conexión</div>' +
                    '<button class="btn" style="margin-top:16px" onclick="App._runAutoConfig(\'' + camId + '\')">Reintentar</button>' +
                    '<button class="btn btn-ghost" style="margin-top:8px" onclick="App.go(\'cameras\')">Volver</button>' +
                '</div>';
        }
    },

    _showManualCameraConfig(camId) {
        const c = document.getElementById('app-content');
        c.innerHTML = `
            <div style="display:flex;flex-direction:column;height:100%;min-height:0;">
                <div style="flex-shrink:0;display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);">
                    <div style="font-size:1.8rem;">📷</div>
                    <div><div style="font-weight:600;font-size:1rem;">Configura la cámara</div><div style="font-size:0.75rem;color:var(--text-secondary);">Sin imagen aún, pero puedes dejarla lista</div></div>
                </div>
                <div style="flex:1;overflow-y:auto;padding:16px;min-height:0;">
                    <label style="font-size:0.8rem;color:var(--text-secondary);display:block;margin-bottom:4px;">Zona</label>
                    <input id="manual-zone" placeholder="caja, almacén, corral, entrada..." style="width:100%;padding:12px;border:1px solid var(--border);border-radius:10px;font-size:1rem;background:var(--bg-secondary);color:var(--text-primary);margin-bottom:14px;">
                    <label style="font-size:0.8rem;color:var(--text-secondary);display:block;margin-bottom:4px;">Comportamiento 1</label>
                    <input id="manual-rule-1" value="Alerta si entra alguien fuera de horario" style="width:100%;padding:12px;border:1px solid var(--border);border-radius:10px;font-size:1rem;background:var(--bg-secondary);color:var(--text-primary);margin-bottom:10px;">
                    <label style="font-size:0.8rem;color:var(--text-secondary);display:block;margin-bottom:4px;">Comportamiento 2</label>
                    <input id="manual-rule-2" value="Alerta si hay movimiento sospechoso en la zona" style="width:100%;padding:12px;border:1px solid var(--border);border-radius:10px;font-size:1rem;background:var(--bg-secondary);color:var(--text-primary);margin-bottom:10px;">
                    <label style="font-size:0.8rem;color:var(--text-secondary);display:block;margin-bottom:4px;">Comportamiento 3</label>
                    <input id="manual-rule-3" value="Alerta si alguien manipula objetos importantes" style="width:100%;padding:12px;border:1px solid var(--border);border-radius:10px;font-size:1rem;background:var(--bg-secondary);color:var(--text-primary);margin-bottom:10px;">
                    <p class="meta">Cuando la cámara empiece a enviar imagen, Eva ajustará la configuración automáticamente.</p>
                </div>
                <div style="flex-shrink:0;padding:12px 16px;border-top:1px solid var(--border);background:var(--bg);">
                    <div style="display:flex;gap:10px;">
                        <button class="btn" style="flex:1;background:var(--success);padding:13px;font-size:0.95rem;" onclick="App._evaSaveManual('${camId || ''}')">✅ Guardar</button>
                        <button class="btn btn-outline" style="flex:1;padding:13px;font-size:0.95rem;" onclick="App.openEva('${camId || ''}')">✏️ Con Eva</button>
                    </div>
                    <button class="btn btn-ghost" style="width:100%;margin-top:8px;font-size:0.8rem;" onclick="App.go('cameras')">Cancelar</button>
                </div>
            </div>`;
    },

    async _evaSaveManual(camId) {
        const zone = (document.getElementById('manual-zone')?.value || 'zona principal').trim();
        const rules = [];
        ['manual-rule-1', 'manual-rule-2', 'manual-rule-3'].forEach(id => {
            const val = document.getElementById(id)?.value.trim();
            if (val) rules.push(val);
        });
        if (!zone || rules.length === 0) {
            this._toast('Faltan datos', 'Escribe zona y al menos una regla', 'warning');
            return;
        }
        this._evaCamId = camId || '';
        this._evaConfig = {
            camera_id: camId || '',
            zone,
            scanner_question: `Is there any unauthorized person or suspicious movement in ${zone}?`,
            system_prompt: `Security camera covering ${zone}. Alert on observable security risks: unauthorized presence, suspicious movement, objects being moved, or activity outside business hours.`,
            schedule: {open: '07:00', close: '19:00'},
            yolo_triggers: ['person'],
            grid_size: 12
        };
        await this._evaSave(rules, zone);
    },

    _showEvaConfig(config) {
        const c = document.getElementById('app-content');
        const imgSrc = config.image_b64 ? 'data:image/jpeg;base64,' + config.image_b64 : '';
        const rules = config.vigilance?.normal_mode?.alert_behaviors || config.rules || config.rules_es || [];
        const evaMsg = (config.eva_message || 'Configuración para tu cámara:').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        
        this._evaPendingRules = rules;
        
        let rulesHtml = '';
        rules.forEach((r, i) => {
            const text = (typeof r === 'object' ? (r.es || r.en || '') : r).replace(/</g,'&lt;').replace(/>/g,'&gt;');
            rulesHtml += '<div style="padding:10px 14px;background:var(--bg-secondary);border-radius:8px;margin-bottom:8px;display:flex;align-items:flex-start;gap:10px;">' +
                '<span style="font-size:1.1rem;flex-shrink:0;">✓</span>' +
                '<span style="flex:1;font-size:0.95rem;">' + text + '</span>' +
                '</div>';
        });
        
        const imgHtml = imgSrc ? '<img src="' + imgSrc + '" style="width:100%;max-height:180px;object-fit:contain;border-radius:10px;margin-bottom:12px;background:var(--bg-secondary);">' : '';
        
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;height:100%;min-height:0;">' +
                '<div style="flex-shrink:0;display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);">' +
                    '<div style="font-size:1.8rem;">🤖</div>' +
                    '<div><div style="font-weight:600;font-size:1rem;">Eva</div><div style="font-size:0.75rem;color:var(--text-secondary);">Asistente de seguridad</div></div>' +
                '</div>' +
                '<div style="flex:1;overflow-y:auto;overflow-x:hidden;padding:12px 16px;min-height:0;">' +
                    imgHtml +
                    '<div style="font-size:0.95rem;font-weight:600;margin-bottom:10px;">' + evaMsg + '</div>' +
                    '<div style="font-size:.8rem;color:var(--text-secondary);margin-bottom:8px;">Comportamientos que activan alerta:</div>' +
                    '<div>' + rulesHtml + '</div>' +
                '</div>' +
                '<div style="flex-shrink:0;padding:12px 16px;border-top:1px solid var(--border);background:var(--bg);">' +
                    '<div style="display:flex;gap:10px;">' +
                        '<button class="btn" style="flex:1;background:var(--success);padding:13px;font-size:0.95rem;" onclick="App._evaSave(App._evaPendingRules)">✅ Listo, guardar</button>' +
                        '<button class="btn btn-outline" style="flex:1;padding:13px;font-size:0.95rem;" onclick="App._evaShowAdjust(App._evaPendingRules)">🛡️ Ajustar protección</button>' +
                    '</div>' +
                    '<button class="btn btn-ghost" style="width:100%;margin-top:8px;font-size:0.8rem;" onclick="App.go(\'cameras\')">Cancelar</button>' +
                '</div>' +
            '</div>';
        
        this._evaConfig = config;
    },

    async _evaSave(rules, zone) {
        const c = document.getElementById('app-content');
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                '<div style="font-size:3rem;margin-bottom:16px;">⏳</div>' +
                '<div style="font-size:1.1rem;font-weight:600;">Guardando configuración...</div>' +
            '</div>';
        
        const cfg = this._evaConfig || {};
        try {
            const r = await apiFetch(`${this.API}/config/camera_confirm`, {
                method: 'POST',
                body: JSON.stringify({
                    user_id: this.userId,
                    camera_id: this._evaCamId || '',
                    zone: zone || cfg.zone || 'zona principal',
                    rules_es: rules,
                    rules_en: (cfg.rules || []).map(r => typeof r === 'object' ? (r.en || r.es || '') : r),
                    scanner_question: cfg.scanner_question || '',
                    system_prompt: cfg.system_prompt || '',
                    schedule: cfg.schedule || {open:'07:00',close:'19:00'},
                    yolo_triggers: cfg.yolo_triggers || ['person'],
                    grid_size: cfg.grid_size || 12,
                })
            });
            const d = await r.json();
            
            if (d.success) {
                c.innerHTML = 
                    '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                        '<div style="font-size:3rem;margin-bottom:16px;">🎉</div>' +
                        '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">¡Cámara configurada!</div>' +
                        '<div style="color:var(--text-secondary);">Ya está protegiendo tu negocio</div>' +
                    '</div>';
                setTimeout(() => this.go('home'), 2000);
            } else {
                throw new Error('Save failed');
            }
        } catch(e) {
            c.innerHTML = 
                '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                    '<div style="font-size:3rem;margin-bottom:16px;">❌</div>' +
                    '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Error al guardar</div>' +
                    '<button class="btn" style="margin-top:16px" onclick="App._evaShowAdjust(App._evaPendingRules)">Reintentar</button>' +
                '</div>';
        }
    },
    
    _evaShowAdjust(rules) {
        const c = document.getElementById('app-content');
        const cfg = this._evaConfig || {};
        
        let inputsHtml = '';
        const ruleList = Array.isArray(rules) ? rules : (cfg.rules || []);
        ruleList.forEach((r, i) => {
            const text = typeof r === 'object' ? (r.es || '') : r;
            inputsHtml += 
                `<div style="margin-bottom:14px;">` +
                    `<label style="font-size:0.8rem;color:var(--text-secondary);display:block;margin-bottom:4px;">Comportamiento ${i+1}</label>` +
                    `<input class="eva-rule-input" data-idx="${i}" value="${text.replace(/"/g, '&quot;')}" ` +
                    `style="width:100%;padding:10px 14px;border:1px solid var(--border);border-radius:8px;font-size:0.95rem;background:var(--bg-secondary);color:var(--text-primary);">` +
                `</div>`;
        });
        
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;height:100%;min-height:0;">' +
                '<div style="flex-shrink:0;padding:12px 16px;border-bottom:1px solid var(--border);">' +
                    '<div style="font-weight:600;font-size:1rem;">🛡️ Ajusta la protección</div>' +
                    '<div style="color:var(--text-secondary);font-size:0.8rem;margin-top:2px;">Edita como prefieras</div>' +
                '</div>' +
                '<div style="flex:1;overflow-y:auto;padding:12px 16px;min-height:0;">' + inputsHtml + '</div>' +
                '<div style="flex-shrink:0;padding:12px 16px;border-top:1px solid var(--border);background:var(--bg);">' +
                    '<div style="display:flex;gap:10px;">' +
                        '<button class="btn" style="flex:1;background:var(--success);padding:13px;" onclick="App._evaSaveFromInputs()">✅ Guardar</button>' +
                        '<button class="btn btn-outline" style="flex:1;padding:13px;" onclick="App.openEva(\'' + (this._evaCamId || '') + '\')">Cancelar</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
    },
    
    _evaSaveFromInputs() {
        const inputs = document.querySelectorAll('.eva-rule-input');
        const rules = [];
        inputs.forEach(inp => {
            const val = inp.value.trim();
            if (val) rules.push(val);
        });
        if (rules.length === 0) {
            alert('Escribe al menos una regla');
            return;
        }
        this._evaSave(rules, this._evaConfig?.zone || 'zona principal');
    },

    // ── VIEWER ───────────────────────────────────────────────
    openViewer(camId, camName) {
        const modal = document.getElementById('viewer-modal');
        const body = document.getElementById('viewer-body');
        document.getElementById('viewer-title').textContent = camName || 'Ojo';
        modal.style.display = 'flex';
        this._viewerCamId = camId;
        body.innerHTML = `<div class="ojo-placeholder">Cargando...</div>`;
        this._fetchFrame('viewer-body');
        this._fetchViewerGrid();
        if (this._polls.viewer) clearInterval(this._polls.viewer);
        this._polls.viewer = setInterval(() => { this._fetchFrame('viewer-body'); this._fetchViewerGrid(); }, 2000);
    },

    async _fetchViewerGrid() {
        if (!this._viewerCamId) return;
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${this._viewerCamId}/grid`);
            const d = await r.json();
            let gc = document.getElementById('viewer-grid');
            if (!gc) { gc = document.createElement('div'); gc.id = 'viewer-grid'; const vb = document.getElementById('viewer-body'); if (vb) vb.appendChild(gc); }
            if (d.active && d.grid_b64) {
                const f = d.frames || 0;
                gc.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-title">🔲 Área - ${f}/16 <span class="badge ${f >= 16 ? 'badge-alert' : 'badge-ok'}">${f >= 16 ? 'LLENO' : f}</span></div>
                    <div class="prog-bar"><div class="prog-fill" style="width:${Math.round(f/16*100)}%"></div></div>
                    <img src="data:image/jpeg;base64,${d.grid_b64}" style="width:100%;border-radius:8px;display:block;margin-top:8px"></div>`;
            }
        } catch(e) { console.warn('[App] zones preview silent fail:', e); }
    },

    async deleteCamera(camId, camName) {
        if (!confirm(`¿Eliminar "${camName}"? Esta acción no se puede deshacer.`)) return;
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}?user_id=${this.userId}`, { method: 'DELETE' });
            const d = await r.json();
            if (d.success) {
                this.go('cameras');
            } else {
                alert('Error: ' + (d.detail || 'No se pudo eliminar'));
            }
        } catch(e) {
            alert('Error de conexión');
        }
    },

    closeViewer() {
        document.getElementById('viewer-modal').style.display = 'none';
        this._viewerCamId = null;
        if (this._polls.viewer) { clearInterval(this._polls.viewer); delete this._polls.viewer; }
    },

    _skeleton() { return `<div class="skeleton"></div><div class="skeleton" style="height:100px;margin-top:8px"></div>`; },
    _relTime(ts) { const d = (Date.now() - new Date(ts).getTime()) / 1000; if (d < 60) return 'hace un momento'; if (d < 3600) return `hace ${Math.floor(d/60)} min`; if (d < 86400) return `hace ${Math.floor(d/3600)}h`; return `hace ${Math.floor(d/86400)} días`; },

    // ── ZONE DRAWER ──────────────────────────────────────────────
    _zoneDrawerState: null,

    async _openZoneDrawer(camId) {
        this._zoneDrawerCamId = camId;
        this._zoneEditMode = false; // 'draw', 'edit', or false

        // Fetch existing zones
        let zones = [];
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/zones?user_id=${this.userId}`);
            const d = await r.json();
            zones = d.zones || [];
        } catch(e) {
            console.warn('Error fetching zones:', e);
        }
        this._zoneZones = zones;

        // Fetch zone types
        let zoneTypes = [];
        try {
            const r = await apiFetch(`${this.API}/api/zone-types`);
            const d = await r.json();
            zoneTypes = d.zone_types || [];
        } catch(e) {
            // Use defaults
            zoneTypes = camera_zones ? camera_zones.get_zone_types() : [];
        }
        this._zoneTypes = zoneTypes.length ? zoneTypes : [
            {id: "entrance", name: "Entrada", icon: "🚪"},
            {id: "cashier", name: "Caja / Cobro", icon: "💰"},
            {id: "dining", name: "Comedor", icon: "🍽️"},
            {id: "hall", name: "Sala / Hall", icon: "🏠"},
            {id: "office", name: "Oficina", icon: "💼"},
            {id: "restricted", name: "Área restringida", icon: "🚫"},
            {id: "other", name: "Otra", icon: "📍"}
        ];

        // Build the drawer HTML
        const cam = this._currentCamConfig || {};
        const frameUrl = `${this.API}/frames/latest-raw.jpg?camera_id=${camId}&user_id=${this.userId}&_=${Date.now()}`;

        const colorPalette = ['#ff0a4e', '#1dd132', '#ff9800', '#2196f3', '#e91e63', '#9c27b0', '#00bcd4', '#ffc107'];
        const typeOptions = this._zoneTypes.map(t => `<option value="${t.id}" style="color:${this._getZoneColor(t.id)}">${t.icon} ${t.name}</option>`).join('');
        const colorSwatches = colorPalette.map(c => `<div class="zone-color-swatch" style="background:${c}" data-color="${c}" onclick="App._selectZoneColor('${c}', this)"></div>`).join('');

        // Get current frame dimensions
        const img = new Image();
        img.onload = () => {
            this._zoneFrameW = img.naturalWidth;
            this._zoneFrameH = img.naturalHeight;
        };
        img.src = frameUrl;

        const html = `<div class="zone-drawer-overlay" id="zone-drawer-overlay">
            <div class="zone-drawer">
                <div class="zone-drawer-header">
                    <h3>🗺️ Zonas de ${cam.name || camId}</h3>
                    <button class="zone-drawer-close" onclick="App._closeZoneDrawer()">✕</button>
                </div>
                <div class="zone-drawer-body">
                    <!-- Frame with canvas overlay -->
                    <div class="zone-drawer-frame" id="zone-drawer-frame">
                        <img src="${frameUrl}" id="zone-frame-img" style="width:100%;height:100%;object-fit:contain;display:block">
                        <canvas id="zone-canvas" width="640" height="360"></canvas>
                    </div>

                    <!-- Drawing tools -->
                    <div class="zone-drawer-tools" id="zone-tools">
                        <button class="zone-tool-btn" data-tool="draw" onclick="App._setZoneTool('draw', this)">✏️ Dibujar zona</button>
                        <button class="zone-tool-btn" data-tool="edit" onclick="App._setZoneTool('edit', this)">✋ Mover / Redimensionar</button>
                        <button class="zone-tool-btn" data-tool="delete" onclick="App._setZoneTool('delete', this)">🗑️ Borrar zona</button>
                    </div>

                    <!-- Zone type selection -->
                    <select class="zone-type-select" id="zone-type-select">
                        ${typeOptions}
                    </select>

                    <!-- Zone name input -->
                    <input type="text" class="zone-name-input" id="zone-name-input" placeholder="Nombre de la zona (ej: Entrada principal)">

                    <!-- Color palette -->
                    <div class="zone-color-palette" id="zone-color-palette">
                        ${colorSwatches}
                    </div>

                    <!-- Zone list -->
                    <div id="zone-list-container" style="margin-top:16px">
                        ${this._renderZoneList(zones, this._zoneTypes)}
                    </div>
                </div>
                <div class="zone-drawer-footer">
                    <button class="btn btn-sm btn-outline" onclick="App._suggestZones('${this._escAttr(camId)}', this)">✨ Eva sugiere zonas</button>
                    <button class="btn btn-sm btn-outline" onclick="App._clearAllZones('${this._escAttr(camId)}')">🗑️ Borrar todas</button>
                    <button class="btn btn-sm btn-primary" style="flex:1" onclick="App._saveZoneDrawer('${this._escAttr(camId)}')">💾 Guardar</button>
                </div>
            </div>
        </div>`;

        // Add to body
        document.body.insertAdjacentHTML('beforeend', html);
        this._zoneEditMode = 'draw';

        // Set up canvas
        const canvas = document.getElementById('zone-canvas');
        const imgEl = document.getElementById('zone-frame-img');
        const frameEl = document.getElementById('zone-drawer-frame');
        
        if (imgEl.complete && imgEl.naturalWidth) {
            this._setupZoneCanvas(canvas, imgEl, frameEl);
        } else {
            imgEl.onload = () => {
                this._setupZoneCanvas(canvas, imgEl, frameEl);
                this._redrawZones();
            };
        }

        // Select first color by default
        this._zoneSelectedColor = colorPalette[0];
        this._updateZoneColorSelection(canvas);

        // Draw existing zones
        this._redrawZones();
    },

    _setupZoneCanvas(canvas, imgEl, frameEl) {
        if (!canvas || !imgEl) return;
        const dpr = window.devicePixelRatio || 1;
        const cw = imgEl.clientWidth || imgEl.naturalWidth;
        const ch = imgEl.clientHeight || imgEl.naturalHeight;
        canvas.width = Math.max(1, Math.floor(cw * dpr));
        canvas.height = Math.max(1, Math.floor(ch * dpr));
        canvas.style.width = `${cw}px`;
        canvas.style.height = `${ch}px`;

        const scale = Math.min(cw / imgEl.naturalWidth, ch / imgEl.naturalHeight);
        const drawW = imgEl.naturalWidth * scale;
        const drawH = imgEl.naturalHeight * scale;
        const offsetX = (cw - drawW) / 2;
        const offsetY = (ch - drawH) / 2;

        this._zoneCanvasCtx = canvas.getContext('2d');
        this._zoneCanvasCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
        this._zoneDrawScale = scale;
        this._zoneOffsetX = offsetX;
        this._zoneOffsetY = offsetY;

        // Set up mouse handlers
        this._setupZoneCanvasHandlers(canvas, imgEl);
    },

    _setupZoneCanvasHandlers(canvas, imgEl) {
        let isDrawing = false;
        let startX = 0, startY = 0;
        let currentRect = null;

        // F0.3: estado de edición (mover / redimensionar / borrar por clic)
        let editAction = null;      // 'move' | 'resize'
        let editZone = null;        // zona objetivo
        let editStartCoords = null; // coords relativas al iniciar edición
        let editStartPos = null;    // posición del mouse al iniciar (px canvas)

        const getRelativePos = (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
            const y = (e.touches ? e.touches[0].clientY : e.clientY) - rect.top;
            return { x, y };
        };

        // Convierte px de canvas → coords relativas 0-1 sobre la imagen
        const pxToRel = (x, y) => {
            const nw = imgEl.naturalWidth, nh = imgEl.naturalHeight;
            const scale = this._zoneDrawScale || 1;
            const ox = this._zoneOffsetX || 0, oy = this._zoneOffsetY || 0;
            return {
                x: (x - ox) / (nw * scale),
                y: (y - oy) / (nh * scale),
            };
        };

        // Rect de una zona en px de canvas
        const zoneRectPx = (zone) => {
            const nw = imgEl.naturalWidth, nh = imgEl.naturalHeight;
            const scale = this._zoneDrawScale || 1;
            const ox = this._zoneOffsetX || 0, oy = this._zoneOffsetY || 0;
            const c = zone.coords || {};
            return {
                x: ox + c.x * nw * scale,
                y: oy + c.y * nh * scale,
                w: c.w * nw * scale,
                h: c.h * nh * scale,
            };
        };

        // Zona bajo un punto (px canvas), priorizando la última dibujada
        const zoneAt = (x, y) => {
            const zones = this._zoneZones || [];
            for (let i = zones.length - 1; i >= 0; i--) {
                const r = zoneRectPx(zones[i]);
                if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) {
                    return zones[i];
                }
            }
            return null;
        };

        const HANDLE = 12; // px de la esquina de resize

        const startDraw = (e) => {
            const pos = getRelativePos(e);
            const mode = this._zoneEditMode;

            // F0.3: modo borrar — clic sobre una zona la elimina
            if (mode === 'delete') {
                e.preventDefault();
                const z = zoneAt(pos.x, pos.y);
                if (z) {
                    this._zoneZones = this._zoneZones.filter(zz => zz.id !== z.id);
                    this._redrawZones();
                    this._updateZoneList();
                }
                return;
            }

            // F0.3: modo editar — mover o redimensionar la zona bajo el cursor
            if (mode === 'edit') {
                e.preventDefault();
                const z = zoneAt(pos.x, pos.y);
                if (!z) return;
                const r = zoneRectPx(z);
                const nearBR = (Math.abs(pos.x - (r.x + r.w)) <= HANDLE &&
                                Math.abs(pos.y - (r.y + r.h)) <= HANDLE);
                editAction = nearBR ? 'resize' : 'move';
                editZone = z;
                editStartCoords = { ...z.coords };
                editStartPos = pos;
                return;
            }

            if (mode !== 'draw') return;
            e.preventDefault();
            startX = pos.x;
            startY = pos.y;
            isDrawing = true;
        };

        const updateDraw = (e) => {
            // F0.3: arrastre en modo edición
            if (this._zoneEditMode === 'edit' && editZone && editAction) {
                e.preventDefault();
                const pos = getRelativePos(e);
                const nw = imgEl.naturalWidth, nh = imgEl.naturalHeight;
                const scale = this._zoneDrawScale || 1;
                const dx = (pos.x - editStartPos.x) / (nw * scale);
                const dy = (pos.y - editStartPos.y) / (nh * scale);
                const c0 = editStartCoords;
                if (editAction === 'move') {
                    editZone.coords = {
                        x: Math.max(0, Math.min(1 - c0.w, c0.x + dx)),
                        y: Math.max(0, Math.min(1 - c0.h, c0.y + dy)),
                        w: c0.w,
                        h: c0.h,
                    };
                } else { // resize (esquina inferior-derecha)
                    editZone.coords = {
                        x: c0.x,
                        y: c0.y,
                        w: Math.max(0.02, Math.min(1 - c0.x, c0.w + dx)),
                        h: Math.max(0.02, Math.min(1 - c0.y, c0.h + dy)),
                    };
                }
                this._redrawZones();
                return;
            }

            if (this._zoneEditMode !== 'draw' || !isDrawing) return;
            e.preventDefault();
            const pos = getRelativePos(e);
            const x = Math.min(startX, pos.x);
            const y = Math.min(startY, pos.y);
            const w = Math.abs(startX - pos.x);
            const h = Math.abs(startY - pos.y);
            currentRect = { x, y, w, h, startX, startY, endX: pos.x, endY: pos.y };
            this._zoneCurrentDrawRect = currentRect; // F0.3: feedback visual
            this._redrawZones();
        };

        const finishDraw = (e) => {
            // F0.3: terminar edición
            if (this._zoneEditMode === 'edit' && editZone) {
                e.preventDefault();
                editZone = null;
                editAction = null;
                editStartCoords = null;
                editStartPos = null;
                this._redrawZones();
                this._updateZoneList();
                return;
            }
            if (this._zoneEditMode !== 'draw' || !isDrawing) return;
            e.preventDefault();
            isDrawing = false;
            this._zoneCurrentDrawRect = null; // F0.3
            if (currentRect && currentRect.w > 10 && currentRect.h > 10) {
                const imgEl = document.getElementById('zone-frame-img');
                if (imgEl && imgEl.naturalWidth) {
                    const scale = this._zoneDrawScale || 1;
                    const offsetX = this._zoneOffsetX || 0;
                    const offsetY = this._zoneOffsetY || 0;
                    
                    // Convert to relative coordinates (0-1)
                    const relX = (currentRect.x - offsetX) / (imgEl.naturalWidth * scale);
                    const relY = (currentRect.y - offsetY) / (imgEl.naturalHeight * scale);
                    const relW = currentRect.w / (imgEl.naturalWidth * scale);
                    const relH = currentRect.h / (imgEl.naturalHeight * scale);

                    const type = document.getElementById('zone-type-select')?.value || 'other';
                    const name = document.getElementById('zone-name-input')?.value || `Zona ${this._zoneZones.length + 1}`;
                    
                    const zoneTypes = this._zoneTypes || [];
                    const typeObj = zoneTypes.find(t => t.id === type) || {id: type, name: type, icon: '📍'};
                    
                    // Get color from palette or use default
                    const color = this._zoneSelectedColor || '#ff0a4e';

                    // F0.1: antes se llamaba round() global (inexistente) → ReferenceError
                    // y la zona nunca se agregaba. Usar helper local.
                    const _r4 = (n) => Math.round(n * 10000) / 10000;
                    const newZone = {
                        id: 'zone_' + Date.now(),
                        name: name,
                        type: type,
                        coords: {x: _r4(relX), y: _r4(relY), w: _r4(relW), h: _r4(relH)},
                        color: color,
                        created_at: Date.now() / 1000
                    };

                    this._zoneZones = this._zoneZones || [];
                    this._zoneZones.push(newZone);
                    currentRect = null;
                    this._redrawZones();
                    this._updateZoneList();

                    // Reset inputs
                    document.getElementById('zone-name-input').value = '';
                } else {
                    currentRect = null;
                    this._redrawZones();
                }
            } else {
                currentRect = null;
                this._redrawZones();
            }
        };

        canvas.addEventListener('mousedown', startDraw);
        canvas.addEventListener('mousemove', updateDraw);
        canvas.addEventListener('mouseup', finishDraw);
        canvas.addEventListener('mouseleave', finishDraw);
        canvas.addEventListener('touchstart', startDraw, {passive: false});
        canvas.addEventListener('touchmove', updateDraw, {passive: false});
        canvas.addEventListener('touchend', finishDraw);
    },

    round(num, decimals = 2) {
        return Math.round(num * Math.pow(10, decimals)) / Math.pow(10, decimals);
    },

    _redrawZones() {
        const canvas = document.getElementById('zone-canvas');
        if (!canvas) return;
        const ctx = this._zoneCanvasCtx || canvas.getContext('2d');
        if (!ctx) return;

        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const offsetX = this._zoneOffsetX || 0;
        const offsetY = this._zoneOffsetY || 0;
        const scale = this._zoneDrawScale || 1;
        const frameW = this._zoneFrameW || 640;
        const frameH = this._zoneFrameH || 360;

        (this._zoneZones || []).forEach(zone => {
            if (zone.coords && zone.coords.x !== undefined) {
                const c = zone.coords;
                const x = offsetX + c.x * frameW * scale;
                const y = offsetY + c.y * frameH * scale;
                const w = c.w * frameW * scale;
                const h = c.h * frameH * scale;

                ctx.strokeStyle = zone.color || '#ff0a4e';
                ctx.lineWidth = 3;
                ctx.strokeRect(x, y, w, h);

                // Fill with semi-transparent
                ctx.fillStyle = this._hexToRgba(zone.color || '#ff0a4e', 0.3);
                ctx.fillRect(x, y, w, h);

                // Draw zone name
                ctx.font = '12px sans-serif';
                ctx.fillStyle = '#fff';
                ctx.textBaseline = 'top';
                ctx.fillText(zone.name || 'Zona', x + 4, y + 4);

                // F0.3: en modo editar, dibujar handle de resize (esquina inf-der)
                if (this._zoneEditMode === 'edit') {
                    ctx.fillStyle = zone.color || '#ff0a4e';
                    ctx.fillRect(x + w - 6, y + h - 6, 12, 12);
                    ctx.strokeStyle = '#fff';
                    ctx.lineWidth = 2;
                    ctx.strokeRect(x + w - 6, y + h - 6, 12, 12);
                }
            }
        });

        // F0.3: rectángulo en curso durante el dibujo
        const handlers = this._zoneCurrentDrawRect;
        if (this._zoneEditMode === 'draw' && handlers) {
            ctx.setLineDash([6, 4]);
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 2;
            ctx.strokeRect(handlers.x, handlers.y, handlers.w, handlers.h);
            ctx.setLineDash([]);
        }
    },

    _hexToRgba(hex, alpha = 1) {
        let r = 0, g = 0, b = 0;
        if (hex.length === 4) {
            r = parseInt(hex[1] + hex[1], 16);
            g = parseInt(hex[2] + hex[2], 16);
            b = parseInt(hex[3] + hex[3], 16);
        } else if (hex.length === 7) {
            r = parseInt(hex[2] + hex[3], 16);
            g = parseInt(hex[4] + hex[5], 16);
            b = parseInt(hex[6] + hex[7], 16);
        }
        return `rgba(${r},${g},${b},${alpha})`;
    },

    _getZoneColor(type) {
        const colorMap = {
            'entrance': '#2196f3',
            'cashier': '#ff9800',
            'cashier': '#ff9800',
            'register': '#ff9800',
            'kitchen': '#f44336',
            'dining': '#4caf50',
            'inventory': '#9c27b0',
            'counter': '#e91e63',
            'hall': '#607d8b',
            'parking': '#8bc34a',
            'restricted': '#f44336',
            'office': '#3f51b5',
            'storage': '#795548',
            'hallway': '#607d8b',
            'production': '#607d8b',
            'other': '#9e9e9e'
        };
        return colorMap[type] || '#9e9e9e';
    },

    _setZoneTool(tool, btn) {
        this._zoneEditMode = tool;
        document.querySelectorAll('.zone-tool-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        // F0.3: cursor contextual según herramienta
        const canvas = document.getElementById('zone-canvas');
        if (canvas) {
            canvas.style.cursor = tool === 'draw' ? 'crosshair'
                : tool === 'edit' ? 'move'
                : tool === 'delete' ? 'not-allowed' : 'default';
        }
    },

    _selectZoneColor(color, swatch) {
        this._zoneSelectedColor = color;
        document.querySelectorAll('.zone-color-swatch').forEach(s => s.classList.remove('active'));
        swatch.classList.add('active');
    },

    _updateZoneColorSelection(canvas) {
        // Update color palette swatches based on selected color
        const swatches = document.querySelectorAll('.zone-color-swatch');
        if (swatches.length > 0) {
            swatches[0].classList.add('active');
            this._zoneSelectedColor = swatches[0].dataset.color;
        }
    },

    _renderZoneList(zones, zoneTypes) {
        if (!zones || zones.length === 0) {
            return '<div class="zones-empty">Sin zonas. Dibuja una zona en el frame para comenzar.</div>';
        }
        const typeMap = {};
        zoneTypes.forEach(t => { typeMap[t.id] = t; });
        return zones.map(zone => {
            const typeObj = typeMap[zone.type] || {name: zone.type, icon: '📍'};
            return `<div class="zone-list-item" style="border-left-color:${zone.color || '#0a84ff'};flex-direction:column;gap:6px">
                <div style="display:flex;align-items:center;width:100%">
                    <div class="zone-type-icon">${typeObj.icon || '📍'}</div>
                    <div class="zone-info">
                        <div class="zone-name">${zone.name || 'Sin nombre'}</div>
                        <div class="zone-type-name">${typeObj.name || zone.type}</div>
                    </div>
                    <button class="zone-delete" onclick="App._deleteZoneDrawer('${zone.id}')" title="Eliminar zona">✕</button>
                </div>
                <input type="text" class="zone-attention-input" data-zone-id="${this._escAttr(zone.id)}"
                       placeholder="¿Qué vigilo en ${this._escAttr(zone.name || 'esta zona')}? (ej: que nadie pase detrás del mostrador)"
                       value="${this._escAttr(zone.attention || '')}"
                       style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:8px;font-size:12px;background:var(--bg-secondary);color:var(--text-primary)"
                       oninput="App._setZoneAttention('${this._escAttr(zone.id)}', this.value)">
            </div>`;
        }).join('');
    },

    _setZoneAttention(zoneId, value) {
        const z = (this._zoneZones || []).find(zz => zz.id === zoneId);
        if (z) z.attention = value;
    },

    _updateZoneList() {
        const container = document.getElementById('zone-list-container');
        if (!container) return;
        container.innerHTML = this._renderZoneList(this._zoneZones || [], this._zoneTypes || []);
    },

    _deleteZoneDrawer(zoneId) {
        this._zoneZones = (this._zoneZones || []).filter(z => z.id !== zoneId);
        this._redrawZones();
        this._updateZoneList();
    },

    // F2.1: Eva sugiere zonas (endpoint /suggest-zones con Qwen) — pre-llenado editable
    async _suggestZones(camId, btn) {
        if (btn) { btn.disabled = true; btn.textContent = '⏳ Analizando imagen...'; }
        try {
            const cam = this._currentCamConfig || {};
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/suggest-zones`, {
                method: 'POST',
                body: JSON.stringify({
                    user_id: this.userId,
                    zone: cam.zone || '',
                    business_type: cam.business_type || ''
                })
            });
            const d = await r.json();
            if (!d.success || !d.zones || !d.zones.length) {
                this._toast('', d.error || 'Eva no pudo sugerir zonas ahora. Dibújalas manualmente.', 'danger');
                return;
            }
            // Pre-llenar: respetar zonas existentes (no duplicar tipos ya dibujados)
            const existingTypes = new Set((this._zoneZones || []).map(z => z.type));
            const newZones = d.zones.filter(z => !existingTypes.has(z.type));
            if (!newZones.length) {
                this._toast('', `Ya tienes dibujadas zonas de los tipos que Eva sugeriría (${d.zones.length})`, 'success');
                return;
            }
            this._zoneZones = (this._zoneZones || []).concat(newZones);
            this._redrawZones();
            this._updateZoneList();
            this._toast('', `✨ Eva sugirió ${newZones.length} zona(s) — ajústalas o bórralas`, 'success');
        } catch (e) {
            this._toast('', 'Error de conexión: ' + e.message, 'danger');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '✨ Eva sugiere zonas'; }
        }
    },

    async _clearAllZones(camId) {
        if (!confirm('¿Estás seguro de borrar todas las zonas?')) return;
        this._zoneZones = [];
        this._redrawZones();
        this._updateZoneList();
    },

    async _saveZoneDrawer(camId) {
        if (!this._zoneZones || this._zoneZones.length === 0) {
            alert('Añade al menos una zona antes de guardar');
            return;
        }
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/zones?user_id=${this.userId}`, {
                method: 'POST',
                body: JSON.stringify({ zones: this._zoneZones })
            });
            const d = await r.json();
            if (d.success || d.zones) {
                // F2.3: guardar frases de atención por zona (attention_phrases_zones)
                // y derivar attention_phrases de nivel cámara en camera.json
                try {
                    const apz = {};
                    (this._zoneZones || []).forEach(z => {
                        if (z.attention && z.attention.trim()) {
                            apz[z.attention.trim()] = z.name || 'zona';
                        }
                    });
                    const phrases = Object.keys(apz);
                    if (phrases.length) {
                        await apiFetch(`${this.API}/api/cameras/${camId}/vigilance?user_id=${this.userId}`, {
                            method: 'PUT',
                            body: JSON.stringify({
                                user_id: this.userId,
                                vigilance: {
                                    attention_phrases_zones: apz,
                                    attention_phrases: phrases,
                                    enabled: true
                                }
                            })
                        });
                    }
                } catch (e) {
                    console.warn('[Zones] vigilance update silent fail:', e);
                }
                this._closeZoneDrawer();
                this._refreshZonesList(camId);
                alert('✅ Zonas guardadas correctamente');
            } else {
                alert('❌ Error al guardar: ' + (d.error || 'Error desconocido'));
            }
        } catch(e) {
            alert('❌ Error de conexión: ' + e.message);
        }
    },

    async _refreshZonesList(camId) {
        const container = document.getElementById(`zones-list-${camId}`);
        if (!container) return;
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/zones?user_id=${this.userId}`);
            const d = await r.json();
            const zones = d.zones || [];
            const zoneTypes = this._zoneTypes || [];
            if (zones.length === 0) {
                container.innerHTML = '<div class="zones-empty">Sin zonas. Haz clic en "Gestionar zonas" para añadir.</div>';
            } else {
                const typeMap = {};
                zoneTypes.forEach(t => { typeMap[t.id] = t; });
                container.innerHTML = zones.map(zone => {
                    const typeObj = typeMap[zone.type] || {name: zone.type, icon: '📍'};
                    return `<div class="zone-list-item" style="border-left-color:${zone.color || '#0a84ff'}">
                        <div class="zone-type-icon">${typeObj.icon || '📍'}</div>
                        <div class="zone-info">
                            <div class="zone-name">${zone.name || 'Sin nombre'}</div>
                            <div class="zone-type-name">${typeObj.name || zone.type}</div>
                        </div>
                    </div>`;
                }).join('');
            }
        } catch(e) {
            container.innerHTML = '<div class="zones-empty">Error cargando zonas</div>';
        }
    },

    _closeZoneDrawer() {
        const overlay = document.getElementById('zone-drawer-overlay');
        if (overlay) {
            overlay.remove();
        }
        this._zoneDrawerCamId = null;
        this._zoneZones = [];
        this._zoneEditMode = false;
    },

    // ── PWA INSTALL ──────────────────────────────────────────────
    _deferredPrompt: null,
    _initPWAInstall() {
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            this._deferredPrompt = e;
        });
        window.addEventListener('appinstalled', () => {
            this._deferredPrompt = null;
        });
    },
    async _installApp() {
        if (!this._deferredPrompt) {
            // Fallback: mostrar instrucciones manuales
            const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
            if (isIOS) {
                alert('Para instalar en iOS: toca el boton Compartir y luego "Agregar a inicio"');
            } else {
                alert('Para instalar: toca los 3 puntos del navegador y luego "Instalar app" o "Agregar a inicio"');
            }
            return;
        }
        this._deferredPrompt.prompt();
        const { outcome } = await this._deferredPrompt.userChoice;
        this._deferredPrompt = null;
    },
    _isPWAInstalled() {
        return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
    },

    async _saveCooldown(camId, btn) {
            this._setConfigButtonBusy(btn, true);
            try {
                const el = document.getElementById('cfg_cooldown_min');
                const val = Math.max(5, Math.min(60, parseInt(el?.value) || 5));
                const r = await apiFetch(`${this.API}/api/cameras/${camId}/cooldown`, {
                    method: 'POST',
                    body: JSON.stringify({cooldown_min: val})
                });
                const d = await r.json();
                if (d.ok || d.success) {
                    this._toast('', `Cooldown: ${val} min`, 'success');
                } else {
                    this._toast('', d.error || 'Error', 'danger');
                }
            } catch(e) {
                this._toast('', 'Error de red', 'danger');
            } finally {
                this._setConfigButtonBusy(btn, false);
            }
        },
};

// Init PWA install handler
document.addEventListener('DOMContentLoaded', () => {
    App._initPWAInstall();
    App.init();
});// cache-bust-fingerprint: 20260822-z
