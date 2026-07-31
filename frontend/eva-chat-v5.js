/**
 * eva-chat-v5.js — Chat con Eva (Sistema Operativo + Setup)
 * 
 * FLUJO:
 * - Sin cámaras: Eva entra en modo setup (configuración determinista)
 * - Con cámaras: Eva entra en modo OS con sugerencias rápidas
 * 
 * Las sugerencias se muestran como botones clickeables arriba del input.
 * La conversación persiste al cambiar de tab.
 */

const EvaChat = {
    userId: null,
    userName: '',
    API: (() => {
        const h = window.location.hostname;
        if (h === '10.0.0.44' || h === 'localhost' || h === '') return 'http://localhost:8007';
        return 'https://api.ojoia.com.do';
    })(),
    sessionId: null,
    history: [],
    isLoading: false,
    _greeted: false,
    _suggestions: [],
    _sessionKey: '',
    _summary: '',
    _visibleLimit: 10,
    _loadingOlder: false,
    _hasRecentAlerts: false,
    _briefEvent: null,
    _frameTimer: null,

    // ── INIT ────────────────────────────────────────────────────
    async init(userId, userName) {
        // F-DUP: guard idempotente — si init() ya se llamó para el mismo user,
        // NO re-registrar listeners ni re-corres el flujo. Solo re-render para
        // re-encajar el contenedor en el DOM (que _pageEva pudo haber limpiado).
        // Esto previene duplicacion de storage listeners y "chats fantasma"
        // cuando el usuario tab-entraga el chat varias veces desde el bottom nav.
        if (this._initializedFor === userId) {
            this.render();
            return;
        }
        // Si se cambia de user o es primera vez, limpiar cualquier instalación previa
        if (this._initializedFor && this._initializedFor !== userId) {
            this.teardown();
        }
        this._initializedFor = userId;
        this.userId = userId;
        this.userName = userName || '';
        this._sessionKey = `eva_session_${userId}`;
        const savedSessionId = localStorage.getItem(this._sessionKey);
        this.sessionId = savedSessionId || `eva_${userId}_single`;
        localStorage.setItem(this._sessionKey, this.sessionId);
        this._historyKey = `eva_history_${userId}`;
        this._historyTsKey = `eva_history_ts_${userId}`;
        this._greeted = false;
        this._visibleLimit = 10;
        this._loadingOlder = false;
        this._hasRecentAlerts = false;
        _initKeyboardWatcher();
        // ── Sync cross-tab: otra pestaña escribió en localStorage → recargar ──
        // Comparamos por firma de contenido (ts o longitud+último msg), NO solo length.
        // Esto cubre 3 casos donde length es igual pero el contenido cambió:
        //   1) asistente regenera respuesta (mismo #msgs, texto distinto)
        //   2) _maybeSummarize reescribe summary sin tocar history
        //   3) brief diario inicial segundo-msg reescrito en otra pestaña
        // F-DUP: guardamos el handler para poder removerlo en teardown/init subsiguiente
        this._lastHistoryTs = localStorage.getItem(this._historyTsKey) || '';
        // Remover handler anterior si existe (defensa en profundidad)
        if (this._storageHandler) {
            try { window.removeEventListener('storage', this._storageHandler); } catch (e) {}
        }
        this._storageHandler = (e) => {
            if (e.key !== this._historyKey) return;
            try {
                const saved = JSON.parse(e.newValue || 'null');
                if (!saved || !Array.isArray(saved.history)) return;
                // Firma robusta: usa ts si existe, si no deriva length + último content
                const lastMsg = saved.history[saved.history.length - 1];
                const lastLen = lastMsg && typeof lastMsg.content === 'string' ? lastMsg.content.length : 0;
                const newSig = saved.ts || `${saved.history.length}:${lastLen}`;
                if (newSig === this._lastHistoryTs) return; // sin cambios reales
                this._lastHistoryTs = newSig;
                const prevLen = this.history.length;
                const prevSummary = this._summary;
                // F-DUP-CHAT: MERGE (NO reemplazar) — igual que el polling remoto.
                // Si reemplazo, pestanas que escriban a la vez se sobreescriben.
                // Merge conserva los locales y agrega los remotos nuevos por firma.
                const localKeys = new Set();
                for (const m of (this.history || [])) {
                    localKeys.add(`${m.role}|${m.content}|${m.timestamp || 0}`);
                }
                let added = 0;
                for (const m of saved.history) {
                    const k = `${m.role}|${m.content}|${m.timestamp || 0}`;
                    if (localKeys.has(k)) continue;
                    this.history.push(m);
                    added++;
                }
                if (this.history.length > 200) this.history = this.history.slice(-200);
                this._summary = saved.summary || this._summary;
                // Re-renderiza si hubo agregados o cambio el summary
                if (added > 0 || this._summary !== prevSummary) {
                    this.render();
                    this.scrollToBottom();
                }
            } catch (err) { console.warn('[EvaChat] cross-tab sync failed:', err); }
        };
        window.addEventListener('storage', this._storageHandler);
        await this._loadSavedConversation();
        await this.loadBusinessContext();
        if (this.history.length) {
            this.render();
        } else {
            this._renderShell('Conectando con Eva...');
            await this._sendGreeting(true);
        }
        // F2: iniciar polling remoto (10s) para sincronizar con otros dispositivos
        this._startRemoteSync();
    },

    // F-DUP: teardown elimina todos los listeners e intervalos creados por init().
    // LLamar antes de recrear EvaChat o al cerrar sesión.
    teardown() {
        if (this._storageHandler) {
            try { window.removeEventListener('storage', this._storageHandler); } catch (e) {}
            this._storageHandler = null;
        }
        if (this._remoteSyncInterval) {
            clearInterval(this._remoteSyncInterval);
            this._remoteSyncInterval = null;
        }
        if (this._saveDebounce) {
            clearTimeout(this._saveDebounce);
            this._saveDebounce = null;
        }
        this._remoteSyncStarted = false;
        this._initializedFor = null;
    },

    _renderShell(statusText) {
        const c = document.getElementById('app-content');
        if (!c) return;
        if (!statusText) return;
        const name = (this.userName || 'amigo').split(' ')[0];
        const greeting = statusText || `Hola, ${name}.\n\nEstoy lista para ayudarte.\n\n¿Qué necesitas?`;
        // NO reemplazar this.history — solo mostrar la shell de carga sin persistir
        const prevHistory = this.history;
        this.history = [{ role: 'assistant', content: greeting, summary: true }];
        this.render();
        this.history = prevHistory;
    },

    // ── GREETING ────────────────────────────────────────────────
    async _sendGreeting(force = false) {
        if (!force && this.history.length) return;
        // ANTI-SPAM: si el último mensaje ya es un greeting reciente (misma hora),
        // no generar otro. Esto previene duplicados cuando el usuario reabre la app.
        const now = Math.floor(Date.now() / 1000);
        const lastMsg = this.history[this.history.length - 1];
        if (lastMsg && lastMsg.role === 'assistant' && lastMsg.summary) {
            const lastTs = lastMsg.timestamp || 0;
            const ageMin = (now - lastTs) / 60;
            if (ageMin < 30) return; // ya hay un greeting reciente (< 30 min)
        }
        // Construir el greeting
        let greeting;
        try {
            const brief = await this._buildDailyBrief();
            greeting = brief.text;
            this._hasRecentAlerts = brief.alerts > 0;
            this._briefEvent = brief.event || null;
            var events = brief.events || [];
        } catch (e) {
            const name = (this.userName || 'amigo').split(' ')[0];
            greeting = `Hola, ${name}.\n\nHoy tu negocio se ve tranquilo.\n\nRevisé tus cámaras y no encontré alertas reales.\n¿Quieres ver qué está pasando ahora?`;
            var events = [];
        }
        // APPEND (no replace) — solo agregar si history está vacío o si el último
        // mensaje no es ya este greeting (anti-dup)
        if (!this.history.length) {
            this.history = [{ role: 'assistant', content: greeting, summary: true, events, timestamp: now }];
        } else {
            // Verificar que no exista ya un greeting idéntico en los últimos 5 msgs
            const recent = this.history.slice(-5);
            const isDup = recent.some(m => m.role === 'assistant' && m.content === greeting);
            if (!isDup) {
                this.history.push({ role: 'assistant', content: greeting, summary: true, events, timestamp: now });
            }
        }
        this._saveConversation();
        this.render();
    },

    // ── RENDER ──────────────────────────────────────────────────
    render() {
        const c = document.getElementById('app-content');
        if (!c) return;
        // ANTI-MIX: no renderizar el chat de Eva si el usuario está en Otra tab.
        // Esto previene que el chat aparezca mezclado con Settings, Eventos, etc.
        const onEva = (typeof App !== 'undefined' && App.page === 'eva') ||
                      document.getElementById('eva-chat-container');
        if (!onEva) return;
        let chatEl = document.getElementById('eva-chat-container');
        if (!chatEl) {
            chatEl = document.createElement('div');
            chatEl.id = 'eva-chat-container';
            chatEl.className = 'eva-chat-container';
            c.appendChild(chatEl);
        }
        chatEl.style.cssText = 'height:100%;width:100%;min-height:0;flex:1;display:flex;flex-direction:column;';
        // Reset cola de heatmaps pendientes (se rellena durante render y se dibujan tras innerHTML)
        this._pendingHeatmaps = [];

        const visibleHistory = this.history.slice(-this._visibleLimit);
        const hasOlder = this._visibleLimit < this.history.length;
        let messagesHtml = '';
        if (hasOlder) {
            messagesHtml += `<div style="display:flex;justify-content:center;margin:4px 0 12px"><button class="eva-load-older" onclick="EvaChat.loadOlderMessages()">↑ Cargar anteriores</button></div>`;
        }
        if (visibleHistory.length > 0) {
            let lastRole = null;
            for (const msg of visibleHistory) {
                const isUser = msg.role === 'user';
                const align = isUser ? 'flex-end' : 'flex-start';
                const bubbleBg = isUser ? 'var(--accent)' : 'rgba(44,44,46,0.82)';
                const br = isUser ? '18px 18px 4px 18px' : '18px 18px 18px 4px';
                const msgMargin = (lastRole === msg.role) ? '4px' : '14px';
                lastRole = msg.role;
                const text = (msg.content || '').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
                let formatted = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                // B4: mapear tags internos del backend (guardados crudos en history) a texto visible.
                formatted = formatted
                    .replace(/__daily_summary__/g, '📊 Resumen del día')
                    .replace(/__yesterday_summary__/g, '📅 Resumen de ayer')
                    .replace(/__adjust_protection__/g, '🛡️ Ajustar protección');
                let imgHtml = '';
                if (msg.image_url && msg.image_url.length > 10) {
                    let imgSrc = msg.image_url;
                    if (imgSrc.startsWith('/eva-image/') || imgSrc.startsWith('/eva-frame/')) imgSrc = EvaChat.API + imgSrc;
                    imgHtml = '<div style="margin-top:8px;"><img src="' + imgSrc + '" style="width:100%;max-height:300px;object-fit:contain;border-radius:12px;background:#0a0a0a;cursor:pointer;" onclick="var t=this;if(t.style.maxHeight===\'300px\'){t.style.maxHeight=\'none\'}else{t.style.maxHeight=\'300px\'};window._evaScroll=true;"></div>';
                }
                if (msg.image_b64 && msg.image_b64.length > 10) {
                    imgHtml = '<div style="margin-top:8px;"><img src="data:image/jpeg;base64,' + msg.image_b64 + '" style="width:100%;max-height:300px;object-fit:contain;border-radius:12px;background:#0a0a0a;cursor:pointer;" onclick="var t=this;if(t.style.maxHeight===\'300px\'){t.style.maxHeight=\'none\'}else{t.style.maxHeight=\'300px\'};window._evaScroll=true;"></div>';
                }
                let eventsHtml = '';
                if (msg.events && msg.events.length > 0) eventsHtml = EvaChat.renderEventCarousel(msg.events);
                let heatmapHtml = '';
                if (msg.heatmap && msg.heatmap_meta) heatmapHtml = EvaChat.renderHeatmap(msg.heatmap, msg.heatmap_meta);
                const bubbleStyle = isUser
                    ? 'background:var(--accent);color:#fff;'
                    : 'background:rgba(44,44,46,0.92);border:1px solid rgba(255,255,255,0.06);';
                const isSummary = msg.summary ? 'margin-top:auto;' : '';
                messagesHtml += `<div style="display:flex;justify-content:${align};${isSummary}margin-top:${msgMargin};margin-bottom:2px;">` +
                    `<div style="max-width:${isUser ? '80%' : '92%'};${bubbleStyle}border-radius:${br};padding:12px 15px;font-size:0.94rem;line-height:1.5;box-shadow:0 1px 2px rgba(0,0,0,0.2);">` +
                    formatted + imgHtml + eventsHtml + heatmapHtml + `</div><div style="font-size:0.65rem;color:var(--text-secondary);margin-top:4px;align-self:flex-end;">${msg.timestamp ? new Date(msg.timestamp*1000).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit'}) : ''}</div></div>`;
            }
        } else {
            messagesHtml = '<div class="eva-welcome"><div class="eva-welcome-icon">👋</div><div class="eva-welcome-text">Conectando con Eva...</div></div>';
        }

        const chips = this._buildQuickChips();
        const suggestionsHtml = chips.length ? `<div class="eva-suggestions" style="display:flex;gap:8px;overflow-x:auto;padding:0 16px 10px">${chips.map(chip => `<button class="suggestion-btn" onclick="EvaChat.sendSuggestion('${chip.text.replace(/'/g, "\\'")}')">${chip.label}</button>`).join('')}</div>` : '';

        chatEl.innerHTML = `
            <div class="eva-chat-header" style="height:72px;display:flex;align-items:center;justify-content:space-between;padding:0 18px;border-bottom:1px solid rgba(255,255,255,0.06);background:rgba(28,28,30,0.72);backdrop-filter:blur(24px)">
                <div style="display:flex;align-items:center;gap:10px;min-width:0">
                    <div class="eva-avatar" style="width:36px;height:36px;border-radius:12px;background:linear-gradient(135deg,#0a84ff,#30d158);display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:.95rem;box-shadow:0 10px 28px rgba(10,132,255,.20)">E</div>
                    <div class="eva-header-info" style="min-width:0">
                        <div class="eva-name" style="font-weight:600;font-size:1rem">Eva</div>
                        <div class="eva-status" id="eva-status" style="font-size:0.72rem;color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Asistente de seguridad</div>
                    </div>
                </div>
            </div>
             <div class="eva-messages" id="eva-messages" style="flex:1;min-height:0;overflow-y:auto;padding:12px 16px 8px;display:flex;flex-direction:column;gap:8px;">
                 ${messagesHtml}
             </div>
             ${suggestionsHtml}
            <div class="eva-input-area" style="padding:10px 16px 12px;background:rgba(0,0,0,0.18)">
                <div class="eva-input-row" style="display:flex;gap:8px;align-items:center">
                    <input id="eva-input" placeholder="Escribe un mensaje a Eva..." autocomplete="off" style="flex:1;padding:12px 15px;border-radius:999px;border:1px solid rgba(255,255,255,0.08);background:rgba(44,44,46,0.72);color:var(--text-primary);font-size:0.95rem;outline:none" onkeypress="if(event.key==='Enter') EvaChat.sendMessage()">
                    <button class="eva-send-btn" id="eva-send-btn" onclick="EvaChat.sendMessage()" style="width:42px;height:42px;border-radius:50%;border:none;background:var(--accent);color:#fff;font-size:1rem;cursor:pointer">↑</button>
                </div>
            </div>
        `;
        const messages = document.getElementById('eva-messages');
        if (messages) messages.onscroll = () => {
            if (messages.scrollTop < 80 && hasOlder && !this._loadingOlder) this.loadOlderMessages();
        };
        setTimeout(() => {
            const input = document.getElementById('eva-input');
            if (input) input.focus();
            this._drawPendingHeatmaps();
            this.scrollToBottom(true);
        }, 300);
        this.scrollToBottom(true);
    },

    _isInstallCameraIntent(msg) {
        const text = (msg || '').trim().toLowerCase()
            .replace(/á/g, 'a')
            .replace(/é/g, 'e')
            .replace(/í/g, 'i')
            .replace(/ó/g, 'o')
            .replace(/ú/g, 'u')
            .replace(/ñ/g, 'n');
        if (text.includes('no se pudo') || text.includes('fallo') || text.includes('falló') || text.includes('error') || text.includes('confund') || text.includes('desubic') || text.includes('instarlar')) return false;
        return text.includes('instalar camara') ||
            text.includes('agregar camara') ||
            text.includes('añadir camara') ||
            text.includes('poner camara') ||
            text.includes('montar camara') ||
            text.includes('crear camara') ||
            text.includes('configurar una camara nueva') ||
            text.includes('configurar camara nueva') ||
            text.includes('nueva camara') ||
            text.includes('quiero una camara');
    },

    _isSetupArtifact(text) {
        const t = String(text || '').toLowerCase();
        return t.includes('vamos a instalar') ||
            t.includes('¿dónde la vas a poner') ||
            t.includes('configuración') ||
            t.includes('voy a crear el sistema de protección') ||
            t.includes('ya tengo todo el contexto') ||
            t.includes('apruebas esta configuración') ||
            t.includes('retomo la instalación');
    },

    _isOsIntentText(msg) {
        const text = (msg || '').trim().toLowerCase()
            .replace(/á/g, 'a')
            .replace(/é/g, 'e')
            .replace(/í/g, 'i')
            .replace(/ó/g, 'o')
            .replace(/ú/g, 'u')
            .replace(/ñ/g, 'n');
        return text.startsWith('__') ||
            text.includes('resumen') ||
            text.includes('paso ayer') ||
            text.includes('paso hoy') ||
            text.includes('ha visto algo hoy') ||
            text.includes('visto algo hoy') ||
            text.includes('algo hoy') ||
            text.includes('que hay hoy') ||
            text.includes('paso esta noche') ||
            text.includes('pasado esta noche') ||
            text.includes('ha pasado esta noche') ||
            text.includes('esta pasando ahora') ||
            text.includes('está pasando ahora') ||
            text.includes('pasando ahora') ||
            text.includes('como esta la noche') ||
            text.includes('cómo esta la noche') ||
            text.includes('como está la noche') ||
            text.includes('cómo está la noche') ||
            text.includes('que tal todo') ||
            text.includes('sin ninguna novedad') ||
            text.includes('sin novedad') ||
            text.includes('alguna novedad') ||
            text.includes('novedad') ||
            text.includes('hay novedades') ||
            text.includes('que hay de nuevo') ||
            text.includes('vi una alerta') ||
            text.includes('hubo una alerta') ||
            text.includes('vio una alerta') ||
            text.includes('que paso =? vi una alerta') ||
            text.includes('alta importancia') ||
            text.includes('baja importancia') ||
            text.includes('media importancia') ||
            text.includes('ajustar proteccion') ||
            text.includes('ajustar protección') ||
            text.includes('alerta') ||
            text.includes('sospechoso') ||
            text.includes('diario') ||
            text.includes('quien eres') ||
            text.includes('quién eres') ||
            text.includes('que eres') ||
            text.includes('qué eres') ||
            text.includes('para que sirves') ||
            text.includes('para qué sirves') ||
            text.includes('eres una persona') ||
            text.includes('eres humano') ||
            text.includes('piensa que eva') ||
            text.includes('otra persona') ||
            text.includes('confund') ||
            text.includes('desubic') ||
            text.includes('sigue en configuracion') ||
            text.includes('sigue en configuración') ||
            text.includes('no se pudo instalar') ||
            text.includes('no se pudo instarlar');
    },

    // ── SEND MESSAGE ────────────────────────────────────────────
    async sendMessage() {
        if (this.isLoading || (this._lastSend && Date.now() - this._lastSend < 600)) return;
        this._lastSend = Date.now();
        const input = document.getElementById('eva-input');
        const msg = input?.value.trim();
        const intentText = input?.dataset.intentText || msg;
        if (!msg) return;
        input.value = '';
        input.dataset.intentText = '';
        this.isLoading = true;
        const sendBtn = document.getElementById('eva-send-btn');
        if (sendBtn) sendBtn.disabled = true;
        this.addMessage('user', msg);
        this.showTyping();
        if (this._isInstallCameraIntent(intentText)) {
            this.hideTyping();
            this.addMessage('assistant', 'Perfecto. Voy a iniciar la instalación contigo paso a paso.');
            this.isLoading = false;
            if (sendBtn) sendBtn.disabled = false;
            setTimeout(() => {
                if (window.App && typeof App.newCamera === 'function') App.newCamera();
            }, 250);
            return;
        }
        try {
            const chatSessionId = this._isOsIntentText(intentText) && !this._isInstallCameraIntent(intentText)
                ? `os_${this.userId}`
                : this.sessionId;
            let r = null, lastErr = null;
            // Reintentar 2 veces con backoff para resistir microcortes de red móvil.
            for (let attempt = 0; attempt < 3; attempt++) {
                try {
                    r = await fetch(`${this.API}/config/chat`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            user_id: this.userId,
                            message: intentText,
                            session_id: chatSessionId,
                            user_name: this.userName || '',
                            business_name: this._businessName || ''
                        })
                    });
                    if (r.ok) break;
                    lastErr = new Error('HTTP ' + r.status);
                } catch (re) {
                    lastErr = re;
                    if (attempt < 2) await new Promise(res => setTimeout(res, 800 * (attempt + 1)));
                }
            }
            if (!r || !r.ok) throw lastErr || new Error('fetch-failed');
            const data = await r.json();
            if (data.success) {
                this.sessionId = data.sessionId || this.sessionId;
                if (this._isOsIntentText(intentText) && !this._isInstallCameraIntent(intentText)) {
                    this.sessionId = data.sessionId || this.sessionId;
                    this.history = this.history.filter(m => !(m.role === 'assistant' && this._isSetupArtifact(m.content || '')));
                }
                localStorage.setItem(this._sessionKey, this.sessionId);
                
                // Detectar fase del wizard para mostrar UI especial
                const nextPhase = data.next_phase || data.phase;
                const camId = data.camera_id || data.data?.camera_id;
                let extraHtml = '';
                
                if (nextPhase === 'WIZARD_QR' && data.claim_token) {
                    // Mostrar QR y código
                    const qrUrl = `https://api.ojoia.com.do/api/claim-qr?token=${data.claim_token}`;
                    extraHtml = `<div style="margin-top:12px;text-align:center"><div style="background:#fff;padding:12px;border-radius:12px;display:inline-block"><img src="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(qrUrl)}" style="width:200px;height:200px"></div><div style="margin-top:8px;font-weight:600">Código: <span style="font-family:monospace;font-size:1.1rem">${data.claim_token}</span></div><div class="meta">Escanea el QR o escribe el código en el portal del ESP32</div></div>`;
                }
                
                if (nextPhase === 'WIZARD_ZONES_DRAW' && camId) {
                    // Mostrar botón para ir a configurar zonas
                    extraHtml = `<div style="margin-top:12px"><button class="btn" style="width:100%;background:var(--accent);color:#fff" onclick="EvaChat._openZoneEditorFromWizard('${camId}')">📍 Ir a Configurar Zonas</button><div class="meta" style="margin-top:8px;text-align:center">Dibuja las zonas importantes (caja, entrada, cocina) y luego regresa aquí y dime "listo"</div></div>`;
                }
                
                const msg = { role: 'assistant', content: data.response + (extraHtml ? '\n\n' + extraHtml : ''), image_url: data.image_url || '', events: data.events_found || data.events || [], timestamp: Date.now() / 1000 };
                if (data.heatmap && data.heatmap_meta) {
                    msg.heatmap = data.heatmap;
                    msg.heatmap_meta = data.heatmap_meta;
                }
                this.history.push(msg);
                if (data.suggestions && data.suggestions.length > 0) this._suggestions = data.suggestions;
                this._maybeSummarize();
                this._hasRecentAlerts = (await this._readStatus()).alerts > 0;
                this._saveConversation();
                this.render();
            } else {
                this.addMessage('assistant', data.response || 'Error procesando tu mensaje.');
            }
        } catch (e) {
            this.hideTyping();
            this.addMessage('assistant', 'Error de conexión. Verifica que el servidor esté activo.');
        } finally {
            this.isLoading = false;
            if (sendBtn) sendBtn.disabled = false;
        }
    },

    async showAlertEvent(eventId) {
        if (!eventId) return;
        try {
            const r = await fetch(`${this.API}/api/events/${eventId}?user_id=${this.userId}`);
            if (!r.ok) return;
            const event = await r.json();
            const qa = event.qwen_analysis || {};
            const qjson = event.qwen_json || {};
            const desc = this.cleanEventDescription(qa.description || qa.summary || qjson.description || event.description || event.summary || '');
            const cam = event.camera_name || event.camera_id || 'la cámara';
            const time = event.datetime || 'este momento';
            const text = `Detecté una alerta en ${time} en ${cam}. Eva vio: ${desc}`;
            this.history.push({ role: 'assistant', content: text, summary: true, events: [event], alert_event_id: eventId });
            this._briefEvent = event;
            this._saveConversation();
            this.render();
        } catch(e) { console.error('Error showing alert event:', e); }
    },

    openCameraLive(camId) {
        if (window.App && typeof window.App._openCameraLive === 'function') {
            window.App._openCameraLive(camId);
        }
    },

    _openZoneEditorFromWizard(camId) {
        // Marcar que estamos en modo wizard de zonas
        this._inWizardZoneDraw = true;
        // Navegar a la página de ajustes de la cámara
        if (window.App && typeof App._openCameraConfig === 'function') {
            App.go('settings');
            setTimeout(() => {
                App._openCameraConfig(camId);
            }, 100);
        }
    },

    // ── SEND SUGGESTION ─────────────────────────────────────────
    sendSuggestion(text) {
        if (this._lastSend && Date.now() - this._lastSend < 600) return;
        if (text === '__show_brief_event__') {
            if (this._briefEvent?.event_id) {
                this.openEventDetail(this._briefEvent.event_id);
                return;
            }
            const input = document.getElementById('eva-input');
            if (!input) return;
            input.value = 'Ver alerta';
            input.dataset.intentText = text;
            this.sendMessage();
            return;
        }
        const input = document.getElementById('eva-input');
        if (!input) return;
        const visibleText = text.startsWith('__')
            ? text.replace('__daily_summary__', 'Resumen del día')
                  .replace('__adjust_protection__', 'Ajustar protección')
                  .replace('__yesterday_summary__', 'Resumen de ayer')
            : text;
        input.value = visibleText;
        input.dataset.intentText = text;
        this.sendMessage();
    },

    // ── ADD MESSAGE ─────────────────────────────────────────────
    addMessage(role, text, events = null) {
        this.history.push({ role, content: text, events: events || [], timestamp: Date.now() / 1000 });
        this._saveConversation();
        this.render();
    },

    cleanEventDescription(desc) {
        if (!desc) return '';
        let text = String(desc).trim();
        if (!text) return '';
        try {
            const parsed = JSON.parse(text);
            if (parsed && typeof parsed === 'object') text = parsed.summary || parsed.description || text;
        } catch (e) {}
        return text
            .replace(/- If ALL checks NO[\s\S]*/m, '')
            .replace(/No violation detected[\s\S]*/i, 'Sin actividad sospechosa')
            .replace(/The employee's hands[\s\S]*/i, 'Sin actividad sospechosa')
            .replace(/The provided (images|frames)[\s\S]*/i, 'Sin actividad sospechosa')
            .replace(/Error analizando[\s\S]*/i, 'Sin actividad sospechosa')
            .trim() || 'Actividad normal';
    },

    // ── HEATMAP OVERLAY ─────────────────────────────────────────
    renderHeatmap(heatmap, meta) {
        if (!heatmap || !heatmap.length) return '';
        const grid = heatmap.length;
        const W = 320, H = 240;
        const canvasId = 'heat_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
        let maxVal = 0;
        for (let gy = 0; gy < grid; gy++) for (let gx = 0; gx < grid; gx++) if (heatmap[gy][gx] > maxVal) maxVal = heatmap[gy][gx];
        if (maxVal === 0) maxVal = 1;
        const hotspots = (meta.hotspots || []).slice(0, 5);
        let hotspotsHtml = hotspots.map((h, i) => `<div style="display:inline-block;background:rgba(255,90,30,${0.12 + 0.1*i});color:#fff;border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0;font-size:0.72rem">🔥 #${i+1} (${h.gx},${h.gy}) · ${h.count}</div>`).join('');
        let zonesHtml = '';
        if (meta.zone_counts && meta.zone_counts.length) {
            zonesHtml = meta.zone_counts.slice(0, 4).map(([name, count]) => `<div style="display:inline-block;background:rgba(10,132,255,0.12);color:var(--text-primary);border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0;font-size:0.72rem">📍 ${name} · ${count}</div>`).join('');
        }
        // Guardar datos para dibujar tras render (innerHTML no evalúa <script>)
        this._pendingHeatmaps = this._pendingHeatmaps || [];
        this._pendingHeatmaps.push({ canvasId, heatmap, maxVal, grid, W, H, hotspots });
        return `<div style="margin-top:12px;border-radius:12px;overflow:hidden;background:#0a0a0a;border:1px solid rgba(255,255,255,0.08)">
            <div style="padding:8px 12px;font-size:0.8rem;font-weight:600;color:var(--text-primary)">🔥 Mapa de Calor · ${meta.date || ''} · ${meta.total_points || 0} puntos</div>
            <canvas id="${canvasId}" width="${W}" height="${H}" style="width:100%;max-width:${W}px;height:auto;display:block"></canvas>
            <div style="padding:8px 12px;font-size:0.72rem;color:var(--text-secondary)">Hotspots:</div>
            <div style="padding:0 12px 8px">${hotspotsHtml}</div>
            ${zonesHtml ? `<div style="padding:4px 12px 8px;font-size:0.72rem;color:var(--text-secondary)">Zonas:</div><div style="padding:0 12px 10px">${zonesHtml}</div>` : ''}
        </div>`;
    },

    _drawPendingHeatmaps() {
        if (!this._pendingHeatmaps || !this._pendingHeatmaps.length) return;
        // Filtrar los que aún existen en el DOM (los descartados por re-render no se dibujan)
        this._pendingHeatmaps = this._pendingHeatmaps.filter(({ canvasId, heatmap, maxVal, grid, W, H, hotspots }) => {
            const canvas = document.getElementById(canvasId);
            if (!canvas) return false; // ya no existe, eliminar de la cola
            const ctx = canvas.getContext('2d');
            const cw = W / grid, ch = H / grid;
            for (let gy = 0; gy < grid; gy++) {
                for (let gx = 0; gx < grid; gx++) {
                    const v = heatmap[gy][gx] / maxVal;
                    if (v <= 0) continue;
                    const t = Math.min(1, v);
                    const g_ = Math.round(255 * (1 - t * 0.6));
                    const b = Math.round(80 * (1 - t));
                    ctx.fillStyle = `rgba(255,${g_},${b},${(0.12 + 0.7 * t).toFixed(3)})`;
                    ctx.fillRect(gx * cw, gy * ch, cw + 0.5, ch + 0.5);
                }
            }
            hotspots.forEach((h, i) => {
                ctx.strokeStyle = `rgba(255,255,255,${(0.9 - i * 0.15).toFixed(2)})`;
                ctx.lineWidth = 2 - 0.3 * i;
                ctx.strokeRect(h.gx * cw - 0.5, h.gy * ch - 0.5, cw + 1, ch + 1);
            });
            return true; // mantener para re-render si el canvas sigue visible
        });
    },

    // ── EVENT CAROUSEL ──────────────────────────────────────────
    renderEventCarousel(events) {
        if (!events || events.length === 0) return '';
        let html = `<div class="eva-carousel"><div class="carousel-title">📸 ${events.length} momento${events.length === 1 ? '' : 's'} encontrado${events.length === 1 ? '' : 's'}</div><div class="carousel-scroll">`;
        for (const evt of events.slice(0, 10)) {
            const anomalyClass = evt.anomaly || this._isAlertEvent(evt) ? 'anomaly' : '';
            const desc = EvaChat.cleanEventDescription(evt.summary || evt.description || '');
            const persons = evt.persons ?? 0;
            const framesCount = evt.frames_count || (evt.frames || []).length || 0;
            const _thumbRaw = evt.thumb_url || '';
            const _thumbSrc = _thumbRaw.startsWith('http') ? _thumbRaw : (_thumbRaw ? (this.API + _thumbRaw) : '');
            const imgSrc = evt.thumb_url || evt.frame_url ? (_thumbSrc || this.API + (evt.frame_url || '')) : '';
            const imgHtml = imgSrc
                ? `<img src="${imgSrc}" onerror="this.onerror=null;this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect width=%22100%22 height=%22100%22 fill=%22%232c2c2e%22/><text x=%2250%22 y=%2252%22 text-anchor=%22middle%22 font-size=%2226%22 fill=%22%238e8e93%22>📷</text></svg>'" loading="lazy">`
                : `<div style="width:100%;height:90px;display:flex;align-items:center;justify-content:center;background:var(--bg-tertiary);color:var(--text-secondary);font-size:1.5rem">📷</div>`;
            const videoBadge = framesCount ? '<div class="card-video-badge">▶ Video</div>' : '';
            html += `<div class="carousel-card ${anomalyClass}" onclick="EvaChat.openEventDetail('${evt.event_id}')"><div class="card-time">${this.escapeHtml(this._formatEventDateTime(evt))}</div><div class="card-camera">${this.escapeHtml(evt.camera_name || evt.camera_id || '')}</div><div class="card-frame">${imgHtml}${videoBadge}</div><div class="card-desc">${this.escapeHtml(desc).substring(0, 90)}</div><div class="card-persons">👥 ${persons || '—'} persona${persons === 1 ? '' : 's'}</div>${anomalyClass ? '<div class="card-anomaly-badge">⚠️ Alerta</div>' : ''}</div>`;
        }
        html += `</div></div>`;
        return html;
    },

    async openEventDetail(eventId) {
        this.stopEventAutoplay();
        // Usar SIEMPRE el viewer principal (App._openEvent)
        // Esto evita que se abran DOS visualizadores al hacer clic en eventos
        if (typeof App !== "undefined" && App._openEvent) {
            try {
                App._openEvent(eventId);
                return;
            } catch(e) {
                console.warn("[EvaChat] Falló App._openEvent:", e);
            }
        }
        // Fallback: intentar con App.go
        if (typeof App !== "undefined" && App.go) {
            App._pendingEventsDeepLink = eventId;
            if (App.go("events", eventId)) {
                return;
            }
        }
        // Fallback final (NO DEBERÍA USARSE NORMALMENTE)
        console.error("[EvaChat] App._openEvent no disponible, usando modal propio");
    },

    async sendFeedback(eventId, isReal, btn) {
        try {
            await fetch(`${this.API}/api/chat/eva/feedback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: this.userId, event_id: eventId, is_real: isReal }) });
            const parent = btn.parentElement;
            parent.querySelectorAll('.feedback-btn').forEach(b => b.disabled = true);
            btn.style.opacity = '1';
            btn.style.fontWeight = '700';
        } catch (e) { console.error('Error sending feedback:', e); }
    },

    _showEventFrame(eventId, rawIndex, total) {
        const index = Math.max(0, Math.min(parseInt(rawIndex || '0', 10), total - 1));
        const img = document.getElementById('eva-event-frame-img');
        const status = document.getElementById('eva-event-frame-status');
        if (img) img.src = `${this.API}/api/events/${eventId}/frame/${index}?user_id=${this.userId}&_=${Date.now()}`;
        if (status) status.textContent = `${index + 1}/${total}`;
    },

    closeEventModal(modal) {
        this.stopEventAutoplay();
        if (modal) modal.remove();
    },

    startEventAutoplay(eventId, total) {
        this.stopEventAutoplay();
        let index = 0;
        this._frameTimer = setInterval(() => {
            index = (index + 1) % total;
            const img = document.getElementById('eva-event-frame-img');
            const status = document.getElementById('eva-event-frame-status');
            const range = document.getElementById('eva-event-frame-range');
            if (img) img.src = `${this.API}/api/events/${eventId}/frame/${index}?user_id=${this.userId}&_=${Date.now()}`;
            if (status) status.textContent = `${index + 1}/${total}`;
            if (range) range.value = index;
        }, 280);
        const btn = document.getElementById('eva-frame-play-btn');
        if (btn) btn.textContent = 'Pausar video';
    },

    stopEventAutoplay() {
        if (this._frameTimer) {
            clearInterval(this._frameTimer);
            this._frameTimer = null;
        }
    },

    toggleEventAutoplay(eventId, total) {
        if (this._frameTimer) {
            this.stopEventAutoplay();
            const btn = document.getElementById('eva-frame-play-btn');
            if (btn) btn.textContent = 'Reproducir video';
        } else {
            this.startEventAutoplay(eventId, total);
        }
    },

    async _buildDailyBrief() {
        const status = await this._readStatus();
        // M4.6: excluir vigilance_alert (centinela de 1 frame) del "último análisis".
        // El brief debe basarse en análisis reales de Eva, no en disparos operativos.
        const eventsR = await fetch(`${this.API}/api/user/events?user_id=${this.userId}&filter=today&limit=20&exclude_vigilance=true`);
        const events = eventsR.ok ? (await eventsR.json()).events || [] : [];
        const name = (this.userName || 'amigo').split(' ')[0];
        const business = this._businessName || 'tu negocio';
        const activeText = `${status.active} cámara${status.active === 1 ? '' : 's'} activa${status.active === 1 ? '' : 's'}`;
        const alertEvents = events.filter(e => this._isAlertEvent(e));
        const alertCount = alertEvents.length;
        const event = alertEvents[0] || this._pickBriefEvent(events, alertEvents);
        const minimalEvent = event ? this._minimalEvent(event) : null;
        const lastAnalysisAge = event && event.timestamp ? Math.max(0, Math.round((Date.now() / 1000 - event.timestamp) / 60)) : 0;
        const totalEvents = events.length;
        let lines = [];

        if (alertCount > 0) {
            lines.push(`Hola, ${name}.`);
            lines.push('');
            lines.push(`Encontré ${alertCount} alerta${alertCount === 1 ? '' : 's'} real${alertCount === 1 ? '' : 'es'} hoy en ${business}.`);
            lines.push('');
            if (event) {
                lines.push(this._eventSentence(event, true));
                lines.push('');
                lines.push('¿Quieres ver la alerta?');
            }
        } else if (totalEvents > 0) {
            lines.push(`Hola, ${name}.`);
            lines.push('');
            lines.push(`Hoy ${business} se ve tranquilo.`);
            lines.push(`Se registraron ${totalEvents} análisis sin alertas reales.`);
            lines.push('');
            if (event) {
                lines.push('Último análisis:');
                lines.push(this._eventSentence(event, false));
            }
            lines.push('');
            lines.push('¿Qué quieres revisar?');
        } else {
            lines.push(`Hola, ${name}.`);
            lines.push('');
            lines.push(`Tu cámara está activa y vigilando ${business}.`);
            lines.push('');
            lines.push('Aún no hay análisis registrados hoy.');
            lines.push('¿En qué te puedo ayudar?');
        }

        return { text: lines.join('\n'), alerts: alertCount, event: minimalEvent, events: minimalEvent ? [minimalEvent] : [] };
    },

    _pickBriefEvent(events, alertEvents) {
        const nonNormal = events.find(e => {
            const importance = (e.qwen_json?.importancia || e.qwen_analysis?.importancia || e.metadata?.importance || '').toString().toLowerCase();
            const desc = this.cleanEventDescription(e.summary || e.description || '');
            return importance !== 'normal' || /sospech|raro|alert|movimiento|puerta|caja|después de|fuera de horario/i.test(desc);
        });
        return nonNormal || events[0] || null;
    },

    _minimalEvent(e) {
        return {
            event_id: e.event_id || '',
            datetime: e.datetime || '',
            timestamp: e.timestamp || 0,
            camera_id: e.camera_id || '',
            camera_name: e.camera_name || '',
            event_type: e.event_type || '',
            description: e.description || e.summary || '',
            summary: e.summary || e.description || '',
            qwen: e.qwen || null,
            qwen_json: e.qwen_json || null,
            qwen_analysis: e.qwen_analysis || null,
            persons: e.persons ?? (e.qwen_analysis?.persons ?? ''),
            anomaly: e.anomaly || false,
            thumb_url: e.thumb_url || '',
            frame_url: e.frame_url || '',
            frames: e.frames || [],
            frames_count: e.frames_count || (e.frames || []).length || 0,
            video_file: e.video_file || '',
            clip_type: e.clip_type || ''
        };
    },

    _isAlertEvent(e) {
        return e.event_type === 'violation' || e.qwen?.violation || e.qwen_json?.violation || e.qwen_analysis?.violation || e.anomaly;
    },

    _eventSentence(e, isAlert) {
        const time = this._formatEventTime(e);
        const camera = e.camera_name || e.camera_id || 'la cámara';
        const desc = this.cleanEventDescription(e.summary || e.description || e.qwen_analysis?.summary || e.qwen_json?.summary || '');
        const clean = desc.replace(/\.$/, '');
        if (isAlert) return `A las ${time}, en ${camera}, detecté: ${clean}.`;
        return `A las ${time}, en ${camera}, se registró: ${clean}.`;
    },

    _formatEventTime(e) {
        try {
            const ts = e.timestamp ? e.timestamp * 1000 : 0;
            const d = e.datetime ? new Date(e.datetime) : new Date(ts);
            return d.toLocaleTimeString('es-DO', { hour: '2-digit', minute: '2-digit' });
        } catch (err) {
            return 'hora pendiente';
        }
    },

    _formatEventDateTime(e) {
        try {
            const ts = e.timestamp ? e.timestamp * 1000 : 0;
            const d = e.datetime ? new Date(e.datetime) : new Date(ts);
            return d.toLocaleString('es-DO', { weekday: 'short', hour: '2-digit', minute: '2-digit' });
        } catch (err) {
            return e.datetime || '';
        }
    },

    _formatDuration(minutes) {
        if (!minutes || minutes < 1) return 'unos minutos';
        if (minutes < 60) return `${minutes} minutos`;
        const hours = Math.floor(minutes / 60);
        const mins = minutes % 60;
        if (hours < 24) return mins ? `${hours} horas y ${mins} minutos` : `${hours} horas`;
        const days = Math.floor(hours / 24);
        const remHours = hours % 24;
        return remHours ? `${days} días y ${remHours} horas` : `${days} días`;
    },

    // ── UI HELPERS ──────────────────────────────────────────────
    showTyping() {
        const container = document.getElementById('eva-messages');
        if (!container) return;
        const typing = document.createElement('div');
        typing.className = 'eva-msg assistant typing-indicator';
        typing.id = 'eva-typing';
        typing.innerHTML = `<div class="eva-bubble"><span class="typing-label">Eva está revisando</span><span class="typing-dots"><span>●</span><span>●</span><span>●</span></span></div>`;
        container.appendChild(typing);
        this.scrollToBottom();
    },

    hideTyping() { const t = document.getElementById('eva-typing'); if (t) t.remove(); },

    formatText(text) {
        if (!text) return '';
        let html = this.escapeHtml(text);
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\n/g, '<br>');
        return html;
    },

    escapeHtml(text) { const d = document.createElement('div'); d.textContent = text; return d.innerHTML; },

    scrollToBottom(force = false) {
        const c = document.getElementById('eva-messages');
        if (!c) return;
        const doScroll = () => { c.scrollTop = c.scrollHeight; };
        doScroll();
        requestAnimationFrame(() => {
            doScroll();
            setTimeout(doScroll, 100);
            setTimeout(doScroll, 300);
            if (force) setTimeout(doScroll, 650);
        });
        if (window._evaScroll) {
            window._evaScroll = false;
            setTimeout(doScroll, 500);
            setTimeout(doScroll, 1000);
        }
    },

    async _loadSavedConversation() {
        // 1. Cargar localStorage PRIMERO (responde al instante, sin espera de red)
        let local = null;
        try {
            local = JSON.parse(localStorage.getItem(`eva_history_${this.userId}`) || 'null');
        } catch(e) {}
        this.history = (local && Array.isArray(local.history)) ? local.history : [];
        this._summary = (local && local.summary) || '';
        this._remoteHistoryTs = 0;

        // 2. GET del remote para sincronizar (merge, no reemplazo)
        try {
            const r = await fetch(`${this.API}/api/chat/eva/history?user_id=${this.userId}`);
            if (r.ok) {
                const data = await r.json();
                if (data && Array.isArray(data.history)) {
                    this._remoteHistoryTs = parseInt(data.ts || 0, 10) || 0;
                    // MERGE: si tenemos local state, mezclar; sino usar el remoto
                    if (this.history.length > 0 && Array.isArray(data.history) && data.history.length) {
                        // Firma local (sin timestamp: solo role+content). El timestamp
                        // cliente/servidor puede diferir en milisegundos y causar duplicados
                        // al merge. Toleramos re-procesar el mismo content (raro) antes que
                        // duplicar un mensaje real.
                        const localKeys = new Set();
                        for (const m of this.history) {
                            localKeys.add(`${m.role}|${m.content}`);
                        }
                        let appended = 0;
                        for (const rm of data.history) {
                            const k = `${rm.role}|${rm.content}`;
                            if (!localKeys.has(k)) {
                                this.history.push(rm);
                                appended++;
                            }
                        }
                        // Recortar a ultimos 200
                        if (this.history.length > 200) this.history = this.history.slice(-200);
                        if (data.summary) this._summary = data.summary;
                    } else if (data.history.length) {
                        // No hay local state: usar el remoto
                        this.history = data.history;
                        if (data.summary) this._summary = data.summary;
                    }
                    // Ordenar cronologicamente (viejo -> nuevo) por si merge desordeno o el
                    // remote venia invertido. El render usa slice(-N) para mostrar recientes.
                    this.history.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
                    if (this.history.length > 200) this.history = this.history.slice(-200);
                    // Inicializar firma local sync con el server
                    this._lastHistoryTs = String(this._remoteHistoryTs || (this.history.at(-1)?.timestamp || 0));
                    this._saveConversation();
                    return;
                }
            }
        } catch(e) {}
        // Si GET falló, ya tenemos el local state cargado
    },

    // F2: Polling cada 10s para detectar cambios remotos (otro dispositivo).
    // Compara el ts del server; si cambio, MERGE los msgs remotos con los locales
    // (NO reemplaza) para no perder mensajes recientes que aun estan en debounce
    // y todavia no llegaron al backend. Solo re-renderiza si hubo cambios reales.
    // No dispara si la pestana esta oculta (visibility hidden) para ahorrar.
    async _startRemoteSync() {
        if (this._remoteSyncStarted) return;
        this._remoteSyncStarted = true;
        const mergeRemote = (remoteHistory, remoteSummary) => {
            // MERGE: conservar mensajes locales con ts > remoteTs (escritos hace poco,
            // todavia no flushed al backend por debounce de 800ms), y agregar remotos
            // nuevos con ts > remoteTs actual que no tengamos.
            if (!Array.isArray(remoteHistory) || !remoteHistory.length) return false;
            const localByTs = new Map();
            for (const m of (this.history || [])) {
                const k = `${m.role}|${m.content}|${m.timestamp || 0}`;
                localByTs.set(k, true);
            }
            let appended = 0;
            for (const r of remoteHistory) {
                const k = `${r.role}|${r.content}|${r.timestamp || 0}`;
                if (localByTs.has(k)) continue;
                // Es remoto nuevo (de otro dispositivo) que no tenemos localmente
                this.history.push(r);
                appended++;
            }
            // Recortar a ultimos 200
            if (this.history.length > 200) this.history = this.history.slice(-200);
            const summaryChanged = !!remoteSummary && remoteSummary !== this._summary;
            if (remoteSummary) this._summary = remoteSummary;
            return appended > 0 || summaryChanged;
        };
        const poll = async () => {
            if (document.visibilityState === 'hidden') return;
            try {
                const r = await fetch(`${this.API}/api/chat/eva/history?user_id=${this.userId}&limit=1`);
                if (!r.ok) return;
                const data = await r.json();
                const remoteTs = parseInt(data.ts || 0, 10) || 0;
                if (!remoteTs || remoteTs === this._remoteHistoryTs) return;
                // Hubo cambios en otro dispositivo -> fetch completo y MERGE
                const full = await fetch(`${this.API}/api/chat/eva/history?user_id=${this.userId}`);
                if (!full.ok) return;
                const fullData = await full.json();
                const prevLen = this.history.length;
                const prevSummary = this._summary;
                const changed = mergeRemote(fullData.history || [], fullData.summary);
                this._remoteHistoryTs = remoteTs;
                this._lastHistoryTs = String(remoteTs);
                // Sync también localStorage para que otras pestañas locales lo vean
                if (changed) {
                    try {
                        const lsTs = String(Date.now());
                        localStorage.setItem(this._historyKey, JSON.stringify({ history: this.history, summary: this._summary, ts: lsTs }));
                        localStorage.setItem(this._historyTsKey, lsTs);
                    } catch(e) {}
                    if (this.history.length !== prevLen || this._summary !== prevSummary) {
                        // Solo re-renderizar si estamos en la tab de Eva — si no,
                        // el chat se renderizaria encima de Otra pagina (Settings, Eventos, etc.)
                        const onEva = (typeof App !== 'undefined' && App.page === 'eva') || document.getElementById('eva-chat-container');
                        if (onEva) {
                            this.render();
                            this.scrollToBottom();
                        }
                    }
                }
            } catch (e) { /* silencioso */ }
        };
        // Polling cada 10s
        this._remoteSyncInterval = setInterval(poll, 10000);
        // Poll inmediato cuando la pestana vuelve a ser visible
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                setTimeout(poll, 300);
            }
        });
    },

    _saveConversation() {
        // F2-fix: guardar en localStorage sincrono (rapido) Y hacer POST con
        // debounce de 800ms para evitar race conditions cross-device.
        const ts = String(Date.now());
        try {
            localStorage.setItem(this._historyKey, JSON.stringify({ history: this.history, summary: this._summary, ts }));
            localStorage.setItem(this._historyTsKey, ts);
        } catch(e) {}
        // Asegurar que cada msg tenga timestamp en segundos (entero) para que el
        // backend pueda hacer merge por ts.
        const safeHistory = this.history.map(m => {
            const ts_s = m.timestamp || Math.floor(Date.now() / 1000);
            return {
                role: m.role || 'user',
                content: m.content || '',
                timestamp: typeof ts_s === 'number' ? Math.floor(ts_s) : Math.floor(Date.now() / 1000),
                ...(m.events ? { events: m.events } : {}),
                ...(m.summary ? { summary: m.summary } : {}),
                ...(m.is_daily_report ? { is_daily_report: m.is_daily_report } : {}),
                ...(m.report_url ? { report_url: m.report_url } : {}),
                ...(m.image_url ? { image_url: m.image_url } : {}),
                ...(m.image_b64 ? { image_b64: m.image_b64 } : {}),
                ...(m.heatmap ? { heatmap: m.heatmap } : {}),
                ...(m.heatmap_meta ? { heatmap_meta: m.heatmap_meta } : {}),
            };
        });
        clearTimeout(this._saveDebounce);
        this._saveDebounce = setTimeout(() => {
            try {
                fetch(`${this.API}/api/chat/eva/history`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: this.userId, history: safeHistory, summary: this._summary })
                }).then(r => r.ok ? r.json() : null).then(data => {
                    if (data && data.ts) this._remoteHistoryTs = parseInt(data.ts, 10) || 0;
                }).catch(() => {});
            } catch(e) {}
        }, 800);
    },

    _maybeSummarize() {
        const realMessages = this.history.filter(m => m.role === 'user' || m.role === 'assistant');
        if (realMessages.length < 10 || realMessages.length % 10 !== 0) return;
        const last = realMessages.slice(-10);
        const userLines = last.filter(m => m.role === 'user').map(m => (m.content || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        const assistantLines = last.filter(m => m.role === 'assistant').map(m => (m.content || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        const topics = userLines.slice(0, 2).join(' · ');
        const eva = assistantLines.slice(-1).join(' ');
        this._summary = `Resumen reciente: ${topics ? 'hablaste de ' + topics.slice(0, 180) : 'revisamos el estado de seguridad'}. ${eva ? eva.slice(0, 180) : ''}`;
    },

    loadOlderMessages() {
        if (this._loadingOlder || this._visibleLimit >= this.history.length) return;
        this._loadingOlder = true;
        const messagesEl = document.getElementById('eva-messages');
        const oldHeight = messagesEl ? messagesEl.scrollHeight : 0;
        this._visibleLimit = Math.min(this.history.length, this._visibleLimit + 10);
        this.render();
        setTimeout(() => {
            const next = document.getElementById('eva-messages');
            if (next) next.scrollTop = next.scrollHeight - oldHeight;
            this._loadingOlder = false;
        }, 50);
    },

    _buildQuickChips() {
        const chips = [];
        if (this._briefEvent) {
            chips.push({ label: this._hasRecentAlerts ? 'Ver alerta' : 'Ver momento', text: '__show_brief_event__' });
        }
        chips.push({ label: 'Instalar cámara nueva', text: 'Quiero instalar una cámara nueva' });
        chips.push({ label: 'Resumen del día', text: '__daily_summary__' });
        chips.push({ label: 'Ajustar protección', text: '__adjust_protection__' });
        return chips.slice(0, 3);
    },

    async _readStatus() {
        try {
            const [camsR, evtsR] = await Promise.all([
                fetch(`${this.API}/api/cameras?user_id=${this.userId}`),
                fetch(`${this.API}/api/user/events?user_id=${this.userId}&filter=today&limit=1`)
            ]);
            const cams = (await camsR.json()).cameras || [];
            const events = (await evtsR.json()).events || [];
            const active = cams.filter(c => c.active).length;
            const alerts = events.filter(e => e.qwen?.violation).length;
            this._hasRecentAlerts = alerts > 0;
            return { active, total: cams.length, alerts };
        } catch(e) {
            return { active: 0, total: 0, alerts: 0 };
        }
    },

    clearChat(render = true) {
        this.history = [];
        this._greeted = false;
        this.sessionId = `chat_${this.userId}_${Date.now()}`;
        this._suggestions = [];
        this._briefEvent = null;
        this._hasRecentAlerts = false;
        this._visibleLimit = 10;
        this._saveConversation();
        if (render) {
            this.render();
            this._sendGreeting();
        }
    },

    // ── LOAD BUSINESS CONTEXT ───────────────────────────────────
    async loadBusinessContext() {
        try {
            const r = await fetch(`${this.API}/api/user/profile?user_id=${this.userId}`);
            if (r.ok) {
                const profile = await r.json();
                if (profile.name) this.userName = profile.name;
                this._businessName = profile.business_name || '';
                const statusEl = document.getElementById('eva-status');
                if (statusEl) {
                    const biz = this._businessName;
                    const camCount = profile.camera_count || profile.cameras?.length || 0;
                    statusEl.textContent = biz
                        ? `${biz} — ${camCount} cámaras`
                        : 'Asistente de seguridad';
                }
            }
        } catch (e) { /* Silenciar */ }
    }
};

// ── Teclado móvil: ocultar bottom-tabs para liberar espacio del chat ──
// Usa visualViewport (iOS Safari + Chrome Android). Cuando el teclado
// reduce la altura visible, se agrega body.keyboard-open para esconder las
// tabs y reposicionar el chat.
function _initKeyboardWatcher() {
    if (window.__kbdWatcher) return;
    window.__kbdWatcher = true;
    const update = () => {
        const vv = window.visualViewport;
        if (!vv) return;
        const open = (window.innerHeight - vv.height) > 80;
        document.body.classList.toggle('keyboard-open', open);
        if (open) {
            const evaWrap = document.getElementById('eva-chat-container');
            if (evaWrap) evaWrap.style.height = vv.height + 'px';
        } else {
            const evaWrap = document.getElementById('eva-chat-container');
            if (evaWrap) evaWrap.style.height = '';
        }
    };
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', update);
        window.visualViewport.addEventListener('scroll', update);
    }
    update();
}

