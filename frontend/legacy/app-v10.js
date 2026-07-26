// ============================================================
// OjoIA - App v6 Production Ready
// api.ojoia.com.do | Firebase: ojoia-67216
// Análisis: YOLO detecta objetos -> Qwen valida reglas
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
    _viewerCamId: null,
    _apiReady: false,

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
            const r = await fetch('https://api.ojoia.com.do/admin/server/status', {
                mode: 'no-cors',
                signal: AbortSignal.timeout(3000)
            });
            this.API = 'https://api.ojoia.com.do';
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
                    }, 2000);
                } else {
                    this._err(this._fbErr(e));
                }
            }
        }
    },

    _showLogin() {
        document.getElementById('screen-login').style.display = 'flex';
        document.getElementById('screen-app').style.display = 'none';
    },

    async doLogin() {
        // Solo registro — no hay login separado
        const name = document.getElementById('reg-name').value.trim();
        const biz = document.getElementById('reg-business').value.trim();
        const bizType = document.getElementById('reg-biztype').value;
        const email = document.getElementById('login-email').value.trim();
        const pw = document.getElementById('login-pw').value;
        const pw2 = document.getElementById('login-pw2').value;

        if (!name) { this._err('Escribe tu nombre'); return; }
        if (!biz) { this._err('Escribe el nombre de tu negocio'); return; }
        if (!email) { this._err('Escribe tu correo electrónico'); return; }
        if (!pw || pw.length < 6) { this._err('La contraseña debe tener al menos 6 caracteres'); return; }
        if (pw !== pw2) { this._err('Las contraseñas no coinciden'); return; }

        const btn = document.getElementById('btn-auth');
        btn.disabled = true;
        btn.textContent = 'Creando cuenta...';

        try {
            const cred = await firebase.auth().createUserWithEmailAndPassword(email, pw);
            // Actualizar perfil con el nombre
            await cred.user.updateProfile({ displayName: name });
            // Verificar con backend — esto crea el user.json
            const result = await this._verifyFB(cred.user, {
                name: name,
                business_name: biz,
                business_type: bizType || 'other',
                schedule_open: '08:00',
                schedule_close: '20:00'
            });
            if (!result || !result.success) {
                this._err(result?.error || 'Error al crear la cuenta');
                btn.disabled = false;
                btn.textContent = 'Crear cuenta y empezar';
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = 'Crear cuenta y empezar';
            this._err(this._fbErr(e));
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
        this.go('eva');
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
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
        ({ home: () => this._pageHome(c), eva: () => this._pageEva(c), events: () => this._pageEvents(c), settings: () => this._pageSettings(c) })[page]?.();
    },

    _clearAllPolls() { Object.values(this._polls).forEach(id => clearInterval(id)); this._polls = {}; },

    _poll(key, fn, ms) {
        if (this._polls[key]) clearInterval(this._polls[key]);
        fn();
        this._polls[key] = setInterval(() => {
            if (this.page !== key.split('_')[0]) { clearInterval(this._polls[key]); delete this._polls[key]; return; }
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

            const on = cams.filter(x => x.active).length;
            const lastEvt = evts[0];
            
            const heroText = on > 0 
                ? `✅ ${on} de ${cams.length} cámaras activas` 
                : cams.length > 0 
                    ? `⚠️ ${cams.length} cámaras sin conexión` 
                    : '📹 Sin cámaras';
            const heroClass = on > 0 ? 'ok' : 'off';

            // Build camera selector cards
            let camCardsHTML = '';
            cams.forEach((cam, i) => {
                const isOnline = cam.active;
                const statusColor = isOnline ? 'var(--success)' : 'var(--danger)';
                const statusText = isOnline ? 'En vivo' : 'Desconectado';
                const name = cam.name || cam.camera_id;
                const shortId = cam.camera_id.substring(0, 12);
                camCardsHTML += `
                    <div class="cam-card-selector ${i === 0 ? 'selected' : ''}" 
                         onclick="App._switchHomeCamera('${cam.camera_id}', this)"
                         data-cam-id="${cam.camera_id}">
                        <div class="cam-selector-dot" style="background:${statusColor}"></div>
                        <div class="cam-selector-info">
                            <div class="cam-selector-name">ojo-${shortId}</div>
                            <div class="cam-selector-zone">${cam.zone || 'sin zona'} · ${name}</div>
                        </div>
                        <span class="cam-selector-status" style="color:${statusColor}">${statusText}</span>
                    </div>`;
            });

            let lastAlertHTML = '';
            if (lastEvt && lastEvt.qwen?.violation) {
                const ts = lastEvt.timestamp ? new Date(lastEvt.timestamp * 1000).toLocaleTimeString('es-ES', {hour:'2-digit', minute:'2-digit', hour12:true}) : '--';
                lastAlertHTML = `<div class="last-alert" onclick="App.go('events')">
                    <div style="font-size:.75rem;color:var(--danger);font-weight:600;margin-bottom:4px">🚨 ÚLTIMA ALERTA — ${ts}</div>
                    <div style="font-size:.88rem">${lastEvt.qwen?.description || 'Actividad detectada'}</div>
                    <div style="font-size:.75rem;color:var(--text-secondary);margin-top:4px">Toca para ver →</div>
                </div>`;
            }

            // Default to first camera
            const defaultCam = cams.length > 0 ? cams[0] : null;
            const defaultCamId = defaultCam ? defaultCam.camera_id : '';

            // Determinar modo centinela (fuera de horario)
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

            c.innerHTML = `
                <div class="home-hero">
                    <div class="hero-status ${heroClass}">${heroText}</div>
                    <div style="margin-top:8px;">${vigilanteBadge}</div>
                </div>
                ${lastAlertHTML}

                <!-- LIVE CAMERA VIEW - ARRIBA DE TODO -->
                <div class="card" id="card-live-view">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                        <div style="font-weight:600">📷 En vivo — <span id="home-live-cam-name">${defaultCam ? defaultCam.name : 'Sin cámara'}</span></div>
                        <span id="home-live-status" class="badge ${defaultCam?.active ? 'badge-ok' : 'badge-alert'}">${defaultCam?.active ? '● En vivo' : '○ Offline'}</span>
                    </div>
                    <div id="live-wrap"><div class="ojo-placeholder">Esperando imagen...</div></div>
                </div>

                <!-- CAM SELECTOR + STATS EN FILA -->
                <div style="display:flex;gap:12px">
                    <div class="card" style="flex:1;min:0">
                        <div class="card-title">🎯 Cámaras <span class="badge badge-${on > 0 ? 'ok' : 'off'}">${on}/${cams.length}</span></div>
                        ${camCardsHTML ? camCardsHTML : '<p class="meta">Sin cámaras configuradas</p>'}
                    </div>
                    <div class="card" style="flex:1;min:0">
                        <div class="card-title">📊 Hoy</div>
                        <div class="stats-row">
                            <div class="stat"><div class="stat-val" id="stat-events">—</div><div class="stat-lbl">Eventos</div></div>
                            <div class="stat"><div class="stat-val danger" id="stat-alerts">—</div><div class="stat-lbl">Alertas</div></div>
                            <div class="stat"><div class="stat-val ok" id="stat-cams">${on}</div><div class="stat-lbl">Activas</div></div>
                        </div>
                    </div>
                </div>

                <!-- GRID -->
                <div class="card" id="card-grid">
                    <div class="card-title">🔲 Grid de detección <span id="grid-badge" class="badge badge-ok" style="margin-left:8px">0/16</span></div>
                    <div class="prog-bar"><div class="prog-fill" id="grid-progress" style="width:0%"></div></div>
                    <div id="grid-wrap"><p class="meta" style="padding:8px 0">YOLO detecta objetos → Qwen analiza el grid</p></div>
                </div>

                <!-- PROMPT DE VIGILANCIA -->
                <div class="card" id="card-vigilance" style="display:none">
                    <div class="card-title">🛡️ Vigilancia — <span id="vigilance-cam-name"></span></div>
                    <div id="vigilance-section">
                        <div class="vigilance-block">
                            <div class="vigilance-block-title">🎯 Prompt de escaneo</div>
                            <div id="vigilance-prompt-display" class="vigilance-block-content"></div>
                        </div>
                    </div>
                </div>`;

            this._homeCams = cams;
            this._homeActiveCamId = defaultCamId;
            this._fetchStats();
            // Load vigilance for first camera (from list data - may be basic)
            if (defaultCam) {
                this._loadCamVigilance(defaultCam);
                // Fetch full config in background (non-blocking)
                this._fetchFullCamConfig(defaultCamId);
            }
            // Start polls after a short delay to let page render first
            setTimeout(() => {
                this._poll('home_frame', () => this._fetchFrame('live-wrap'), 1500);
                this._poll('home_grid', () => this._fetchGrid('grid-wrap'), 10000);
                this._poll('home_stats', () => this._fetchStats(), 30000);
                this._poll('home_cams', () => this._refreshCamStatus(), 15000);
            }, 500);
        } catch(e) {
            c.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><div class="empty-title">Sin conexión</div><p>Verifica que el servidor esté activo</p><button class="btn btn-sm" onclick="App.go('home')" style="margin-top:12px">Reintentar</button></div>`;
        }
    },

    async _fetchFullCamConfig(camId) {
        try {
            const r = await apiFetch(`${this.API}/api/cameras/${camId}?user_id=${this.userId}`);
            const d = await r.json();
            const cam = d.camera || d || {};
            // Update vigilance if this is still the active camera
            if (this._homeActiveCamId === camId) {
                this._loadCamVigilance(cam);
            }
            // Update cam in cache
            const idx = this._homeCams.findIndex(c => c.camera_id === camId);
            if (idx >= 0) this._homeCams[idx] = { ...this._homeCams[idx], ...cam };
        } catch(e) {}
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
            // Update camera selector cards
            cams.forEach(cam => {
                const card = document.querySelector(`.cam-card-selector[data-cam-id="${cam.camera_id}"]`);
                if (!card) return;
                const dot = card.querySelector('.cam-selector-dot');
                const status = card.querySelector('.cam-selector-status');
                const isOnline = cam.active;
                const color = isOnline ? 'var(--success)' : 'var(--danger)';
                const text = isOnline ? 'En vivo' : 'Desconectado';
                if (dot) dot.style.background = color;
                if (status) { status.style.color = color; status.textContent = text; }
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

async _fetchFrameForCam(camId) {
        try {
            const el = document.getElementById('live-wrap');
            if (!el) return;
            const ts = Date.now();
            const uid = this.userId || 'default';
            const imgUrl = `${this.API}/frames/latest.jpg?camera_id=${camId}&user_id=${uid}&_=${ts}`;
            const r = await apiFetch(`${this.API}/frames/latest?camera_id=${camId}&user_id=${uid}`);
            const d = await r.json();
            if (!d.success || !d.image_b64) {
                el.innerHTML = '<div class="ojo-placeholder">⏸️ Esperando imagen en vivo...</div>';
                return;
            }
            const yolo = d.yolo?.count != null ? `${d.yolo.count} 👁` : '—';
            let ts_str = '';
            if (d.metadata?.timestamp) {
                const raw = d.metadata.timestamp;
                const dt = typeof raw === 'number' ? new Date(raw * 1000) : new Date(raw);
                ts_str = dt.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
            }
            // Marca de agua: OJO-XX | hora | área
            const camIdShort = camId.substring(0, 8);
            const zone = (this._homeCams && this._homeCams.find) ? (this._homeCams.find(c=>c.camera_id===camId)?.zone || '—') : '—';
            const watermark = `OJO-${camIdShort} | ${ts_str} | ${zone}`;
            // Actualizar sin re-crear el DOM (evita saltos de tamaño)
            let imgEl = el.querySelector('img.live-img');
            let wmEl = el.querySelector('.live-watermark');
            let aiRow = el.querySelector('.ai-row');
            if (!imgEl) {
                // Primera vez: crear el DOM completo
                el.innerHTML = `
                    <div style="position:relative;width:100%;max-width:720px;margin:0 auto;background:#1a1a1a;border-radius:8px;overflow:hidden">
                        <img src="${imgUrl}" class="live-img" style="width:100%;height:auto;aspect-ratio:1/1;object-fit:contain;display:block" onerror="this.parentElement.innerHTML='<div class=\\'ojo-placeholder\\'>📡 Sin señal</div>'">
                        <div class="live-watermark" style="position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,0.6);color:#fff;font-size:.65rem;padding:3px 8px;border-radius:4px;font-family:monospace;pointer-events:none">${watermark}</div>
                    </div>
                    <div class="ai-row">
                        <div class="ai-card"><div class="ai-label">YOLO</div><div class="ai-val">${yolo}</div></div>
                        <div class="ai-card"><div class="ai-label">Hora</div><div class="ai-val" style="font-size:.8rem">${ts_str || '—'}</div>
                    </div>`;
            } else {
                // Solo actualizar src, marca de agua y datos (sin re-crear DOM)
                imgEl.src = imgUrl;
                if (wmEl) wmEl.textContent = watermark;
                const yoloEl = el.querySelector('.ai-card:first-child .ai-val');
                const horaEl = el.querySelector('.ai-card:last-child .ai-val');
                if (yoloEl) yoloEl.textContent = yolo;
                if (horaEl) horaEl.textContent = ts_str || '—';
            }
            const timeEl = document.getElementById('frame-time');
            if (timeEl) timeEl.textContent = ts_str ? `· ${ts_str}` : '';
        } catch(e) {}
    },

    _loadCamVigilance(cam) {
        const card = document.getElementById('card-vigilance');
        if (!card) return;
        card.style.display = 'block';
        const nameEl = document.getElementById('vigilance-cam-name');
        if (nameEl) nameEl.textContent = cam.name || cam.zone || cam.camera_id;

        // Prompt
        const promptDisplay = document.getElementById('vigilance-prompt-display');
        if (promptDisplay) {
            promptDisplay.textContent = cam.system_prompt || cam.vigilance_prompt || 'Sin configurar';
        }

        // Rules
        const rules = cam.rules || cam.vigilance_rules || [];
        const rulesEs = cam.rules_es || [];
        const countEl = document.getElementById('vigilance-rules-count');
        const listEl = document.getElementById('vigilance-rules-list');
        if (countEl) countEl.textContent = rules.length > 0 ? `${rules.length} reglas` : 'Sin reglas';
        if (listEl) {
            if (rules.length > 0) {
                listEl.innerHTML = rules.map((r, i) => {
                    const text = Array.isArray(rulesEs) && rulesEs[i] ? rulesEs[i] : (typeof r === 'string' ? r : (r.es || r.en || JSON.stringify(r)));
                    return `<div class="rule-item">
                        <span class="rule-num">${i + 1}</span>
                        <span class="rule-text">${text}</span>
                    </div>`;
                }).join('');
            } else {
                listEl.innerHTML = '<p class="meta">Sin reglas configuradas</p>';
            }
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
                const pct = Math.round((frames / 16) * 100);
                if (badgeEl) { badgeEl.textContent = `${frames}/16`; badgeEl.className = `badge ${frames >= 16 ? 'badge-alert' : 'badge-ok'}`; }
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
                            <span class="meta">${frames}/16 frames${d.partial ? ' (parcial)' : ''}</span>
                            <span class="badge ${frames >= 16 ? 'badge-alert' : 'badge-ok'}">${frames >= 16 ? '✓ GRID LLENO' : `${pct}%`}</span>
                        </div>
                        <div style="background:#1a1a1a;border-radius:8px;overflow:hidden">
                            <img class="grid-img" src="data:image/jpeg;base64,${d.grid_b64}" style="width:100%;max-width:720px;aspect-ratio:1/1;object-fit:contain;display:block;transition:opacity 0.3s ease">
                        </div>`;
                }
            } else {
                if (badgeEl) { badgeEl.textContent = '0/16'; badgeEl.className = 'badge badge-ok'; }
                if (progressEl) { progressEl.style.width = '0%'; progressEl.style.transition = 'width 0.5s ease'; }
                el.innerHTML = '<p class="meta" style="padding:8px 0">YOLO detecta objetos → Qwen analiza el grid</p>';
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
                totalEvents += m.total_events || 0;
                totalAlerts += m.total_alerts || 0;
            }
            if (se) se.textContent = totalEvents;
            if (sa) sa.textContent = totalAlerts;
            if (sc) sc.textContent = active;
        } catch(e) {}
    },

    // ── CAMERAS ──────────────────────────────────────────────
    async _pageCameras(c) {
        c.innerHTML = this._skeleton();
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const cams = (await r.json()).cameras || [];

            if (cams.length === 0) {
                c.innerHTML = `<div class="empty-state">
                    <div class="empty-icon">📷</div>
                    <div class="empty-title">Sin ojos</div>
                    <p>Configura tu primera ojo con Eva</p>
                    <button class="btn" onclick="App.newCamera()" style="margin-top:16px">+ Nueva ojo con Eva</button>
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
                                    📏 ${rulesCount} reglas activas${cam.metrics?.needs_review ? ' · 🔧 necesita revisión' : ''}
                                </div>
                            </div>
                        </div>
                        <div class="ojo-card-actions" style="justify-content:flex-end">
                            <button class="btn btn-sm" onclick="event.stopPropagation();App.openEva('${cam.camera_id}')">✏️ Editar reglas</button>
                            <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();App._openCameraConfig('${cam.camera_id}')">⚙️ Ajustes</button>
                            <button class="btn-ghost btn-sm" style="color:var(--danger)" onclick="App.deleteCamera('${cam.camera_id}','${cam.name}')">🗑️</button>
                        </div>
                    </div>`;
                });
                html += `<button class="btn" style="margin-top:8px" onclick="App.newCamera()">+ Nueva ojo con Eva</button>`;
                c.innerHTML = html;
                cams.forEach(cam => cam.active && this._loadThumb(cam.camera_id));
            }
        } catch(e) {
            c.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><p>Error de conexión</p></div>`;
        }
    },

    // ── EVA CHAT ─────────────────────────────────────────────────
    async _pageEva(c) {
        c.innerHTML = '';
        c.style.padding = '0';
        c.style.overflow = 'hidden';
        c.style.display = 'flex';
        c.style.flexDirection = 'column';
        if (this.userId && typeof EvaChat !== 'undefined') {
            const fbUser = firebase.auth().currentUser; const uName = fbUser?.displayName || fbUser?.email || ''; EvaChat.init(this.userId, uName);
        } else if (this.userId) {
            c.innerHTML = '<div class="empty-state"><div class="empty-icon">🤖</div><div class="empty-title">Cargando Eva...</div></div>';
        } else {
            c.innerHTML = '<div class="empty-state"><div class="empty-icon">🔒</div><div class="empty-title">Inicia sesión</div></div>';
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

    // ── CAMERA CONFIG (ajustes de cámara via ESP32 local API) ──
    async _openCameraConfig(camId) {
        const c = document.getElementById('app-content');
        c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:2rem;margin-bottom:16px;">⏳</div><div style="font-weight:600;">Cargando configuración...</div></div>';
        try {
            const r = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            const d = await r.json();
            const cam = (d.cameras || []).find(x => x.camera_id === camId);
            if (!cam) throw new Error('Camera not found');
            let globalCooldown = 5;
            try {
                const erc = await apiFetch(`${this.API}/admin/eva-config`);
                const erd = await erc.json();
                globalCooldown = erc.ok ? (erd.violation_cooldown_min || 5) : 5;
            } catch(e) {}
            cam.cooldown_min = globalCooldown;
            const fs = cam.active ? 'Online' : 'Offline';
            const fsColor = cam.active ? 'var(--success)' : 'var(--danger)';

            c.innerHTML = `
                <div style="display:flex;flex-direction:column;height:100%;overflow-y:auto;padding:16px;">
                    <!-- HEADER -->
                    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">
                        <span style="font-size:2rem;">⚙️</span>
                        <div style="flex:1">
                            <div style="font-weight:600;font-size:1.1rem;">${cam.name}</div>
                            <div style="color:${fsColor};font-size:0.85rem;">${fs}</div>
                        </div>
                    </div>

                    <!-- VIEWER EN VIVO -->
                    <div style="font-weight:600;margin-bottom:8px;">📷 Vista en vivo</div>
                    <div style="position:relative;width:100%;max-width:720px;margin:0 auto 8px;background:#1a1a1a;border-radius:8px;overflow:hidden">
                        <img id="cfg-live-img" src="${this.API}/frames/latest.jpg?camera_id=${camId}&user_id=${this.userId}&_=${Date.now()}" style="width:100%;height:auto;aspect-ratio:4/3;object-fit:contain;display:block;background:#1a1a1a" onerror="this.parentElement.innerHTML='<div class=\\'ojo-placeholder\\' style=\\'padding:40px;aspect-ratio:4/3;display:flex;align-items:center;justify-content:center\\'>📡 Sin señal</div>'">
                        <div id="cfg-watermark" style="position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,0.7);color:#fff;font-size:.65rem;padding:3px 8px;border-radius:4px;font-family:monospace;pointer-events:none">OJO-${camId.substring(0,8)}</div>
                    </div>

                    <!-- BRILLO / CONTRASTE (se envían al ESP32) -->
                    <div style="font-weight:600;margin:12px 0 8px;">🔆 Brillo</div>
                    <input type="range" id="cfg_brightness" min="-100" max="100" value="0" style="width:100%;margin-bottom:4px;" oninput="App._updateImageFilter('${camId}')">
                    <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-secondary);margin-bottom:8px;">
                        <span>Oscuro</span><span id="cfg_brightness_val">0</span><span>Brillante</span>
                    </div>
                    <div style="font-weight:600;margin-bottom:8px;">🎚️ Contraste</div>
                    <input type="range" id="cfg_contrast" min="-100" max="100" value="0" style="width:100%;margin-bottom:4px;" oninput="App._updateImageFilter('${camId}')">
                    <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-secondary);margin-bottom:16px;">
                        <span>Bajo</span><span id="cfg_contrast_val">0</span><span>Alto</span>
                    </div>
                    <button class="btn btn-sm" onclick="App._sendCamCmd('${camId}','brightness',document.getElementById('cfg_brightness').value)" style="width:100%;margin-bottom:16px;">💾 Aplicar brillo/contraste</button>

                    <!-- ROTACIÓN -->
                    <div style="font-weight:600;margin-bottom:8px;">↻ Rotación</div>
                    <button class="btn" onclick="App._sendCamCmd('${camId}','rotation','next')" style="width:100%;padding:14px;font-size:1rem;margin-bottom:4px;">↻ Rotar 90°</button>
                    <p class="meta" style="margin:4px 0 16px;text-align:center;">Cicla: 0° → 90° → 180° → 270° → 0°</p>

                    <!-- CALIDAD -->
                    <div style="font-weight:600;margin-bottom:8px;">📐 Calidad</div>
                    <div style="display:flex;gap:8px;margin-bottom:16px;">
                        <button class="btn btn-sm btn-outline" onclick="App._sendCamCmd('${camId}','quality',8)" style="flex:1;">Baja</button>
                        <button class="btn btn-sm" onclick="App._sendCamCmd('${camId}','quality',12)" style="flex:1;">Media</button>
                        <button class="btn btn-sm btn-outline" onclick="App._sendCamCmd('${camId}','quality',6)" style="flex:1;">Alta</button>
                    </div>

                    <!-- ILUMINACIÓN -->
                    <div style="font-weight:600;margin-bottom:8px;">💡 Iluminación</div>
                    <div style="display:flex;gap:8px;margin-bottom:16px;">
                        <button class="btn btn-sm" onclick="App._sendCamCmd('${camId}','led',1)" style="flex:1;">💡 On</button>
                        <button class="btn btn-sm btn-outline" onclick="App._sendCamCmd('${camId}','led',0)" style="flex:1;">🌙 Off</button>
                        <button class="btn btn-sm btn-outline" onclick="App._sendCamCmd('${camId}','led_auto',1)" style="flex:1;">⚡ Auto</button>
                    </div>

                    <!-- COOLDOWN -->
                    <div style="font-weight:600;margin-bottom:8px;">⏱️ Cooldown entre alertas</div>
                    <p class="meta" style="margin-bottom:8px;">Tiempo mínimo entre notificaciones de la misma regla</p>
                    <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center;">
                        <input type="number" id="cfg_cooldown_min" value="${cam.cooldown_min}" min="5" max="60" style="flex:1;padding:10px;border:1px solid var(--border);border-radius:8px;font-size:1rem;text-align:center;">
                        <span style="font-size:0.85rem;color:var(--text-secondary);">min</span>
                    </div>
                    <button class="btn btn-sm" onclick="App._saveCooldown('${camId}')" style="width:100%;margin-bottom:16px;">💾 Guardar cooldown</button>

                    <!-- SNAPSHOT + REGLAS -->
                    <div style="display:flex;gap:12px;margin-bottom:20px;">
                        <button class="btn btn-outline" style="flex:1;padding:12px;" onclick="App._sendCamCmd('${camId}','snapshot',0)">📸 Snapshot</button>
                        <button class="btn btn-outline" style="flex:1;padding:12px;" onclick="App.openEva('${camId}')">✏️ Editar reglas</button>
                    </div>

                    <div style="flex:1;"></div>
                    <button class="btn btn-ghost" style="width:100%;margin-top:8px;" onclick="App.go('cameras')">← Volver a cámaras</button>
                </div>`;

            // Iniciar polling del viewer
            this._startConfigViewerPoll(camId);
        } catch(e) {
            c.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;"><div style="font-size:3rem;margin-bottom:16px;">❌</div><div style="font-weight:600;margin-bottom:8px;">Error cargando cámara</div><button class="btn" style="margin-top:16px" onclick="App._openCameraConfig(\''+camId+'\')">Reintentar</button></div>';
        }
    },

    // Polling del viewer en config
    _configViewerPoll: null,
    _startConfigViewerPoll(camId) {
        if (this._configViewerPoll) clearInterval(this._configViewerPoll);
        this._configViewerPoll = setInterval(() => {
            const img = document.getElementById('cfg-live-img');
            const wm = document.getElementById('cfg-watermark');
            if (!img) { clearInterval(this._configViewerPoll); return; }
            const ts = Date.now();
            img.src = `${this.API}/frames/latest.jpg?camera_id=${camId}&user_id=${this.userId}&_=${ts}`;
            if (wm) {
                const now = new Date();
                const ts_str = now.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
                wm.textContent = `OJO-${camId.substring(0,8)} | ${ts_str}`;
            }
        }, 2000);
    },

    // Aplicar filtros de brillo/contraste a la imagen del viewer
    _updateImageFilter(camId) {
        const b = document.getElementById('cfg_brightness').value;
        const c = document.getElementById('cfg_contrast').value;
        document.getElementById('cfg_brightness_val').textContent = b;
        document.getElementById('cfg_contrast_val').textContent = c;
        const img = document.getElementById('cfg-live-img');
        if (img) {
            img.style.filter = `brightness(${100 + parseInt(b)}%) contrast(${100 + parseInt(c)}%)`;
        }
    },

    async _sendCamCmd(camId, cmd, val) {
        try {
            let body = {};
            if (cmd === 'rotation') {
                let current = val === 'next' ? 0 : val;
                const rotText = document.querySelector('.meta')?.textContent || '0°';
                const rotMap = {'0°':0,'90°':1,'180°':2,'270°':3};
                current = rotMap[rotText] || 0;
                if (val === 'next') current = (current + 1) % 4;
                body = {rotation: current};
            } else if (cmd === 'quality') {
                body = {quality: val};
            } else if (cmd === 'led') {
                body = {led_auto: false, led_on: val ? true : false};
            } else if (cmd === 'led_auto') {
                body = {led_auto: true};
            } else if (cmd === 'brightness') {
                body = {brightness: parseInt(val) || 0};
            } else if (cmd === 'snapshot') {
                const imgUrl = `${this.API}/frames/latest.jpg?camera_id=${camId}&user_id=${this.userId}&_=${Date.now()}`;
                window.open(imgUrl, '_blank');
                return;
            } else {
                return;
            }
            console.log('SEND CMD:', camId, cmd, val, body);
            const r = await apiFetch(`${this.API}/cameras/${camId}/cmd`, {
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
                    this._toast('', d.detail || 'Error', 'danger');
                }
                return;
            }
            if (cmd === 'led') {
                this._toast('', 'LED ' + (val ? 'encendido 💡' : 'apagado 🌙'), 'success');
            } else if (cmd === 'led_auto') {
                this._toast('', 'LED automático ⚡', 'success');
            } else if (cmd === 'quality') {
                const labels = {6:'Alta', 12:'Media', 8:'Baja', 10:'Media-Baja'};
                this._toast('', 'Calidad: ' + (labels[val] || val), 'success');
            } else if (cmd === 'rotation') {
                this._toast('', 'Rotación: ' + (body.rotation * 90) + '°', 'success');
            } else if (cmd === 'brightness') {
                this._toast('', 'Brillo/contraste aplicado 🔆', 'success');
            }
        } catch(e) {
            console.error('SEND CMD ERROR:', e);
            this._toast('', 'Error de red — reintenta', 'danger');
        }
    },

    async _saveCooldown(camId) {
        const el = document.getElementById('cfg_cooldown_min');
        const val = parseInt(el?.value) || 5;
        if (val < 5) { this._toast('', 'Mínimo 5 minutos', 'warning'); return; }
        try {
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
        }
    },

    // ── EVENTS ───────────────────────────────────────────────
    async _pageEvents(c) {
        c.innerHTML = `<div class="filters">
            <button class="filter-btn active" onclick="App._filterEvents(this,'today')">📅 Hoy</button>
            <button class="filter-btn" onclick="App._filterEvents(this,'alerts')">🚨 Alertas</button>
            <button class="filter-btn" onclick="App._filterEvents(this,'all')">📋 Todos</button>
            <select id="filter-cam" onchange="App._filterByCam(this.value)" style="margin-left:auto;padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-secondary);color:var(--text);font-size:0.75rem;">
                <option value="all">Todas las cámaras</option>
            </select>
        </div><div id="events-list">${this._skeleton()}</div>`;
        this._eventFilterCam = 'all';
        this._lastEventTs = 0;
        this._loadEvents('today');
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
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=today&limit=1`);
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
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=${filter}`);
            let evts = (await r.json()).events || [];
            
            // Filtrar por cámara si seleccionó una
            if (this._eventFilterCam && this._eventFilterCam !== 'all') {
                evts = evts.filter(e => e.camera_id === this._eventFilterCam);
            }

            if (!evts.length) {
                el.innerHTML = `<div class="empty-state" style="padding:40px 0">
                    <div style="font-size:2.5rem;margin-bottom:12px">👁</div>
                    <div class="empty-title">Todo tranquilo</div>
                    <p>No hay eventos${filter === 'alerts' ? ' de alerta' : ''} para este filtro</p>
                </div>`;
                return;
            }

            // Guardar el último timestamp para smart polling
            if (evts.length > 0) {
                this._lastEventTs = evts[0].timestamp || 0;
            }

            el.innerHTML = evts.map(evt => {
                const violation = evt.qwen?.violation;
                const level = violation ? 'alert' : 'ok';
                const label = violation ? '🚨 Análisis' : '✅ Normal';
                const ts = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleString('es-ES', {hour:'2-digit',minute:'2-digit',month:'short',day:'numeric',hour12:true}) : '--';
                const camName = evt.camera_name || evt.camera_id || 'Sin nombre';
                const camZone = evt.metadata?.zone || '';
                const rawDesc = evt.qwen?.description || (violation ? 'Violación detectada' : 'Sin actividad sospechosa');
                let desc = rawDesc
                    .replace(/- If ALL checks NO[\s\S]*/m, '')
                    .replace(/No violation detected[\s\S]*/i, 'Sin actividad sospechosa')
                    .replace(/The employee's hands[\s\S]*/i, 'Sin actividad sospechosa')
                    .replace(/The provided (images|frames)[\s\S]*/i, 'Sin actividad sospechosa')
                    .replace(/Error analizando[\s\S]*/i, 'Sin actividad sospechosa')
                    .trim();
                if (!desc || desc.length < 3) desc = violation ? 'Violación detectada' : 'Sin actividad sospechosa';
                desc = desc.substring(0, 120);
                let yoloCount = evt.yolo_count || evt.yolo?.count || 0;
                if (evt.qwen_analysis && evt.qwen_analysis.length > 200) {
                    evt.qwen_analysis = evt.qwen_analysis.substring(0, 150) + '...';
                }
                const iconBg = violation ? 'linear-gradient(135deg, #ff453a, #af2c24)' : 'linear-gradient(135deg, #30d158, #1aab3f)';
                const icon = violation ? '🚨' : '✓';
                let thumbHtml = '';
                if (evt.thumb_url) {
                    thumbHtml = `<img src="${evt.thumb_url}" style="width:100%;height:100%;object-fit:cover;border-radius:8px" onerror="this.style.display='none';this.parentElement.innerHTML='<span style=\'font-size:1.3rem\'>${icon}</span>'" />`;
                } else {
                    thumbHtml = `<span style="font-size:1.3rem;">${icon}</span>`;
                }
                const evtDesc = evt.qwen_analysis || evt.description || (violation ? 'Violación detectada' : 'Actividad normal');
                const evtTime = evt.datetime || ts;
                return `<div class="event-row ${violation ? 'event-alert' : ''}" onclick="App._openEvent('${evt.event_id}')">
                    <div class="event-thumb" id="evthumb-${evt.event_id}" style="background:#222;display:flex;align-items:center;justify-content:center;overflow:hidden;border-radius:10px;flex-shrink:0;width:80px;height:60px">
                        ${thumbHtml}
                    </div>
                    <div class="event-info" style="flex:1;min-width:0">
                        <div class="event-title">${camName}${camZone ? ' · ' + camZone : ''}</div>
                        <div class="meta">${evtTime} · YOLO: ${yoloCount} obj</div>
                        <div class="meta event-desc" style="margin-top:2px;font-size:0.78rem;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${evtDesc}</div>
                    </div>
                    <span class="badge badge-${level}" style="flex-shrink:0">${label}</span>
                </div>`;
            }).join('');

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
            cardTitle.textContent = '🤖 Análisis Qwen';
            const desc = d.qwen?.description;
            if (desc) {
                const p = document.createElement('p');
                p.style.cssText = 'font-size:.9rem;margin-bottom:8px';
                // Limpiar texto del template de Qwen (eventos viejos en inglés)
                const cleanDesc = desc
                    .replace(/- If ALL checks NO[\s\S]*/m, '')
                    .replace(/No violation detected[\s\S]*/i, 'Sin actividad sospechosa')
                    .replace(/The employee's hands[\s\S]*/i, 'Sin actividad sospechosa')
                    .replace(/The provided (images|frames)[\s\S]*/i, 'Sin actividad sospechosa')
                    .replace(/Error analizando[\s\S]*/i, 'Sin actividad sospechosa')
                    .trim();
                p.textContent = cleanDesc || 'Sin actividad sospechosa';
                card.appendChild(p);
            }
            const aiRow = document.createElement('div');
            aiRow.className = 'ai-row';
            aiRow.innerHTML = `<div class="ai-card"><div class="ai-label">👁 YOLO</div><div class="ai-val">${d.yolo?.count ?? '—'} obj.</div></div><div class="ai-card"><div class="ai-label">🧠 Qwen</div><div class="ai-val">${violation ? '🚨 Violación' : '✅ Normal'}</div></div>`;
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
                confirmBtn.innerHTML = '⚠️ Confirmar amenaza';
                confirmBtn.onclick = () => { this._confirmThreat(eventId); modal.remove(); };
                btnRow.appendChild(confirmBtn);
            }
            
            modal.appendChild(header);
            content.appendChild(btnRow);
            modal.appendChild(content);
            document.body.appendChild(modal);
        } catch(e) {}
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
        let profile = {};
        let cams = [];
        try { 
            const r = await apiFetch(`${this.API}/api/user/profile?user_id=${this.userId}`); 
            profile = await r.json(); 
        } catch(e) {}
        try {
            const r2 = await apiFetch(`${this.API}/api/cameras?user_id=${this.userId}`);
            cams = (await r2.json()).cameras || [];
        } catch(e) {}
        
        const plan = profile.plan || 'Fundador';
        const active = profile.status === 'active';
        
        // Build camera list HTML
        let camsHtml = '';
        if (cams.length > 0) {
            cams.forEach(cam => {
                const fs = cam.active ? '🟢' : '⚫';
                const zone = cam.zone || 'sin zona';
                const events = cam.metrics?.total_events || 0;
                const alerts = cam.metrics?.total_alerts || 0;
                const rules = cam.rules?.length || 0;
                const lastSeen = cam.last_frame ? this._relTime(cam.last_frame) : 'Sin datos';
                const ip = cam.local_ip || cam.ip || 'N/A';
                
                camsHtml += `
                <div class="settings-row" onclick="App._openCameraConfig('${cam.camera_id}')">
                    <span class="s-icon">📷</span>
                    <div style="flex:1">
                        <div style="font-weight:500">${fs} ${cam.name}</div>
                        <div style="font-size:0.72rem;color:var(--text-secondary)">
                            ${zone} · ${events} evt · ${alerts} alert · ${rules} reglas · ${lastSeen}
                        </div>
                        <div style="font-size:0.72rem;color:var(--text-secondary)">IP: ${ip}</div>
                    </div>
                    <span class="chev">›</span>
                </div>`;
            });
        } else {
            camsHtml = '<div style="padding:12px;color:var(--font-size:.85rem;text-align:center">Sin cámaras configuradas</div>';
        }

        c.innerHTML = `
            <div class="settings-section">
                <div class="section-lbl">Suscripción</div>
                <div class="settings-row" onclick="App._showSubscription()">
                    <span class="s-icon">💳</span><span style="flex:1">Plan ${plan}</span>
                    <span style="color:${active ? 'var(--success)' : 'var(--danger)'};font-size:.85rem">${active ? 'Activo' : 'Inactivo'}</span>
                    <span class="chev">›</span>
                </div>
            </div>
            <div class="settings-section">
                <div class="section-lbl">Cámaras (${cams.length})</div>
                ${camsHtml}
            </div>
            <div class="settings-section">
                <div class="section-lbl">Sistema</div>
                <div class="settings-row" onclick="App._showApiConfig()">
                    <span class="s-icon">🌐</span><span style="flex:1">URL del servidor</span><span class="chev">›</span>
                </div>
                ${!this._isPWAInstalled() ? `
                <div class="settings-row" onclick="App._installApp()">
                    <span class="s-icon">📱</span><span style="flex:1">Instalar app</span><span class="chev">›</span>
                </div>` : ''}
                <div style="padding:8px 12px;font-size:.75rem;color:var(--text-secondary)">${this.API}</div>
            </div>
            <div class="settings-section">
                <div class="settings-row danger-row" onclick="App.logout()">
                    <span class="s-icon">🚪</span><span style="color:var(--danger)">Cerrar sesión</span>
                </div>
            </div>`;
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
                    message: '__greet__',
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
            if (msg.image_url) {
                imgHtml = '<div style="margin-top:8px;margin-bottom:4px;"><img src="' + msg.image_url + '" style="width:100%;max-height:200px;object-fit:contain;border-radius:8px;background:#0a0a0a;"></div>';
            }
            if (msg.image_b64) {
                imgHtml = '<div style="margin-top:8px;margin-bottom:4px;"><img src="data:image/jpeg;base64,' + msg.image_b64 + '" style="width:100%;max-height:200px;object-fit:contain;border-radius:8px;background:#0a0a0a;"></div>';
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
                c.innerHTML = 
                    '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                        '<div style="font-size:3rem;margin-bottom:16px;">⏳</div>' +
                        '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Esperando imagen de la cámara</div>' +
                        '<div style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:24px;">Asegúrate de que la cámara esté conectada y enviando frames</div>' +
                        '<button class="btn" onclick="App._runAutoConfig(\'' + camId + '\')">Reintentar</button>' +
                        '<button class="btn btn-ghost" style="margin-top:8px" onclick="App.go(\'cameras\')">Volver</button>' +
                    '</div>';
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

    _showEvaConfig(config) {
        const c = document.getElementById('app-content');
        const imgSrc = config.image_b64 ? 'data:image/jpeg;base64,' + config.image_b64 : '';
        const rules = config.rules || config.rules_es || [];
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
                    '<div>' + rulesHtml + '</div>' +
                '</div>' +
                '<div style="flex-shrink:0;padding:12px 16px;border-top:1px solid var(--border);background:var(--bg);">' +
                    '<div style="display:flex;gap:10px;">' +
                        '<button class="btn" style="flex:1;background:var(--success);padding:13px;font-size:0.95rem;" onclick="App._evaSave(App._evaPendingRules)">✅ Listo, guardar</button>' +
                        '<button class="btn btn-outline" style="flex:1;padding:13px;font-size:0.95rem;" onclick="App._evaShowAdjust(App._evaPendingRules)">✏️ Ajustar</button>' +
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
                        '<div style="color:var(--text-secondary);">Ya está vigilando tu negocio</div>' +
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
                    `<label style="font-size:0.8rem;color:var(--text-secondary);display:block;margin-bottom:4px;">Regla ${i+1}</label>` +
                    `<input class="eva-rule-input" data-idx="${i}" value="${text.replace(/"/g, '&quot;')}" ` +
                    `style="width:100%;padding:10px 14px;border:1px solid var(--border);border-radius:8px;font-size:0.95rem;background:var(--bg-secondary);color:var(--text-primary);">` +
                `</div>`;
        });
        
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;height:100%;min-height:0;">' +
                '<div style="flex-shrink:0;padding:12px 16px;border-bottom:1px solid var(--border);">' +
                    '<div style="font-weight:600;font-size:1rem;">✏️ Ajusta las reglas</div>' +
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
                gc.innerHTML = `<div class="card" style="margin-top:12px"><div class="card-title">🔲 Grid - ${f}/16 <span class="badge ${f >= 16 ? 'badge-alert' : 'badge-ok'}">${f >= 16 ? 'LLENO' : f}</span></div>
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