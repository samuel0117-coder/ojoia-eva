// ========== OjoIA Unified App ==========
const firebaseConfig = {apiKey:"AIzaSyAtlS7rikClpJBVHM46gPvN4HL_CYyRxP0",authDomain:"ojoia-67216.firebaseapp.com",projectId:"ojoia-67216",storageBucket:"ojoia-67216.firebasestorage.app",messagingSenderId:"490868607747",appId:"1:490868607747:web:f722468d4f3493deb8f736",measurementId:"G-KX5V3B6547"};
try {
    if (!firebase.apps || firebase.apps.length === 0) {
        firebase.initializeApp(firebaseConfig);
    }
} catch(e) {
    console.warn('Firebase init error:', e);
}

// Headers para evitar problemas con ngrok y CORS
const API_HEADERS = { 'ngrok-skip-browser-warning': 'true' };

// Fetch helper con CORS explícito para Firebase Hosting -> ngrok
function apiFetch(url, opts = {}) {
    return fetch(url, {
        mode: 'cors',
        headers: { ...API_HEADERS, ...opts.headers },
        ...opts,
    });
}

const App = {
    userId: localStorage.getItem('ojoia_user_id') || null,
    page: 'home',
    poll: null,
    API: '',
    _unsubscribeFirestore: null,

    init() {
        this.loginMode = 'login';
        const h = window.location.hostname;
        const isLocal = !h.includes('firebaseapp.com') && !h.includes('web.app') && !h.includes('ojoia.com.do') && !h.includes('api.ojoia.com.do');
        const isHttps = window.location.protocol === 'https:';
        
        if (isLocal && !isHttps) {
            // Solo usar IP local si estamos en red local Y en HTTP
            this.API = 'http://10.0.0.44:8005';
            this._startAuth();
        } else {
            // Firebase: leer URL de Firestore con listener en tiempo real
            this._startFirestoreListener();
        }
    },

    _startFirestoreListener() {
        try {
            console.log('Iniciando Firestore listener...');
            if (!firebase.firestore) {
                console.error('firebase.firestore no está disponible');
                this._startAuth();
                return;
            }
            const db = firebase.firestore();
            console.log('Firestore DB:', db ? 'OK' : 'NULL');
            // Listener en tiempo real - se actualiza automáticamente si cambia la URL
            this._unsubscribeFirestore = db.collection('system').doc('server_status').onSnapshot(doc => {
                console.log('Firestore snapshot:', doc.exists);
                if (doc.exists) {
                    const data = doc.data();
                    const newUrl = data.ngrok_url || '';
                    const status = data.status || 'offline';
                    
                    if (newUrl && newUrl !== this.API) {
                        this.API = newUrl;
                        localStorage.setItem('ojoia_api_url', this.API);
                        console.log('API URL actualizada:', this.API, 'Status:', status);
                    } else if (!newUrl && status === 'offline') {
                        console.log('Servidor offline');
                    }
                }
                // Iniciar auth después del primer callback
                if (!this._authStarted) {
                    this._authStarted = true;
                    this._startAuth();
                }
            }, err => {
                console.error('Firestore listener error:', err);
                // Fallback: usar URL guardada
                const saved = localStorage.getItem('ojoia_api_url');
                this.API = saved || '';
                if (!this._authStarted) {
                    this._authStarted = true;
                    this._startAuth();
                }
            });
        } catch(e) {
            console.error('Firestore init error:', e);
            const saved = localStorage.getItem('ojoia_api_url');
            this.API = saved || '';
            this._startAuth();
        }
    },

    _startAuth() {
        // Verificar que Firebase esté inicializado
        if (!firebase.apps || firebase.apps.length === 0) {
            console.error('Firebase no está inicializado');
            document.getElementById('login-err').textContent = 'Error: Firebase no inicializado. Recarga la página.';
            return;
        }
        firebase.auth().onAuthStateChanged(u => {
            if (u) {
                if (!this.userId) {
                    this.verifyFB(u);
                }
                else this.showApp();
            } else {
                this.showLogin();
            }
        });
        document.getElementById('login-email').addEventListener('keypress', e => { if (e.key==='Enter') document.getElementById('login-pw').focus(); });
        document.getElementById('login-pw').addEventListener('keypress', e => { if (e.key==='Enter') this.loginEmail(); });
        document.getElementById('login-pw-confirm').addEventListener('keypress', e => { if (e.key==='Enter') this.loginEmail(); });
    },

    switchLoginMode(mode) {
        this.loginMode = mode;
        const isReg = mode==='register';
        document.getElementById('tab-switch-login').classList.toggle('active', !isReg);
        document.getElementById('tab-switch-register').classList.toggle('active', isReg);
        document.getElementById('name-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('business-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('businesstype-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('monitor-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('schedule-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('pw-confirm-group').style.display = isReg ? 'block' : 'none';
        document.getElementById('btn-login-action').textContent = isReg ? 'Crear mi cuenta' : 'Entrar';
        document.getElementById('login-hint').textContent = isReg ? 'Eva configurará tu primera cámara después del registro.' : '¿Olvidaste tu contraseña? Contacta al administrador.';
        document.getElementById('login-err').style.display = 'none';
        document.getElementById('login-success').style.display = 'none';
    },

    async loginEmail() {
        const email = document.getElementById('login-email').value.trim();
        const pw = document.getElementById('login-pw').value;
        const btn = document.getElementById('btn-login-action');
        
        if (!email) { this.err('login-err', 'Ingresa tu correo'); return; }
        if (!pw || pw.length < 6) { this.err('login-err', 'La contraseña debe tener al menos 6 caracteres'); return; }
        
        btn.disabled = true;
        btn.textContent = this.loginMode === 'register' ? 'Creando...' : 'Entrando...';
        
        try {
            if (this.loginMode === 'register') {
                const pw2 = document.getElementById('login-pw-confirm').value;
                if (pw !== pw2) { this.err('login-err', 'Las contraseñas no coinciden'); btn.disabled = false; btn.textContent = 'Crear mi cuenta'; return; }
                const name = document.getElementById('login-name').value.trim();
                const business = document.getElementById('login-business').value.trim();
                if (!name) { this.err('login-err', 'Ingresa tu nombre'); btn.disabled = false; btn.textContent = 'Crear mi cuenta'; return; }
                if (!business) { this.err('login-err', 'Ingresa el nombre de tu negocio'); btn.disabled = false; btn.textContent = 'Crear mi cuenta'; return; }
                const cred = await firebase.auth().createUserWithEmailAndPassword(email, pw);
                await this.verifyFB(cred.user, name, email, business);
            } else {
                const cred = await firebase.auth().signInWithEmailAndPassword(email, pw);
                await this.verifyFB(cred.user, '', email, '');
            }
        } catch(e) {
            console.error('Login error:', e.code, e.message);
            btn.disabled = false;
            btn.textContent = this.loginMode === 'register' ? 'Crear mi cuenta' : 'Entrar';
            
            if (e.code === 'auth/email-already-in-use' || e.code === 'email-already-in-use') {
                console.log('=== EMAIL YA EXISTE, INTENTANDO LOGIN AUTOMATICO ===');
                try {
                    const cred = await firebase.auth().signInWithEmailAndPassword(email, pw);
                    console.log('Login automático exitoso!');
                    await this.verifyFB(cred.user, '', email, '');
                    return;
                } catch(loginErr) {
                    console.error('Login automático falló:', loginErr.code);
                    this.err('login-err', 'Este usuario ya existe. La contraseña no coincide. Intenta iniciar sesión.');
                    return;
                }
            }
            
            this.err('login-err', this.fbErr(e));
        }
    },

    showLogin() {
        document.getElementById('login-screen').style.display='flex';
        document.getElementById('app-shell').style.display='none';
        this.switchLoginMode('login');
    },
    showApp() {
        document.getElementById('login-screen').style.display='none';
        document.getElementById('app-shell').style.display='flex';
        if (this.poll) clearInterval(this.poll);
        if (this._livePoll) clearInterval(this._livePoll);
        if (this._gridPoll) clearInterval(this._gridPoll);
        this._initFCM();
        this.navigate('home');
    },

    // ========== FIREBASE AUTH ==========
    async verifyFB(user, name, email, business) {
        if (!this.API) {
            this.showLogin();
            return this.err('login-err', 'No se pudo conectar al servidor. Verifica tu conexión o intenta más tarde.');
        }
        try {
            const t = await user.getIdToken();
            const body = {
                id_token: t,
                email: email || user.email || '',
                name: name || '',
                business_name: business || '',
            };
            // Agregar campos nuevos del formulario de registro
            if (this.loginMode === 'register') {
                const bizType = document.getElementById('login-businesstype');
                const monitor = document.getElementById('login-monitor');
                const schedOpen = document.getElementById('login-schedule-open');
                const schedClose = document.getElementById('login-schedule-close');
                if (bizType) body.business_type = bizType.value || '';
                if (monitor) body.what_to_monitor = monitor.value.trim() || '';
                if (schedOpen) body.schedule_open = schedOpen.value || '07:00';
                if (schedClose) body.schedule_close = schedClose.value || '19:00';
            }
            const r = await apiFetch(this.API + '/auth/firebase/verify', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify(body)
            });
            const d = await r.json();
            if (d.success) {
                this.userId = d.user_id;
                localStorage.setItem('ojoia_user_id', this.userId);
                this.showApp();
            } else { this.err('login-err', d.error||'Error'); }
        } catch(e) { this.err('login-err','Error de conexión con el servidor.'); }
    },

    fbErr(e) {
        const code = e.code || '';
        const msgs = {
            'auth/invalid-email': 'Correo inválido',
            'invalid-email': 'Correo inválido',
            'auth/too-many-requests': 'Demasiados intentos. Espera 60 segundos.',
            'too-many-requests': 'Demasiados intentos. Espera 60 segundos.',
            'auth/network-request-failed': 'Error de red. Verifica tu conexión.',
            'network-request-failed': 'Error de red. Verifica tu conexión.',
            'auth/user-disabled': 'Cuenta deshabilitada',
            'user-disabled': 'Cuenta deshabilitada',
            'auth/user-not-found': 'No existe una cuenta con este correo',
            'user-not-found': 'No existe una cuenta con este correo',
            'auth/wrong-password': 'Contraseña incorrecta',
            'wrong-password': 'Contraseña incorrecta',
            'auth/email-already-in-use': 'Este usuario ya está registrado. Inicia sesión para entrar a tu cuenta.',
            'email-already-in-use': 'Este usuario ya está registrado. Inicia sesión para entrar a tu cuenta.',
            'auth/weak-password': 'Contraseña débil. Usa al menos 6 caracteres.',
            'weak-password': 'Contraseña débil. Usa al menos 6 caracteres.',
            'auth/operation-not-allowed': 'Habilita Email/Password en Firebase Console → Authentication → Sign-in method',
            'operation-not-allowed': 'Habilita Email/Password en Firebase Console → Authentication → Sign-in method',
        };
        console.error('Firebase error:', code, e.message);
        return msgs[code] || 'Error: ' + e.message || 'Error desconocido';
    },

    err(id,msg) { const e=document.getElementById(id); e.textContent=msg; e.style.display='block'; },

    // ========== NAVIGATION ==========
    navigate(p) {
        // Clean up home polls when leaving home
        if (this.page === 'home' && p !== 'home') {
            if (this._livePoll) { clearInterval(this._livePoll); this._livePoll = null; }
            if (this._gridPoll) { clearInterval(this._gridPoll); this._gridPoll = null; }
        }
        this.page = p;
        const c = document.getElementById('app-content');
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.page===p));
        switch(p) {
            case 'home': this.home(c); break;
            case 'cameras': this.cameras(c); break;
            case 'events': this.events(c); break;
            case 'settings': this.settings(c); break;
        }
    },

    // ========== FCM PUSH NOTIFICATIONS ==========
    async _initFCM() {
        try {
            if (!('serviceWorker' in navigator)) return;
            if (!('Notification' in window)) return;

            // Request notification permission
            const permission = await Notification.requestPermission();
            if (permission !== 'granted') {
                console.log('Notification permission denied');
                return;
            }

            // Register service worker
            const reg = await navigator.serviceWorker.register('/sw.js');
            console.log('SW registered:', reg.scope);

            // Initialize Firebase Messaging
            if (firebase.messaging) {
                const messaging = firebase.messaging();
                messaging.useServiceWorker(reg);

                // Get FCM token
                const token = await messaging.getToken();
                if (token) {
                    console.log('FCM token:', token.substring(0, 20) + '...');
                    await this._registerToken(token);
                }

                // Listen for token refresh
                messaging.onTokenRefresh(async () => {
                    const newToken = await messaging.getToken();
                    if (newToken) await this._registerToken(newToken);
                });

                // Handle foreground messages
                messaging.onMessage((payload) => {
                    console.log('FCM foreground message:', payload);
                    // Show in-app notification
                    this._showInAppNotification(payload);
                });
            }
        } catch(e) {
            console.log('FCM init skipped:', e.message);
        }
    },

    async _registerToken(token) {
        if (!this.userId || !this.API) return;
        try {
            await apiFetch(this.API + '/api/fcm/register', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: this.userId, fcm_token: token})
            });
        } catch(e) {}
    },

    _showInAppNotification(payload) {
        const title = payload.notification?.title || 'OjoIA';
        const body = payload.notification?.body || '';
        // Create a toast-like notification
        const toast = document.createElement('div');
        toast.style.cssText = 'position:fixed;top:16px;left:16px;right:16px;background:var(--bg-secondary);border:1px solid var(--danger);border-radius:12px;padding:16px;z-index:9999;animation:fadeIn 0.3s ease;';
        toast.innerHTML = '<div style="font-weight:600;margin-bottom:4px">'+title+'</div><div style="font-size:0.85rem;color:var(--text-secondary)">'+body+'</div>';
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 8000);
    },

    // ========== HOME ==========
    async home(c) {
        c.innerHTML = '<div class="loading">Cargando...</div>';
        try {
            const r = await apiFetch(this.API+'/api/cameras?user_id='+this.userId);
            const d = await r.json();
            const cams = d.cameras || [];
            const on = cams.filter(x=>x.active).length;
            const off = cams.length - on;
            const statusIcon = off===0 ? '✅ Todo tranquilo' : '⚠️ Atención';

            // Vigilance config
            let vigilancePrompt = 'Cargando...';
            let vigilanceRules = 'Cargando...';
            try {
                const vr = await apiFetch(this.API+'/api/vigilance/config?user_id='+this.userId);
                const vd = await vr.json();
                vigilancePrompt = vd.prompt || 'No configurado';
                vigilanceRules = (vd.rules && vd.rules.length) ? vd.rules.join('\n') : 'No configuradas';
            } catch(e) {}

            let camList = '';
            if (cams.length===0) {
                camList = '<div class="empty-state"><div class="empty-icon">📷</div><div class="empty-title">Sin cámaras</div><p>Configura tu primera cámara con Eva para empezar</p><button class="btn btn-sm" onclick="App.openEva()" style="margin-top:12px">Configurar con Eva</button></div>';
            } else {
                cams.forEach(cam => {
                    const dot = cam.active ? 'online' : 'offline';
                    const label = cam.active ? 'Online' : 'Offline';
                    camList += '<div class="camera-status-item" onclick="App.viewer(\''+cam.id+'\',\''+cam.name+'\')"><div class="status-dot '+dot+'"></div><span>'+cam.name+'</span><span style="color:var(--text-secondary);font-size:0.8rem;margin-left:auto">'+label+'</span></div>';
                });
            }

            c.innerHTML =
                '<div class="home-status">'+statusIcon+'</div>'+
                '<div class="home-subtitle">'+cams.length+' cámaras · '+on+' activas · '+off+' inactivas</div>'+
                '<div class="card"><div class="card-title">Cámaras</div>'+camList+'</div>'+
                '<div id="live-frame-container"></div>'+
                '<div class="card"><div class="card-title">Hoy</div>'+
                    '<div class="daily-summary">'+
                        '<div class="summary-item"><div class="summary-value" id="evt-count">0</div><div class="summary-label">Eventos</div></div>'+
                        '<div class="summary-item"><div class="summary-value" id="alert-count">0</div><div class="summary-label">Alertas</div></div>'+
                    '</div>'+
                '</div>'+
                '<div class="card"><div class="card-title">🎯 Prompt de Vigilancia</div>'+
                    '<textarea id="vigilance-prompt" readonly style="width:100%;min-height:80px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary);padding:12px;font-size:0.85rem;font-family:inherit;resize:none">'+vigilancePrompt+'</textarea>'+
                '</div>'+
                '<div class="card"><div class="card-title">📋 Reglas de Vigilancia</div>'+
                    '<textarea id="vigilance-rules" readonly style="width:100%;min-height:100px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary);padding:12px;font-size:0.85rem;font-family:inherit;resize:none">'+vigilanceRules+'</textarea>'+
                '</div>'+
                '<div id="home-grid-container"></div>';

            // Start live frame polling
            this._startLiveFramePolling();
            // Start grid polling
            this._startGridPolling();
            // Load events count
            this._loadEventsCount();

        } catch(e) { c.innerHTML='<div class="empty-state"><div class="empty-icon">📡</div><div class="empty-title">Sin conexión</div><p>El servidor no está disponible</p><button class="btn btn-sm" onclick="App.navigate(\'home\')">Reintentar</button></div>'; }
    },

    // ========== LIVE FRAME POLLING ==========
    _startLiveFramePolling() {
        if (this._livePoll) clearInterval(this._livePoll);
        this._livePoll = setInterval(() => this._pollLiveFrame(), 2000);
        this._pollLiveFrame(); // immediate first load
    },

    async _pollLiveFrame() {
        if (this.page !== 'home') {
            if (this._livePoll) { clearInterval(this._livePoll); this._livePoll = null; }
            return;
        }
        try {
            const r = await apiFetch(this.API+'/frames/latest');
            const d = await r.json();
            const container = document.getElementById('live-frame-container');
            if (!container) return;

            if (d.success && d.image_b64) {
                const yoloText = (d.yolo && d.yolo.count!=null) ? d.yolo.count+' pers.' : '--';
                const scannerText = (d.scanner && d.scanner.suspicious) ? '⚠️ Sospechoso' : '✅ Normal';
                const qwenText = (d.qwen && d.qwen.violation) ? '🚨 Violación' : '✅ OK';
                const meta = d.metadata || {};
                const timeStr = meta.timestamp ? new Date(meta.timestamp * 1000).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';

                container.innerHTML =
                    '<div class="card">'+
                        '<div class="card-title">📷 Última imagen en vivo '+(timeStr ? '<span style="font-size:0.75rem;color:var(--text-secondary);font-weight:normal">'+timeStr+'</span>' : '')+'</div>'+
                        '<img id="live-frame-img" src="data:image/jpeg;base64,'+d.image_b64+'" style="width:100%;border-radius:8px;display:block">'+
                        '<div class="analysis-grid">'+
                            '<div class="analysis-card"><h4>👁️ YOLO</h4><div class="analysis-value">'+yoloText+'</div></div>'+
                            '<div class="analysis-card"><h4>🔍 Scanner</h4><div class="analysis-value">'+scannerText+'</div></div>'+
                            '<div class="analysis-card"><h4>🧠 Qwen</h4><div class="analysis-value">'+qwenText+'</div></div>'+
                        '</div>'+
                    '</div>';
            }
        } catch(e) {}
    },

    // ========== GRID POLLING (16 frames) ==========
    _startGridPolling() {
        if (this._gridPoll) clearInterval(this._gridPoll);
        this._gridPoll = setInterval(() => this._pollGrid(), 3000);
        this._pollGrid(); // immediate first load
    },

    async _pollGrid() {
        if (this.page !== 'home') {
            if (this._gridPoll) { clearInterval(this._gridPoll); this._gridPoll = null; }
            return;
        }
        try {
            const r = await apiFetch(this.API+'/grid/latest?partial=1');
            const d = await r.json();
            const container = document.getElementById('home-grid-container');
            if (!container) return;

            if (d.grid_b64) {
                const fullBadge = d.frames_used >= 16
                    ? '<span class="badge badge-err" style="margin-left:8px">GRID LLENO (16/16)</span>'
                    : '<span class="badge badge-ok" style="margin-left:8px">'+d.frames_used+'/16</span>';
                const partialLabel = d.partial ? ' (parcial)' : '';

                container.innerHTML =
                    '<div class="card">'+
                        '<div class="card-title">🔲 Grid de detección '+fullBadge+'</div>'+
                        '<img src="data:image/jpeg;base64,'+d.grid_b64+'" style="width:100%;border-radius:8px;display:block">'+
                        '<div style="margin-top:8px;color:var(--text-secondary);font-size:0.75rem">'+
                            d.frames_used+' frames acumulados'+partialLabel+
                        '</div>'+
                    '</div>';
            } else {
                container.innerHTML =
                    '<div class="card">'+
                        '<div class="card-title">🔲 Grid de detección</div>'+
                        '<p style="color:var(--text-secondary);font-size:0.85rem">Esperando detecciones... El grid se llena con 16 frames cuando YOLO detecta objetos.</p>'+
                    '</div>';
            }
        } catch(e) {}
    },

    // ========== EVENTS COUNT ==========
    async _loadEventsCount() {
        try {
            const r = await apiFetch(this.API+'/api/vigilance/events?user_id='+this.userId);
            const d = await r.json();
            const evts = d.events || [];
            const evtCount = document.getElementById('evt-count');
            const alertCount = document.getElementById('alert-count');
            if (evtCount) evtCount.textContent = evts.length;
            if (alertCount) alertCount.textContent = evts.filter(e => e.qwen && e.qwen.violation).length;
        } catch(e) {}
    },

    // ========== CAMERAS ==========
    async cameras(c) {
        c.innerHTML = '<div class="loading">Cargando cámaras...</div>';
        try {
            const r = await apiFetch(this.API+'/api/cameras?user_id='+this.userId);
            const d = await r.json();
            const cams = d.cameras || [];
            if (cams.length===0) {
                c.innerHTML = '<div class="empty-state"><div class="empty-icon">📷</div><div class="empty-title">Sin cámaras</div><p>Configura tu primera cámara con Eva</p><button class="btn btn-sm" onclick="App.openEva()" style="margin-top:12px">Configurar con Eva</button></div>';
            } else {
                let html = '';
                cams.forEach(cam => {
                    const dot = cam.active ? 'online' : 'offline';
                    html += '<div class="camera-card"><div style="cursor:pointer;flex:1;display:flex;align-items:center;gap:12px" onclick="App.viewer(\''+cam.id+'\',\''+cam.name+'\')"><span>📷</span><div style="flex:1"><div style="font-weight:500">'+cam.name+'</div><div style="color:var(--text-secondary);font-size:0.8rem">'+(cam.zone||'')+'</div></div><div class="status-dot '+dot+'"></div></div><button class="btn btn-sm btn-outline" onclick="App.openEva(\''+cam.id+'\')" style="margin-left:8px">✏️</button></div>';
                });
                html += '<button class="btn" style="margin-top:16px" onclick="App.openEva()">+ Nueva cámara</button>';
                c.innerHTML = html;
            }
        } catch(e) { c.innerHTML='<div class="empty-state"><div class="empty-icon">📡</div><p>Error de conexión</p></div>'; }
    },

    // ========== EVENTS ==========
    async events(c) {
        c.innerHTML = 
            '<div class="filters">'+
                '<button class="filter-btn active" onclick="setFilter(this)">Hoy</button>'+
                '<button class="filter-btn" onclick="setFilter(this)">Importantes</button>'+
                '<button class="filter-btn" onclick="setFilter(this)">Todos</button>'+
            '</div>'+
            '<div class="loading">Cargando eventos...</div>';
        try {
            const r = await apiFetch(this.API + '/api/user/events?user_id=' + this.userId);
            const d = await r.json();
            const evts = d.events || [];
            if (!evts.length) {
                c.innerHTML = 
                    '<div class="filters">'+
                        '<button class="filter-btn active" onclick="setFilter(this)">Hoy</button>'+
                        '<button class="filter-btn" onclick="setFilter(this)">Importantes</button>'+
                        '<button class="filter-btn" onclick="setFilter(this)">Todos</button>'+
                    '</div>'+
                    '<div class="empty-state">'+
                        '<div class="empty-icon">👁</div>'+
                        '<div class="empty-title">Todo tranquilo</div>'+
                        '<p>Los eventos aparecerán aquí cuando se detecte actividad</p>'+
                    '</div>';
            } else {
                let html = '<div class="filters">'+
                    '<button class="filter-btn active" onclick="setFilter(this)">Hoy</button>'+
                    '<button class="filter-btn" onclick="setFilter(this)">Importantes</button>'+
                    '<button class="filter-btn" onclick="setFilter(this)">Todos</button>'+
                '</div>';
                evts.forEach(evt => {
                    const yoloCount = (evt.yolo && evt.yolo.count) ? evt.yolo.count : 0;
                    const suspicious = evt.scanner && evt.scanner.suspicious;
                    const violation = evt.qwen && evt.qwen.violation;
                    const badgeClass = violation ? 'alert' : (suspicious ? 'warn' : 'ok');
                    const badgeText = violation ? '🚨 Alerta' : (suspicious ? '⚠️ Sospechoso' : '✅ Normal');
                    const time = evt.timestamp ? new Date(evt.timestamp * 1000).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit'}) : '--:--';
                    html += '<div class="event-item">'+
                        '<div class="event-thumb">'+(yoloCount > 0 ? '👤' : '📷')+'</div>'+
                        '<div class="event-info">'+
                            '<div class="event-title">'+(evt.camera_id||'Cámara')+' — '+time+'</div>'+
                            '<div class="meta">YOLO: '+yoloCount+' personas</div>'+
                            '<span class="event-badge '+badgeClass+'">'+badgeText+'</span>'+
                        '</div></div>';
                });
                c.innerHTML = html;
            }
        } catch(e) {
            c.innerHTML = 
                '<div class="filters">'+
                    '<button class="filter-btn active" onclick="setFilter(this)">Hoy</button>'+
                    '<button class="filter-btn" onclick="setFilter(this)">Importantes</button>'+
                    '<button class="filter-btn" onclick="setFilter(this)">Todos</button>'+
                '</div>'+
                '<div class="empty-state"><div class="empty-icon">📡</div><p>Error de conexión</p></div>';
        }
    },

    // ========== SETTINGS ==========
    async settings(c) {
        const apiUrl = localStorage.getItem('ojoia_api_url') || '';
        // Cargar perfil del usuario
        let profile = null;
        try {
            const r = await apiFetch(this.API + '/api/user/profile?user_id=' + this.userId);
            profile = await r.json();
        } catch(e) {}
        const plan = (profile && profile.plan) ? profile.plan.charAt(0).toUpperCase() + profile.plan.slice(1) : 'Fundador';
        const planStatus = (profile && profile.status === 'active') ? 'Activo' : 'Inactivo';
        const planColor = (profile && profile.status === 'active') ? 'var(--success)' : 'var(--danger)';
        const bizName = (profile && profile.business_name) ? profile.business_name : '';
        const email = (profile && profile.email) ? profile.email : '';
        c.innerHTML = 
            '<div class="settings-section">'+
                '<div class="settings-section-title">Vigilancia</div>'+
                '<div class="settings-item" onclick="App.openEva()">'+
                    '<span>🗣️</span><span style="flex:1">Configurar con Eva</span><span>›</span>'+
                '</div>'+
            '</div>'+
            '<div class="settings-section">'+
                '<div class="settings-section-title">Cuenta</div>'+
                (bizName ? '<div class="settings-item"><span>🏢</span><span style="flex:1">'+bizName+'</span></div>' : '')+
                (email ? '<div class="settings-item"><span>📧</span><span style="flex:1;font-size:0.85rem">'+email+'</span></div>' : '')+
                '<div class="settings-item"><span>💳</span><span style="flex:1">Plan '+plan+'</span><span style="color:'+planColor+'">'+planStatus+'</span></div>'+
            '</div>'+
            '<div class="settings-section">'+
                '<div class="settings-section-title">Conexión</div>'+
                '<div class="settings-item" onclick="apiPrompt()">'+
                    '<span>🌐</span><span style="flex:1">URL del servidor</span><span>›</span>'+
                '</div>'+
                '<div class="info-box" style="font-size:0.8rem;margin-top:4px">'+(apiUrl ? '✅ '+apiUrl : '⚠️ Sin configurar')+'</div>'+
            '</div>'+
            '<div class="settings-section">'+
                '<div class="settings-item" onclick="App.logout()"><span>🚪</span><span style="flex:1;color:var(--danger)">Cerrar sesión</span></div>'+
            '</div>';
    },

    // ========== EVA CHAT ==========
    evaProfile: null,
    async openEva(camId) {
        const c = document.getElementById('app-content');
        this._evaSession = 'eva_' + this.userId + '_' + Date.now();
        this._evaCamId = camId || '';
        c.innerHTML = 
            '<div style="display:flex;flex-direction:column;height:100%;padding:16px;">'+
                '<div class="eva-header" style="flex-shrink:0;">'+
                    '<div class="eva-avatar">🤖</div>'+
                    '<div class="eva-info"><div class="eva-name">Eva</div><div class="eva-status">Tu asistente de seguridad</div></div>'+
                '</div>'+
                '<div class="chat-container" id="eva-chat"></div>'+
                '<div style="flex-shrink:0;">'+
                    '<div class="chat-input-row"><input id="eva-input" placeholder="Escribe aquí..."><button class="btn-send" id="eva-send" onclick="App.sendEva()">Enviar</button></div>'+
                    (this._evaCamId ? '<button class="btn btn-sm btn-outline" style="margin-top:8px" onclick="App.evaViewCamera()">📷 Ver cámara</button>' : '')+
                    '<button class="btn" id="eva-confirm" style="display:none;background:var(--success);margin-top:8px" onclick="App.confirmEva()">✅ Confirmar configuración</button>'+
                '</div>'+
            '</div>';
        document.getElementById('eva-input').addEventListener('keypress', e => { if (e.key==='Enter') this.sendEva(); });
        
        this.evaReady = false;
        this.evaProfile = null;
        
        // Cargar perfil del usuario para contexto de Eva
        try {
            const r = await apiFetch(this.API + '/api/user/profile?user_id=' + this.userId);
            const d = await r.json();
            if (!d.error) this.evaProfile = d;
        } catch(e) {}
        
        // El saludo lo genera el backend basado en el perfil
        // Si hay cam_id, incluir el frame de la cámara para que Eva la vea
        try {
            const r = await apiFetch(this.API + '/config/chat', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    message: 'hola', 
                    user_id: this.userId, 
                    session_id: this._evaSession,
                    cam_id: this._evaCamId,
                    include_frame: !!this._evaCamId  // Pedir frame si hay cam_id
                })
            });
            const d = await r.json();
            if (d.success) {
                this.addEvaMsg(d.response, d.image_url);  // Mostrar texto + imagen
                if (d.ready_to_confirm) {
                    this.evaReady = true;
                    document.getElementById('eva-confirm').style.display='block';
                }
            }
        } catch(e) {
            this.addEvaMsg("¡Hola! Soy Eva, tu asistente de seguridad. ¿Qué tipo de negocio tienes y cómo se llama?");
        }
    },

    async evaViewCamera() {
        if (!this._evaCamId) return;
        this.addUserMsg("📷 Ver cámara");
        try {
            const r = await apiFetch(this.API+'/config/chat', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({
                    message: "Mira la cámara y dime qué ves. ¿Está bien posicionada? ¿Qué área cubre?",
                    user_id: this.userId,
                    session_id: this._evaSession,
                    cam_id: this._evaCamId,
                    include_frame: true
                })
            });
            const d = await r.json();
            if (d.success) {
                this.addEvaMsg(d.response, d.image_url);
            } else {
                this.addEvaMsg("No pude ver la cámara. Asegúrate de que esté conectada y enviando frames.");
            }
        } catch(e) { this.addEvaMsg('Error de conexión.'); }
    },
    async sendEva() {
        const input = document.getElementById('eva-input');
        const msg = input.value.trim();
        if (!msg || this.evaReady) return;
        input.value = '';
        this.addUserMsg(msg);
        
        // Detectar si el usuario pide ver la cámara
        const msgLower = msg.toLowerCase();
        const pideCamara = ['mira', 'ves', 'cámara', 'camara', 'imagen', 'frame', 'ver', 'analiza', 'revisa', 'observa', 'qué hay', 'que hay'].some(x => msgLower.includes(x));
        const includeFrame = pideCamara;  // Siempre incluir frame si pide ver la cámara
        
        try {
            const r = await apiFetch(this.API+'/config/chat', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({
                    message: msg, 
                    user_id: this.userId, 
                    session_id: this._evaSession,
                    cam_id: this._evaCamId || '',  // Enviar vacío si no hay cam_id
                    include_frame: includeFrame
                })
            });
            const d = await r.json();
            if (d.success) {
                this.addEvaMsg(d.response, d.image_url);
                if (d.ready_to_confirm && !this.evaReady) {
                    this.evaReady = true;
                    document.getElementById('eva-confirm').style.display='block';
                }
                if (d.camera_saved) {
                    this.addEvaMsg("🎉 ¡Cámara configurada y vigilando!");
                    setTimeout(() => this.navigate('cameras'), 2000);
                }
            }
        } catch(e) { this.addEvaMsg('Error de conexión. Intenta de nuevo.'); }
    },

    async confirmEva() {
        try {
            const r = await apiFetch(this.API+'/config/confirm', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({user_id:this.userId, session_id:this._evaSession})
            });
            const d = await r.json();
            if (d.success) {
                this.addEvaMsg('✅ ¡Configuración guardada! Tu cámara está lista.');
                setTimeout(() => this.navigate('cameras'), 1500);
            }
        } catch(e) { this.addEvaMsg('Error al guardar.'); }
    },

    addEvaMsg(t, imgUrl) { this.addMsg('eva', t, imgUrl); },
    addUserMsg(t) { this.addMsg('user', t); },
    addMsg(role, t, imgUrl) {
        const c = document.getElementById('eva-chat');
        if (!c) return;
        const d = document.createElement('div');
        d.className = 'msg '+role;
        let html = '<div class="msg-bubble">';
        if (imgUrl) {
            html += '<img src="'+imgUrl+'" style="max-width:200px;border-radius:8px;display:block;margin-bottom:8px;cursor:pointer" onclick="this.style.maxWidth=this.style.maxWidth===\'200px\'?\'100%\':\'200px\'" title="Clic para agrandar">';
        }
        html += t + '</div>';
        d.innerHTML = html;
        c.appendChild(d);
        // Scroll suave hasta el final
        setTimeout(() => { c.scrollTop = c.scrollHeight; }, 50);
        setTimeout(() => { c.scrollTop = c.scrollHeight; }, 200);
    },

    // ========== VIEWER ==========
    viewer(id, name) {
        document.getElementById('viewer-title').textContent = name;
        document.getElementById('viewer-body').innerHTML = '<div class="camera-frame"><div class="camera-placeholder">Cargando...</div></div>';
        document.getElementById('viewer-modal').style.display = 'flex';
        this._viewerCamId = id;
        this.loadFrame();
        this.loadGrid();
        if (this.poll) clearInterval(this.poll);
        this.poll = setInterval(() => { this.loadFrame(); this.loadGrid(); }, 3000);
    },

    async loadFrame() {
        try {
            const r = await apiFetch(this.API+'/frames/latest');
            const d = await r.json();
            if (d.success && d.image_b64) {
                const yoloText = (d.yolo && d.yolo.count!=null) ? d.yolo.count+' pers.' : '--';
                const scannerText = (d.scanner && d.scanner.suspicious) ? '⚠️ Sospechoso' : '✅ Normal';
                const qwenText = (d.qwen && d.qwen.violation) ? '🚨 Violación' : '✅ OK';
                const meta = d.metadata || {};
                const timeStr = meta.timestamp ? new Date(meta.timestamp * 1000).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
                document.getElementById('viewer-body').innerHTML =
                    '<img src="data:image/jpeg;base64,'+d.image_b64+'" style="width:100%;border-radius:8px;display:block">'+
                    (timeStr ? '<div style="color:var(--text-secondary);font-size:0.75rem;margin-top:4px">📷 '+timeStr+'</div>' : '')+
                    '<div class="analysis-grid">'+
                        '<div class="analysis-card"><h4>👁️ YOLO</h4><div class="analysis-value">'+yoloText+'</div></div>'+
                        '<div class="analysis-card"><h4>🔍 Scanner</h4><div class="analysis-value">'+scannerText+'</div></div>'+
                        '<div class="analysis-card"><h4>🧠 Qwen</h4><div class="analysis-value">'+qwenText+'</div></div>'+
                    '</div>'+
                    '<div id="grid-container" style="margin-top:12px"></div>';
            }
        } catch(e) {}
    },

    async loadGrid() {
        if (!this._viewerCamId) return;
        try {
            const r = await apiFetch(this.API+'/api/cameras/'+this._viewerCamId+'/grid');
            const d = await r.json();
            const container = document.getElementById('grid-container');
            if (!container) return;
            if (d.active && d.grid_b64) {
                const fullBadge = d.full ? '<span class="badge badge-err" style="margin-left:8px">GRID LLENO</span>' : '<span class="badge badge-ok" style="margin-left:8px">'+d.frames+'/16</span>';
                container.innerHTML = 
                    '<div class="card"><div class="card-title">🔲 Grid de sesión '+fullBadge+'</div>'+
                    '<img src="data:image/jpeg;base64,'+d.grid_b64+'" style="width:100%;border-radius:8px;display:block">'+
                    '</div>';
            } else {
                container.innerHTML = '<div class="card"><div class="card-title">🔲 Grid de sesión</div><p style="color:var(--text-secondary);font-size:0.85rem">Esperando detecciones...</p></div>';
            }
        } catch(e) {}
    },

    closeViewer() {
        document.getElementById('viewer-modal').style.display = 'none';
        this._viewerCamId = null;
        if (this.poll) { clearInterval(this.poll); this.poll = null; }
    },

    // ========== UTILS ==========
    setApiUrl(url) {
        if (url) localStorage.setItem('ojoia_api_url', url);
        else localStorage.removeItem('ojoia_api_url');
        this.API = (localStorage.getItem('ojoia_api_url') || '');
    },

    logout() {
        if (this._unsubscribeFirestore) {
            this._unsubscribeFirestore();
            this._unsubscribeFirestore = null;
        }
        localStorage.removeItem('ojoia_user_id');
        if (this.poll) clearInterval(this.poll);
        firebase.auth().signOut();
        this.showLogin();
    }
};

function setFilter(btn) {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
}

function apiPrompt() {
    const cur = localStorage.getItem('ojoia_api_url') || '';
    const url = prompt('URL del servidor (ej: https://xxxxx.ngrok-free.app):', cur);
    if (url !== null) { App.setApiUrl(url.trim() || null); App.settings(document.getElementById('app-content')); }
}

function showStep(n) { alert('Función en desarrollo. Usa el enlace mágico por ahora.'); }

document.addEventListener('DOMContentLoaded', () => App.init());
