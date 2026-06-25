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

function apiFetch(url, opts = {}) {
    const headers = { ...opts.headers };
    if (opts.body && typeof opts.body === 'string') {
        try { JSON.parse(opts.body); headers['Content-Type'] = 'application/json'; } catch(e) {}
    }
    if (!headers['Content-Type']) headers['Content-Type'] = 'application/json';
    return fetch(url, { mode: 'cors', headers, ...opts });
}

const App = {
    userId: null,
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
                    localStorage.setItem('ojoia_uid', this.userId);
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

    go(page) {
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
        if (page !== 'eva') this._removeStaleEvaChat();
        c.scrollTop = 0;
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
        ({ home: () => this._pageHome(c), cameras: () => this._pageHome(c), eva: () => this._pageEva(c), events: () => this._pageEvents(c), settings: () => this._pageSettings(c) })[page]?.();
    },

    _clearAllPolls() { Object.values(this._polls).forEach(id => clearInterval(id)); this._polls = {}; if (this._configViewerPoll) { clearInterval(this._configViewerPoll); this._configViewerPoll = null; } },
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

    _poll(key, fn, ms) {
        if (this._polls[key]) clearInterval(this._polls[key]);
        fn();
        this._polls[key] = setInterval(() => {
            if (document.hidden || this.page !== key.split('_')[0]) { clearInterval(this._polls[key]); delete this._polls[key]; return; }
            fn();
        }, ms);
    },

    async _initPush() {
        try {
            if (!('Notification' in window)) return;
            const perm = await Notification.requestPermission();
            if (perm !== 'granted') return;
            if ('serviceWorker' in navigator) {
                const reg = await navigator.serviceWorker.register('/sw.js');
                if (firebase.messaging) {
                    const msg = firebase.messaging();
                    const token = await msg.getToken({ serviceWorkerRegistration: reg });
                    if (token) {
                        await apiFetch(this.API + '/api/fcm/register', { method: 'POST', body: JSON.stringify({ user_id: this.userId, fcm_token: token }) });
                    }
                    msg.onMessage(p => this._toast(p.notification?.title || 'OjoIA', p.notification?.body || '', 'danger'));
                }
            }
        } catch(e) { console.log('Push init skipped:', e.message); }
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
            if (lastEvt && lastEvt.qwen?.violation) {
                const ts = lastEvt.timestamp ? new Date(lastEvt.timestamp * 1000).toLocaleTimeString('es-ES', {hour:'2-digit', minute:'2-digit', hour12:true}) : '--';
                lastAlertHTML = `<div class="last-alert" onclick="App.go('events')">
                    <div style="font-size:.75rem;color:var(--danger);font-weight:600;margin-bottom:4px">🚨 ÚLTIMA ALERTA — ${ts}</div>
                    <div style="font-size:.88rem">${lastEvt.qwen?.description || 'Actividad detectada'}</div>
                    <div style="font-size:.75rem;color:var(--text-secondary);margin-top:4px">Toca para ver →</div>
                </div>`;
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

    _fetchHomeFrames() {
        const cams = this._getHomeViewCams();
        cams.forEach((cam, i) => this._fetchFrameForCam(cam.camera_id, `home-frame-${i}`));
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
    _homeViewCount: Number(localStorage.getItem('ojoia_home_view_count') || 1),
    _homeFrameInFlight: {},
    _homeLastDetectionsByCam: {},
    _homeWatermarkTextByCam: {},
    _homeLastYoloFetchByCam: {},
    _gridSettingsCamId: null,

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
            const ts = Date.now();
            const rawUrl = `${this.API}/frames/latest-raw.jpg?camera_id=${camId}&user_id=${uid}&_=${ts}`;
            const camIdShort = camId.substring(0, 8);
            const zone = (this._homeCams && this._homeCams.find) ? (this._homeCams.find(c=>c.camera_id===camId)?.zone || '—') : '—';
            const nowText = new Date().toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
                const dateText = new Date().toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit',year:'2-digit'});
                const watermark = `OJO-${camIdShort} | ${dateText} ${nowText} | ${zone}`;

            this._homeWatermarkTextByCam[camId] = watermark;
            const resetTimer = setTimeout(clearInFlight, 2500);
            const onImgLoad = () => { clearTimeout(resetTimer); clearInFlight(); this._drawYoloBoxes(camId, this._homeLastDetectionsByCam[camId] || [], this._homeWatermarkTextByCam[camId] || watermark, targetId); };
            const onImgError = () => { clearTimeout(resetTimer); clearInFlight(); };
            const dom = this._ensureLiveFrameDom(camId, rawUrl, watermark, onImgLoad, onImgError, targetId);
            if (!dom) { clearInFlight(); return; }
            const { imgEl } = dom;
            imgEl.decoding = 'async';
            imgEl.loading = 'eager';

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
                if (typeof EvaChat !== 'undefined') {
                    EvaChat.history = EvaChat.history || [];
                    EvaChat.history.push({ role: 'user', content: msg });
                    EvaChat.history.push({ role: 'assistant', content: d.response || '', image_url: d.image_url || '' });
                    EvaChat.history = EvaChat.history.slice(-50);
                    EvaChat._saveConversation && EvaChat._saveConversation();
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
                                <input class="range-control" id="cfg_brightness" min="-100" max="100" value="0" oninput="App._updateImageFilter('${camId}')">
                                <div class="range-labels"><span>Oscuro</span><span>Brillante</span></div>
                            </div>
                            <div class="control-card">
                                <div class="control-label-row"><span>🎚️ Contraste</span><strong id="cfg_contrast_val" class="value-pill">0</strong></div>
                                <input class="range-control" id="cfg_contrast" min="-100" max="100" value="0" oninput="App._updateImageFilter('${camId}')">
                                <div class="range-labels"><span>Bajo</span><span>Alto</span></div>
                            </div>
                        </div>
                        <button class="btn config-action" data-config-cmd="brightness" onclick="App._sendCamCmd('${camId}','brightness',document.getElementById('cfg_brightness').value,this)">💾 Aplicar brillo/contraste</button>
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
                    <button class="btn btn-outline" onclick="App._openVigilanceSettings('${camId}')">🛡️ Ajustar protección</button>
                    </section>

                    <button class="btn btn-ghost" onclick="App.go('${returnPage}')">← Volver</button>
                    </div>`;

            // Iniciar polling del viewer
            this._startConfigViewerPoll(camId);
        } catch(e) {
            if (this.page !== 'settings' && this.page !== 'cameras' && this.page !== 'home') return;
            c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:3rem;margin-bottom:16px;">❌</div><div style="font-weight:600;margin-bottom:8px;">Error cargando cámara</div><button class="btn" style="margin-top:16px" onclick="App._openCameraConfig(\''+camId+'\')">Reintentar</button></div>';
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

    // Polling del viewer en config
    _configViewerPoll: null,
    _configRotation: 0,
    _configReturnPage: 'cameras',
    _startConfigViewerPoll(camId) {
        if (this._configViewerPoll) clearInterval(this._configViewerPoll);
        this._configViewerPoll = setInterval(() => {
            const img = document.getElementById('cfg-live-img');
            const wm = document.getElementById('cfg-watermark');
            if (!img) { clearInterval(this._configViewerPoll); return; }
            const ts = Date.now();
            img.src = `${this.API}/frames/latest-raw.jpg?camera_id=${camId}&user_id=${this.userId}&_=${ts}`;
            if (wm) {
                const now = new Date();
                const dateText = now.toLocaleDateString('es-ES',{day:'2-digit',month:'2-digit',year:'2-digit'});
                const ts_str = now.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
                wm.textContent = `OJO-${camId.substring(0,8)} | ${dateText} ${ts_str}`;
            }
        }, 1000);
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
            if (val < 5) { this._toast('', 'Mínimo 5 minutos', 'warning'); return; }
            const r = await apiFetch(`${this.API}/api/cameras/${camId}/cooldown`, {
                method: 'POST',
                body: JSON.stringify({ user_id: this.userId, cooldown_min: val })
            });
            const d = await r.json();
            if (d.ok) {
                this._toast('', `Cooldown guardado: ${val} min`, 'success');
            } else {
                this._toast('', 'Error guardando', 'danger');
            }
        } catch(e) {
            this._toast('', 'Error de red', 'danger');
        } finally {
            this._setConfigButtonBusy(btn, false);
        }
    },

    // ── EVENTS ───────────────────────────────────────────────
    async _pageEvents(c) {
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
                        el2.innerHTML = `<img src="${evt.thumb_url}" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="this.style.display='none'" />`;
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
            ? `<img src="${evt.thumb_url}" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="this.style.display='none';this.parentElement.innerHTML='<span style=\'font-size:1.3rem\'>${icon}</span>'" />`
            : `<span style="font-size:1.3rem;">${icon}</span>`;
        const evtTime = evt.datetime || ts;
        return `<div class="event-row ${violation ? 'event-alert' : ''}" onclick="App._openEvent('${evt.event_id}')">
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
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/events/${eventId}?user_id=${uid}`);
            const d = await r.json();
            if (!d || d.error) return;
            const violation = d.qwen?.violation;
            const modal = document.createElement('div');
            modal.style.cssText = 'position:fixed;inset:0;z-index:500;background:#000;display:flex;flex-direction:column;overflow-y:auto';
            
            const header = document.createElement('div');
            header.style.cssText = 'display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--bg-secondary);position:sticky;top:0;z-index:1';
            const closeBtn = document.createElement('button');
            closeBtn.innerHTML = '✕';
            closeBtn.style.cssText = 'background:none;border:none;color:var(--text-secondary);font-size:1.3rem;cursor:pointer;margin-right:12px';
            closeBtn.onclick = () => modal.remove();
            const title = document.createElement('span');
            title.style.fontWeight = '600';
            title.textContent = d.camera_name || 'Evento';
            header.appendChild(closeBtn);
            header.appendChild(title);
            if (violation) {
                const badge = document.createElement('span');
                badge.className = 'badge badge-alert';
                badge.style.marginLeft = 'auto';
                badge.textContent = '🚨 Alerta';
                header.appendChild(badge);
            }
            
            const content = document.createElement('div');
            content.style.padding = '16px';
            
            if (d.frame_b64) {
                const img = document.createElement('img');
                img.src = `data:image/jpeg;base64,${d.frame_b64}`;
                img.style.cssText = 'width:100%;border-radius:10px;display:block;margin-bottom:16px';
                content.appendChild(img);
                if (d.camera_id) {
                    const liveBtn = document.createElement('button');
                    liveBtn.className = 'btn';
                    liveBtn.style.cssText = 'width:100%;margin-bottom:16px';
                    liveBtn.textContent = '📹 Ver cámara en vivo';
                    liveBtn.onclick = () => { modal.remove(); this._openCameraLive(d.camera_id); };
                    content.appendChild(liveBtn);
                }
            }
            if (d.video_file) {
                const videoCard = document.createElement('div');
                videoCard.className = 'card';
                const videoTitle = document.createElement('div');
                videoTitle.className = 'card-title';
                videoTitle.textContent = '🎞️ Video del evento';
                const video = document.createElement('video');
                video.controls = true;
                video.preload = 'metadata';
                video.playsInline = true;
                video.src = `${this.API}/api/events/${eventId}/video.mp4?user_id=${uid}`;
                video.style.cssText = 'width:100%;border-radius:8px;display:block;background:#000';
                videoCard.appendChild(videoTitle);
                videoCard.appendChild(video);
                content.appendChild(videoCard);
            }
            if (Array.isArray(d.frames) && d.frames.length > 1) {
                const frameCard = document.createElement('div');
                frameCard.className = 'card';
                const frameTitle = document.createElement('div');
                frameTitle.className = 'card-title';
                frameTitle.textContent = 'Imágenes atrás/adelante';
                const frameImg = document.createElement('img');
                frameImg.id = `event-frame-img-${eventId}`;
                frameImg.style.cssText = 'width:100%;border-radius:8px;display:block;background:#000';
                const frameStatus = document.createElement('div');
                frameStatus.id = `event-frame-status-${eventId}`;
                frameStatus.className = 'meta';
                frameStatus.style.textAlign = 'center';
                const frameControls = document.createElement('div');
                frameControls.style.cssText = 'display:flex;gap:8px;align-items:center;margin-top:10px';
                frameControls.innerHTML = `
                    <button class="btn btn-sm" onclick="App._moveEventFrame('${eventId}', ${d.frames.length}, -1)">◀ Atrás</button>
                    <input id="event-frame-range-${eventId}" type="range" min="0" max="${d.frames.length - 1}" value="0" style="flex:1" oninput="App._showEventFrame('${eventId}', ${d.frames.length}, this.value)">
                    <button class="btn btn-sm" onclick="App._moveEventFrame('${eventId}', ${d.frames.length}, 1)">Adelante ▶</button>`;
                frameCard.appendChild(frameTitle);
                frameCard.appendChild(frameImg);
                frameCard.appendChild(frameStatus);
                frameCard.appendChild(frameControls);
                content.appendChild(frameCard);
                this._eventFrameIndex = this._eventFrameIndex || {};
                this._eventFrameIndex[eventId] = 0;
                this._showEventFrame(eventId, d.frames.length, 0);
            }
            if (d.grid_b64) {
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
                content.appendChild(gridCard);
            }
            
            const card = document.createElement('div');
            card.className = 'card';
            const cardTitle = document.createElement('div');
            cardTitle.className = 'card-title';
            cardTitle.textContent = '🤖 Análisis de Eva';
            const qa = d.qwen_analysis || {};
            const desc = this._cleanEventDescription(qa.summary || qa.description || d.qwen?.description || d.description);
            if (desc) {
                const p = document.createElement('p');
                p.style.cssText = 'font-size:.9rem;margin-bottom:8px;line-height:1.45;white-space:pre-wrap';
                p.textContent = desc;
                card.appendChild(p);
            }
            const aiRow = document.createElement('div');
            aiRow.className = 'ai-row';
                const yoloCount = (d.yolo?.count ?? 0) || (Array.isArray(d.yolo?.detections) ? d.yolo.detections.length : 0);
                aiRow.innerHTML = `<div class="ai-card"><div class="ai-label">👁 Detección</div><div class="ai-val">${yoloCount} obj.</div></div><div class="ai-card"><div class="ai-label">👥 Personas</div><div class="ai-val">${d.persons ?? d.qwen_analysis?.persons ?? '—'}</div></div><div class="ai-card"><div class="ai-label">🧠 Eva</div><div class="ai-val">${violation ? '🚨 Alerta' : '✅ Normal'}</div></div>`;
            card.appendChild(aiRow);
            content.appendChild(card);
            
            const btnRow = document.createElement('div');
            btnRow.style.cssText = 'display:flex;gap:8px;margin-top:8px';
            const dismissBtn = document.createElement('button');
            dismissBtn.className = 'btn';
            dismissBtn.style.cssText = 'flex:1;background:var(--bg-tertiary);color:var(--text-secondary)';
            dismissBtn.innerHTML = '✓ Falsa alarma';
            dismissBtn.onclick = () => { this._dismissEvent(eventId); modal.remove(); };
            btnRow.appendChild(dismissBtn);
            if (violation) {
                const confirmBtn = document.createElement('button');
                confirmBtn.className = 'btn';
                confirmBtn.style.cssText = 'flex:1;background:var(--danger)';
                confirmBtn.innerHTML = '⚠️ Confirmar alerta';
                confirmBtn.onclick = () => { this._confirmThreat(eventId); modal.remove(); };
                btnRow.appendChild(confirmBtn);
            }
            
            modal.appendChild(header);
            content.appendChild(btnRow);
            modal.appendChild(content);
            document.body.appendChild(modal);
        } catch(e) {}
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
        if (status) status.textContent = `${index + 1}/${total}`;
    },

    _moveEventFrame(eventId, total, delta) {
        const current = this._eventFrameIndex?.[eventId] || 0;
        this._showEventFrame(eventId, total, Math.max(0, Math.min(total - 1, current + delta)));
    },

    async _dismissEvent(id) {
        try {
            const uid = this.userId || 'default';
            await apiFetch(`${this.API}/api/event/${id}/dismiss`, { method: 'POST', body: JSON.stringify({ user_id: uid }) });
            this._toast('', 'Evento marcado como falsa alarma', 'success');
        } catch(e) {}
    },

    async _confirmThreat(id) {
        try {
            const uid = this.userId || 'default';
            await apiFetch(`${this.API}/api/event/${id}/confirm`, { method: 'POST', body: JSON.stringify({ user_id: uid }) });
            this._toast('', '¡Alerta confirmada! Gracias por la confirmación', 'danger');
        } catch(e) {}
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
            const rules = cam.rules?.length || 0;
            const lastSeen = cam.last_frame ? this._relTime(cam.last_frame) : 'Sin datos';
            return `
                <button class="ios-row" onclick="App._openCameraConfig('${cam.camera_id}')">
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
        this._viewerCamId = null;
        if (this._polls.viewer) { clearInterval(this._polls.viewer); delete this._polls.viewer; }
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