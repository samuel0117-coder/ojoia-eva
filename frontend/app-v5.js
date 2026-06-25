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
    _unsubFS: null,
    _authStarted: false,
    _loginMode: 'login',
    _evaSession: null,
    _evaCamId: null,
    _evaReady: false,
    _viewerCamId: null,

    init() {
        const h = window.location.hostname;
        if (h === '10.0.0.44' || h === 'localhost' || h === '') {
            this.API = 'http://10.0.0.44:8005';
            this._startAuth();
            return;
        }
        const db = firebase.firestore();
        this._unsubFS = db.collection('system').doc('server_status').onSnapshot(doc => {
            if (doc.exists) {
                const data = doc.data();
                const url = data.backend || data.ngrok_url || this.API;
                if (url) this.API = url;
            }
            if (!this._authStarted) { this._authStarted = true; this._startAuth(); }
        }, () => {
            if (!this._authStarted) { this._authStarted = true; this._startAuth(); }
        });
    },

    _startAuth() {
        firebase.auth().onAuthStateChanged(u => {
            if (u) this._verifyFB(u);
            else this._showLogin();
        });
        ['login-email','login-pw','login-pw2'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('keypress', e => { if (e.key === 'Enter') this.doLogin(); });
        });
        // Update concerns textarea when clicking concern items
        document.querySelectorAll('.concern-item').forEach(el => {
            el.addEventListener('click', () => {
                const txt = document.getElementById('reg-concerns');
                if (!txt) return;
                const val = el.dataset.value;
                let current = txt.value.split(',').map(s => s.trim()).filter(s => s);
                if (current.includes(val)) {
                    current = current.filter(s => s !== val);
                } else {
                    current.push(val);
                }
                txt.value = current.join(', ');
            });
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
        document.getElementById('pw2-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('btn-auth').textContent = isReg ? 'Crear cuenta' : 'Entrar';
        document.getElementById('auth-hint').textContent = isReg
            ? 'Eva configurará tu primera ojo después.' : '¿Olvidaste tu contraseña? Contacta al administrador.';
        this._clearErr();
    },

    async doLogin() {
        const email = document.getElementById('login-email').value.trim();
        const pw = document.getElementById('login-pw').value;
        if (!email || !pw) { this._err('Completa todos los campos'); return; }
        const btn = document.getElementById('btn-auth');
        btn.disabled = true; btn.textContent = '...';
        try {
            if (this._loginMode === 'register') {
                const name = document.getElementById('reg-name').value.trim();
                const biz = document.getElementById('reg-business').value.trim();
                const pw2 = document.getElementById('login-pw2').value;
                if (!name || !biz) { this._err('Completa tu nombre y negocio'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
                if (pw !== pw2) { this._err('Las contraseñas no coinciden'); btn.disabled = false; btn.textContent = 'Crear cuenta'; return; }
                const bizType = document.getElementById('reg-biztype').value;
                const phone = document.getElementById('reg-phone')?.value.trim();
                const concerns = document.getElementById('reg-concerns')?.value.trim() || '';
                                const camCount = document.querySelector('input[name="cam_count"]:checked')?.value || '1';
                const cred = await firebase.auth().createUserWithEmailAndPassword(email, pw);
                await this._verifyFB(cred.user, { 
                    name, business: biz, email,
                    business_type: bizType, phone, main_concerns: concerns, 
                    employee_count: document.querySelector('input[name="emp_count"]:checked')?.value || '1', camera_expected_count: camCount,
                    schedule_open: (() => {
                        const h = document.getElementById('reg-open-hour')?.value || '8';
                        const ap = document.getElementById('reg-open-ampm')?.value || 'AM';
                        const hr = parseInt(h);
                        const hour24 = ap === 'AM' ? (hr === 12 ? 0 : hr) : (hr === 12 ? 12 : hr + 12);
                        return String(hour24).padStart(2, '0') + ':00';
                    })(),
                    schedule_close: (() => {
                        const h = document.getElementById('reg-close-hour')?.value || '10';
                        const ap = document.getElementById('reg-close-ampm')?.value || 'PM';
                        const hr = parseInt(h);
                        const hour24 = ap === 'PM' ? (hr === 12 ? 12 : hr + 12) : hr;
                        return String(hour24).padStart(2, '0') + ':00';
                    })()
                });
            } else {
                const cred = await firebase.auth().signInWithEmailAndPassword(email, pw);
                await this._verifyFB(cred.user);
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = this._loginMode === 'register' ? 'Crear cuenta' : 'Entrar';
            this._err(this._fbErr(e));
        }
    },

    async _verifyFB(user, extra = {}) {
        try {
            const token = await user.getIdToken();
            const body = { id_token: token, email: user.email, ...extra };
            if (this._loginMode === 'register') {
                const bt = document.getElementById('reg-biztype');
                const mon = document.getElementById('reg-monitor');
                const op = document.getElementById('reg-open');
                const cl = document.getElementById('reg-close');
                if (bt) body.business_type = bt.value;
                if (mon) body.what_to_monitor = mon.value.trim();
                if (op) body.schedule_open = op.value || '07:00';
                if (cl) body.schedule_close = cl.value || '19:00';
            }
            const r = await apiFetch(this.API + '/auth/firebase/verify', { method: 'POST', body: JSON.stringify(body) });
            const d = await r.json();
            if (d.success) {
                this.userId = d.user_id;
                localStorage.setItem('ojoia_uid', this.userId);
                this._showApp();
            } else this._err(d.error || 'Error de autenticación');
        } catch(e) { this._err('Error conectando al servidor'); }
    },

    _showApp() {
        document.getElementById('screen-login').style.display = 'none';
        document.getElementById('screen-app').style.display = 'flex';
        this._initPush();
        this.go('home');
    },

    logout() {
        this._clearAllPolls();
        if (this._unsubFS) { this._unsubFS(); this._unsubFS = null; }
        localStorage.removeItem('ojoia_uid');
        this.userId = null;
        firebase.auth().signOut();
        this._showLogin();
    },

    _fbErr(e) {
        const m = {
            'auth/user-not-found': 'Correo no registrado',
            'auth/wrong-password': 'Contraseña incorrecta',
            'auth/email-already-in-use': 'Este correo ya está registrado',
            'auth/weak-password': 'Contraseña muy corta (mín. 6)',
            'auth/invalid-email': 'Correo inválido',
            'auth/too-many-requests': 'Demasiados intentos. Espera.',
        };
        return m[e.code] || e.message || 'Error desconocido';
    },

    _err(msg) { const el = document.getElementById('login-err'); el.textContent = msg; el.style.display = 'block'; },
    _clearErr() { const el = document.getElementById('login-err'); if (el) { el.textContent = ''; el.style.display = 'none'; } },

    go(page) {
        if (this.page !== page) this._clearAllPolls();
        this.page = page;
        const c = document.getElementById('app-content');
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.page === page));
        ({ home: () => this._pageHome(c), cameras: () => this._pageCameras(c), events: () => this._pageEvents(c), settings: () => this._pageSettings(c) })[page]?.();
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

            c.innerHTML = `
                <div class="home-hero">
                    <div class="hero-status ${heroClass}">${heroText}</div>
                </div>
                ${lastAlertHTML}

                <!-- LIVE CAMERA VIEW -->
                <div class="card" id="card-live-view">
                    <div class="card-title">
                        📷 En vivo — <span id="home-live-cam-name">${defaultCam ? defaultCam.name : 'Sin cámara'}</span>
                        <span id="home-live-status" class="badge ${defaultCam?.active ? 'badge-ok' : 'badge-alert'}" style="margin-left:8px">${defaultCam?.active ? '● En vivo' : '○ Offline'}</span>
                    </div>
                    <div id="live-wrap"><div class="ojo-placeholder">Esperando imagen...</div></div>
                </div>

                <!-- CAM SELECTOR -->
                <div class="card">
                    <div class="card-title">🎯 Cámaras <span class="badge badge-${on > 0 ? 'ok' : 'off'}">${on}/${cams.length}</span></div>
                    ${camCardsHTML ? camCardsHTML : '<p class="meta">Sin cámaras configuradas</p>'}
                </div>

                <!-- STATS -->
                <div class="card">
                    <div class="card-title">📊 Hoy</div>
                    <div class="stats-row">
                        <div class="stat"><div class="stat-val" id="stat-events">—</div><div class="stat-lbl">Eventos</div></div>
                        <div class="stat"><div class="stat-val danger" id="stat-alerts">—</div><div class="stat-lbl">Alertas</div></div>
                        <div class="stat"><div class="stat-val ok" id="stat-cams">${on}</div><div class="stat-lbl">Activas</div></div>
                    </div>
                </div>

                <!-- GRID -->
                <div class="card" id="card-grid">
                    <div class="card-title">🔲 Grid de detección <span id="grid-badge" class="badge badge-ok" style="margin-left:8px">0/16</span></div>
                    <div class="prog-bar"><div class="prog-fill" id="grid-progress" style="width:0%"></div></div>
                    <div id="grid-wrap"><p class="meta" style="padding:8px 0">YOLO detecta objetos → Qwen analiza el grid</p></div>
                </div>

                <!-- PROMPT + RULES COMBINED -->
                <div class="card" id="card-vigilance" style="display:none">
                    <div class="card-title">🛡️ Vigilancia — <span id="vigilance-cam-name"></span></div>
                    <div id="vigilance-section">
                        <div class="vigilance-block">
                            <div class="vigilance-block-title">🎯 Prompt</div>
                            <div id="vigilance-prompt-display" class="vigilance-block-content"></div>
                        </div>
                        <div class="vigilance-block">
                            <div class="vigilance-block-title">📋 Reglas <span id="vigilance-rules-count" class="badge" style="margin-left:4px"></span></div>
                            <div id="vigilance-rules-list" class="vigilance-block-content"></div>
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
                this._poll('home_frame', () => this._fetchFrame('live-wrap'), 5000);
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
            const r = await apiFetch(`${this.API}/frames/latest?camera_id=${camId}`);
            const d = await r.json();
            const el = document.getElementById('live-wrap');
            if (!el || !d.success || !d.image_b64) {
                if (el) el.innerHTML = '<div class="ojo-placeholder">Sin señal de cámara</div>';
                return;
            }
            const yolo = d.yolo?.count != null ? `${d.yolo.count} 👁` : '—';
            let ts = '';
            if (d.metadata?.timestamp) {
                const raw = d.metadata.timestamp;
                const dt = typeof raw === 'number' ? new Date(raw * 1000) : new Date(raw);
                ts = dt.toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
            }
            const timeEl = document.getElementById('frame-time');
            if (timeEl) timeEl.textContent = ts ? `· ${ts}` : '';
            el.innerHTML = `
                <img src="data:image/jpeg;base64,${d.image_b64}" style="width:100%;border-radius:8px;display:block">
                <div class="ai-row">
                    <div class="ai-card"><div class="ai-label">YOLO</div><div class="ai-val">${yolo}</div></div>
                    <div class="ai-card"><div class="ai-label">Hora</div><div class="ai-val" style="font-size:.8rem">${ts || '—'}</div></div>
                </div>`;
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
            const r = await apiFetch(`${this.API}/grid/latest?partial=1`);
            const d = await r.json();
            const el = document.getElementById(targetId);
            const badgeEl = document.getElementById('grid-badge');
            const progressEl = document.getElementById('grid-progress');
            if (!el) return;
            if (d.grid_b64) {
                const frames = d.frames_used || 0;
                const pct = Math.round((frames / 16) * 100);
                if (badgeEl) { badgeEl.textContent = `${frames}/16`; badgeEl.className = `badge ${frames >= 16 ? 'badge-alert' : 'badge-ok'}`; }
                if (progressEl) progressEl.style.width = `${pct}%`;
                el.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                        <span class="meta">${frames}/16 frames acumulados${d.partial ? ' (parcial)' : ''}</span>
                        <span class="badge ${frames >= 16 ? 'badge-alert' : 'badge-ok'}">${frames >= 16 ? 'GRID LLENO' : `${pct}%`}</span>
                    </div>
                    <img src="data:image/jpeg;base64,${d.grid_b64}" style="width:100%;border-radius:8px;display:block;margin-top:10px">`;
            } else {
                if (badgeEl) { badgeEl.textContent = '0/16'; badgeEl.className = 'badge badge-ok'; }
                if (progressEl) progressEl.style.width = '0%';
                el.innerHTML = '<p class="meta" style="padding:8px 0">YOLO detecta objetos → Qwen analiza el grid</p>';
            }
        } catch(e) {}
    },

    async _fetchStats() {
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&date=today`);
            const d = await r.json();
            const evts = d.events || [];
            const alerts = evts.filter(e => e.qwen?.violation).length;
            const se = document.getElementById('stat-events');
            const sa = document.getElementById('stat-alerts');
            if (se) se.textContent = evts.length;
            if (sa) sa.textContent = alerts;
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
                    <button class="btn" onclick="App.openEva()" style="margin-top:16px">+ Nueva ojo con Eva</button>
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
                            <button class="btn-ghost btn-sm" style="color:var(--danger)" onclick="App.deleteCamera('${cam.camera_id}','${cam.name}')">🗑️</button>
                        </div>
                    </div>`;
                });
                html += `<button class="btn" style="margin-top:8px" onclick="App.openEva()">+ Nueva ojo con Eva</button>`;
                c.innerHTML = html;
                cams.forEach(cam => cam.active && this._loadThumb(cam.camera_id));
            }
        } catch(e) {
            c.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div><p>Error de conexión</p></div>`;
        }
    },

    async _loadThumb(camId) {
        try {
            const r = await apiFetch(`${this.API}/frames/latest?camera_id=${camId}`);
            const d = await r.json();
            const el = document.getElementById(`thumb-${camId}`);
            if (el && d.success && d.image_b64) {
                el.innerHTML = `<img src="data:image/jpeg;base64,${d.image_b64}" style="width:100%;height:100%;object-fit:cover;border-radius:8px">`;
            }
        } catch(e) {}
    },

    // ── EVENTS ───────────────────────────────────────────────
    async _pageEvents(c) {
        c.innerHTML = `<div class="filters">
            <button class="filter-btn active" onclick="App._filterEvents(this,'today')">Hoy</button>
            <button class="filter-btn" onclick="App._filterEvents(this,'alerts')">Alertas</button>
            <button class="filter-btn" onclick="App._filterEvents(this,'all')">Todos</button>
        </div><div id="events-list">${this._skeleton()}</div>`;
        this._loadEvents('today');
    },

    _filterEvents(btn, filter) {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._loadEvents(filter);
    },

    async _loadEvents(filter) {
        const el = document.getElementById('events-list');
        if (!el) return;
        el.innerHTML = this._skeleton();
        try {
            const uid = this.userId || 'default';
            const r = await apiFetch(`${this.API}/api/user/events?user_id=${uid}&filter=${filter}`);
            const evts = (await r.json()).events || [];

            if (!evts.length) {
                el.innerHTML = `<div class="empty-state" style="padding:40px 0">
                    <div style="font-size:2.5rem;margin-bottom:12px">👁</div>
                    <div class="empty-title">Todo tranquilo</div>
                    <p>Los eventos aparecerán aquí cuando Qwen detecte actividad</p>
                </div>`;
                return;
            }

            el.innerHTML = evts.map(evt => {
                const violation = evt.qwen?.violation;
                const level = violation ? 'alert' : 'ok';
                const label = violation ? '🚨 Análisis' : '✅ Normal';
                const ts = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleString('es-ES', {hour:'2-digit',minute:'2-digit',month:'short',day:'numeric',hour12:true}) : '--';
                const desc = evt.qwen?.description || (violation ? 'Violación detectada' : 'Actividad detectada');
                const yoloCount = evt.yolo?.count || 0;
                return `<div class="event-row" onclick="App._openEvent('${evt.event_id}')">
                    <div class="event-thumb ${violation ? 'alert-bg' : ''}" id="evthumb-${evt.event_id}">
                        ${violation ? '🚨' : '📷'}
                    </div>
                    <div class="event-info">
                        <div class="event-title">${evt.camera_name || evt.camera_id || 'Ojo'}</div>
                        <div class="meta">${ts} · YOLO: ${yoloCount} obj.</div>
                        <div class="meta" style="margin-top:2px">${desc}</div>
                        <span class="badge badge-${level}" style="margin-top:4px">${label}</span>
                    </div>
                    <span class="chev">›</span>
                </div>`;
            }).join('');

            for (const evt of evts) {
                if (evt.frame_b64) {
                    const el2 = document.getElementById(`evthumb-${evt.event_id}`);
                    if (el2) el2.innerHTML = `<img src="data:image/jpeg;base64,${evt.frame_b64}" style="width:100%;height:100%;object-fit:cover;border-radius:8px">`;
                }
            }

            // Auto-open event from URL parameter
            const hash = window.location.hash;
            const eventMatch = hash.match(/[?&]event=([^&]+)/);
            if (eventMatch) {
                const eventId = eventMatch[1];
                const found = evts.find(e => e.event_id === eventId);
                if (found) {
                    setTimeout(() => this._openEvent(eventId), 300);
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
                p.textContent = desc;
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
        try { const r = await apiFetch(`${this.API}/api/user/profile?user_id=${this.userId}`); profile = await r.json(); } catch(e) {}
        const plan = profile.plan || 'Fundador';
        const active = profile.status === 'active';

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
                <div class="section-lbl">Sistema</div>
                <div class="settings-row" onclick="App._showApiConfig()">
                    <span class="s-icon">🌐</span><span style="flex:1">URL del servidor</span><span class="chev">›</span>
                </div>
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

    // ── EVA: Configuración simple (sin chat) ────────────────────
    
    async openEva(camId) {
        const c = document.getElementById('app-content');
        this._evaCamId = camId || '';
        this._evaMode = camId ? 'edit' : 'new';
        
        // Pantalla de carga
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                '<div style="font-size:3rem;margin-bottom:16px;">🤖</div>' +
                '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Eva está analizando tu cámara...</div>' +
                '<div style="color:var(--text-secondary);font-size:0.9rem;">Esto toma unos segundos</div>' +
                '<div class="skeleton" style="width:200px;height:200px;margin-top:24px;border-radius:12px;"></div>' +
            '</div>';
        
        // Llamar a auto-config
        try {
            const r = await apiFetch(`${this.API}/config/auto_config`, {
                method: 'POST',
                body: JSON.stringify({ user_id: this.userId, camera_id: this._evaCamId || '' })
            });
            const d = await r.json();
            
            if (!d.ready) {
                c.innerHTML = 
                    '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;padding:32px;text-align:center;">' +
                        '<div style="font-size:3rem;margin-bottom:16px;">⏳</div>' +
                        '<div style="font-size:1.1rem;font-weight:600;margin-bottom:8px;">Esperando imagen de la cámara</div>' +
                        '<div style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:24px;">Asegúrate de que la cámara esté conectada y enviando frames</div>' +
                        '<button class="btn" onclick="App.openEva(\'' + (camId || '') + '\')">Reintentar</button>' +
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
                    '<button class="btn" style="margin-top:16px" onclick="App.openEva(\'' + (camId || '') + '\')">Reintentar</button>' +
                    '<button class="btn btn-ghost" style="margin-top:8px" onclick="App.go(\'cameras\')">Volver</button>' +
                '</div>';
        }
    },
    
    _showEvaConfig(config) {
        const c = document.getElementById('app-content');
        const imgSrc = config.image_b64 ? 'data:image/jpeg;base64,' + config.image_b64 : '';
        const rules = config.rules || config.rules_es || [];
        const evaMsg = (config.eva_message || 'Configuración para tu cámara:').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        
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
                        '<button class="btn" style="flex:1;background:var(--success);padding:13px;font-size:0.95rem;" onclick="App._evaShowAdjust(' + JSON.stringify(rules) + ')">✅ Listo, guardar</button>' +
                        '<button class="btn btn-outline" style="flex:1;padding:13px;font-size:0.95rem;" onclick="App._evaShowAdjust(' + JSON.stringify(rules) + ')">✏️ Ajustar</button>' +
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
                    '<button class="btn" style="margin-top:16px" onclick="App._evaShowAdjust(' + JSON.stringify(rules) + ')">Reintentar</button>' +
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
        this._polls.viewer = setInterval(() => { this._fetchFrame('viewer-body'); this._fetchViewerGrid(); }, 5000);
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
    _relTime(ts) { const d = (Date.now() - new Date(ts).getTime()) / 1000; if (d < 60) return 'hace un momento'; if (d < 3600) return `hace ${Math.floor(d/60)} min`; if (d < 86400) return `hace ${Math.floor(d/3600)}h`; return `hace ${Math.floor(d/86400)} días`; }
};

document.addEventListener('DOMContentLoaded', () => App.init());