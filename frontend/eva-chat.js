/**
 * eva-chat.js — Chat con Eva como Sistema Operativo
 * 
 * Maneja la interfaz de chat, el envío de mensajes,
 * la visualización de resultados y el carrusel de frames.
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

    // ── INIT ────────────────────────────────────────────────────
    async init(userId, userName) {
        this.userId = userId;
        this.userName = userName || '';
        this.sessionId = `chat_${userId}_${Date.now()}`;
        this._greeted = false;
        await this.loadBusinessContext();
        this.render();
        setTimeout(() => this.sendMessageToBackend('__greet__'), 300);
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

        // Si ya hay historial, renderizar los mensajes
        let messagesHtml = '';
        if (this.history && this.history.length > 0) {
            for (const msg of this.history) {
                const isUser = msg.role === 'user';
                const bg = isUser ? 'var(--bg-tertiary)' : 'var(--bg-secondary)';
                const align = isUser ? 'flex-end' : 'flex-start';
                const br = isUser ? '12px 12px 4px 12px' : '12px 12px 12px 4px';
                const text = (msg.content || '').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
                let formatted = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                let imgHtml = '';
                if (msg.image_url && msg.image_url.length > 10) {
                    let imgSrc = msg.image_url;
                    if (imgSrc.startsWith('/eva-image/')) {
                        imgSrc = EvaChat.API + imgSrc;
                    }
                    imgHtml = '<div style="margin-top:8px;"><img src="' + imgSrc + '" style="width:100%;max-height:300px;object-fit:contain;border-radius:8px;background:#0a0a0a;cursor:pointer;" onclick="this.style.maxHeight=this.style.maxHeight===\'300px\'?\'none\':\'300px\'"></div>';
                }
                messagesHtml += '<div style="display:flex;justify-content:' + align + ';margin-bottom:10px;">' +
                    '<div style="max-width:85%;background:' + bg + ';border-radius:' + br + ';padding:12px 16px;font-size:0.92rem;line-height:1.5;">' +
                    formatted + imgHtml + '</div></div>';
            }
        }

        // Sugerencias rápidas como botones (solo si hay cámaras)
        let suggestionsHtml = '';
        if (this._suggestions && this._suggestions.length > 0) {
            suggestionsHtml = '<div class="eva-suggestions">';
            this._suggestions.forEach(s => {
                const safe = s.replace(/'/g, "\\'").replace(/"/g, '&quot;');
                suggestionsHtml += '<button class="suggestion-btn" onclick="EvaChat.sendSuggestion(\'' + safe + '\')">' + safe + '</button>';
            });
            suggestionsHtml += '</div>';
        }

        chatEl.innerHTML = `
            <div class="eva-chat-header">
                <div class="eva-avatar">🤖</div>
                <div class="eva-header-info">
                    <div class="eva-name">Eva</div>
                    <div class="eva-status" id="eva-status">Asistente de seguridad</div>
                </div>
                <button class="eva-clear-btn" onclick="EvaChat.clearChat()" title="Nueva conversación">🔄</button>
            </div>
            <div class="eva-messages" id="eva-messages">
                ${messagesHtml || '<div class="eva-welcome"><div class="eva-welcome-icon">👋</div><div class="eva-welcome-text">Conectando con Eva...</div></div>'}
            </div>
            ${suggestionsHtml}
            <div class="eva-input-area">
                <div class="eva-input-row">
                    <input id="eva-input" placeholder="Escribe un mensaje a Eva..." autocomplete="off"
                           onkeypress="if(event.key==='Enter') EvaChat.sendMessage()">
                    <button class="eva-send-btn" id="eva-send-btn" onclick="EvaChat.sendMessage()">↑</button>
                </div>
            </div>
        `;
        setTimeout(() => { const input = document.getElementById('eva-input'); if (input) input.focus(); }, 300);
    },

    async sendMessage() {
        const input = document.getElementById('eva-input');
        const msg = input?.value.trim();
        if (!msg || this.isLoading) return;
        input.value = '';
        this.isLoading = true;
        const sendBtn = document.getElementById('eva-send-btn');
        if (sendBtn) sendBtn.disabled = true;
        this.addMessage('user', msg);
        this.showTyping();
        try {
            const r = await fetch(`${this.API}/config/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: this.userId,
                    message: msg,
                    session_id: this.sessionId,
                    user_name: this.userName || '',
                    business_name: this._businessName || ''
                })
            });
            const data = await r.json();
            this.hideTyping();
            if (data.success) {
                this.history.push({ role: 'user', content: msg });
                this.history.push({ role: 'assistant', content: data.response, image_url: data.image_url || '' });
                this.addMessage('assistant', data.response, data.events_found);
                if (data.image_url) this.addImageMessage(data.image_url);
                if (data.suggestions && data.suggestions.length > 0) {
                    this._suggestions = data.suggestions;
                }
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

    async sendMessageToBackend(msg) {
        try {
            const r = await fetch(`${this.API}/config/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: this.userId,
                    message: msg,
                    session_id: this.sessionId,
                    user_name: this.userName || '',
                    business_name: this._businessName || ''
                })
            });
            const data = await r.json();
            if (data.success) {
                const welcomeEl = document.querySelector('.eva-welcome');
                if (welcomeEl) welcomeEl.remove();
                this.addMessage('assistant', data.response, data.events_found);
                if (data.image_url) this.addImageMessage(data.image_url);
                // Guardar sugerencias del backend
                if (data.suggestions && data.suggestions.length > 0) {
                    this._suggestions = data.suggestions;
                    this.render(); // Re-render para mostrar botones
                }
                this.loadBusinessContext();
            }
        } catch (e) { console.error('Eva chat error:', e); }
    },

    addImageMessage(imgUrl) {
        const container = document.getElementById('eva-messages');
        if (!container) return;
        let fullUrl = imgUrl;
        if (imgUrl.startsWith('/eva-image/')) {
            fullUrl = this.API + imgUrl;
        }
        const imgDiv = document.createElement('div');
        imgDiv.className = 'eva-msg assistant';
        imgDiv.innerHTML = `<div class="eva-bubble"><img src="${fullUrl}" style="max-width:100%;border-radius:8px;cursor:pointer" onload="EvaChat.scrollToBottom()" onclick="this.style.maxWidth=this.style.maxWidth==='100%'?'300px':'100%'"></div>`;
        container.appendChild(imgDiv);
        // Scroll immediately + after image loads
        this.scrollToBottom();
        setTimeout(() => this.scrollToBottom(), 100);
        setTimeout(() => this.scrollToBottom(), 500);
    },

    sendSuggestion(text) {
        const input = document.getElementById('eva-input');
        if (input) { input.value = text; this.sendMessage(); }
    },

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

    renderEventCarousel(events) {
        if (!events || events.length === 0) return '';
        let html = `<div class="eva-carousel"><div class="carousel-title">📸 ${events.length} frame(s) encontrado(s)</div><div class="carousel-scroll">`;
        for (const evt of events.slice(0, 10)) {
            const anomalyClass = evt.anomaly ? 'anomaly' : '';
            html += `<div class="carousel-card ${anomalyClass}" onclick="EvaChat.openEventDetail('${evt.event_id}')"><div class="card-time">📅 ${evt.datetime || ''}</div><div class="card-camera">📷 ${evt.camera_name || ''}</div><div class="card-frame"><img src="${this.API}${evt.frame_url}" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.5em%22 font-size=%2240%22>📷</text></svg>'" loading="lazy"></div><div class="card-desc">${this.escapeHtml(evt.description || '').substring(0, 80)}</div><div class="card-persons">👥 ${evt.persons || 0} personas</div>${evt.anomaly ? '<div class="card-anomaly-badge">⚠️ Anomalía</div>' : ''}</div>`;
        }
        html += `</div></div>`;
        return html;
    },

    async openEventDetail(eventId) {
        try {
            const r = await fetch(`${this.API}/api/events/${eventId}?user_id=${this.userId}`);
            if (!r.ok) return;
            const event = await r.json();
            const modal = document.createElement('div');
            modal.className = 'eva-event-modal';
            modal.innerHTML = `<div class="modal-overlay" onclick="this.parentElement.remove()"></div><div class="modal-content"><div class="modal-header"><span>📅 ${event.datetime || ''} — ${event.camera_name || event.camera_id}</span><button onclick="this.closest('.eva-event-modal').remove()">✕</button></div><div class="modal-body"><img src="${this.API}${event.frame_url || '/api/event-frame/' + eventId + '?user_id=' + this.userId}" onerror="this.style.display='none'" style="width:100%;border-radius:8px;margin-bottom:12px"><div class="event-description">${this.formatText(event.qwen_analysis?.description || event.description || '')}</div><div class="event-meta"><span>👥 ${event.qwen_analysis?.persons || 0} personas</span><span>📷 ${event.camera_name || event.camera_id}</span>${event.qwen_analysis?.anomaly ? '<span class="anomaly-tag">⚠️ Anomalía</span>' : ''}</div><div class="feedback-buttons"><button class="feedback-btn confirm" onclick="EvaChat.sendFeedback('${eventId}', true, this)">✅ Alerta real</button><button class="feedback-btn dismiss" onclick="EvaChat.sendFeedback('${eventId}', false, this)">❌ Falsa alarma</button></div></div></div>`;
            document.body.appendChild(modal);
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

    showTyping() {
        const container = document.getElementById('eva-messages');
        if (!container) return;
        const typing = document.createElement('div');
        typing.className = 'eva-msg assistant typing-indicator';
        typing.id = 'eva-typing';
        typing.innerHTML = `<div class="eva-bubble"><span>●</span><span>●</span><span>●</span></div>`;
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

    scrollToBottom() {
        const c = document.getElementById('eva-messages');
        if (!c) return;
        c.scrollTop = c.scrollHeight;
        setTimeout(() => { c.scrollTop = c.scrollHeight; }, 50);
        setTimeout(() => { c.scrollTop = c.scrollHeight; }, 200);
        setTimeout(() => { c.scrollTop = c.scrollHeight; }, 500);
    },

    clearChat() { this.history = []; this._greeted = false; this.sessionId = `chat_${this.userId}_${Date.now()}`; this.render(); },

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
