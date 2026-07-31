// ============================================================
// OjoIA - App v6 Production Ready
// api.ojoia.com.do | Firebase: ojoia-67216
// Análisis: detección encuentra objetos -> Eva revisa el área
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

// S1: Auth token (Bearer) para la API. Soft rollout: backend acepta con o sin
// token y solo loguea warnings. El token se pide una vez por sesion/login en
// _startAuth() y se guarda en localStorage.
const AUTH_TOKEN_KEY = 'ojoia_api_token';
function _getAuthToken() {
    try { return localStorage.getItem(AUTH_TOKEN_KEY) || ''; } catch(e) { return ''; }
}
function _setAuthToken(t) {
    try {
        if (t) localStorage.setItem(AUTH_TOKEN_KEY, t);
        else localStorage.removeItem(AUTH_TOKEN_KEY);
    } catch(e) {}
}

// M4.2: helper global de escapado de HTML para uso en interpolaciones de innerHTML.
// Prevencion de XSS: cualquier string que venga del backend (cam.name, evt.description,
// business_name, etc.) y se vaya a insertar como HTML debe pasar por aqui.
function escHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
        '&':'&','<':'<','>':'>','"':'"',"'":'&#39;'
    }[ch]));
}

function apiFetch(url, opts = {}) {
    const headers = { ...opts.headers };
    if (opts.body && typeof opts.body === 'string') {
        try { JSON.parse(opts.body); headers['Content-Type'] = 'application/json'; } catch(e) {}
    }
    if (!headers['Content-Type']) headers['Content-Type'] = 'application/json';
    // S1: inyectar Bearer token si tenemos uno y el destino es la API de OjoIA.
    const token = _getAuthToken();
    if (token && !headers['Authorization'] && typeof url === 'string' &&
        (url.indexOf('ojoia.com.do') !== -1 || url.indexOf('10.0.0.44') !== -1)) {
        headers['Authorization'] = 'Bearer ' + token;
    }
    return fetch(url, { mode: 'cors', headers, ...opts });
}

