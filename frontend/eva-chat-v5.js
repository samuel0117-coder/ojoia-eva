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
        this.userId = userId;
        this.userName = userName || '';
        this._sessionKey = `eva_session_${userId}`;
        const savedSessionId = localStorage.getItem(this._sessionKey);
        this.sessionId = savedSessionId || `eva_${userId}_single`;
        localStorage.setItem(this._sessionKey, this.sessionId);
        this._greeted = false;
        this._visibleLimit = 10;
        this._loadingOlder = false;
        this._hasRecentAlerts = false;
        this._loadSavedConversation();
        await this.loadBusinessContext();
        if (this.history.length) {
            this.render();
        } else {
            this._renderShell('Conectando con Eva...');
            await this._sendGreeting(true);
        }
    },

    _renderShell(statusText) {
        const c = document.getElementById('app-content');
        if (!c) return;
        const name = (this.userName || 'amigo').split(' ')[0];
        const greeting = statusText || `Hola, ${name}.\n\nEstoy lista para ayudarte.\n\n¿Qué necesitas?`;
        this.history = [{ role: 'assistant', content: greeting, summary: true }];
        this.render();
    },

    // ── GREETING ────────────────────────────────────────────────
    async _sendGreeting(force = false) {
        if (!force && this.history.length) return;
        try {
            const brief = await this._buildDailyBrief();
            const greeting = brief.text;
            this.history = [{ role: 'assistant', content: greeting, summary: true, events: brief.events || [] }];
            this._hasRecentAlerts = brief.alerts > 0;
            this._briefEvent = brief.event || null;
        } catch (e) {
            const name = (this.userName || 'amigo').split(' ')[0];
            this.history = [{ role: 'assistant', content: `Hola, ${name}.\n\nHoy tu negocio se ve tranquilo.\n\nRevisé tus cámaras y no encontré alertas reales.\n¿Quieres ver qué está pasando ahora?`, summary: true }];
        }
        this._saveConversation();
        this.render();
    },

    // ── RENDER ──────────────────────────────────────────────────
    render() {
        const c = document.getElementById('app-content');
        if (!c) return;
        let chatEl = document.getElementById('eva-chat-container');
        if (!chatEl) {
            chatEl = document.createElement('div');
            chatEl.id = 'eva-chat-container';
            chatEl.className = 'eva-chat-container';
            c.appendChild(chatEl);
        }
        chatEl.style.cssText = 'height:100%;min-height:0;flex:none;display:flex;flex-direction:column;';

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
                const bubbleStyle = isUser
                    ? 'background:var(--accent);color:#fff;'
                    : 'background:rgba(44,44,46,0.92);border:1px solid rgba(255,255,255,0.06);';
                messagesHtml += `<div style="display:flex;justify-content:${align};margin-top:${msgMargin};margin-bottom:2px;">` +
                    `<div style="max-width:${isUser ? '80%' : '92%'};${bubbleStyle}border-radius:${br};padding:12px 15px;font-size:0.94rem;line-height:1.5;box-shadow:0 1px 2px rgba(0,0,0,0.2);">` +
                    formatted + imgHtml + eventsHtml + '</div></div>';
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
             <div class="eva-messages" id="eva-messages" style="flex:1;min-height:0;overflow-y:auto;padding:12px 16px 8px;display:flex;flex-direction:column;gap:0;">
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
        const input = document.getElementById('eva-input');
        const msg = input?.value.trim();
        const intentText = input?.dataset.intentText || msg;
        if (!msg || this.isLoading) return;
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
            const r = await fetch(`${this.API}/config/chat`, {
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
            const data = await r.json();
            if (data.success) {
                this.sessionId = data.sessionId || this.sessionId;
                if (this._isOsIntentText(intentText) && !this._isInstallCameraIntent(intentText)) {
                    this.sessionId = data.sessionId || this.sessionId;
                    this.history = this.history.filter(m => !(m.role === 'assistant' && this._isSetupArtifact(m.content || '')));
                }
                localStorage.setItem(this._sessionKey, this.sessionId);
                this.history.push({ role: 'user', content: msg });
                this.history.push({ role: 'assistant', content: data.response, image_url: data.image_url || '', events: data.events_found || data.events || [] });
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

    // ── SEND SUGGESTION ─────────────────────────────────────────
    sendSuggestion(text) {
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
        const container = document.getElementById('eva-messages');
        if (!container) return;
        const msgDiv = document.createElement('div');
        msgDiv.className = `eva-msg ${role}`;
        let html = `<div class="eva-bubble">`;
        if (role === 'assistant') html += this.formatText(text);
        else html += `<span>${this.escapeHtml(text)}</span>`;
        html += `</div>`;
        if (events && events.length > 0) html += this.renderEventCarousel(events);
        msgDiv.innerHTML = html;
        container.appendChild(msgDiv);
        this.scrollToBottom();
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

    // ── EVENT CAROUSEL ──────────────────────────────────────────
    renderEventCarousel(events) {
        if (!events || events.length === 0) return '';
        let html = `<div class="eva-carousel"><div class="carousel-title">📸 ${events.length} momento${events.length === 1 ? '' : 's'} encontrado${events.length === 1 ? '' : 's'}</div><div class="carousel-scroll">`;
        for (const evt of events.slice(0, 10)) {
            const anomalyClass = evt.anomaly || this._isAlertEvent(evt) ? 'anomaly' : '';
            const desc = EvaChat.cleanEventDescription(evt.summary || evt.description || '');
            const persons = evt.persons ?? 0;
            const framesCount = evt.frames_count || (evt.frames || []).length || 0;
            const imgSrc = evt.thumb_url || evt.frame_url ? (evt.thumb_url || this.API + evt.frame_url) : '';
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
        if (typeof App !== 'undefined' && App.go) {
            App._pendingEventId = eventId;
            if (App.go('events', eventId)) {
                return;
            }
        }
        try {
            const r = await fetch(`${this.API}/api/events/${eventId}?user_id=${this.userId}`);
            if (!r.ok) return;
            const event = await r.json();
            const qa = event.qwen_analysis || {};
            const qjson = event.qwen_json || {};
            const desc = EvaChat.cleanEventDescription(qa.description || qa.summary || qjson.description || qjson.summary || event.description || event.summary || '');
            const persons = qa.persons ?? event.persons ?? qjson.details?.persons ?? event.yolo?.count ?? '—';
            const videoUrl = event.video_file ? `${this.API}/api/events/${eventId}/video.mp4?user_id=${this.userId}` : '';
            const frameCount = Array.isArray(event.frames) ? event.frames.length : 0;
            const frameUrl = frameCount ? `${this.API}/api/events/${eventId}/frame/0?user_id=${this.userId}` : (event.frame_url ? (event.frame_url.startsWith('http') ? event.frame_url : this.API + event.frame_url) : `${this.API}/api/event-frame/${eventId}?user_id=${this.userId}`);
            const modal = document.createElement('div');
            modal.className = 'eva-event-modal';
            const videoHtml = videoUrl ? `<video src="${videoUrl}" controls autoplay muted playsinline style="width:100%;border-radius:8px;margin-bottom:12px;background:#000"></video>` : '';
            const fallbackImgHtml = !videoUrl && !frameCount ? `<img src="${frameUrl}" onerror="this.style.display='none'" style="width:100%;border-radius:8px;margin-bottom:12px;background:#000">` : '';
            const carouselHtml = frameCount ? `<div style="text-align:center;margin-bottom:8px"><img id="eva-event-frame-img" src="${this.API}/api/events/${eventId}/frame/0?user_id=${this.userId}" style="width:100%;border-radius:8px;background:#000"><div class="meta" id="eva-event-frame-status">1/${frameCount}</div><input id="eva-event-frame-range" type="range" min="0" max="${frameCount - 1}" value="0" oninput="EvaChat._showEventFrame('${eventId}', this.value, ${frameCount})" style="width:100%;margin-top:8px"><button class="eva-frame-play-btn" onclick="EvaChat.toggleEventAutoplay('${eventId}', ${frameCount})" id="eva-frame-play-btn">Pausar video</button></div>` : '';
            const cameraLiveButton = event.camera_id ? `<button class="feedback-btn confirm" onclick="EvaChat.openCameraLive('${event.camera_id}')">📹 Ver cámara en vivo</button>` : '';
            const attentionHits = event.attention_hits || [];
            const isSentinel = event.event_type === 'sentinel' || (qjson.after_hours && qjson.importancia === 'alta');
            const alertTag = isSentinel ? '<span class="anomaly-tag" style="background:var(--warning,#f5a623)">🛡️ Fuera de horario</span>' : (attentionHits.length ? '<span class="anomaly-tag">🔍 Observación</span>' : (event.qwen?.violation || qjson.violation || qa.anomalias?.length ? '<span class="anomaly-tag">⚠️ Alerta</span>' : ''));
            const hitsHtml = attentionHits.length ? `<div style="background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:10px 14px;margin-bottom:12px"><div style="font-size:.78rem;color:var(--warning,#f5a623);font-weight:600;margin-bottom:4px">🔍 Observaciones detectadas:</div><div style="font-size:.85rem;line-height:1.4">${attentionHits.map(h => `• ${this.escapeHtml(h)}`).join('<br>')}</div></div>` : '';
            const sentinelHtml = isSentinel ? `<div style="background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);border-radius:10px;padding:10px 14px;margin-bottom:12px"><div style="font-size:.82rem;color:var(--warning,#f5a623);font-weight:600">🛡️ Modo centinela — Se detectó presencia fuera del horario de trabajo</div></div>` : '';
            modal.innerHTML = `<div class="modal-overlay" onclick="EvaChat.closeEventModal(this.closest('.eva-event-modal'))"></div><div class="modal-content"><div class="modal-header"><span>📅 ${this.escapeHtml(event.datetime || '')} — ${this.escapeHtml(event.camera_name || event.camera_id || '')}</span><button onclick="EvaChat.closeEventModal(this.closest('.eva-event-modal'))">✕</button></div><div class="modal-body">${videoHtml}${fallbackImgHtml}${carouselHtml}<div class="event-description">${this.formatText(desc)}</div>${hitsHtml}${sentinelHtml}<div class="event-meta"><span>👥 ${this.escapeHtml(String(persons))} persona${String(persons) === '1' ? '' : 's'}</span><span>📷 ${this.escapeHtml(event.camera_name || event.camera_id || '')}</span>${alertTag}</div><div class="feedback-buttons">${cameraLiveButton}<button class="feedback-btn confirm" onclick="EvaChat.sendFeedback('${eventId}', true, this)">${attentionHits.length ? '🏷️ Marcar como falta' : '✅ Alerta real'}</button><button class="feedback-btn dismiss" onclick="EvaChat.sendFeedback('${eventId}', false, this)">❌ Falsa alarma</button></div></div></div>`;
            document.body.appendChild(modal);
            if (frameCount) this.startEventAutoplay(eventId, frameCount);
        } catch (e) { console.error('Error loading event detail:', e); }
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
        const eventsR = await fetch(`${this.API}/api/user/events?user_id=${this.userId}&filter=today&limit=20`);
        const events = eventsR.ok ? (await eventsR.json()).events || [] : [];
        const name = (this.userName || 'amigo').split(' ')[0];
        const business = this._businessName || 'tu negocio';
        const activeText = `${status.active} cámara${status.active === 1 ? '' : 's'} activa${status.active === 1 ? '' : 's'}`;
        const alertEvents = events.filter(e => this._isAlertEvent(e));
        const alertCount = alertEvents.length;
        const event = alertEvents[0] || this._pickBriefEvent(events, alertEvents);
        const minimalEvent = event ? this._minimalEvent(event) : null;
        const lastAnalysisAge = event && event.timestamp ? Math.max(0, Math.round((Date.now() / 1000 - event.timestamp) / 60)) : 0;
        let lines = [];

        if (alertCount > 0) {
            lines.push(`Hola, ${name}.`);
            lines.push('');
            lines.push(`Hay algo importante que revisar en ${business}.`);
            lines.push(`Encontré ${alertCount} alerta${alertCount === 1 ? '' : 's'} real${alertCount === 1 ? '' : 'es'} hoy.`);
            lines.push('');
            if (event) {
                lines.push(this._eventSentence(event, true));
                lines.push('¿Quieres ver la alerta?');
            } else {
                lines.push('Revisé tus cámaras y no encontré detalles urgentes.');
                lines.push('¿Quieres ver qué está pasando ahora?');
            }
        } else if (event) {
            lines.push(`Hola, ${name}.`);
            lines.push('');
            lines.push(`Hoy ${business} se ve tranquilo.`);
            lines.push(`Revisé ${activeText} y no encontré alertas reales.`);
            lines.push('');
            lines.push('Último análisis registrado:');
            lines.push(this._eventSentence(event, false));
            if (lastAnalysisAge > 30) lines.push(`No hay análisis nuevo desde hace ${this._formatDuration(lastAnalysisAge)}.`);
            lines.push('¿Quieres verlo?');
        } else {
            lines.push(`Hola, ${name}.`);
            lines.push('');
            lines.push(`Hoy ${business} se ve tranquilo.`);
            lines.push(`Revisé ${activeText} y no encontré alertas reales.`);
            lines.push('');
            lines.push('Todo está bajo control.');
            lines.push('¿Quieres ver qué está pasando ahora?');
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

    _loadSavedConversation() {
        try {
            const saved = JSON.parse(localStorage.getItem(`eva_history_${this.userId}`) || 'null');
            if (saved && Array.isArray(saved.history)) this.history = saved.history;
            else this.history = [];
            if (saved && saved.summary) this._summary = saved.summary;
            else this._summary = '';
        } catch(e) { this.history = []; }
    },

    _saveConversation() {
        try {
            localStorage.setItem(`eva_history_${this.userId}`, JSON.stringify({ history: this.history, summary: this._summary }));
        } catch(e) {}
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
