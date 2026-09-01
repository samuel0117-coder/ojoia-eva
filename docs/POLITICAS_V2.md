# Políticas de Uso y Privacidad — OjoIA (Versión 2)

> Vigente desde: 2026-09-01 · Este documento corresponde al consentimiento
> `consent_terms_version: "v2"` registrado al crear una cuenta.

---

## 1. Qué es OjoIA

OjoIA es un asistente de vigilancia con inteligencia artificial. Tu cámara
OjoIA (o una cámara IP compatible que registres) envía imágenes a nuestro
servidor para analizarlas según **las reglas que tú defines**, y te notifica
solo cuando una de esas reglas se viola.

## 2. Descubrimiento de cámaras en tu red (cláusula v2)

Al aceptar estas políticas autorizas específicamente:

- **Qué hacemos**: cuando TÚ lo solicitas (por ejemplo, diciéndole a Eva
  "escanear mi red" o "tengo una Hikvision"), tu cámara OjoIA — que está
  dentro de tu propia red local — busca otros dispositivos de cámara IP
  (Hikvision, Dahua, TP-Link, etc.) mediante protocolos estándar de cámaras
  (SSDP/ONVIF).
- **Qué registramos de cada cámara encontrada**: su dirección IP local,
  puerto, marca y modelo. Nada más.
- **Qué NO hacemos**:
  - No escaneamos otros tipos de dispositivos (televisores, teléfonos,
    computadoras, impresoras) — solo dispositivos que se identifican como
    cámaras o grabadores de video.
  - No hacemos escaneo genérico de puertos de tu red.
  - No escaneamos automáticamente: **solo cuando tú nos lo pides** en la
    conversación, y los resultados se eliminan a las 48 horas si no los usas.
- **Credenciales de tus cámaras IP**: si decides vigilar una cámara de otra
  marca, nos das su usuario y clave. Se guardan cifradas en permisos
  restringidos (solo el servidor puede leerlas), únicamente para extraer
  imágenes de esa cámara. Nunca aparecen en logs, notificaciones ni eventos.

## 3. Imágenes y análisis

- Las imágenes capturadas por tus cámaras se transmiten cifradas (HTTPS) a
  nuestros servidores para analizarlas con modelos de IA (detección de
  personas/objetos y descripción de escena).
- Los eventos con evidencia (imágenes del momento de una alerta) se
  conservan según tu plan y pueden eliminarse a petición tuya en cualquier
  momento escribiéndonos.
- No vendemos, compartimos ni cedemos tus imágenes a terceros. Se usan
  únicamente para la vigilancia que TÚ configuraste.

## 4. Notificaciones

- Usamos Firebase Cloud Messaging para enviarte las alertas de tus reglas.
- Puedes desactivarlas desde la app en cualquier momento.

## 5. Tus derechos

- Puedes solicitar la eliminación de tu cuenta y todos tus datos
  (imágenes, eventos, configuración) escribiéndonos; se eliminan en un
  plazo máximo de 72 horas.
- Puedes revocar el consentimiento de escaneo de red en cualquier momento;
  la función simplemente dejará de estar disponible para tu cuenta.

## 6. Cambios a estas políticas

- Si cambiamos esta política, la versión nueva requerirá aceptación
  explícita tuya antes de aplicar (nunca consentimiento implícito).

---

*OjoIA — República Dominicana · soporte@ojoia.com.do*