const App = {
    userId: null,
    API: '',
    page: 'home',
    _polls: {},
    _authStarted: false,
    _authInFlight: false,
    _loginMode: 'login',
    _evaSession: null,
    _evaCamId: null,
    _evaReady: false,
    _pendingNotificationEventId: '',
    _pendingNotificationCameraId: '',
    _minimalEvaMessages: [],
    _minimalEvaSession: '',
    _minimalEvaLoading: false,
    _minimalEvaBusiness: '',
    _viewerCamId: null,
    _apiReady: false,
    _homeFrameInFlight: false,
    _homeLastYoloFetch: 0,
    _homeYoloPollMs: 2000,
    _homeLastDetections: [],
    _homeWatermarkText: '',
    // B2: tiempo por defecto del auto-advance de eventos (ms). Const; no se inicializaba
    // y los toasts decian "NaN s" porque _eventAutoAdvanceTimeout era undefined.
    _eventAutoAdvanceTimeout: 6000,
    // --- Streaming robusto ---
    _streamWatchdogMs: 8000,
    _streamReconnectDelays: [1500, 3000, 6000, 10000],
    _streamErrors: {},
    _streamLastOnloadTs: {},
    _streamWatchdogTimers: {},

    init() {
        document.addEventListener('visibilitychange', () => this._onVisibilityChange());
        const h = window.location.hostname;
        if (h === '10.0.0.44' || h === 'localhost' || h === '') {
            this.API = 'http://10.0.0.44:8005';
        this._apiReady = true;
        this._startAuth();
        this._loadSupportInfo();
        return;
    }
    this._fetchServerUrl();
    },

    async _fetchServerUrl() {
        const h = window.location.hostname;
        const healthUrl = 'https://api.ojoia.com.do/health?_=' + Date.now();
        // Reintentos para tolerar restarts fugaces del backend (502 durante ~10s
        // mientras arranca el YOLO worker). 3 intentos con backoff.
        let httpsOk = false;
        for (let attempt = 1; attempt <= 3; attempt++) {
            try {
                const r = await fetch(healthUrl, {
                    mode: 'cors',
                    signal: AbortSignal.timeout(8000)
                });
                if (r.ok) { httpsOk = true; break; }
                console.warn(`[API] HTTPS health returned ${r.status} (intento ${attempt}/3)`);
            } catch(e) {
                console.warn(`[API] HTTPS failed (intento ${attempt}/3):`, e.message);
            }
            if (attempt < 3) { await new Promise(rs => setTimeout(rs, 1200 * attempt)); }
        }
        if (httpsOk) {
            this.API = 'https://api.ojoia.com.do';
            this._apiReady = true;
            this._startAuth();
            this._loadSupportInfo();
            return;
        }
        // Fallback a HTTP local solo si la página actual es insegura o es localhost
        const isLocal = h === '10.0.0.44' || h === 'localhost' || h === '127.0.0.1' || h === '';
        if (window.location.protocol === 'http:' || isLocal) {
            this.API = 'http://10.0.0.44:8005';
        } else {
            // En una página HTTPS el navegador bloquea HTTP por mixed-content policy.
            // Mantenemos HTTPS para que el error sea visible y no silencioso.
            this.API = 'https://api.ojoia.com.do';
            console.error('[API] HTTPS endpoint unreachable from secure page; cannot fall back to HTTP due to mixed-content policy.');
            if (typeof this._toast === 'function') {
                this._toast('', 'No se pudo conectar a api.ojoia.com.do. Verifica el túnel de red.', 'error');
            }
        }
        this._apiReady = true;
        this._startAuth();
        this._loadSupportInfo();
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
                // Guard anti-reentrante: si ya estamos autenticando, no re-prompt
                if (this._authInFlight) { return; }
                this._authInFlight = true;
                try {
                    const apiUrl = await this._waitForAPI();
                    const token = await u.getIdToken();
                    // Reintentar verify hasta 3 veces (API puede estar reiniciando)
                    let d = null;
                    for (let attempt = 0; attempt < 3; attempt++) {
                        try {
                            const r = await fetch(apiUrl + '/auth/firebase/verify', {
                                method: 'POST', mode: 'cors',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ id_token: token, email: u.email, name: u.displayName || '' })
                            });
                            if (!r.ok) { await new Promise(rs => setTimeout(rs, 800 * (attempt + 1))); continue; }
                            d = await r.json();
                            if (d && d.success) break;
                        } catch (fetchErr) {
                            // Error de red (API reiniciando): esperar y reintentar, NO signOut
                            await new Promise(rs => setTimeout(rs, 800 * (attempt + 1)));
                        }
                    }
                    if (d && d.success) {
                        this.userId = d.user_id;
                        localStorage.setItem('ojoia_uid', this.userId);
                        // S1: pedir Bearer token propio y guardarlo en localStorage.
                        // Soft rollout: si falla, el backend sigue atendiendo los requests
                        // sin Authorization (modo legacy con warnings en logs).
                        this._ensureApiToken();
                        this._showApp();
                    } else if (d && d.success === false && d.error && /not.*found|incomplet|no.*registr/i.test(d.error)) {
                        // Usuario Firebase OK pero sin registro completo en backend
                        firebase.auth().signOut();
                        this._showLogin();
                        this.setLoginMode('register');
                        this._err('Tu cuenta no está completa. Regístrate para empezar.');
                    } else {
                        // API devolvió respuesta pero no successful y no es un error de cuenta
                        // No signOut (evita re-prompt); mantener sesión con un userId fallback
                        const cachedUid = localStorage.getItem('ojoia_uid');
                        if (cachedUid) {
                            this.userId = cachedUid;
                            this._showApp();
                        } else {
                            this._showLogin();
                            this._err('No pude conectar con el servidor. Recarga en unos segundos.');
                        }
                    }
                } finally {
                    this._authInFlight = false;
                }
            } else {
                if (typeof EvaChat !== 'undefined' && EvaChat.teardown) EvaChat.teardown();
                this._showLogin();
            }
        });
        ['login-email','login-pw','login-pw2','reg-name','reg-business'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('keypress', e => { if (e.key === 'Enter') this.doLogin(); });
        });
    },

    // S1: Emite/recupera el Bearer token de la API y lo guarda en localStorage.
    // Idempotente: si ya tenemos token no cambia. Re-emite solo si está vacío
    // (caso nuevo dispositivo o logout). Backend: POST /api/auth/token, soft mode.
    _ensureApiToken() {
        if (!this.userId) return;
        // Si ya hay token en localStorage, no re-emitir (evita crear tokens de más).
        if (_getAuthToken()) return;
        const apiUrl = this.API || 'https://api.ojoia.com.do';
        const dev = navigator.userAgent.substring(0, 80) + '|' + (navigator.userAgentData?.platform || 'unknown');
        fetch(apiUrl + '/api/auth/token', {
            method: 'POST', mode: 'cors',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: this.userId, device: dev })
        }).then(r => r.ok ? r.json() : null).then(d => {
            if (d && d.success && d.token) {
                _setAuthToken(d.token);
                console.info('[auth] Bearer token guardado (soft rollout)');
            } else {
                console.warn('[auth] no se pudo emitir Bearer token; fallback a modo legacy');
            }
        }).catch(e => console.warn('[auth] error pidiendo token:', e.message));
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
            btn.textContent = 'Creando cuenta...';
            try {
                const cred = await firebase.auth().createUserWithEmailAndPassword(email, pw);
                await cred.user.updateProfile({ displayName: name });
                const ok = await this._verifyFB(cred.user, {
                    name, business_name: biz, email, business_type: bizType || 'other',
                    schedule_open: '08:00', schedule_close: '20:00'
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
                    localStorage.setItem('ojoia_uid', this.userId);
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
                if (code === 'auth/user-not-found' || code === 'auth/invalid-credential' || code === 'auth/wrong-password') {
                    this._err('⚠️ Usuario no registrado o contraseña incorrecta. Regístrate para acceder.');
                    // Cambiar a registro después de mostrar el error
                    setTimeout(() => {
                        this.setLoginMode('register');
                        this._err('⚠️ Regístrate para acceder. Completa todos los campos.');
        }, 1000);
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
            localStorage.setItem('ojoia_uid', this.userId);
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
    },

    logout() {
        this._clearAllPolls();
        // S1: revocar el Bearer token en el backend antes de limpiar localmente.
        const token = _getAuthToken();
        const uid = this.userId;
        if (token && uid) {
            const apiUrl = this.API || 'https://api.ojoia.com.do';
            fetch(apiUrl + '/api/auth/token?user_id=' + encodeURIComponent(uid) +
                 '&token_to_revoke=' + encodeURIComponent(token),
                 { method: 'DELETE', mode: 'cors' }).catch(() => {});
        }
        _setAuthToken('');
        localStorage.removeItem('ojoia_uid');
        this.userId = null;
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
        if (this.page !== page) {
            // M6.4: cerrar streams MJPEG antes de limpiar polls, para evitar que sigan
            // bombando frames en background (consumo CPU/bateria del cliente).
            this._stopAllStreams();
            this._clearAllPolls();
            // Invalidar caché de streams al cambiar de tab (viewer vacío bug fix)
            this._homeStreamStarted = {};
            this._timelineFrames = {};
            this._timelineIndex = {};
        }
        this.page = page;
        const c = document.getElementById('app-content');
        c.className = `page ${page}-page`;
        c.style.display = '';
        c.style.overflow = '';
        c.style.padding = '';
        c.style.flex = '';
        c.style.height = '';
        c.style.minHeight = '';
        if (page !== 'eva') this._removeStaleEvaChat();
        c.scrollTop = 0;
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
        ({ home: () => this._pageHome(c), cameras: () => this._pageHome(c), eva: () => this._pageEva(c), events: () => this._pageEvents(c, eventId), settings: () => this._pageSettings(c) })[page]?.();
    },

    _clearAllPolls() { Object.values(this._polls).forEach(id => clearInterval(id)); this._polls = {}; if (this._configViewerPoll) { clearInterval(this._configViewerPoll); this._configViewerPoll = null; } },

    _onVisibilityChange() {
        const hidden = document.hidden;
        this._streamWatchdogPaused = hidden;
        if (!hidden) {
            // Reanudar polls de la página actual (home o cameras)
            try {
                if (this.page === 'home' || this.page === 'cameras') {
                    this._restartHomeStreamPolls();
                    this._restartViewerPolls();
                    // Forzar reconexión inmediata de streams activos
                    this._reconnectAllActiveStreams();
                } else if (this._viewerCamId) {
                    this._restartViewerPolls();
                    this._reconnectAllActiveStreams();
                }
            } catch(e) { console.warn('visibility resume:', e); }
        }
    },

    _restartHomeStreamPolls() {
        if (!this._homeCams || !this._homeCams.length) return;
        const frameInterval = Math.max(1000, (this._homeYoloPollMs || 2000));
        const cams = this._getHomeViewCams ? this._getHomeViewCams() : this._homeCams;
        cams.forEach((cam, i) => { if (!this._polls['home_frames']) this._fetchFrameForCam(cam.camera_id, `home-frame-${i}`); });
        if (!this._polls.home_frames) this._poll('home_frames', () => this._fetchHomeFrames(), frameInterval);
        if (!this._polls.home_stats) this._poll('home_stats', () => this._fetchStats(), 30000);
        if (!this._polls.home_cams) this._poll('home_cams', () => this._refreshCamStatus(), 15000);
    },

    _restartViewerPolls() {
        if (!this._viewerCamId) return;
        const camId = this._viewerCamId;
        this._fetchFrame('viewer-body');
        this._fetchViewerGrid();
        if (this._polls.viewer) clearInterval(this._polls.viewer);
        // Poll más lento: el MJPEG ya se actualiza solo; solo refrescar grid (5s)
        this._polls.viewer = setInterval(() => { this._fetchViewerGrid(); }, 5000);
    },

    _reconnectAllActiveStreams() {
        Object.keys(this._homeStreamStarted || {}).forEach((key) => {
            const [targetId, ...camParts] = key.split(':');
            const camId = camParts.join(':');
            const el = document.getElementById(targetId);
            const imgEl = el && el.querySelector('img.live-img');
            if (!imgEl) return;
            const uid = this.userId || 'default';
            const baseStreamUrl = `${this.API}/cameras/${camId}/stream?user_id=${uid}&fps=5`;
            imgEl.src = `${baseStreamUrl}&_=${Date.now()}`;
            this._streamLastOnloadTs[key] = Date.now();
        });
    },
    // M6.4: cerrar todos los streams MJPEG activos. Para cada clave en
    // _homeStreamStarted poner img.src='' (el browser corta la conexion HTTP)
    // y descartar el tag <img> para que el reproductor de vuelta a la tab no
    // reutilice un objeto con un stream ya cancelado.
    _stopAllStreams() {
        try {
            Object.keys(this._homeStreamStarted || {}).forEach((key) => {
                const [targetId, ...camParts] = key.split(':');
                const el = document.getElementById(targetId);
                const imgEl = el && el.querySelector('img.live-img');
                if (imgEl) {
                    imgEl.onerror = null;
                    imgEl.onload = null;
                    imgEl.src = '';
                }
                // tambien cancelar watchdog pendiente
                if (this._streamWatchdogTimers && this._streamWatchdogTimers[key]) {
                    clearTimeout(this._streamWatchdogTimers[key]);
                    delete this._streamWatchdogTimers[key];
                }
            });
        } catch(e) { console.warn('[streams] _stopAllStreams:', e.message); }
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
    _imgFallback(img, icon) {
        if (!img || !img.parentElement) return;
        img.style.display = 'none';
        const span = document.createElement('span');
        span.style.fontSize = '1.3rem';
        span.textContent = icon || '✓';
        img.parentElement.appendChild(span);
    },
    _handleInitialRoute() {
        const hash = decodeURIComponent(window.location.hash || '').replace(/^#/, '');
        const [pageName, query = ''] = hash.split('?');
        const params = new URLSearchParams(query);
        const eventId = params.get('event') || params.get('alert') || '';
        const cameraId = params.get('camera') || params.get('cam') || '';
        if (eventId || (cameraId && pageName === 'live')) {
            this._handleEventDeepLink(pageName, eventId, cameraId);
            return;
        }
        this.go('eva');
    },

    /**
     * Procesa un deep-link de evento (desde la URL inicial o desde un mensaje del SW
     * cuando el usuario clica una notificación push mientras la app ya está abierta).
     * pageName: 'eva' | 'cameras' | 'events' | 'live'
     */
    _handleEventDeepLink(pageName, eventId, cameraId) {
        // Push (Fase 0): tanto centinela como modo normal abren la tab cámara
        // con el modal del evento encima. pageName==='eva' cae aquí por
        // deep-links legacy (#eva?alert=...). Redirigimos a cameras en ambos.
        if (eventId && (pageName === 'cameras' || pageName === 'eva')) {
            this.go('cameras');
            if (window.location.hash !== '#cameras') window.location.hash = '#cameras';
            const evtId = eventId;
            const camId = cameraId || '';
            // Guardar referencia para que el banner (_activeAlertEvent) refleje esta alerta.
            this._activeAlertEvent = this._activeAlertEvent || {};
            this._activeAlertEvent.eventId = evtId;
            this._activeAlertEvent.cameraId = camId;
            this._activeAlertEvent.ts = Date.now();
            setTimeout(() => {
                try { this._openEvent(evtId); } catch(e) { console.error('open event from push:', e); }
            }, 700);
            return;
        }
        if (eventId && pageName === 'events') {
            this.go('events');
            // NO auto-abrir el modal: mostrar toast con link clickeable (control del usuario)
            if (window.App?._showToast) {
                App._showToast(`🔔 Nuevo evento disponible — click "Ver"`, 5000, {
                    action: { label: 'Ver', onClick: () => this._openEvent(eventId) }
                });
            }
            return;
        }
        if (cameraId && pageName === 'live') {
            this._openCameraLive(cameraId);
            return;
        }
        // Sin deep-link válido: a Home/Eva
        this.go('eva');
    },

    _poll(key, fn, ms) {
        if (this._polls[key]) clearInterval(this._polls[key]);
        fn();
        this._polls[key] = setInterval(() => {
            if (document.hidden || !this._pollPageMatches(key)) { clearInterval(this._polls[key]); delete this._polls[key]; return; }
            fn();
        }, ms);
    },

    _pollPageMatches(key) {
        const base = key.split('_')[0];
        if (base === 'home') return this.page === 'home' || this.page === 'cameras';
        if (base === 'viewer') return !!this._viewerCamId;
        return this.page === base;
    },

    async _initPush() {
        try {
            if (!('Notification' in window)) return;
            const perm = await Notification.requestPermission();
            if (perm !== 'granted') return;
            if ('serviceWorker' in navigator) {
                const reg = await navigator.serviceWorker.register('/sw.js');
                // Procesar clics en notificaciones push mientras la app ya está abierta:
                // el SW hace postMessage({type:'ojoia-event', url}) y delega el deep-link aquí.
                navigator.serviceWorker.addEventListener('message', (ev) => {
                    const data = ev.data || {};
                    if (data.type !== 'ojoia-event' || !data.url) return;
                    try {
                        // data.url puede ser "https://ojoia.com.do/#cameras?event=..." o "#cameras?..."
                        const u = new URL(data.url, location.href);
                        const hash = decodeURIComponent(u.hash || data.url).replace(/^#/, '');
                        const [pageName, query = ''] = hash.split('?');
                        const params = new URLSearchParams(query);
                        const eventId = params.get('event') || params.get('alert') || '';
                        const cameraId = params.get('camera') || params.get('cam') || '';
                        console.log('[push] deep-link del SW →', { pageName, eventId, cameraId });
                        if (eventId || (cameraId && pageName === 'live')) {
                            this._handleEventDeepLink(pageName, eventId, cameraId);
                        }
                    } catch (err) { console.error('[push] error procesando deep-link del SW:', err); }
                });
                if (firebase.messaging) {
                    const msg = firebase.messaging();
                    const ua = navigator.userAgent || '';
                    const device = /Edg\//.test(ua) ? 'edge'
                                : /Chrome\//.test(ua) ? 'chrome'
                                : /Firefox\//.test(ua) ? 'firefox'
                                : /Safari\//.test(ua) ? 'safari'
                                : /^Mozilla/.test(ua) ? 'chrome' : 'web';

                    const send = async (token) => {
                        if (!token) return;
                        // Prefer the nuevo endpoint estable; fallback al viejo
                        try {
                            await apiFetch(this.API + '/api/users/push-token', {
                                method: 'POST',
                                body: JSON.stringify({ user_id: this.userId, token, device })
                            });
                        } catch (e) {
                            await apiFetch(this.API + '/api/fcm/register', {
                                method: 'POST',
                                body: JSON.stringify({ user_id: this.userId, fcm_token: token })
                            });
                        }
                        // Si falla la nueva, intentar también la vieja
                        console.log('[push] token registrado:', token.slice(0, 18) + '...', 'device:', device);
                    };

                    const token = await msg.getToken({ serviceWorkerRegistration: reg });
                    if (token) await send(token);

                    // Auto-renovar cuando Firebase detecte cambio de token
                    msg.onTokenRefresh(async () => {
                        try {
                            const t = await msg.getToken({ serviceWorkerRegistration: reg });
                            await send(t);
                        } catch (e) { console.log('[push] token refresh error:', e.message); }
                    });

                    msg.onMessage(p => this._toast(p.notification?.title || 'OjoIA', p.notification?.body || '', 'danger'));
                }
            }
        } catch(e) { console.log('Push init skipped:', e.message); }
    },

    _toast(title, msg, type = 'info', opts = {}) {
        const colors = { danger: '#ff453a', warning: '#ffd60a', success: '#30d158', info: '#0a84ff' };
        const t = document.createElement('div');
        // B1: duration configurable via opts.duration (default 6000ms); action button opcional
        const duration = opts.duration || 6000;
        t.style.cssText = `position:fixed;top:60px;left:16px;right:16px;background:#1c1c1e;border-left:3px solid ${colors[type] || colors.info};border-radius:12px;padding:14px 16px;z-index:9999;box-shadow:0 8px 32px rgba(0,0,0,0.5);animation:slideDown .3s ease;cursor:pointer`;
        // Build inner HTML: title (optional) + body + optional action button (B1)
        let innerHTML = '';
        if (title) innerHTML += `<div style="font-weight:600;font-size:.9rem;margin-bottom:3px"></div>`;
        // Escape content to avoid XSS (M4.2)
        const esc = (s) => String(s || '').replace(/[&<>"']/g, ch => ({ '&':'&','<':'<','>':'>','"':'"',"'":'&#39;' }[ch]));
        innerHTML = '';
        if (title) innerHTML += `<div style="font-weight:600;font-size:.9rem;margin-bottom:3px">${esc(title)}</div>`;
        innerHTML += `<div style="font-size:.82rem;color:#aeaeb2">${esc(msg)}</div>`;
        if (opts && opts.action && opts.action.label) {
            innerHTML += `<button class="toast-action" style="margin-top:8px;background:${colors[type] || colors.info};color:white;border:0;padding:6px 14px;border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer">${esc(opts.action.label)}</button>`;
        }
        t.innerHTML = innerHTML;
        document.body.appendChild(t);
        if (opts && opts.action && opts.action.onClick) {
            const btn = t.querySelector('.toast-action');
            if (btn) btn.addEventListener('click', e => { e.stopPropagation(); opts.action.onClick(); t.remove(); });
        }
        // Click en el toast entero tambien lo descarta
        t.addEventListener('click', () => t.remove());
        setTimeout(() => { t.style.animation = 'fadeOut .3s ease forwards'; setTimeout(() => t.remove(), 300); }, duration);
    },

    // B1: alias compatible con llamadas existentes _showToast(msg, ms, opts).
    // Antes NO existia y estos toasts nunca aparecian. Ahora enruta a _toast.
    _showToast(msg, durationOrType, opts) {
        if (typeof durationOrType === 'number') {
            // Formato: _showToast(msg, ms, opts)
            this._toast('', msg, 'info', { ...(opts || {}), duration: durationOrType });
        } else {
            // Formato: _showToast(msg, type) - тихо extendido
            this._toast('', msg, durationOrType || 'info');
        }
    },

    // ── HOME ─────────────────────────────────────────────────
    async _pageHome(c) {
        this._resetScrollContent(c);
        c.innerHTML = this._skeleton();
        try {
            const [camsSettled, evtsSettled, profileSettled] = await Promise.allSettled([
                apiFetch(`${this.API}/api/cameras?user_id=${this.userId || 'default'}`),
                apiFetch(`${this.API}/api/user/events?user_id=${this.userId || 'default'}&limit=20`),
                apiFetch(`${this.API}/api/user/profile?user_id=${this.userId || 'default'}`)
            ]);
            // Si el endpoint crítico (cámaras) falla, sí mostrar "Sin conexión".
            // Pero si solo fallan eventos o perfil, continuar con datos parciales.
            if (camsSettled.status === 'rejected') {
                throw new Error('cameras-fetch-failed');
            }
            const cams = (await camsSettled.value.json()).cameras || [];
            const evts = evtsSettled.status === 'fulfilled' ? ((await evtsSettled.value.json()).events || []) : [];
            const profile = profileSettled.status === 'fulfilled' ? (await profileSettled.value.json()) : {};
            if (!this._isCurrentPage('home') && !this._isCurrentPage('cameras')) return;

            this._homeCams = cams;
            this._homeViewCount = Number(localStorage.getItem('ojoia_home_view_count') || 1);
            this._homeViewCount = [1, 2, 4, 8, 16].includes(this._homeViewCount) ? this._homeViewCount : 1;

            const on = cams.filter(x => x.active).length;
            // Banner de alerta: REUSA .last-alert.
            // El evento que se muestra es el ÚLTIMO evento que generó una alerta
            // (attention/violation/sentinel/vigilance_alert O con attention_hits),
            // no "el último evento que vio la cámara". Se prioriza _activeAlertEvent
            // (fijado por un push reciente) sobre el más reciente del poll.
            const isAlertEvt = (e) => {
                if (!e) return false;
                const t = (e.event_type || '').toLowerCase();
                if (['attention','violation','sentinel','vigilance_alert'].includes(t)) return true;
                if (e.qwen && e.qwen.violation) return true;
                if (Array.isArray(e.attention_hits) && e.attention_hits.length > 0) return true;
                if (e.qwen_json && e.qwen_json.after_hours && e.qwen_json.importancia === 'alta') return true;
                return false;
            };
            let alertEvt = null;
            // 1) Si llego un push reciente (_activeAlertEvent), buscar ese evento en evts
            //    para tener todos sus datos. Si no está en la página, sigue la 2.
            if (this._activeAlertEvent && this._activeAlertEvent.eventId) {
                alertEvt = evts.find(e => e.event_id === this._activeAlertEvent.eventId) || null;
                if (!alertEvt) {
                    // No estaba en los 20 más recientes: fetch directo.
                    try {
                        const r = await apiFetch(`${this.API}/api/events/${this._activeAlertEvent.eventId}?user_id=${this.userId || 'default'}`);
                        if (r.ok) alertEvt = await r.json();
                    } catch(e) {}
                }
            }
            // 2) Sino, primer evento de alerta del poll (filtrado).
            if (!alertEvt) {
                alertEvt = evts.find(isAlertEvt) || null;
            }
            // Saldud del banner: si refrescó Home tras descartar, _activeAlertEvent
            // queda null y no se repinta.
            const heroClass = on > 0 ? 'ok' : 'off';

            let lastAlertHTML = '';
            if (alertEvt && isAlertEvt(alertEvt)) {
                const lastEvt = alertEvt;
                // Guardar como alerta activa para el resto de la sesión.
                this._activeAlertEvent = this._activeAlertEvent || {};
                this._activeAlertEvent.eventId = lastEvt.event_id;
                this._activeAlertEvent.cameraId = lastEvt.camera_id || '';
                this._activeAlertEvent.ts = Date.now();

                const isViolation = lastEvt.qwen?.violation || lastEvt.event_type === 'violation';
                const isAttention = (lastEvt.attention_hits && lastEvt.attention_hits.length > 0) || lastEvt.event_type === 'attention';
                const isSentinel = lastEvt.event_type === 'sentinel' || lastEvt.event_type === 'vigilance_alert' || (lastEvt.qwen_json?.after_hours && lastEvt.qwen_json?.importancia === 'alta');
                const ts = lastEvt.timestamp ? new Date(lastEvt.timestamp * 1000).toLocaleTimeString('es-ES', {hour:'2-digit', minute:'2-digit', hour12:true}) : '--';
                const evtDesc = lastEvt.description || lastEvt.summary || (lastEvt.qwen_json?.summary || '');
                const evtHits = lastEvt.attention_hits || [];
                const zoneName = (Array.isArray(lastEvt.attention_hits_zones) && lastEvt.attention_hits_zones.length && lastEvt.attention_hits_zones[0])
                    || lastEvt.camera_zone || (lastEvt.qwen_json && lastEvt.qwen_json.zone) || '';
                const alertColor = isSentinel ? 'var(--warning, #f5a623)' : 'var(--danger)';
                const alertIcon = isSentinel ? '🛡️' : (isAttention ? '🔍' : '🚨');
                const alertTitle = isSentinel ? 'FUERA DE HORARIO — Se detectó presencia' : (isAttention ? 'Observación relevante' : 'Alerta');
                const blinkClass = isSentinel ? '' : ' alert-blink';
                lastAlertHTML = `<div class="last-alert${blinkClass}" onclick="App._openEvent('${lastEvt.event_id}')" style="background:${isSentinel ? 'rgba(245,166,35,0.08)' : 'rgba(255,59,48,0.06)'};border:2px solid ${alertColor};padding:14px 16px;border-radius:12px;margin-bottom:12px;cursor:pointer">
                    <div style="font-size:.78rem;color:${alertColor};font-weight:700;margin-bottom:6px">${alertIcon} ${alertTitle}${zoneName ? ' · ' + escHtml(zoneName) : ''} — ${ts}</div>
                    <div style="font-size:.92rem;line-height:1.4;margin-bottom:6px">${escHtml((evtDesc || 'Se detectó actividad en la zona').substring(0,220))}</div>
                    ${evtHits.length ? `<div style="font-size:.78rem;color:var(--text-secondary);margin-bottom:6px">🔍 ${evtHits.slice(0, 2).map(escHtml).join(', ')}</div>` : ''}
                    <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
                        <button class="btn btn-sm" onclick="event.stopPropagation();App._openEvent('${lastEvt.event_id}')" style="background:var(--accent);color:#fff;border:none;padding:8px 16px;border-radius:8px;font-size:.82rem">Ver detalle</button>
                        <button class="btn btn-sm" onclick="event.stopPropagation();App._dismissEvent('${lastEvt.event_id}')" style="background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);padding:8px 16px;border-radius:8px;font-size:.82rem">✓ Falsa alarma</button>
                    </div>
                </div>`;
            } else {
                this._activeAlertEvent = null;
            }

            const heroText = on > 0 
                ? `${on} de ${cams.length} cámaras activas` 
                : cams.length > 0 
                    ? `${cams.length} cámaras sin conexión` 
                    : 'Sin cámaras';

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
                                <div style="font-weight:600">${escHtml(cam.name || `ojo-${shortId}`)}</div>
                                <div class="meta">${escHtml(cam.zone || 'sin zona')} · ${escHtml(shortId)}</div>
                            </div>
                            <span class="home-cam-status" data-home-status="${cam.camera_id}" style="color:${statusColor}">${status}</span>
                        </div>
                        <div id="home-frame-${i}" class="home-frame-wrap"><div class="ojo-placeholder">Esperando imagen...</div></div>
                        <button class="btn btn-sm btn-outline" style="width:100%;margin-top:8px" onclick="event.stopPropagation();App._saveRecentClip('${cam.camera_id}')">Guardar 45 min</button>
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
        } catch(e) {}
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
        } catch(e) {}
    },

    _homeCams: [],
    _homeActiveCamId: null,
    _activeAlertEvent: null,
    _homeViewCount: Number(localStorage.getItem('ojoia_home_view_count') || 1),
    _homeFrameInFlight: {},
    _homeLastDetectionsByCam: {},
    _homeWatermarkTextByCam: {},
    _homeLastYoloFetchByCam: {},
    _homeStreamStarted: {},  // {camId: true} — MJPEG stream ya iniciado
    _gridSettingsCamId: null,

    _fetchHomeFrames() {
        const cams = this._getHomeViewCams();
        cams.forEach((cam, i) => {
            const camId = cam.camera_id;
            const targetId = `home-frame-${i}`;
            const key = `${targetId}:${camId}`;
            // Iniciar MJPEG stream solo una vez por cámara
            if (!this._homeStreamStarted[key]) {
                this._homeStreamStarted[key] = true;
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

        // Refrescar frame: resetear estado de streamviejo para que _fetchFrameForCam vuelva a instalar el MJPEG
        const liveKey = `live-wrap:${this._homeActiveCamId}`;
        if (this._homeActiveCamId && this._homeActiveCamId !== camId) {
            const oldKey = `live-wrap:${this._homeActiveCamId}`;
            if (this._streamWatchdogTimers && this._streamWatchdogTimers[oldKey]) { clearTimeout(this._streamWatchdogTimers[oldKey]); delete this._streamWatchdogTimers[oldKey]; }
            this._homeStreamStarted[oldKey] = false;
        }
        this._homeStreamStarted[liveKey] = false;
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
                    <img src="${rawUrl}" class="live-img" decoding="async" style="width:100%;height:auto;aspect-ratio:1/1;object-fit:contain;display:block">
                    <canvas class="yolo-canvas" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></canvas>
                    <div class="stream-overlay" style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,0.6);color:#fff;font-size:.7rem;padding:3px 8px;border-radius:6px;display:none"></div>
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
            
            // Dibujar pose/silueta si hay keypoints
            const keypoints = d.keypoints || d.pose?.keypoints || [];
            if (keypoints && keypoints.length >= 15) {
                this._drawPoseSkeleton(ctx, keypoints, offsetX, offsetY, sx, sy, color, drawW, drawH);
            }
        });
    },
    
    _drawPoseSkeleton(ctx, keypoints, offsetX, offsetY, sx, sy, color, drawW, drawH) {
        // Keypoints COCO: 0=nose, 1=LEye, 2=REye, 3=LEar, 4=REar, 5=LSho, 6=RSho, 7=LElb, 8=RElb, 9=LWri, 10=RWri, 11=LHip, 12=RHip, 13=LKne, 14=RKne, 15=LAnk, 16=RAnk
        const skeleton = [
            [16, 14], [14, 12], [15, 13], [13, 11], // piernas (R: ankle→knee→hip, L: ankle→knee→hip)
            [12, 11], // caderas
            [6, 5], // hombros
            [11, 5], [12, 6], // torso (hip→shoulder)
            [5, 7], [7, 9], [6, 8], [8, 10], // brazos (shoulder→elbow→wrist)
            [0, 1], [0, 2], // nariz→ojos
            [1, 3], [2, 4], // ojos→orejas
            [5, 6] // conectar hombros
        ];
        const kpSize = Math.max(3, Math.min(drawW, drawH) * 0.005);
        const lineWidth = Math.max(2, Math.min(drawW, drawH) * 0.004);
        
        // Dibujar líneas del esqueleto
        ctx.strokeStyle = color;
        ctx.lineWidth = lineWidth;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        skeleton.forEach(([i, j]) => {
            if (i < keypoints.length && j < keypoints.length) {
                const kp1 = keypoints[i];
                const kp2 = keypoints[j];
                if (kp1 && kp2 && kp1.length >= 2 && kp2.length >= 2) {
                    const [x1, y1] = kp1;
                    const [x2, y2] = kp2;
                    // Verificar que las coordenadas sean válidas (> 0 y dentro de la imagen)
                    if (x1 > 0 && y1 > 0 && x2 > 0 && y2 > 0 && x1 < 10000 && y1 < 10000 && x2 < 10000 && y2 < 10000) {
                        ctx.beginPath();
                        ctx.moveTo(offsetX + x1 * sx, offsetY + y1 * sy);
                        ctx.lineTo(offsetX + x2 * sx, offsetY + y2 * sy);
                        ctx.stroke();
                    }
                }
            }
        });
        
        // Dibujar puntos de keypoints
        ctx.fillStyle = color;
        keypoints.forEach((kp, idx) => {
            if (kp && kp.length >= 2 && kp[0] > 0 && kp[1] > 0 && kp[0] < 10000 && kp[1] < 10000) {
                const [x, y] = kp;
                ctx.beginPath();
                ctx.arc(offsetX + x * sx, offsetY + y * sy, kpSize, 0, Math.PI * 2);
                ctx.fill();
            }
        });
    },

    _drawZonesOnStream(camId, targetId) {
        // Cache de zonas por cámara (refrescar cada 30s)
        if (!this._zoneCache) this._zoneCache = {};
        const cache = this._zoneCache[camId];
        if (cache && Date.now() - cache.ts < 30000 && cache.zones.length > 0) {
            this._renderZonesOnCanvas(targetId, cache.zones);
            return;
        }
        
        apiFetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}/zones?user_id=${encodeURIComponent(this.userId || 'default')}`)
            .then(r => r.json())
            .then(d => {
                const zones = d.zones || [];
                this._zoneCache[camId] = { ts: Date.now(), zones };
                this._renderZonesOnCanvas(targetId, zones);
            })
            .catch(() => {});
    },

    _renderZonesOnCanvas(targetId, zones) {
        const el = document.getElementById(targetId);
        if (!el || !zones.length) return;
        const canvasEl = el.querySelector('canvas.yolo-canvas');
        const imgEl = el.querySelector('img.live-img');
        if (!canvasEl || !imgEl) return;
        
        const ctx = canvasEl.getContext('2d');
        const cw = canvasEl.clientWidth || 1;
        const ch = canvasEl.clientHeight || 1;
        
        zones.forEach((zone, idx) => {
            const c = zone.coords || { x: 0, y: 0, w: 0, h: 0 };
            const x = c.x * cw;
            const y = c.y *  ch;
            const w = Math.abs(c.w * cw);
            const h = Math.abs(c.h * ch);
            
            ctx.strokeStyle = '#0a84ff';
            ctx.lineWidth = 2;
            ctx.setLineDash([5, 3]);
            ctx.strokeRect(x, y, w, h);
            ctx.setLineDash([]);
            
            ctx.fillStyle = 'rgba(10,132,255,0.15)';
            ctx.fillRect(x, y, w, h);
            
            ctx.fillStyle = '#fff';
            ctx.font = 'bold 13px sans-serif';
            ctx.fillText(`${idx+1}. ${zone.name}`, x + 4, y + 16);
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
        const el = document.getElementById(targetId);
        if (!el) return;
        const uid = this.userId || 'default';
        const camIdShort = camId.substring(0, 8);
        const zone = (this._homeCams && this._homeCams.find) ? (this._homeCams.find(c=>c.camera_id===camId)?.zone || '—') : '—';
        const nowText = new Date().toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
        const dateText = new Date().toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit',year:'2-digit'});
        const watermark = `OJO-${camIdShort} | ${dateText} ${nowText} | ${zone}`;
        this._homeWatermarkTextByCam[camId] = watermark;

        const baseStreamUrl = `${this.API}/cameras/${camId}/stream?user_id=${uid}&fps=5`;
        const onImgLoad = () => {
            this._streamErrors[key] = 0;
            this._streamLastOnloadTs[key] = Date.now();
            this._setStreamOverlay(el, '');
            this._drawYoloBoxes(camId, this._homeLastDetectionsByCam[camId] || [], this._homeWatermarkTextByCam[camId] || watermark, targetId);
            // P2 (zonas Fase 1): dibujar overlay de zonas configuradas sobre el live stream
            this._drawZonesOnStream(camId, targetId);
        };
        const onImgError = () => { this._reconnectStream(camId, targetId, baseStreamUrl, watermark, onImgLoad, onImgError); };

        // Iniciar el stream solo una vez por (camId, targetId). No reasignar src si ya vivo.
        const existing = el.querySelector('img.live-img');
        if (existing && existing.dataset.camId === camId && this._homeStreamStarted[key]) {
            // Stream ya activo: solo refrescar YOLO metadata
            this._armStreamWatchdog(camId, targetId, baseStreamUrl, watermark, onImgLoad, onImgError);
        } else {
            this._homeStreamStarted[key] = true;
            const dom = this._ensureLiveFrameDom(camId, `${baseStreamUrl}&_=${Date.now()}`, watermark, onImgLoad, onImgError, targetId);
            if (dom) { dom.imgEl.dataset.camId = camId; this._streamLastOnloadTs[key] = Date.now(); this._armStreamWatchdog(camId, targetId, baseStreamUrl, watermark, onImgLoad, onImgError); }
        }

        // YOLO metadata polling (cada 2s, independiente del stream)
        const lastYoloFetch = this._homeLastYoloFetchByCam[camId] || 0;
        const shouldFetchYolo = Date.now() - lastYoloFetch >= this._homeYoloPollMs;
        if (shouldFetchYolo) {
            this._homeLastYoloFetchByCam[camId] = Date.now();
            this._fetchYoloMetadata(camId, el, nowText, camIdShort, zone, targetId);
        }
    },

    _setStreamOverlay(el, msg) {
        const o = el && el.querySelector('.stream-overlay');
        if (!o) return;
        if (msg) { o.textContent = msg; o.style.display = 'block'; } else { o.style.display = 'none'; }
    },

    _reconnectStream(camId, targetId, baseStreamUrl, watermark, onImgLoad, onImgError) {
        const key = `${targetId}:${camId}`;
        this._streamErrors[key] = (this._streamErrors[key] || 0) + 1;
        const n = this._streamErrors[key];
        const el = document.getElementById(targetId);
        this._setStreamOverlay(el, n === 1 ? '⏳ Reconectando…' : `⏳ Reintento ${n}`);
        const delay = (this._streamReconnectDelays[n - 1] != null) ? this._streamReconnectDelays[n - 1] : 12000;
        if (!this._streamErrTimers) this._streamErrTimers = {};
        if (this._streamErrTimers[key]) clearTimeout(this._streamErrTimers[key]);
        this._streamErrTimers[key] = setTimeout(() => {
            const e2 = document.getElementById(targetId);
            if (!e2) return;
            let imgEl = e2.querySelector('img.live-img');
            const url = `${baseStreamUrl}&_=${Date.now()}`;
            if (!imgEl) {
                const dom = this._ensureLiveFrameDom(camId, url, watermark, onImgLoad, onImgError, targetId);
                if (dom) { imgEl = dom.imgEl; imgEl.dataset.camId = camId; }
            } else {
                imgEl.onload = onImgLoad;
                imgEl.onerror = onImgError;
                this._homeStreamStarted[key] = true;
                imgEl.src = url;
            }
            this._streamLastOnloadTs[key] = Date.now();
        }, delay);
    },

    _armStreamWatchdog(camId, targetId, baseStreamUrl, watermark, onImgLoad, onImgError) {
        const key = `${targetId}:${camId}`;
        if (this._streamWatchdogTimers[key]) clearTimeout(this._streamWatchdogTimers[key]);
        const tick = () => {
            const el = document.getElementById(targetId);
            const imgEl = el && el.querySelector('img.live-img');
            if (!el || !imgEl) { return; }
            if (this._streamWatchdogPaused) { schedule(); return; }
            const last = this._streamLastOnloadTs[key] || 0;
            if (imgEl.complete && Date.now() - last > this._streamWatchdogMs) {
                this._setStreamOverlay(el, '⏳ Reiniciando stream…');
                this._reconnectStream(camId, targetId, baseStreamUrl, watermark, onImgLoad, onImgError);
            }
            schedule();
        };
        const schedule = () => { this._streamWatchdogTimers[key] = setTimeout(tick, this._streamWatchdogMs); };
        schedule();
    },

    _loadCamVigilance(cam) {
        // B5: antes era `return;` silencioso. Ahora aplica el estado de centinela
        // a la cam activa y refresca el badge correspondiente si esta en DOM.
        // No es bloqueante (best-effort) para no romper el flujo del Home.
        if (!cam || !cam.camera_id) return;
        try {
            // Detectar si estamos en horario de centinela (mismo algoritmo que _pageHome)
            const sched = cam.schedule || {};
            const vig = cam.vigilance || {};
            let isVigilante = false;
            if (sched && sched.open && sched.close) {
                const now = new Date();
                const curMin = now.getHours() * 60 + now.getMinutes();
                const openParts = (sched.open).split(':');
                const closeParts = (sched.close).split(':');
                const openMin = parseInt(openParts[0]) * 60 + parseInt(openParts[1]);
                const closeMin = parseInt(closeParts[0]) * 60 + parseInt(closeParts[1]);
                const graceMin = vig.grace_minutes || 15;
                const vigilanteStart = closeMin + graceMin;
                isVigilante = (curMin < openMin || curMin >= vigilanteStart);
            }
            // Guardar el flag en la cam del cache (para que _pageHome lo use al re-render)
            cam._isVigilanteCached = isVigilante;
            // Actualizar el badge del home en vivo si existe
            const badge = document.getElementById('home-vigilance-badge');
            if (badge) {
                if (isVigilante) {
                    badge.textContent = '🛡️ CENTINELA';
                    badge.style.background = 'var(--danger)';
                    badge.style.color = '#fff';
                    badge.style.display = 'inline-block';
                    const cooldownMin = cam.cooldown_min || 5;
                    badge.title = `Modo centinela activo — notificará cada ${cooldownMin} min ante detección`;
                } else {
                    badge.textContent = '● NORMAL';
                    badge.style.background = 'var(--success)';
                    badge.style.color = '#fff';
                    badge.style.display = 'inline-block';
                    badge.title = 'En horario normal — vigilancia desactivada';
                }
            }
            // Sync el cache local del home
            const idx = this._homeCams ? this._homeCams.findIndex(c => c.camera_id === cam.camera_id) : -1;
            if (idx >= 0) {
                this._homeCams[idx] = { ...this._homeCams[idx], ...cam };
            }
        } catch(e) {
            console.warn('[vigilance] _loadCamVigilance error:', e.message);
        }
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
        } catch(e) {}
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
        } catch(e) {}
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
                                    <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();App._openCameraTimeline('${cam.camera_id}')">Ver últimos 45 min</button>
                                    <button class="btn btn-sm" onclick="event.stopPropagation();App._saveRecentClip('${cam.camera_id}')">Guardar 45 min</button>
                                    <button class="btn btn-sm" onclick="event.stopPropagation();App._openVigilanceSettings('${cam.camera_id}')">🛡️ Ajustar protección</button>
                            <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();App._openCameraConfig('${cam.camera_id}')">⚙️ Ajustes de cámara</button>
                            <button class="btn-ghost btn-sm" style="color:var(--danger)" onclick="App.deleteCamera('${cam.camera_id}','${cam.name}')">🗑️</button>
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

    async _ensureMinimalEvaBusiness() {
        if (this._minimalEvaBusiness || !this.userId) return;
        try {
            const r = await apiFetch(`${this.API}/api/user/profile?user_id=${this.userId}`);
            const d = await r.json();
            this._minimalEvaBusiness = d.business_name || '';
        } catch(e) {}
    },

    _renderMinimalEva() {
        const c = document.getElementById('app-content');
        if (!c) return;
        const messages = this._minimalEvaMessages || [];
        const html = messages.map(m => {
            const isUser = m.role === 'user';
            const text = (m.content || '').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
            const img = m.image_url ? `<div style="margin-top:10px"><img src="${m.image_url}" style="width:100%;max-height:300px;object-fit:contain;border-radius:12px;background:#0a0a0a"></div>` : '';
            return `<div style="display:flex;justify-content:${isUser ? 'flex-end' : 'flex-start'};margin-bottom:10px"><div style="max-width:86%;background:${isUser ? 'var(--accent)' : 'var(--bg-secondary)'};border:1px solid rgba(255,255,255,0.06);border-radius:18px;padding:12px 15px;line-height:1.45;font-size:.95rem;color:var(--text-primary)">${text}${img}</div></div>`;
        }).join('');
        c.innerHTML = `<div id="eva-chat-container" data-minimal-eva="true" style="height:100%;width:100%;min-height:0;display:flex;flex-direction:column;background:var(--bg-primary)">
            <div style="flex-shrink:0;height:72px;display:flex;align-items:center;justify-content:space-between;padding:0 18px;border-bottom:1px solid rgba(255,255,255,0.06);background:rgba(28,28,30,0.72);backdrop-filter:blur(24px)">
                <div style="display:flex;align-items:center;gap:10px;min-width:0">
                    <div style="width:36px;height:36px;border-radius:12px;background:linear-gradient(135deg,#0a84ff,#30d158);display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:.95rem">E</div>
                    <div style="min-width:0"><div style="font-weight:600;font-size:1rem">Eva</div><div style="font-size:.72rem;color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Asistente de seguridad</div></div>
                </div>
            </div>
            <div id="eva-messages" style="flex:1;min-height:0;overflow-y:auto;padding:16px;display:flex;flex-direction:column">${html}${this._minimalEvaLoading ? '<div style="color:var(--text-secondary);font-size:.9rem">Eva está escribiendo...</div>' : ''}</div>
            <div style="flex-shrink:0;padding:10px 16px 12px;background:rgba(0,0,0,0.18)">
                <div style="display:flex;gap:8px;align-items:center">
                    <input id="eva-input" autocomplete="off" placeholder="Escribe un mensaje a Eva..." style="flex:1;padding:12px 15px;border-radius:999px;border:1px solid rgba(255,255,255,0.08);background:rgba(44,44,46,0.72);color:var(--text-primary);font-size:.95rem;outline:none" onkeydown="if(event.key==='Enter') App._minimalEvaSend()">
                    <button id="eva-send-btn" onclick="App._minimalEvaSend()" style="width:42px;height:42px;border-radius:50%;border:none;background:var(--accent);color:#fff;font-size:1rem;cursor:pointer">↑</button>
                </div>
            </div>
        </div>`;
        const msg = document.getElementById('eva-messages');
        if (msg) msg.scrollTop = msg.scrollHeight;
        const input = document.getElementById('eva-input');
        if (input) setTimeout(() => input.focus(), 100);
    },

    _restoreMinimalEva() {
        const chat = document.getElementById('eva-chat-container');
        if (chat) chat.setAttribute('data-minimal-eva', 'true');
        if (!this._minimalEvaMessages || !this._minimalEvaMessages.length) {
            this._minimalEvaMessages = [{ role: 'assistant', content: 'Conectando con Eva...' }];
        }
        this._renderMinimalEva();
    },

    async _minimalEvaSend() {
        const input = document.getElementById('eva-input');
        const btn = document.getElementById('eva-send-btn');
        const msg = input?.value?.trim();
        if (!msg || this._minimalEvaLoading || !this.userId) return;
        input.value = '';
        this._minimalEvaLoading = true;
        this._minimalEvaMessages.push({ role: 'user', content: msg });
        this._renderMinimalEva();
        await this._ensureMinimalEvaBusiness();
        try {
            const r = await apiFetch(`${this.API}/config/chat`, {
                method: 'POST',
                body: JSON.stringify({
                    user_id: this.userId,
                    message: msg,
                    session_id: this._minimalEvaSession || '',
                    user_name: firebase.auth().currentUser?.displayName || '',
                    business_name: this._minimalEvaBusiness || ''
                })
            });
            const d = await r.json();
            if (d.success) {
                this._minimalEvaSession = d.sessionId || this._minimalEvaSession;
                localStorage.setItem(`eva_session_${this.userId}`, this._minimalEvaSession);
                this._minimalEvaMessages.push({ role: 'assistant', content: d.response || '', image_url: d.image_url || '' });
                // F-DUP-CHAT: NO mutar EvaChat.history desde el chat minimal.
                // Antes este codigo hacia EvaChat.history.push + slice(-50) que
                // truncaba la conversacion real del chat principal y disparaba
                // race conditions con el polling remoto. El chat minimal tiene su
                // propio _minimalEvaMessages; el chat principal (EvaChat) debe
                // ser gestionado unicamente por eva-chat-v5.js.
                // Si el chat principal esta activo (mismo DOM), delegar a su sendMessage.
                if (typeof EvaChat !== 'undefined' && EvaChat.userId === this.userId && document.getElementById('eva-chat-container') && !document.getElementById('eva-chat-container').getAttribute('data-minimal-eva')) {
                    // Chat principal activo: solo dispara el render para mostrar el msg via polling/storage
                    // (no duplicamos el push — el backend ya tiene el msg y el polling lo trae en 10s)
                }
            } else {
                this._minimalEvaMessages.push({ role: 'assistant', content: d.response || d.error || 'No pude procesar ese mensaje.' });
            }
        } catch(e) {
            this._minimalEvaMessages.push({ role: 'assistant', content: 'Error de conexión. Verifica que el servidor esté activo.' });
        } finally {
            this._minimalEvaLoading = false;
            if (btn) btn.disabled = false;
            this._renderMinimalEva();
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
        } catch(e) {}
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
                            <button class="btn" id="save-clip-btn" onclick="App._saveRecentClip('${camId}')">Guardar últimos 45 min</button>
                        </div>
                        <div style="margin-top:16px;text-align:center">
                            <img id="timeline-img" style="width:100%;max-height:420px;object-fit:contain;background:#000;border-radius:14px" alt="Imagen reciente">
                        </div>
                        <div id="timeline-status" class="meta" style="margin-top:10px;text-align:center">—</div>
                        <div style="display:flex;gap:8px;align-items:center;margin-top:14px">
                            <button class="btn btn-sm" onclick="App._moveTimeline('${camId}', -1)">◀ Atrás</button>
                            <input id="timeline-range" type="range" min="0" max="${Math.max(0, frames.length - 1)}" value="${Math.max(0, frames.length - 1)}" style="flex:1" oninput="App._showTimelineFrame('${camId}', this.value)">
                            <button class="btn btn-sm" onclick="App._moveTimeline('${camId}', 1)">Adelante ▶</button>
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
        let cam;
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const d = await r.json();
            cam = (d.cameras || []).find(x => x.camera_id === camId);
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
                        <button class="btn" style="width:100%;margin-top:12px" onclick="App._openZoneEditor('${camId}')">📍 Configurar zonas de interés</button>
                    </section>

                    <section class="config-section" id="zone-editor-section" style="display:none">
                        <div class="section-heading">
                            <div>
                                <div class="section-kicker">Zonas de interés</div>
                                <div class="section-title">📍 Dibuja las áreas importantes</div>
                            </div>
                        </div>
                        <p class="meta" style="margin-bottom:12px;">Pulsa "➕ Nueva zona", ponle nombre y describe qué pasa ahí. Luego dibuja el rectángulo sobre la imagen. Eva vigilará esas áreas de forma prioritaria.</p>
                        <div id="zone-form" style="margin-bottom:12px"></div>
                        <div id="zone-canvas-container" style="position:relative;width:100%;max-width:640px;margin:0 auto;background:#000;border-radius:12px;overflow:hidden">
                            <img id="zone-canvas-bg" src="" style="width:100%;display:block;opacity:0.5">
                            <canvas id="zone-canvas" style="position:absolute;top:0;left:0;width:100%;height:100%;cursor:crosshair"></canvas>
                        </div>
                        <div id="zone-list" style="margin-top:16px"></div>
                        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
                            <button class="btn" onclick="App._saveZones('${camId}')">💾 Guardar zonas</button>
                            <button class="btn btn-outline" onclick="App._closeZoneEditor()">✕ Cancelar</button>
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
                                <input class="range-control" id="cfg_brightness" type="range" min="-100" max="100" value="0" oninput="App._updateImageFilter('${camId}')">
                                <div class="range-labels"><span>Oscuro</span><span>Brillante</span></div>
                            </div>
                            <div class="control-card">
                                <div class="control-label-row"><span>🎚️ Contraste</span><strong id="cfg_contrast_val" class="value-pill">0</strong></div>
                                <input class="range-control" id="cfg_contrast" type="range" min="-100" max="100" value="0" oninput="App._updateImageFilter('${camId}')">
                                <div class="range-labels"><span>Bajo</span><span>Alto</span></div>
                            </div>
                        </div>
                        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
                        <button class="btn config-action" data-config-cmd="brightness" onclick="App._sendCamCmd('${camId}','brightness',document.getElementById('cfg_brightness').value,this)">💾 Aplicar</button>
                        <button class="btn config-action" onclick="App._resetBrightnessContrast('${camId}')">↩️ Restablecer valores</button>
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
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="0" onclick="App._sendCamCmd('${camId}','rotation',0,this)">0°</button>
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="1" onclick="App._sendCamCmd('${camId}','rotation',1,this)">90°</button>
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="2" onclick="App._sendCamCmd('${camId}','rotation',2,this)">180°</button>
                            <button class="btn-ghost" data-config-cmd="rotation" data-config-value="3" onclick="App._sendCamCmd('${camId}','rotation',3,this)">270°</button>
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
                            <button class="btn-ghost" data-config-cmd="quality" data-config-value="8" onclick="App._sendCamCmd('${camId}','quality',8,this)">Baja</button>
                            <button class="btn-ghost" data-config-cmd="quality" data-config-value="12" onclick="App._sendCamCmd('${camId}','quality',12,this)">Media</button>
                            <button class="btn-ghost" data-config-cmd="quality" data-config-value="6" onclick="App._sendCamCmd('${camId}','quality',6,this)">Alta</button>
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
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="200" onclick="App._sendCamCmd('${camId}','fps',200,this)">5 fps</button>
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="500" onclick="App._sendCamCmd('${camId}','fps',500,this)">2 fps</button>
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="1000" onclick="App._sendCamCmd('${camId}','fps',1000,this)">1 fps</button>
                            <button class="btn-ghost" data-config-cmd="fps" data-config-value="2000" onclick="App._sendCamCmd('${camId}','fps',2000,this)">0.5 fps</button>
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
                            <button class="btn-ghost" data-config-cmd="led" data-config-value="1" onclick="App._sendCamCmd('${camId}','led',1,this)">💡 On</button>
                            <button class="btn-ghost" data-config-cmd="led" data-config-value="0" onclick="App._sendCamCmd('${camId}','led',0,this)">🌙 Off</button>
                            <button class="btn-ghost" data-config-cmd="led_auto" data-config-value="1" onclick="App._sendCamCmd('${camId}','led_auto',1,this)">⚡ Auto</button>
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
                        <button class="btn config-action" onclick="App._saveCooldown('${camId}',this)">💾 Guardar cooldown</button>
                    </section>

                    <section class="config-actions">
                        <button class="btn" onclick="App._sendCamCmd('${camId}','snapshot',0,this)">📸 Snapshot</button>
                        <button class="btn btn-outline" onclick="App._exportVideo('${camId}', 45, this)">🎬 Guardar video (45 min)</button>
                        <button class="btn btn-outline" onclick="App._openVigilanceSettings('${camId}')">🛡️ Ajustar protección</button>
                    </section>

                    <button class="btn btn-ghost" onclick="App.go('${returnPage}')">← Volver</button>
                    </div>`;

            // Iniciar polling del viewer
            this._startConfigViewerPoll(camId);
        } catch(e) {
            if (this.page !== 'settings' && this.page !== 'cameras' && this.page !== 'home') return;
            c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:3rem;margin-bottom:16px;">❌</div><div style="font-weight:600;margin-bottom:8px;">Error cargando cámara</div><button class="btn" style="margin-top:16px" onclick="App._openCameraConfig(\''+camId+'\')">Reintentar</button></div>';
            return;
        }

        // Aplicar valores por defecto si la cámara es nueva (nunca configurada)
        if (cam) this._applyCamDefaults(camId, cam);
    },

async _applyCamDefaults(camId, cam) {
        // Solo aplicar si la cámara nunca ha sido configurada (first_seen == last_announce o no tiene interval_ms)
        const isNew = cam.first_seen && cam.last_announce && (cam.first_seen === cam.last_announce || !cam.interval_ms);
        if (!isNew) return;
        
        // Aplicar defaults
        this._configRotation = 0;
        const rotBtn = document.querySelector(`[data-config-cmd="rotation"][data-config-value="0"]`);
        if (rotBtn) rotBtn.click();
    },

    // ═══════════════════════════════════════════════════════════════════════
    // ZONE EDITOR — Dibujar zonas de interés sobre la imagen
    // ═══════════════════════════════════════════════════════════════════════
    
    _zoneEditorCamId: null,
    _zoneDrawing: false,
    _zoneStartPos: null,
    _zoneCurrentRect: null,
    _zoneList: [],  // [{id, name, type, coords: {x,y,w,h}, color, description}]
    // Zona en preparación (nombre+tipo+descripción ya definidos, esperando dibujo)
    _zonePending: null,  // {name, type, description} | null
    _zoneTypesCache: null,  // cache de /api/zone-types

    async _openZonesFromSettings() {
        // Entrada desde Ajustes > Detección > Zonas de interés.
        // Si hay una sola cámara, abre su config y el editor de zonas.
        // Si hay varias, primero pide seleccionar la cámara.
        let cams = this._homeCams || [];
        if (!cams.length) {
            try {
                const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
                cams = (await r.json()).cameras || [];
            } catch(e) {}
        }
        if (!cams.length) {
            this._toast('', 'Primero instala una cámara', 'danger');
            return;
        }
        const camId = cams[0].camera_id;
        // Abrir la config de la cámara y, tras render, abrir el editor de zonas.
        await this._openCameraConfig(camId);
        // _openCameraConfig renderiza #zone-editor-section en display:none.
        // Lo abrimos automáticamente y precargamos el editor.
        setTimeout(() => {
            try { this._openZoneEditor(camId); } catch(e) { console.warn('openZoneEditor auto:', e); }
            // auto-guardar: tras abrir el editor, la sección ya está visible.
            const section = document.getElementById('zone-editor-section');
            if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 400);
    },

    async _openZoneEditor(camId) {
        this._zoneEditorCamId = camId;
        this._zoneDrawing = false;
        this._zoneStartPos = null;
        this._zoneCurrentRect = null;
        this._zonePending = null;
        const section = document.getElementById('zone-editor-section');
        if (!section) return;
        section.style.display = 'block';

        // Punto #2: al abrir el editor de zonas, ocultar las demás secciones de
        // config (brillo, sensibilidad, etc.) para que el usuario NO se confunda
        // tocando botones irrelevantes mientras dibuja. Aplicamos una clase
        // '.zone-editing-active' al contenedor .camera-config-page que oculta vía
        // CSS todas las .config-section excepto #zone-editor-section y el hero.
        const page = document.querySelector('.camera-config-page');
        if (page) page.classList.add('zone-editing-active');

        // Cargar tipos de zona (cache) desde el backend (15 tipos) una sola vez.
        if (!this._zoneTypesCache) {
            try {
                const tr = await fetch(`${this.API}/api/zone-types`);
                const td = await tr.json();
                this._zoneTypesCache = (td.zone_types || []);
            } catch(e) {
                this._zoneTypesCache = [
                    {id:'cashier',name:'Caja',icon:'💰'},{id:'entrance',name:'Entrada',icon:'🚪'},
                    {id:'kitchen',name:'Cocina',icon:'🍳'},{id:'dining',name:'Comedor',icon:'🍽️'},
                    {id:'other',name:'Otra',icon:'📍'}
                ];
            }
        }

        // Cargar zonas existentes
        try {
            const r = await fetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}/zones?user_id=${encodeURIComponent(this.userId)}`);
            const d = await r.json();
            this._zoneList = d.zones || [];
        } catch(e) {
            this._zoneList = [];
        }

        // Configurar canvas con imagen actual de la cámara
        const canvas = document.getElementById('zone-canvas');
        const bgImg = document.getElementById('zone-canvas-bg');
        const liveImg = document.getElementById('cfg-live-img');
        if (liveImg && bgImg) {
            if (liveImg.src) bgImg.src = liveImg.src;
            // Esperar a que la imagen cargue para dimensionar el canvas correctamente
            const setupCanvas = () => this._setupZoneCanvas(canvas);
            if (bgImg.complete && bgImg.naturalWidth) setupCanvas();
            else bgImg.onload = setupCanvas;
        }
        this._renderZoneForm();
        this._renderZoneList();
    },

    _setupZoneCanvas(canvas) {
        if (!canvas) return;
        const container = canvas.parentElement;
        if (!container) return;
        // Forzar dimensiones del canvas al contenedor (igual a la imagen bg)
        const rect = container.getBoundingClientRect();
        const w = Math.max(100, Math.floor(rect.width));
        // Proporción de la imagen bg, fallback 4:3
        const bgImg = document.getElementById('zone-canvas-bg');
        let ratio = 0.75;
        if (bgImg && bgImg.naturalWidth && bgImg.naturalHeight) ratio = bgImg.naturalHeight / bgImg.naturalWidth;
        const h = Math.max(60, Math.floor(w * ratio));
        container.style.height = h + 'px';
        canvas.width = w;
        canvas.height = h;
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';

        // Limpiar listeners previos asignando handlers nativos
        const self = this;
        const getPos = (clientX, clientY) => {
            const r = canvas.getBoundingClientRect();
            return { x: clientX - r.left, y: clientY - r.top };
        };
        const start = (x, y) => {
            self._zoneDrawing = true;
            const p = getPos(x, y);
            self._zoneStartPos = p;
            self._zoneCurrentRect = { x: p.x, y: p.y, w: 0, h: 0 };
        };
        const move = (x, y) => {
            if (!self._zoneDrawing) return;
            const p = getPos(x, y);
            const sx = self._zoneStartPos.x, sy = self._zoneStartPos.y;
            self._zoneCurrentRect = {
                x: Math.min(sx, p.x), y: Math.min(sy, p.y),
                w: Math.abs(p.x - sx), h: Math.abs(p.y - sy)
            };
            self._drawZonesOnCanvas(canvas);
        };
        const end = (x, y) => {
            if (!self._zoneDrawing) return;
            self._zoneDrawing = false;
            if (x != null && y != null) { const p = getPos(x, y); self._zoneLastEnd = p; }
            self._finalizeZoneDraw(canvas);
        };

        canvas.onmousedown = (e) => { e.preventDefault(); start(e.clientX, e.clientY); };
        canvas.onmousemove = (e) => move(e.clientX, e.clientY);
        canvas.onmouseup = () => end(null, null);

        canvas.ontouchstart = (e) => { e.preventDefault(); const t = e.touches[0]; start(t.clientX, t.clientY); };
        canvas.ontouchmove = (e) => { e.preventDefault(); const t = e.touches[0]; move(t.clientX, t.clientY); };
        canvas.ontouchend = (e) => { e.preventDefault(); const t = (e.changedTouches && e.changedTouches[0]); end(t ? t.clientX : null, t ? t.clientY : null); };

        this._drawZonesOnCanvas(canvas);
    },

    _zoneTypeLabel(typeId) {
        const t = (this._zoneTypesCache || []).find(x => x.id === typeId);
        return t ? t.name : typeId;
    },
    _zoneTypeIcon(typeId) {
        const t = (this._zoneTypesCache || []).find(x => x.id === typeId);
        return t ? (t.icon || '📍') : '📍';
    },

    // Renderiza el formulario inline "+ Nueva zona" (nombre + tipo + descripción).
    // Sustituye a los prompt() nativos. La zona se confirma al "dibujar el área".
    _renderZoneForm() {
        const container = document.getElementById('zone-form');
        if (!container) return;
        const p = this._zonePending;
        if (!p) {
            container.innerHTML = `<button class="btn" style="width:100%" onclick="App._startNewZone()">➕ Nueva zona</button>`;
            return;
        }
        const types = this._zoneTypesCache || [];
        container.innerHTML = `
            <div class="zone-form-card">
                <div class="zone-form-title">Nueva zona</div>
                <div class="zone-field">
                    <input id="zone-input-name" class="zone-input" type="text" placeholder="Nombre (ej: Caja, Entrada…)" value="${(p.name||'').replace(/"/g,'"')}" oninput="App._zonePending.name=this.value">
                </div>
                <div class="zone-field">
                    <select id="zone-input-type" class="zone-select" onchange="App._zonePending.type=this.value">
                        ${types.map(t => `<option value="${t.id}" ${t.id===p.type?'selected':''}>${t.icon||''} ${t.name}</option`).join('')}
                    </select>
                </div>
                <div class="zone-field">
                    <textarea id="zone-input-desc" class="zone-input" rows="2" placeholder="¿Qué pasa en esta zona? (ej: aquí el cajero cobra y guarda dinero)" oninput="App._zonePending.description=this.value">${(p.description||'').replace(/</g,'<')}</textarea>
                </div>
                <div class="zone-form-hint">👇 Ahora dibuja el rectángulo sobre la imagen para definir el área.</div>
                <div style="display:flex;gap:8px">
                    <button class="btn" onclick="App._zonePreviewManual()">✅ Dibujé el área</button>
                    <button class="btn btn-outline" onclick="App._cancelZonePending()">Cancelar</button>
                </div>
            </div>`;
        // focus nombre
        setTimeout(() => { const el = document.getElementById('zone-input-name'); if (el && !p.name) el.focus(); }, 50);
    },

    _startNewZone() {
        // Inicia el flujo crear-zona: pide nombre/tipo/descripción primero.
        this._zonePending = { name: '', type: 'cashier', description: '' };
        this._renderZoneForm();
    },

    _cancelZonePending() {
        this._zonePending = null;
        this._renderZoneForm();
        const canvas = document.getElementById('zone-canvas');
        if (canvas) this._drawZonesOnCanvas(canvas);
    },

    _zonePreviewManual() {
        // Si el usuario ya dibujó un rectángulo en el canvas (modo legacy: dibujo
        // antes de rellenar el form), lo finaliza ahora.
        if (this._zoneCurrentRect && Math.abs(this._zoneCurrentRect.w) > 20) {
            const canvas = document.getElementById('zone-canvas');
            this._finalizeZoneDraw(canvas || document.getElementById('zone-canvas'));
            return;
        }
        // Si no, entra en modo "esperando dibujo": el siguiente drag sobre el
        // canvas creará la zona con los datos del formulario.
        this._toast('', 'arrastra el rectángulo sobre la imagen', 'info');
    },

    _finalizeZoneDraw(canvas) {
        const r = this._zoneCurrentRect;
        if (!r || Math.abs(r.w) < 20 || Math.abs(r.h) < 20) {
            // Dibujo muy chico: si hay pending, avisar que dibuje más grande.
            if (this._zonePending) {
                this._toast('', 'dibuja un rectángulo más grande', 'info');
            }
            this._drawZonesOnCanvas(canvas);
            return;
        }
        // Normalizar coords (0-1) relativas al canvas
        const cw = canvas.width, ch = canvas.height;
        const normX = r.x / cw, normY = r.y / ch, normW = r.w / cw, normH = r.h / ch;

        // Datos: si hay _zonePending (flujo nuevo), usarlos; si no (wizard legacy),
        // crear zona por defecto con nombre "Zona N".
        let name, zoneType, description;
        if (this._zonePending) {
            name = (this._zonePending.name || '').trim() || `Zona ${this._zoneList.length + 1}`;
            zoneType = this._zonePending.type || 'other';
            description = (this._zonePending.description || '').trim();
            this._zonePending = null;
        } else {
            name = `Zona ${this._zoneList.length + 1}`;
            zoneType = 'other';
            description = '';
        }
        const colors = ['rgba(255,0,0,0.3)', 'rgba(0,255,0,0.3)', 'rgba(0,0,255,0.3)', 'rgba(255,255,0,0.3)', 'rgba(255,0,255,0.3)'];
        const color = colors[this._zoneList.length % colors.length];
        this._zoneList.push({
            id: 'zone_' + Date.now(), name: name, type: zoneType,
            coords: { x: normX, y: normY, w: normW, h: normH },
            description: description, color: color
        });
        this._renderZoneList();
        this._renderZoneForm();
        this._drawZonesOnCanvas(canvas);
    },

    _drawZonesOnCanvas(canvas) {
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        
        // Dibujar zonas existentes
        this._zoneList.forEach((zone, idx) => {
            const x = zone.coords.x * canvas.width;
            const y = zone.coords.y * canvas.height;
            const w = zone.coords.w * canvas.width;
            const h = zone.coords.h * canvas.height;
            
            ctx.fillStyle = zone.color || 'rgba(0,168,255,0.3)';
            ctx.fillRect(x, y, w, h);
            ctx.strokeStyle = '#0a84ff';
            ctx.lineWidth = 2;
            ctx.strokeRect(x, y, w, h);
            
            // Etiqueta de nombre(más legible que solo texto)
            const label = `${idx+1}. ${zone.name}`;
            ctx.font = 'bold 13px sans-serif';
            const tw = ctx.measureText(label).width + 10;
            ctx.fillStyle = 'rgba(0,0,0,0.65)';
            ctx.fillRect(x + 2, y + 2, tw, 20);
            ctx.fillStyle = '#fff';
            ctx.fillText(label, x + 7, y + 16);
        });
        
        // Dibujar rectángulo actual (si se está dibujando) + preview del nombre
        if (this._zoneDrawing && this._zoneCurrentRect) {
            const x = this._zoneCurrentRect.x;
            const y = this._zoneCurrentRect.y;
            const w = this._zoneCurrentRect.w;
            const h = this._zoneCurrentRect.h;
            
            ctx.fillStyle = 'rgba(10,132,255,0.2)';
            ctx.fillRect(x, y, w, h);
            ctx.strokeStyle = '#0a84ff';
            ctx.lineWidth = 2;
            ctx.strokeRect(x, y, w, h);

            // Preview del nombre en tiempo real (si hay pending)
            const previewName = (this._zonePending && this._zonePending.name)
                ? this._zonePending.name
                : (this._zonePending ? `Zona ${this._zoneList.length + 1}` : null);
            if (previewName && Math.abs(w) > 30) {
                ctx.font = 'bold 13px sans-serif';
                const tw = ctx.measureText(previewName).width + 10;
                ctx.fillStyle = 'rgba(10,132,255,0.85)';
                ctx.fillRect(x + 2, y + 2, tw, 20);
                ctx.fillStyle = '#fff';
                ctx.fillText(previewName, x + 7, y + 16);
            }
            // Tooltip área
            if (Math.abs(w) > 40 && Math.abs(h) > 30) {
                const pct = (Math.abs(w) * Math.abs(h) / (canvas.width * canvas.height) * 100);
                ctx.fillStyle = 'rgba(0,0,0,0.6)';
                ctx.font = '11px sans-serif';
                ctx.fillText(`${pct.toFixed(1)}%`, x + 4, y + Math.max(22, h - 6));
            }
        }
    },

    _renderZoneList() {
        const container = document.getElementById('zone-list');
        if (!container) return;
        
        if (this._zoneList.length === 0) {
            container.innerHTML = '<div class="meta" style="text-align:center;padding:20px">No hay zonas configuradas. Pulsa "➕ Nueva zona" para crear una.</div>';
            return;
        }
        
        container.innerHTML = '<div style="margin-bottom:12px;font-weight:600">Zonas creadas:</div>' + 
            this._zoneList.map((z, idx) => `
                <div class="zone-row" data-zone-idx="${idx}">
                    <div class="zone-chip" style="background:${z.color};border:2px solid #0a84ff">${this._zoneTypeIcon(z.type)}</div>
                    <div style="flex:1;min-width:0">
                        <div style="font-weight:600">${idx+1}. ${escHtml(z.name)}</div>
                        <div class="meta">${this._zoneTypeLabel(z.type)} — (${(z.coords.x*100).toFixed(0)}%, ${(z.coords.y*100).toFixed(0)}) → (${((z.coords.x+z.coords.w)*100).toFixed(0)}%, ${((z.coords.y+z.coords.h)*100).toFixed(0)}%)</div>
                        ${z.description ? `<div class="meta" style="margin-top:4px;color:var(--text-secondary)">📝 ${escHtml(z.description)}</div>` : ''}
                    </div>
                    <button class="btn btn-sm btn-outline" onclick="App._editZone(${idx})">Editar</button>
                    <button class="btn btn-sm btn-danger" onclick="App._deleteZone(${idx})">✕</button>
                </div>
            `).join('');
    },

    _editZone(idx) {
        // Abre un sheet inline para editar nombre/tipo/descripción de una zona existente.
        const z = this._zoneList[idx];
        if (!z) return;
        const types = this._zoneTypesCache || [];
        const container = document.getElementById('zone-list');
        // Reemplazar la fila por el formulario de edición
        const rows = container.querySelectorAll('.zone-row');
        const row = rows[idx];
        if (!row) return;
        row.innerHTML = `
            <div class="zone-form-card" style="flex:1 1 100%;border:1px solid var(--accent)">
                <div class="zone-form-title">Editar ${escHtml(z.name)}</div>
                <div class="zone-field">
                    <input id="zone-edit-name" class="zone-input" type="text" value="${(z.name||'').replace(/"/g,'"')}">
                </div>
                <div class="zone-field">
                    <select id="zone-edit-type" class="zone-select">
                        ${types.map(t => `<option value="${t.id}" ${t.id===z.type?'selected':''}>${t.icon||''} ${t.name}</option`).join('')}
                    </select>
                </div>
                <div class="zone-field">
                    <textarea id="zone-edit-desc" class="zone-input" rows="2" placeholder="¿Qué pasa en esta zona?">${(z.description||'').replace(/</g,'<')}</textarea>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn" onclick="App._saveZoneEdit(${idx})">Guardar</button>
                    <button class="btn btn-outline" onclick="App._renderZoneList()">Cancelar</button>
                </div>
            </div>`;
    },

    _saveZoneEdit(idx) {
        const z = this._zoneList[idx];
        if (!z) return;
        const name = (document.getElementById('zone-edit-name')?.value || '').trim();
        if (name) z.name = name;
        const typeEl = document.getElementById('zone-edit-type');
        if (typeEl) z.type = typeEl.value;
        const desc = (document.getElementById('zone-edit-desc')?.value || '').trim();
        z.description = desc;
        this._renderZoneList();
        const canvas = document.getElementById('zone-canvas');
        if (canvas) this._drawZonesOnCanvas(canvas);
    },

    _deleteZone(idx) {
        this._zoneList.splice(idx, 1);
        this._renderZoneList();
        const canvas = document.getElementById('zone-canvas');
        if (canvas) this._drawZonesOnCanvas(canvas);
    },

    async _saveZones(camId) {
        const btn = event?.target;
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Guardando...';
        }
        
        try {
            const r = await fetch(`${this.API}/api/cameras/${encodeURIComponent(camId)}/zones?user_id=${encodeURIComponent(this.userId)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: this.userId,
                    zones: this._zoneList
                })
            });
            const d = await r.json();
            if (d.success) {
                alert(`✅ ${this._zoneList.length} zona(s) guardada(s) correctamente`);
                this._closeZoneEditor();
                // Si venimos del wizard de Eva, notificar que terminamos
                if (window.EvaChat && EvaChat._inWizardZoneDraw) {
                    EvaChat._inWizardZoneDraw = false;
                    EvaChat.addMessage('assistant', '¡Perfecto! Veo que configuraste las zonas. Ahora Eva está vigilando esas áreas de forma prioritaria.');
                }
            } else {
                alert('Error: ' + (d.error || 'No se pudo guardar'));
            }
        } catch(e) {
            alert('Error de conexión: ' + e.message);
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '💾 Guardar zonas';
            }
        }
    },

    _closeZoneEditor() {
        const section = document.getElementById('zone-editor-section');
        if (section) section.style.display = 'none';
        // Restaurar las demás secciones de config que ocultamos al abrir el editor.
        const page = document.querySelector('.camera-config-page');
        if (page) page.classList.remove('zone-editing-active');
        this._zoneEditorCamId = null;
        this._zoneList = [];
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

                    <button class="btn" style="width:100%;margin-bottom:8px;" onclick="App._saveVigilanceSettings('${camId}')">💾 Guardar y regenerar prompt</button>
                    <button class="btn btn-outline" style="width:100%;margin-bottom:8px;" onclick="App._regenerateVigilancePrompt('${camId}')">🔄 Regenerar prompt</button>
                    <button class="btn btn-outline" style="width:100%;margin-bottom:8px;" onclick="App._testVigilancePrompt('${camId}')">🧪 Probar con última imagen</button>
                    <button class="btn btn-outline" style="width:100%;" onclick="App._editVigilanceWithEva('${camId}')">🤖 Editar con Eva</button>
                    <button class="btn btn-ghost" style="width:100%;" onclick="App.go('settings')">← Volver</button>
                </div>`;
        } catch(e) {
            c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:3rem;margin-bottom:16px;">❌</div><div style="font-weight:600;">Error cargando protección</div></div>';
        }
    },

    async _saveVigilanceSettings(camId) {
        // M5.2: validar HH:MM antes de guardar para no romper _is_vigilante_mode del backend.
        const _isValidHHMM = (s) => /^\d{2}:\d{2}$/.test(s) && (() => {
            const [h, m] = s.split(':').map(Number);
            return h >= 0 && h <= 23 && m >= 0 && m <= 59;
        })();
        const openVal = document.getElementById('vig-open').value.trim() || '08:00';
        const closeVal = document.getElementById('vig-close').value.trim() || '22:00';
        if (!_isValidHHMM(openVal) || !_isValidHHMM(closeVal)) {
            this._toast('Horario inválido', 'Usa formato HH:MM (24h), ej: 08:00 / 22:00', 'danger');
            return;
        }
        const payload = {
            schedule: {
                open: openVal,
                close: closeVal
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
                    sel.innerHTML += `<option value="${escHtml(cam.camera_id)}">${escHtml(cam.name)}</option>`;
                });
            }
        } catch(e) {}
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
        } catch(e) {}
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
            // M4.4: ahora backend filtra por camera_id (antes el cliente filtraba 50 y podia salir vacio).
            const camParam = (this._eventFilterCam && this._eventFilterCam !== 'all')
                ? `&camera_id=${encodeURIComponent(this._eventFilterCam)}` : '';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=${filter}&limit=50${camParam}`);
            let evts = (await r.json()).events || [];
            // Doble check defensivo (por si el backend _global mezcla)
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
            // M4.5: thumbs con loading="lazy" directo en el row. Eliminamos el loop manual
            // de asignacion que hacia fetch extra por thumb.
            el.innerHTML = evts.map(evt => this._eventRowHtml(evt)).join('') + moreHtml;
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
            // M4.4: mismo - pasar camera_id backend-side
            const camParam = (this._eventFilterCam && this._eventFilterCam !== 'all')
                ? `&camera_id=${encodeURIComponent(this._eventFilterCam)}` : '';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=${filter}&limit=100${camParam}`);
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
        } catch(e) {}
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
        const camName = escHtml(evt.camera_name || evt.camera_id || 'Sin nombre');
        const camZone = escHtml(evt.metadata?.zone || '');
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
            // P2/P5: preferir person_tracking.unique_persons (filtra false-positives por track)
            const trackingUnique = evt.metadata?.person_tracking?.unique_persons;
            const yoloCount = (
                Number(trackingUnique) ||
                Number(evt.yolo?.count) ||
                Number(evt.yolo_count) ||
                (Array.isArray(yolo.detections) ? yolo.detections.length : 0) ||
                0
            );
        const icon = violation ? '🚨' : '✓';
        // M4.5: loading="lazy" directo en el row elimina el loop manual de crispado de thumbs.
        // Usar thumb_url con escHtml para evitar XSS via URL manipulada.
        const safeThumbUrl = escHtml(evt.thumb_url || '');
        const thumbHtml = evt.thumb_url
            ? `<img src="${safeThumbUrl}" loading="lazy" decoding="async" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="App._imgFallback(this,'${icon}')" />`
            : `<span style="font-size:1.3rem;">${icon}</span>`;
        const evtTime = evt.datetime || ts;
        // M4.6: badge "🛡️ Centinela" cuando el evento es vigilance/night_alert (1 frame, no analisis).
        const isCentinela = evt.is_centinela === true ||
            evt.event_type === 'vigilance_alert' || evt.event_type === 'night_alert';
        const centinelaBadge = isCentinela
            ? '<span class="badge" style="background:#ff9500;color:#fff;font-size:0.62rem;padding:1px 6px;border-radius:8px;margin-left:6px">🛡️ Centinela</span>'
            : '';
        return `<div class="event-row ${violation ? 'event-alert' : ''}" onclick="App._openEvent('${escHtml(evt.event_id)}')">
            <div class="event-thumb" id="evthumb-${escHtml(evt.event_id)}" style="background:#222;display:flex;align-items:center;justify-content:center;overflow:hidden;border-radius:10px;flex-shrink:0;width:80px;height:60px">
                ${thumbHtml}
            </div>
            <div class="event-info" style="flex:1;min-width:0">
                <div class="event-title">${camName}${camZone ? ' · ' + camZone : ''}${centinelaBadge}</div>
                <div class="meta">${escHtml(evtTime)} · Detección: ${yoloCount} objeto(s)</div>
                <div class="meta event-desc" style="margin-top:2px;font-size:0.78rem;color:var(--text-secondary);white-space:normal;line-height:1.35">${escHtml(enrichedDesc)}</div>
            </div>
            <span class="badge badge-${level}" style="flex-shrink:0">${label}</span>
        </div>`;
    },

    async _openEvent(eventId) {
    if (!eventId) return;
    try {
        const uid = this.userId || 'default';
        const r = await apiFetch(`${this.API}/api/events/${eventId}?user_id=${uid}`);
        const d = await r.json();
        if (!d || d.error) return;
        
        const violation = d.qwen?.violation || d.event_type === 'violation';
        const isAttention = (d.attention_hits && d.attention_hits.length > 0) || d.event_type === 'attention';
        const isSentinel = d.event_type === 'sentinel' || (d.qwen_json?.after_hours && d.qwen_json?.importancia === 'alta');
        // Eventos vigilance_alert (modo centinela) guardan 1 solo frame: son instantáneas
        // de alerta, no clips reproducibles. Se muestran en un viewer simple (sin slider/autoplay).
        const isVigilance = d.event_type === 'vigilance_alert';
        const attentionHits = d.attention_hits || [];
        const total = Array.isArray(d.frames) ? d.frames.length : 0;
        const hasFrames = total > 1 && !isVigilance;
        const hasVideo = !!d.video_file;
        const hasGrid = !!d.grid_b64;
        
        // ═══════════════════════════════════════════════════════════
        // OVERLAY (fondo oscuro con blur)
        // ═══════════════════════════════════════════════════════════
        const overlay = document.createElement('div');
        overlay.id = `event-overlay-${eventId}`;
        overlay.style.cssText = 'position:fixed;inset:0;z-index:500;background:rgba(0,0,0,0.85);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:24px;animation:fadeIn 0.2s ease';
        
        // ═══════════════════════════════════════════════════════════
        // MODAL (contenido principal)
        // ═══════════════════════════════════════════════════════════
        const modal = document.createElement('div');
        modal.id = `event-modal-${eventId}`;
        modal.style.cssText = 'width:100%;max-width:720px;max-height:calc(100vh - 48px);background:var(--bg-secondary);border-radius:16px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,0.6);animation:scaleIn 0.25s cubic-bezier(0.4,0,0.2,1)';
        
        // ═══════════════════════════════════════════════════════════
        // HEADER (sticky, con botones en orden correcto)
        // ═══════════════════════════════════════════════════════════
        const header = document.createElement('div');
        header.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);background:var(--bg-tertiary);flex-shrink:0';
        
        // Botón cerrar
        const closeBtn = document.createElement('button');
        closeBtn.className = 'btn-icon';
        closeBtn.innerHTML = '✕';
        closeBtn.title = 'Cerrar';
        closeBtn.style.cssText = 'background:none;border:none;color:var(--text-secondary);font-size:1.4rem;cursor:pointer;padding:4px';
        closeBtn.onclick = (e) => {
    e.stopPropagation();
    // Limpiar estado de autoplay antes de cerrar
    this._cancelAutoAdvance?.(eventId);
    this._stopEventFramePlayback(eventId, total);
    overlay.remove();
};
        
        // Badge de alerta (si aplica)
        const alertBadge = document.createElement('span');
        const badgeLevel = isSentinel ? 'badge-sentinel' : (violation ? 'badge-alert' : (isAttention ? 'badge-attention' : ''));
        alertBadge.className = `badge ${badgeLevel}`;
        alertBadge.textContent = isSentinel ? '🛡️' : (violation ? '🚨' : (isAttention ? '🔍' : ''));
        alertBadge.title = isSentinel ? 'Centinela' : (violation ? 'Alerta' : (isAttention ? 'Observación' : ''));
        alertBadge.style.cssText = badgeLevel ? '' : 'display:none';
        
        // Título de la cámara
        const title = document.createElement('span');
        title.style.cssText = 'flex:1;font-weight:600;font-size:0.95rem;text-overflow:ellipsis;overflow:hidden;white-space:nowrap';
        title.textContent = d.camera_name || 'Evento';
        
        // Indicador de autoplay (siempre visible si hay frames)
        const autoplayBadge = document.createElement('span');
        autoplayBadge.id = `autoplay-status-${eventId}`;
        autoplayBadge.className = 'badge';
        autoplayBadge.style.cssText = 'background:rgba(48,209,88,0.18);color:#30d158;padding:3px 10px;font-size:0.75rem;cursor:pointer;user-select:none;border-radius:6px';
        if (isVigilance) {
            autoplayBadge.innerHTML = '🛡️ Centinela';
            autoplayBadge.title = 'Instantánea de alerta (1 frame, sin clip)';
            autoplayBadge.style.background = 'rgba(245,166,35,0.15)';
            autoplayBadge.style.color = '#f5a623';
            autoplayBadge.style.cursor = 'default';
        } else if (hasVideo) {
            autoplayBadge.innerHTML = '🎬 Video';
            autoplayBadge.title = 'Clip de video del evento';
            autoplayBadge.style.background = 'rgba(10,132,255,0.15)';
            autoplayBadge.style.color = '#0a84ff';
            autoplayBadge.style.cursor = 'default';
        } else {
            autoplayBadge.innerHTML = hasFrames ? '▶ Auto' : '⏸';
            autoplayBadge.title = hasFrames ? 'Click para pausar' : 'Sin autoplay';
            if (hasFrames) {
                autoplayBadge.onclick = (e) => { e.stopPropagation(); this._toggleEventFramePlayback(eventId, total); };
            }
        }
        
        // Botón "← Anterior"
        const navPrev = document.createElement('button');
        navPrev.className = 'btn btn-sm';
        navPrev.innerHTML = '← Anterior';
        navPrev.title = 'Evento anterior';
        navPrev.style.cssText = 'padding:5px 10px;font-size:0.82rem';
        navPrev.onclick = async (e) => {
            e.stopPropagation();
            this._pauseEventAutoplay(eventId);
            await this._navigateSiblingEvent(eventId, 'prev', overlay);
        };
        
        // Orden en header: ✕ Título [Alerta] [Autoplay] [← Anterior]
        header.appendChild(closeBtn);
        header.appendChild(title);
        header.appendChild(alertBadge);
        header.appendChild(autoplayBadge);
        header.appendChild(navPrev);
        
        // ═══════════════════════════════════════════════════════════
        // BODY (contenido con scroll discreto)
        // ═══════════════════════════════════════════════════════════
        const body = document.createElement('div');
        body.style.cssText = 'flex:1;overflow-y:auto;padding:16px;-webkit-overflow-scrolling:touch';
        
        // ═══════════════════════════════════════════════════════════
        // SECCIÓN 1: Player (frames autoplay | video | viewer simple de 1 frame)
        // ═══════════════════════════════════════════════════════════
        const playerWrap = document.createElement('div');
        playerWrap.style.cssText = 'position:relative;background:#000;border-radius:12px;overflow:hidden;aspect-ratio:4/3;margin-bottom:12px';
        
        const playerImg = document.createElement('img');
        playerImg.id = `event-frame-img-${eventId}`;
        playerImg.style.cssText = 'width:100%;height:100%;object-fit:contain;background:#000;display:block';
        playerImg.alt = 'Frame del evento';
        playerWrap.appendChild(playerImg);
        
        if (hasVideo) {
            // ── Video clip ──
            const video = document.createElement('video');
            video.id = `event-video-${eventId}`;
            video.src = `${this.API}/api/events/${eventId}/video.mp4?user_id=${uid}`;
            video.style.cssText = 'width:100%;height:100%;object-fit:contain;background:#000;display:block';
            video.controls = true;
            video.playsInline = true;
            video.preload = 'metadata';
            playerImg.remove();
            playerWrap.appendChild(video);
        } else if (hasFrames) {
            // ── Frames autoplay ──
            // Overlay de play/pause
            const playOverlay = document.createElement('button');
            playOverlay.id = `event-frame-play-${eventId}`;
            playOverlay.style.cssText = 'position:absolute;inset:0;background:transparent;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:2.8rem;color:#fff;text-shadow:0 2px 12px rgba(0,0,0,0.8);opacity:0.9;transition:opacity 0.2s';
            playOverlay.innerHTML = '⏸';
            playOverlay.onclick = (e) => { e.stopPropagation(); this._toggleEventFramePlayback(eventId, total); };
            playerWrap.appendChild(playOverlay);
        }
        
        // ═══════════════════════════════════════════════════════════
        // SECCIÓN 2: Controles (solo frames autoplay; oculto en viewer simple/video)
        // ═══════════════════════════════════════════════════════════
        const controls = document.createElement('div');
        
        if (hasFrames && !hasVideo) {
            controls.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:16px';
            
            const prevBtn = document.createElement('button');
            prevBtn.className = 'btn-icon btn-sm';
            prevBtn.innerHTML = '⏮';
            prevBtn.title = 'Frame anterior';
            prevBtn.onclick = (e) => { e.stopPropagation(); this._moveEventFrame(eventId, total, -1); this._stopEventFramePlayback(eventId, total); };
            
            const playBtn = document.createElement('button');
            playBtn.id = `event-play-btn-${eventId}`;
            playBtn.className = 'btn-icon btn-sm';
            playBtn.innerHTML = '⏸';
            playBtn.title = 'Pausar';
            playBtn.onclick = (e) => { e.stopPropagation(); this._toggleEventFramePlayback(eventId, total); };
            
            const nextBtn = document.createElement('button');
            nextBtn.className = 'btn-icon btn-sm';
            nextBtn.innerHTML = '⏭';
            nextBtn.title = 'Frame siguiente';
            nextBtn.onclick = (e) => { e.stopPropagation(); this._moveEventFrame(eventId, total, 1); this._stopEventFramePlayback(eventId, total); };
            
            const slider = document.createElement('input');
            slider.id = `event-frame-range-${eventId}`;
            slider.type = 'range';
            slider.min = '0';
            slider.max = String(Math.max(0, total - 1));
            slider.value = '0';
            slider.style.cssText = 'flex:1;accent-color:#0a84ff;height:4px';
            slider.oninput = (e) => {
                e.stopPropagation();
                this._showEventFrame(eventId, total, slider.value);
                this._stopEventFramePlayback(eventId, total);
            };
            
            const status = document.createElement('span');
            status.id = `event-frame-status-${eventId}`;
            status.className = 'meta';
            status.style.cssText = 'min-width:52px;text-align:right;font-variant-numeric:tabular-nums;font-size:0.82rem;color:var(--text-secondary)';
            status.textContent = `1/${total}`;
            
            controls.appendChild(prevBtn);
            controls.appendChild(playBtn);
            controls.appendChild(nextBtn);
            controls.appendChild(slider);
            controls.appendChild(status);
        } else if (isVigilance) {
            // Badge informativo: 1 frame estático
            controls.style.cssText = 'display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:16px;padding:8px 12px;background:rgba(245,166,35,0.10);border:1px solid rgba(245,166,35,0.25);border-radius:10px';
            controls.innerHTML = `<span style="font-size:0.85rem;color:#f5a623">📸 Instantánea de alerta (1 frame) · Modo centinela</span>`;
        }
        
        // ═══════════════════════════════════════════════════════════
        // SECCIÓN 3: Análisis de Eva (card prominente)
        // ═══════════════════════════════════════════════════════════
        const aiCard = document.createElement('div');
        aiCard.className = 'card';
        aiCard.style.cssText = 'margin-bottom:16px';
        const aiTitle = document.createElement('div');
        aiTitle.className = 'card-title';
        aiTitle.textContent = '🤖 Análisis de Eva';
        aiCard.appendChild(aiTitle);
        
        const qa = d.qwen_analysis || {};
        const qjson = d.qwen_json || {};
        const desc = this._cleanEventDescription(qa.summary || qa.description || qjson.summary || qjson.description || d.qwen?.description || d.description);
        if (desc) {
            const p = document.createElement('p');
            p.style.cssText = 'font-size:0.9rem;margin-bottom:10px;line-height:1.5;white-space:pre-wrap';
            p.textContent = desc;
            aiCard.appendChild(p);
        }
        
        if (attentionHits.length) {
            const hitsDiv = document.createElement('div');
            hitsDiv.style.cssText = 'background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:10px 14px;margin-bottom:12px';
            hitsDiv.innerHTML = `<div style="font-size:0.78rem;color:var(--warning,#f5a623);font-weight:600;margin-bottom:4px">🔍 Observaciones detectadas:</div><div style="font-size:0.85rem;line-height:1.4">${attentionHits.map(h => '• ' + h).join('<br>')}</div>`;
            aiCard.appendChild(hitsDiv);
        }
        
        if (isSentinel) {
            const sentinelDiv = document.createElement('div');
            sentinelDiv.style.cssText = 'background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:10px 14px;margin-bottom:12px';
            sentinelDiv.innerHTML = `<div style="font-size:0.82rem;color:var(--warning,#f5a623);font-weight:600">🛡️ Modo centinela — Se detectó presencia fuera del horario de trabajo</div>`;
            aiCard.appendChild(sentinelDiv);
        }
        
        // Stats row
        const aiRow = document.createElement('div');
        aiRow.className = 'ai-row';
        const trackingUnique2 = d.metadata?.person_tracking?.unique_persons;
        const qwenPersonsVisible = d.qwen_json?.vision?.persons?.length ?? d.qwen_details?.persons_visible;
        const yoloCount = (
            Number(trackingUnique2) ||
            Number(d.yolo?.count) ||
            (Array.isArray(d.yolo?.detections) ? d.yolo.detections.length : 0) ||
            0
        );
        const personsDisplay = (trackingUnique2 || qwenPersonsVisible || d.persons || d.qwen_analysis?.persons || '—');
        aiRow.innerHTML = `<div class="ai-card"><div class="ai-label">👁 Detección</div><div class="ai-val">${yoloCount} obj.</div></div><div class="ai-card"><div class="ai-label">👥 Personas</div><div class="ai-val">${personsDisplay}</div></div><div class="ai-card"><div class="ai-label">🧠 Eva</div><div class="ai-val">${isSentinel ? '🛡️ Centinela' : (isAttention || violation ? '🔍 Observación' : '✅ Normal')}</div></div>`;
        aiCard.appendChild(aiRow);
        
        // ═══════════════════════════════════════════════════════════
        // SECCIÓN 4: Botones de acción
        // ═══════════════════════════════════════════════════════════
        const actions = document.createElement('div');
        actions.style.cssText = 'display:flex;gap:8px;margin-bottom:12px';
        
        const dismissBtn = document.createElement('button');
        dismissBtn.className = 'btn';
        dismissBtn.style.cssText = 'flex:1;background:var(--bg-tertiary);color:var(--text-secondary)';
        dismissBtn.innerHTML = '✓ Falsa alarma';
        dismissBtn.onclick = (e) => { e.stopPropagation(); this._dismissEvent(eventId); overlay.remove(); };
        
        actions.appendChild(dismissBtn);
        
        if (violation || isAttention) {
            const confirmBtn = document.createElement('button');
            confirmBtn.className = 'btn';
            confirmBtn.style.cssText = 'flex:1;background:var(--accent)';
            confirmBtn.innerHTML = isAttention ? '🏷️ Marcar como falta real' : '⚠️ Confirmar alerta';
            confirmBtn.onclick = (e) => { e.stopPropagation(); this._confirmThreat(eventId); overlay.remove(); };
            actions.appendChild(confirmBtn);
        }
        
        // Botón "Ver cámara en vivo" (si hay camera_id)
        if (d.camera_id) {
            const liveBtn = document.createElement('button');
            liveBtn.className = 'btn';
            liveBtn.style.cssText = 'width:100%;background:var(--bg-tertiary);color:var(--text-primary)';
            liveBtn.innerHTML = '📹 Ver cámara en vivo';
            liveBtn.onclick = (e) => { e.stopPropagation(); overlay.remove(); this._openCameraLive(d.camera_id); };
            actions.appendChild(liveBtn);
        }
        
        // ═══════════════════════════════════════════════════════════
        // SECCIÓN 5: Grid 4×4 (opcional, si existe)
        // ═══════════════════════════════════════════════════════════
        if (hasGrid) {
            const gridCard = document.createElement('div');
            gridCard.className = 'card';
            const gridTitle = document.createElement('div');
            gridTitle.className = 'card-title';
            gridTitle.textContent = '🔲 Sesión completa (grid 4×4)';
            const gridImg = document.createElement('img');
            gridImg.src = `data:image/jpeg;base64,${d.grid_b64}`;
            gridImg.style.cssText = 'width:100%;border-radius:8px;display:block';
            gridCard.appendChild(gridTitle);
            gridCard.appendChild(gridImg);
            body.appendChild(gridCard);
        }
        
        // ═══════════════════════════════════════════════════════════
        // ENSAMBLAR
        // ═══════════════════════════════════════════════════════════
        body.appendChild(playerWrap);
        body.appendChild(controls);
        body.appendChild(aiCard);
        body.appendChild(actions);
        
        modal.appendChild(header);
        modal.appendChild(body);
        overlay.appendChild(modal);
        
        // Click en overlay (fuera del modal) → cerrar
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                this._cancelAutoAdvance?.(eventId);
                this._stopEventFramePlayback(eventId, total);
                overlay.remove();
            }
        });
        
        // Keyboard escape → cerrar
        const escapeHandler = (e) => {
            if (e.key === 'Escape') {
                this._cancelAutoAdvance?.(eventId);
                this._stopEventFramePlayback(eventId, total);
                overlay.remove();
                document.removeEventListener('keydown', escapeHandler);
            }
        };
        document.addEventListener('keydown', escapeHandler);
        
        // Click en modal (pero no en botones/inputs) → pausar autoplay
        // DESACTIVADO temporalmente - estaba pausando el autoplay al abrir
        /* modal.addEventListener('click', (e) => {
            if (e.target.closest('button, input, [role="button"]')) return;
            if (hasFrames && this._eventFramePlaying?.[eventId]) {
                this._pauseEventAutoplay(eventId);
            }
        }); */
        
        document.body.appendChild(overlay);
        
        // ═══════════════════════════════════════════════════════════
        // INICIALIZAR
        // ═══════════════════════════════════════════════════════════
        if (hasVideo) {
            // El <video> ya tiene su src; no hay nada más que inicializar.
        } else if (hasFrames) {
            this._eventFrameIndex = this._eventFrameIndex || {};
            this._eventFrameIndex[eventId] = 0;
            this._showEventFrame(eventId, total, 0);
            setTimeout(() => this._startEventFramePlayback(eventId, total), 800);
        } else if (d.frame_b64) {
            // Viewer simple: centinela (1 frame) u evento sin secuencia.
            playerImg.src = `data:image/jpeg;base64,${d.frame_b64}`;
        } else if (d.grid_b64) {
            playerImg.src = `data:image/jpeg;base64,${d.grid_b64}`;
        } else {
            // No hay frames disponibles - mostrar mensaje
            playerImg.alt = 'No hay frames disponibles para este evento';
            playerImg.style.background = '#1a1a1a';
        }
        
        // Detectar error de carga inicial (API caída) — solo para el <img>, no el <video>
        if (!hasVideo && playerImg.parentNode) {
            playerImg.onerror = () => {
                playerImg.style.background = '#1a1a1a';
                playerImg.alt = '⚠️ API no disponible - Verifica tu conexión o el estado del servidor';
                playerImg.style.cursor = 'pointer';
                playerImg.title = 'Click para reintentar';
                playerImg.onclick = (e) => {
                    e.stopPropagation();
                    playerImg.src = `${this.API}/api/events/${eventId}/frame/0?user_id=${uid}&_=${Date.now()}`;
                };
            };
        }
    } catch(e) {
        console.error('Error en _openEvent', e);
    }
},
    _showEventFrame(eventId, total, rawIndex) {
        const index = Math.max(0, Math.min(total - 1, parseInt(rawIndex || '0', 10)));
        const uid = this.userId || 'default';
        this._eventFrameIndex = this._eventFrameIndex || {};
        this._eventFrameIndex[eventId] = index;
        const img = document.getElementById(`event-frame-img-${eventId}`);
        const status = document.getElementById(`event-frame-status-${eventId}`);
        const range = document.getElementById(`event-frame-range-${eventId}`);
        if (img) {
            const frameUrl = `${this.API}/api/events/${eventId}/frame/${index}?user_id=${uid}&_=${Date.now()}`;
            console.log(`[Frame] Showing frame ${index + 1}/${total} - ${frameUrl}`);
            // Manejo de error de carga
            img.onerror = () => {
                console.warn(`[Frame] Error loading frame ${index}`);
                img.style.background = '#1a1a1a';
                img.alt = 'Error cargando frame - API no disponible';
            };
            img.onload = () => {
                console.log(`[Frame] ✅ Loaded frame ${index + 1}/${total}`);
            };
            img.src = frameUrl;
        }
        if (range) range.value = index;
        if (status) status.textContent = `${index + 1}/${total}`;
    },

    async _navigateSiblingEvent(currentEventId, direction, currentModal) {
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/events/siblings?user_id=${uid}&event_id=${currentEventId}&camera_id=${encodeURIComponent(currentModal?.dataset?.cameraId || '')}`);
            const data = await r.json();
            if (!data.success || !Array.isArray(data.events) || data.events.length === 0) {
                if (direction === 'next') {
                    currentModal?.remove();
                    if (window.App?._showToast) App._showToast('✅ Último evento — fin de la lista');
                } else {
                    if (window.App?._showToast) App._showToast('✅ Primer evento — no hay anteriores');
                }
                return;
            }
            let targetEvent = null;
            if (direction === 'next') {
                targetEvent = data.events[0];
            } else {
                targetEvent = data.events[data.events.length - 1];
            }
            if (!targetEvent || targetEvent.event_id === currentEventId) {
                if (window.App?._showToast) App._showToast(`⚠️ No hay evento ${direction === 'next' ? 'siguiente' : 'anterior'}`);
                return;
            }
            currentModal?.remove();
            setTimeout(() => {
                this._openEvent(targetEvent.event_id);
            }, 50);
        } catch (e) {
            console.error('Error navegando eventos:', e);
            if (window.App?._showToast) App._showToast('❌ Error al navegar');
        }
    },

    /**
     * OPCIÓN A — Auto-advance:
     * Cuando el playback del grid termina (llega al último frame y completa un ciclo),
     * muestra un toast "Próximo en 2s..." cancelable y luego carga el evento siguiente.
     * Si el usuario pausó o la tab está oculta (document.hidden), no avanza.
     */
    async _scheduleAutoAdvance(eventId, modal) {
        // Solo si autoplay está todavía activo (usuario no pausó)
        if (!this._eventFramePlaying?.[eventId]) return;
        // Si la tab está oculta, no auto-avanzar — el usuario no está mirando
        if (typeof document !== 'undefined' && document.hidden) return;
        const uid = this.userId || 'default';
        let data;
        try {
            const r = await apiFetch(`${this.API}/api/events/siblings?user_id=${uid}&event_id=${eventId}`);
            data = await r.json();
        } catch (e) {
            return;
        }
        if (!data.success || !Array.isArray(data.events) || data.events.length === 0) {
            if (window.App?._showToast) App._showToast('✅ Fin de los eventos — fin del listado');
            this._stopEventFramePlayback(eventId, 0);
            return;
        }
        const nextEvent = data.events[0];
        if (!nextEvent || nextEvent.event_id === eventId) return;

        // M4.7: prefetch del detalle del siguiente evento para que el cambio sea instantaneo.
        // Solo se hace durante la pausa de auto-advance (no bloquea, fetch en background).
        // Abortamos cualquier prefetch previo para no encolar varios.
        if (this._eventPrefetchController?.[eventId]) {
            try { this._eventPrefetchController[eventId].abort(); } catch(e) {}
        }
        this._eventPrefetchController = this._eventPrefetchController || {};
        const abortCtl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        this._eventPrefetchController[eventId] = abortCtl;
        const prefetchUrl = `${this.API}/api/event-thumb/${encodeURIComponent(nextEvent.event_id)}?user_id=${encodeURIComponent(uid)}`;
        fetch(prefetchUrl, { signal: abortCtl ? abortCtl.signal : undefined, mode: 'cors' })
            .then(() => { /* warms el thumbnail en el cache HTTP del navegador */ })
            .catch(() => { /* abort o fallo de red: no crítico */ });

        if (window.App?._showToast) {
            App._showToast(`▶ Próximo evento en ${(this._eventAutoAdvanceTimeout / 1000).toFixed(1)}s — click para pausar`, 2000);
        }

        if (this._eventAutoAdvanceTimer?.[eventId]) {
            clearTimeout(this._eventAutoAdvanceTimer[eventId]);
        }
        this._eventAutoAdvanceTimer = this._eventAutoAdvanceTimer || {};
        this._eventAutoAdvanceTimer[eventId] = setTimeout(() => {
            if (!this._eventFramePlaying?.[eventId]) return;
            // Re-chequear que la tab sigue visible antes de navegar
            if (typeof document !== 'undefined' && document.hidden) {
                this._pauseEventAutoplay(eventId);
                return;
            }
            modal?.remove();
            this._openEvent(nextEvent.event_id);
        }, this._eventAutoAdvanceTimeout || 2000);
    },

    _cancelAutoAdvance(eventId) {
        if (this._eventAutoAdvanceTimer?.[eventId]) {
            clearTimeout(this._eventAutoAdvanceTimer[eventId]);
            this._eventAutoAdvanceTimer[eventId] = null;
        }
    },

    _pauseEventAutoplay(eventId) {
        this._cancelAutoAdvance(eventId);
        // M4.8: simplificado. El ternario anidado previo era ilegible y frágil;
        // _eventFrameTotal[eventId] se setea en _showEventFramePlayback y es la fuente de verdad.
        const total = this._eventFrameTotal?.[eventId] || 0;
        this._stopEventFramePlayback(eventId, total);
        const indicator = document.getElementById(`autoplay-status-${eventId}`);
        if (indicator) {
            indicator.innerHTML = '⏸ Pausa';
            indicator.style.background = 'rgba(255,159,10,0.15)';
            indicator.style.color = '#ff9f0a';
            indicator.style.borderColor = 'rgba(255,159,10,0.3)';
        }
    },

    _moveEventFrame(eventId, total, delta) {
        const current = this._eventFrameIndex?.[eventId] || 0;
        this._showEventFrame(eventId, total, Math.max(0, Math.min(total - 1, current + delta)));
    },

    _startEventFramePlayback(eventId, total) {
        this._stopEventFramePlayback(eventId, total);
        this._eventFramePlaying = this._eventFramePlaying || {};
        this._eventFramePlaying[eventId] = true;
        this._eventFrameTotal = this._eventFrameTotal || {};
        this._eventFrameTotal[eventId] = total;
        this._autoplayCompletedOnce = this._autoplayCompletedOnce || {};
        const overlay = document.getElementById(`event-frame-play-${eventId}`);
        if (overlay) overlay.innerHTML = '⏸';
        const indicator = document.getElementById(`autoplay-status-${eventId}`);
        if (indicator) {
            indicator.innerHTML = '▶ Auto';
            indicator.style.background = 'rgba(48,209,88,0.15)';
            indicator.style.color = '#30d158';
            indicator.style.borderColor = 'rgba(48,209,88,0.3)';
        }
        const tick = () => {
            console.log(`[Autoplay] tick - playing=${this._eventFramePlaying?.[eventId]}`);
            if (!this._eventFramePlaying?.[eventId]) return;
            const current = this._eventFrameIndex?.[eventId] ?? 0;
            const nextIndex = current + 1;
            if (nextIndex >= total) {
                this._autoplayCompletedOnce[eventId] = true;
                this._showEventFrame(eventId, total, 0);
                this._eventFrameTimer[eventId] = setTimeout(() => {
                    if (this._eventFramePlaying?.[eventId]) {
                        this._scheduleAutoAdvance(eventId, document.getElementById(`event-modal-${eventId}`));
                    }
                }, 1500);
                return;
            }
            this._showEventFrame(eventId, total, nextIndex);
            this._eventFrameTimer = this._eventFrameTimer || {};
            this._eventFrameTimer[eventId] = setTimeout(tick, 300);
        };
        this._eventFrameTimer = this._eventFrameTimer || {};
        this._eventFrameTimer[eventId] = setTimeout(tick, 300);
    },

    _stopEventFramePlayback(eventId, total) {
        this._eventFramePlaying = this._eventFramePlaying || {};
        this._eventFramePlaying[eventId] = false;
        this._cancelAutoAdvance(eventId);
        if (this._eventFrameTimer?.[eventId]) {
            clearTimeout(this._eventFrameTimer[eventId]);
            this._eventFrameTimer[eventId] = null;
        }
        const overlay = document.getElementById(`event-frame-play-${eventId}`);
        if (overlay) overlay.innerHTML = '▶';
    },

    _toggleEventFramePlayback(eventId, total) {
        if (this._eventFramePlaying?.[eventId]) {
            this._pauseEventAutoplay(eventId);
        } else {
            this._startEventFramePlayback(eventId, total);
        }
    },

    // Fase 2: feedback editable + entrenamiento.
    // Abre un sheet (no prompt) para marcar "Falsa alarma" con opción de editar
    // qué creyó el sistema que pasó. Se envía a /api/chat/eva/feedback que
    // persiste feedback + registra false_alarm + attention_corrections.
    async _dismissEvent(id) {
        if (!id) return;
        const uid = this.userId || 'default';
        // Cargar el evento para pre-cargar el campo editable con el hit original.
        let originalHit = '';
        let evtDesc = '';
        try {
            const r = await apiFetch(`${this.API}/api/events/${id}?user_id=${uid}`);
            if (r.ok) {
                const d = await r.json();
                originalHit = (Array.isArray(d.attention_hits) && d.attention_hits.length) ? d.attention_hits[0] : '';
                evtDesc = (d.qwen_json && d.qwen_json.summary) || d.summary || d.description || '';
            }
        } catch(e) {}
        this._openFeedbackSheet(id, false, originalHit || evtDesc || 'Actividad observada');
    },

    async _confirmThreat(id) {
        if (!id) return;
        const uid = this.userId || 'default';
        this._openFeedbackSheet(id, true, '');
    },

    // Sheet inline para feedback: precarga, editable, envía a /api/chat/eva/feedback.
    _openFeedbackSheet(eventId, isReal, presetText) {
        // Evitar duplicar si ya hay uno abierto.
        const existing = document.getElementById('feedback-sheet');
        if (existing) existing.remove();
        const overlay = document.createElement('div');
        overlay.id = 'feedback-sheet';
        overlay.style.cssText = 'position:fixed;inset:0;z-index:600;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);display:flex;align-items:flex-end;justify-content:center;padding:0;animation:fadeIn 0.2s ease';
        const sheet = document.createElement('div');
        sheet.style.cssText = 'width:100%;max-width:560px;background:var(--bg-secondary);border-radius:18px 18px 0 0;padding:20px 20px calc(20px + env(safe-area-inset-bottom));animation:slideUp 0.25s cubic-bezier(0.4,0,0.2,1)';
        const title = isReal ? '✅ Vio bien' : '✓ Falsa alarma';
        const titleColor = isReal ? 'var(--success)' : 'var(--danger)';
        const subtitle = isReal
            ? 'Confirma que Eva detectó bien. Esto refuerza la vigilancia.'
            : '¿Qué pasó realmente? Esto ayuda a Eva a ajustar sus reglas.';
        sheet.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
                <div style="font-size:1.1rem;font-weight:700;color:${titleColor}">${title}</div>
                <button class="btn-icon" data-fb-close style="background:none;border:none;color:var(--text-secondary);font-size:1.4rem;padding:4px">✕</button>
            </div>
            <div style="font-size:.85rem;color:var(--text-secondary);margin-bottom:12px">${subtitle}</div>
            <label style="display:block;font-size:.8rem;font-weight:600;margin-bottom:6px">${isReal ? 'Nota (opcional)' : 'Lo que crees que pasó'}</label>
            <textarea id="fb-correction" rows="3" style="width:100%;box-sizing:border-box;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:10px;padding:12px;color:var(--text-primary);font-size:1rem;font-family:inherit;resize:vertical" placeholder="${isReal ? 'Ej: el cajero sí se llevó la mano al bolsillo' : 'Ej: solo estaba acomodando la camiseta, no metió la mano'}">${(presetText||'').replace(/</g,'<')}</textarea>
            <div style="display:flex;gap:10px;margin-top:14px">
                <button class="btn" data-fb-cancel style="flex:1;background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border)">Cancelar</button>
                <button class="btn" data-fb-send style="flex:2;background:${isReal ? 'var(--success)' : 'var(--accent)'};color:#fff;border:none;font-weight:600">${isReal ? '✅ Confirmar' : 'Marcar falsa alarma'}</button>
            </div>`;
        overlay.appendChild(sheet);
        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
        sheet.querySelector('[data-fb-close]').onclick = close;
        sheet.querySelector('[data-fb-cancel]').onclick = close;
        sheet.querySelector('[data-fb-send]').onclick = async (e) => {
            const btn = e.currentTarget;
            const text = (sheet.querySelector('#fb-correction').value || '').trim();
            btn.disabled = true; btn.textContent = 'Enviando…';
            try {
                const r = await apiFetch(`${this.API}/api/chat/eva/feedback`, {
                    method: 'POST',
                    body: JSON.stringify({
                        user_id: this.userId || 'default',
                        event_id: eventId,
                        is_real: isReal,
                        correction_note: isReal ? '' : text,
                        notes: isReal ? text : '',
                    }),
                });
                const d = await r.json();
                if (d.success) {
                    close();
                    // Si era falsa alarma y la corregiste, el banner activo se descarta.
                    if (!isReal && this._activeAlertEvent && this._activeAlertEvent.eventId === eventId) {
                        this._activeAlertEvent = null;
                    }
                    this._toast('', isReal ? '¡Gracias! Eva recordará que vio bien' : 'Marcado como falsa alarma. Eva lo aprenderá', isReal ? 'success' : 'info');
                    // Si estamos en Home/Cameras, refrescar para quitar el banner.
                    if (this.page === 'home' || this.page === 'cameras') {
                        setTimeout(() => this._pageHome(document.getElementById('app-content')), 400);
                    }
                } else {
                    btn.disabled = false; btn.textContent = isReal ? '✅ Confirmar' : 'Marcar falsa alarma';
                    this._toast('', 'No se pudo enviar: ' + (d.error || ''), 'danger');
                }
            } catch(err) {
                btn.disabled = false; btn.textContent = isReal ? '✅ Confirmar' : 'Marcar falsa alarma';
                this._toast('', 'Error de conexión: ' + err.message, 'danger');
            }
        };
        // Re-enable textareas (fallback if markdown rendering: textarea content is raw text)
        const correctionTa = sheet.querySelector('#fb-correction');
        if (correctionTa && presetText) { /* el value ya está seteado en el markup */ }
    },

    // ── SETTINGS ─────────────────────────────────────────────
    async _pageSettings(c) {
        this._resetScrollContent(c);
        let profile = {};
        let cams = [];
        try { 
            const r = await apiFetch(`${this.API}/api/user/profile?user_id=${this.userId}`); 
            profile = await r.json(); 
        } catch(e) {}
        try {
            const r2 = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            cams = (await r2.json()).cameras || [];
            this._homeCams = cams;
        } catch(e) {}
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
            const lastSeen = cam.last_frame ? this._relTime(cam.last_frame) : 'Sin datos';
            return `
                <button class="ios-row" onclick="App._openCameraConfig('${cam.camera_id}')">
                    <span class="ios-icon">📷</span>
                    <div class="ios-row-main">
                        <div class="ios-row-title">${cam.name || cam.camera_id}</div>
                        <div class="ios-row-sub">${zone} · ${events} eventos · ${alertsCam} alertas · ${lastSeen}</div>
                    </div>
                    <span class="ios-value ${cam.active ? 'ios-value-ok' : 'ios-value-danger'}">${cam.active ? '🟢' : '⚫'}</span>
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
            <button class="ios-row" onclick="App._openVigilanceSettings('${cam.camera_id}')">
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

                <div class="ios-group collapsed" data-group="cuenta">
                    <div class="ios-group-header" onclick="App._toggleGroup('cuenta')">
                        <span class="ios-group-header-icon">👤</span>
                        <div class="ios-group-header-title">Cuenta</div>
                        <span class="ios-group-header-badge">${plan}</span>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
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
                    </div>
                </div>

                <div class="ios-group collapsed" data-group="camaras">
                    <div class="ios-group-header" onclick="App._toggleGroup('camaras')">
                        <span class="ios-group-header-icon">📷</span>
                        <div class="ios-group-header-title">Cámaras</div>
                        <span class="ios-group-header-badge">${cams.length}</span>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
                        ${cameraRows}
                        <button class="ios-row" onclick="App.newCamera()">
                            <span class="ios-icon">➕</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Instalar cámara nueva con Eva</div>
                                <div class="ios-row-sub">Eva guía la instalación paso a paso</div>
                            </div>
                            <span class="ios-chevron">›</span>
                        </button>
                        </div>
                    </div>
                </div>

                <div class="ios-group collapsed" data-group="proteccion">
                    <div class="ios-group-header" onclick="App._toggleGroup('proteccion')">
                        <span class="ios-group-header-icon">🛡️</span>
                        <div class="ios-group-header-title">Protección</div>
                        <span class="ios-group-header-badge">${activeCams}</span>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
                        ${vigilanceRows}
                        </div>
                    </div>
                </div>

                <div class="ios-group collapsed" data-group="deteccion">
                    <div class="ios-group-header" onclick="App._toggleGroup('deteccion')">
                        <span class="ios-group-header-icon">🔲</span>
                        <div class="ios-group-header-title">Detección</div>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
                        <button class="ios-row" onclick="App._openZonesFromSettings()">
                            <span class="ios-icon">📍</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Zonas de interés</div>
                                <div class="ios-row-sub">Caja, Entrada, Cocina… dibuja y nombra cada área</div>
                            </div>
                            <span class="ios-chevron">›</span>
                        </button>
                        <button class="ios-row" onclick="App._openGridSettings()">
                            <span class="ios-icon">🔲</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Área de análisis</div>
                                <div class="ios-row-sub">Cuántas imágenes revisa Eva por cámara</div>
                            </div>
                            <span class="ios-chevron">›</span>
                        </button>
                        </div>
                    </div>
                </div>

                <div class="ios-group collapsed" data-group="eva">
                    <div class="ios-group-header" onclick="App._toggleGroup('eva')">
                        <span class="ios-group-header-icon">🧹</span>
                        <div class="ios-group-header-title">Eva</div>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
                        <button class="ios-row" onclick="App._clearEvaChat()">
                            <span class="ios-icon">🧹</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Limpiar conversación de Eva</div>
                                <div class="ios-row-sub">Borra el chat actual para que el saludo vuelva a aparecer limpio</div>
                            </div>
                            <span class="ios-value">Acción segura</span>
                        </button>
                        </div>
                    </div>
                </div>

                <div class="ios-group collapsed" data-group="soporte">
                    <div class="ios-group-header" onclick="App._toggleGroup('soporte')">
                        <span class="ios-group-header-icon">💬</span>
                        <div class="ios-group-header-title">Soporte</div>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
                        <button class="ios-row" onclick="App._openWhatsApp()">
                            <span class="ios-icon">💬</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Soporte por WhatsApp</div>
                                <div class="ios-row-sub">Escríbenos directamente, respondemos rápido</div>
                            </div>
                            <span class="ios-chevron">›</span>
                        </button>
                        <button class="ios-row" onclick="App._openEmail()">
                            <span class="ios-icon">✉️</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Correo de soporte</div>
                                <div class="ios-row-sub" id="support-email-sub">soporte@ojoia.com.do</div>
                            </div>
                            <span class="ios-chevron">›</span>
                        </button>
                        <button class="ios-row" onclick="App._showBankInfo()">
                            <span class="ios-icon">🏦</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Datos para pagar</div>
                                <div class="ios-row-sub">Cuenta bancaria y referencia</div>
                            </div>
                            <span class="ios-chevron">›</span>
                        </button>
                        </div>
                    </div>
                </div>

                <div class="ios-group collapsed" data-group="sistema">
                    <div class="ios-group-header" onclick="App._toggleGroup('sistema')">
                        <span class="ios-group-header-icon">🌐</span>
                        <div class="ios-group-header-title">Sistema</div>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
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
                    </div>
                </div>

                <div class="ios-group danger-group collapsed" data-group="sesion">
                    <div class="ios-group-header" onclick="App._toggleGroup('sesion')">
                        <span class="ios-group-header-icon" style="background:rgba(255,69,58,.12)">🚪</span>
                        <div class="ios-group-header-title" style="color:var(--danger)">Cerrar sesión</div>
                        <span class="ios-group-header-chevron">›</span>
                    </div>
                    <div class="ios-group-body">
                        <div class="ios-group-body-inner">
                        <button class="ios-row danger-row" onclick="App.logout()">
                            <span class="ios-icon">🚪</span>
                            <div class="ios-row-main">
                                <div class="ios-row-title">Cerrar sesión</div>
                                <div class="ios-row-sub">Salir de esta cuenta en este dispositivo</div>
                            </div>
                        </button>
                        </div>
                    </div>
                </div>
            </div>`;
    },

    _toggleGroup(name) {
        const el = document.querySelector(`.ios-group[data-group="${name}"]`);
        if (!el) return;
        el.classList.toggle('collapsed');
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
                        ${cams.map(cam => `<option value="${escHtml(cam.camera_id)}" ${cam.camera_id === this._gridSettingsCamId ? 'selected' : ''}>${escHtml(cam.name || cam.camera_id)} · ${escHtml(cam.zone || 'sin zona')}</option>`).join('')}
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
            } catch(e) {}

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

    _supportInfo: null,
    async _loadSupportInfo() {
        try {
            const url = await this._waitForAPI();
            const r = await fetch(url + '/api/support-info', { mode: 'cors' });
            if (r.ok) this._supportInfo = await r.json();
        } catch (e) { /* no critico */ }
        if (!this._supportInfo) this._supportInfo = { whatsapp: '', email: 'soporte@ojoia.com.do', phone: '', bank_info: '' };
        const sub = document.getElementById('support-email-sub');
        if (sub && this._supportInfo.email) sub.textContent = this._supportInfo.email;
    },
    _openWhatsApp() {
        const wa = (this._supportInfo && this._supportInfo.whatsapp) || '';
        const url = wa ? ('https://wa.me/' + wa + '?text=' + encodeURIComponent('Hola OjoIA, necesito ayuda con mi cuenta.')) : '';
        if (url) window.open(url, '_blank');
        else this._toast('', 'Número de WhatsApp no configurado. Contacta a soporte@ojoia.com.do', 'error');
    },
    _openEmail() {
        const em = (this._supportInfo && this._supportInfo.email) || 'soporte@ojoia.com.do';
        window.location.href = 'mailto:' + em + '?subject=' + encodeURIComponent('Soporte OjoIA');
    },
    _showBankInfo() {
        const info = (this._supportInfo && this._supportInfo.bank_info) || 'Datos bancarios no configurados todavía. Contacta a soporte@ojoia.com.do para pagar.';
        alert('Datos para pagar tu suscripción:\n\n' + info + '\n\nDespués de transferir, envía el comprobante por WhatsApp a ' + ((this._supportInfo && this._supportInfo.whatsapp) || 'nuestro WhatsApp') + '.');
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
        if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
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
        // Reiniciar estado de stream para el viewer (nuevo modal)
        const vkey = `viewer-body:${camId}`;
        this._homeStreamStarted[vkey] = false;
        this._streamErrors[vkey] = 0;
        body.innerHTML = `<div class="ojo-placeholder">Cargando...</div>`;
        this._fetchFrame('viewer-body');
        this._fetchViewerGrid();
        if (this._polls.viewer) clearInterval(this._polls.viewer);
        // El MJPEG ya se actualiza solo; solo refrescar el grid (cada 5s)
        this._polls.viewer = setInterval(() => { this._fetchViewerGrid(); }, 5000);
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
        } catch(e) {}
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
        const cam = this._viewerCamId;
        this._viewerCamId = null;
        if (this._polls.viewer) { clearInterval(this._polls.viewer); delete this._polls.viewer; }
        if (cam) {
            const vkey = `viewer-body:${cam}`;
            if (this._streamWatchdogTimers && this._streamWatchdogTimers[vkey]) { clearTimeout(this._streamWatchdogTimers[vkey]); delete this._streamWatchdogTimers[vkey]; }
            if (this._streamErrTimers && this._streamErrTimers[vkey]) { clearTimeout(this._streamErrTimers[vkey]); delete this._streamErrTimers[vkey]; }
            this._homeStreamStarted[vkey] = false;
        }
    },

    _skeleton() { return `<div class="skeleton"></div><div class="skeleton" style="height:100px;margin-top:8px"></div>`; },
    _relTime(ts) { const d = (Date.now() - new Date(ts).getTime()) / 1000; if (d < 60) return 'hace un momento'; if (d < 3600) return `hace ${Math.floor(d/60)} min`; if (d < 86400) return `hace ${Math.floor(d/3600)}h`; return `hace ${Math.floor(d/86400)} días`; },

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
};

// Init PWA install handler
document.addEventListener('DOMContentLoaded', () => {
    App._initPWAInstall();
    App.init();
});