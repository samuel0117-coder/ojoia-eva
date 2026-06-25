/**
 * OjoIA ESP32-CAM v6.1
 * - POST /devices/announce (sin user_id, usa firmware_id)
 * - GET /camera/config/{id} (poll cada 30s)
 * - POST /ingest/frame (bytes puros con X-Camera-Id header)
 * - HTTPS con setInsecure() para Cloudflare Tunnel
 * - LED auto/manual, flip, calidad, intervalo
 * - NTP, watchdog, portal cautivo AP
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <esp_camera.h>
#include <esp_task_wdt.h>
#include <time.h>
#include <esp32-hal-ledc.h>

#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22
#define LED_PIN          4
#define LEDC_LED_CHANNEL  1  // Canal separado del XCLK (canal 0)

void setLED(uint8_t brightness) {
  ledCurrent = brightness;
  analogWrite(LED_PIN, brightness);
}
function chk(){
  var pw=document.getElementById('pw').value;
  var srv=document.getElementById('srv').value;
  document.getElementById('b1').disabled=!(ss&&pw.length>=4&&srv.startsWith('http'));
}
function save(){
  var b=document.getElementById('b1');b.disabled=true;b.textContent='Guardando...';
  var p=new URLSearchParams();
  p.append('ssid',ss);
  p.append('password',document.getElementById('pw').value);
  p.append('server_url',document.getElementById('srv').value);
  fetch('/save',{method:'POST',body:p,headers:{'Content-Type':'application/x-www-form-urlencoded'}})
  .then(r=>{if(!r.ok)throw 0;})
  .then(()=>{document.getElementById('setupCard').style.display='none';document.getElementById('okCard').style.display='block';setTimeout(()=>window.location.reload(),3000);})
  .catch(()=>{b.disabled=false;b.textContent='Conectar →';});
}
window.onload=scan;
</script></body></html>)RAW";

void setLED(uint8_t brightness) {
  ledCurrent = brightness;
  analogWrite(LED_PIN, brightness);
}

void syncNTP() {
  configTime(NTP_TZ_OFF, 0, NTP_SERVER, "time.cloudflare.com");
  Serial.print("[NTP] Sincronizando...");
  unsigned long t = millis();
  struct tm ti;
  while (!getLocalTime(&ti) && millis() - t < 10000) {
    delay(300);
    Serial.print(".");
  }
  ntpReady = getLocalTime(&ti);
  Serial.println(ntpReady ? " ✓" : " ✗ (sin NTP)");
}

String getTimestamp() {
  if (!ntpReady) return String(millis());
  struct tm ti;
  if (!getLocalTime(&ti)) return String(millis());
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &ti);
  return String(buf);
}

uint8_t avgBrightness(camera_fb_t* fb) {
  if (!fb || fb->len < 100) return 128;
  uint32_t sum = 0;
  int step = fb->len / 100;
  for (int i = 0; i < 100; i++) {
    sum += fb->buf[i * step];
  }
  return (uint8_t)(sum / 100);
}

void autoLED(camera_fb_t* fb) {
  if (!cfg.led_auto) return;
  uint8_t bright = avgBrightness(fb);
  if (bright < LIGHT_LOW_THRESH && ledCurrent == 0) {
    setLED(cfg.led_bright);
    Serial.printf("[LED] Auto ON (bright=%d)\n", bright);
  } else if (bright > LIGHT_HIGH_THRESH && ledCurrent > 0) {
    setLED(0);
    Serial.printf("[LED] Auto OFF (bright=%d)\n", bright);
  }
}

bool loadConfig() {
  prefs.begin(PREF_NS, true);
  bool ok  = prefs.getBool("ok", false);
  String s = prefs.getString("ssid", "");
  String p = prefs.getString("pw", "");
  String u = prefs.getString("srv_url", DEFAULT_SERVER_URL);
  prefs.end();
  if (ok && s.length() > 0) {
    strlcpy(ssid,      s.c_str(), sizeof(ssid));
    strlcpy(password,  p.c_str(), sizeof(password));
    strlcpy(serverUrl, u.c_str(), sizeof(serverUrl));
    return true;
  }
  return false;
}

bool saveConfig(const String& s, const String& p, const String& url) {
  prefs.begin(PREF_NS, false);
  prefs.putBool("ok", true);
  prefs.putString("ssid", s);
  prefs.putString("pw", p);
  prefs.putString("srv_url", url);
  prefs.end();
  return true;
}

void saveCamConfig() {
  prefs.begin(PREF_NS, false);
  prefs.putUInt("interval",    cfg.interval_ms);
  prefs.putUChar("quality",    cfg.quality);
  prefs.putUChar("framesize",  cfg.framesize);
  prefs.putBool("led_auto",    cfg.led_auto);
  prefs.putUChar("led_bright", cfg.led_bright);
  prefs.putBool("h_mirror",    cfg.h_mirror);
  prefs.putBool("v_flip",      cfg.v_flip);
  prefs.putBool("stream_all",  cfg.stream_always);
  prefs.end();
}

void loadCamConfig() {
  prefs.begin(PREF_NS, true);
  cfg.interval_ms   = prefs.getUInt("interval",    DEFAULT_INTERVAL_MS);
  cfg.quality       = prefs.getUChar("quality",    DEFAULT_QUALITY);
  cfg.framesize     = (framesize_t)prefs.getUChar("framesize", DEFAULT_FRAMESIZE);
  cfg.led_auto      = prefs.getBool("led_auto",    DEFAULT_LED_AUTO);
  cfg.led_bright    = prefs.getUChar("led_bright", DEFAULT_LED_BRIGHT);
  cfg.h_mirror      = prefs.getBool("h_mirror",    DEFAULT_H_MIRROR);
  cfg.v_flip        = prefs.getBool("v_flip",      DEFAULT_V_FLIP);
  cfg.stream_always = prefs.getBool("stream_all",  true);
  prefs.end();
  if (cfg.interval_ms < 500)   cfg.interval_ms = 500;
  if (cfg.interval_ms > 30000) cfg.interval_ms = 30000;
  if (cfg.quality < 4)         cfg.quality = 4;
  if (cfg.quality > 63)        cfg.quality = 63;
}

void generateID() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(cameraID, sizeof(cameraID), "OJO-%02X%02X%02X", mac[3], mac[4], mac[5]);
}

void applyCameraSettings() {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return;
  s->set_framesize(s,  (framesize_t)cfg.framesize);
  s->set_quality(s,    cfg.quality);
  s->set_hmirror(s,    cfg.h_mirror ? 1 : 0);
  s->set_vflip(s,      cfg.v_flip   ? 1 : 0);
  s->set_brightness(s,   0);
  s->set_contrast(s,     0);
  s->set_saturation(s,   0);
  s->set_whitebal(s,     1);
  s->set_awb_gain(s,     1);
  s->set_wb_mode(s,      0);
  s->set_exposure_ctrl(s,1);
  s->set_aec2(s,         1);
  s->set_gain_ctrl(s,    1);
  s->set_agc_gain(s,     0);
  s->set_gainceiling(s, GAINCEILING_4X);
  s->set_bpc(s,          1);
  s->set_wpc(s,          1);
  s->set_raw_gma(s,      1);
  s->set_lenc(s,         1);
  s->set_dcw(s,          1);
  s->set_colorbar(s,     0);
  Serial.printf("[CAM] Ajustes: size=%d quality=%d mirror=%d flip=%d\n",
                cfg.framesize, cfg.quality, cfg.h_mirror, cfg.v_flip);
}

bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk  = XCLK_GPIO_NUM;
  c.pin_pclk  = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href  = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn  = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz  = 20000000;
  c.pixel_format  = PIXFORMAT_JPEG;
  c.frame_size    = (framesize_t)cfg.framesize;
  c.jpeg_quality  = cfg.quality;
  c.fb_count      = 2;

  if (esp_camera_init(&c) != ESP_OK) return false;

  for (int i = 0; i < 3; i++) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
    delay(100);
  }
  applyCameraSettings();
  return true;
}

bool connectWiFi() {
  Serial.printf("[WiFi] Conectando a '%s'...\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  WiFi.setSleep(false);
  unsigned long t = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] ✓ IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
  }
  return false;
}

void startAP() {
  WiFi.disconnect(true);
  delay(200);
  WiFi.mode(WIFI_AP);
  delay(200);
  IPAddress apIP(192, 168, 4, 1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  String apName = "OjoIA-" + String(cameraID);
  WiFi.softAP(apName.c_str(), AP_PASSWORD);
  dns.start(53, "*", apIP);

  portalServer.on("/", HTTP_GET, []() {
    portalServer.send_P(200, "text/html", PORTAL_HTML);
  });
  portalServer.on("/scan", HTTP_GET, []() {
    if (WiFi.getMode() == WIFI_AP) WiFi.mode(WIFI_AP_STA);
    int n = WiFi.scanNetworks(false, false);
    String json = "{\"networks\":[";
    for (int i = 0; i < n; i++) {
      if (i) json += ",";
      String s = WiFi.SSID(i);
      s.replace("\"", "\\\"");
      json += "{\"ssid\":\"" + s + "\",\"rssi\":" + String(WiFi.RSSI(i)) +
              ",\"sec\":" + String(WiFi.encryptionType(i) != WIFI_AUTH_OPEN ? 1 : 0) + "}";
    }
    json += "]}";
    WiFi.scanDelete();
    WiFi.mode(WIFI_AP);
    portalServer.sendHeader("Access-Control-Allow-Origin", "*");
    portalServer.send(200, "application/json", json);
  });
  portalServer.on("/save", HTTP_POST, []() {
    String s   = portalServer.arg("ssid");
    String p   = portalServer.arg("password");
    String url = portalServer.arg("server_url");
    s.trim(); p.trim(); url.trim();
    if (s.isEmpty() || p.length() < 4 || !url.startsWith("http")) {
      portalServer.send(400, "text/plain", "Datos inválidos");
      return;
    }
    saveConfig(s, p, url);
    portalServer.send(200, "text/plain", "OK");
    portalServer.client().flush();
    delay(500);
    ESP.restart();
  });
  portalServer.onNotFound([]() {
    if (portalServer.uri() == "/scan" || portalServer.uri() == "/save") {
      portalServer.send(404);
      return;
    }
    portalServer.sendHeader("Location", "http://192.168.4.1/");
    portalServer.send(302);
  });
  portalServer.begin();
  Serial.printf("[AP] ✓ SSID: %s | Pass: %s\n", apName.c_str(), AP_PASSWORD);
}

void setupConfigServer() {
  configServer.on("/status", HTTP_GET, []() {
    String ts = getTimestamp();
    String json = "{";
    json += "\"camera_id\":\"" + String(cameraID) + "\",";
    json += "\"firmware\":\"v6.1\",";
    json += "\"timestamp\":\"" + ts + "\",";
    json += "\"wifi_rssi\":" + String(WiFi.RSSI()) + ",";
    json += "\"interval_ms\":" + String(cfg.interval_ms) + ",";
    json += "\"quality\":" + String(cfg.quality) + ",";
    json += "\"framesize\":" + String(cfg.framesize) + ",";
    json += "\"led_auto\":" + String(cfg.led_auto ? "true" : "false") + ",";
    json += "\"led_bright\":" + String(cfg.led_bright) + ",";
    json += "\"led_current\":" + String(ledCurrent) + ",";
    json += "\"h_mirror\":" + String(cfg.h_mirror ? "true" : "false") + ",";
    json += "\"v_flip\":" + String(cfg.v_flip ? "true" : "false") + ",";
    json += "\"stream_always\":" + String(cfg.stream_always ? "true" : "false") + ",";
    json += "\"ntp_ready\":" + String(ntpReady ? "true" : "false");
    json += "}";
    configServer.sendHeader("Access-Control-Allow-Origin", "*");
    configServer.send(200, "application/json", json);
  });

  configServer.on("/config", HTTP_POST, []() {
    configServer.sendHeader("Access-Control-Allow-Origin", "*");
    if (!configServer.hasArg("plain")) {
      configServer.send(400, "application/json", "{\"error\":\"no body\"}");
      return;
    }
    String body = configServer.arg("plain");
    auto getInt = [&](const char* key, int def) -> int {
      String k = "\"" + String(key) + "\":";
      int pos = body.indexOf(k);
      if (pos < 0) return def;
      pos += k.length();
      while (pos < (int)body.length() && (body[pos] == ' ')) pos++;
      int end = pos;
      while (end < (int)body.length() && body[end] != ',' && body[end] != '}') end++;
      return body.substring(pos, end).toInt();
    };
    auto getBool = [&](const char* key, bool def) -> bool {
      String k = "\"" + String(key) + "\":";
      int pos = body.indexOf(k);
      if (pos < 0) return def;
      pos += k.length();
      while (pos < (int)body.length() && (body[pos] == ' ')) pos++;
      return body.substring(pos, pos + 4) == "true";
    };

    bool changed    = false;
    bool reinitCam  = false;

    int q = getInt("quality", -1);
    if (q >= 4 && q <= 63 && q != cfg.quality) { cfg.quality = q; changed = true; }

    int fs = getInt("framesize", -1);
    if (fs >= 0 && fs <= 13 && fs != cfg.framesize) { cfg.framesize = fs; changed = true; reinitCam = true; }

    int iv = getInt("interval_ms", -1);
    if (iv >= 200 && iv <= 30000 && (uint32_t)iv != cfg.interval_ms) { cfg.interval_ms = iv; changed = true; }

    int lb = getInt("led_bright", -1);
    if (lb >= 0 && lb <= 255 && lb != cfg.led_bright) { cfg.led_bright = lb; changed = true; }

    if (body.indexOf("\"led_auto\"") >= 0) {
      bool la = getBool("led_auto", cfg.led_auto);
      if (la != cfg.led_auto) { cfg.led_auto = la; changed = true; }
    }
    if (body.indexOf("\"h_mirror\"") >= 0) {
      bool hm = getBool("h_mirror", cfg.h_mirror);
      if (hm != cfg.h_mirror) { cfg.h_mirror = hm; changed = true; }
    }
    if (body.indexOf("\"v_flip\"") >= 0) {
      bool vf = getBool("v_flip", cfg.v_flip);
      if (vf != cfg.v_flip) { cfg.v_flip = vf; changed = true; }
    }
    if (body.indexOf("\"stream_always\"") >= 0) {
      bool sa = getBool("stream_always", cfg.stream_always);
      if (sa != cfg.stream_always) { cfg.stream_always = sa; changed = true; }
    }

    if (body.indexOf("\"led_on\":true") >= 0) {
      setLED(cfg.led_bright);
    } else if (body.indexOf("\"led_on\":false") >= 0) {
      setLED(0);
    }

    if (changed) {
      saveCamConfig();
      if (reinitCam) {
        sensor_t* s = esp_camera_sensor_get();
        if (s) s->set_framesize(s, (framesize_t)cfg.framesize);
      }
      applyCameraSettings();
      Serial.println("[CONFIG] ✓ Actualizado desde app");
    }

    configServer.send(200, "application/json",
      "{\"ok\":true,\"camera_id\":\"" + String(cameraID) + "\"}");
  });

  configServer.on("/snapshot", HTTP_GET, []() {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
      configServer.send(503, "text/plain", "Camera error");
      return;
    }
    configServer.sendHeader("Access-Control-Allow-Origin", "*");
    configServer.sendHeader("X-Camera-Id", cameraID);
    configServer.sendHeader("X-Timestamp", getTimestamp());
    configServer.send_P(200, "image/jpeg", (const char*)fb->buf, fb->len);
    esp_camera_fb_return(fb);
  });

  configServer.onNotFound([]() {
    if (configServer.method() == HTTP_OPTIONS) {
      configServer.sendHeader("Access-Control-Allow-Origin", "*");
      configServer.sendHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
      configServer.sendHeader("Access-Control-Allow-Headers", "*");
      configServer.send(204);
    } else {
      configServer.send(404);
    }
  });

  configServer.begin();
  Serial.printf("[API] Config server en http://%s:81\n", WiFi.localIP().toString().c_str());
}

void pollServerConfig() {
  HTTPClient http;
  String url = String(serverUrl) + "/camera/config/" + String(cameraID);
  http.begin(url);
  http.addHeader("X-Camera-Id", cameraID);
  http.setTimeout(5000);
  int code = http.GET();

  if (code == 200) {
    String body = http.getString();
    http.end();

    auto getInt = [&](const char* key, int def) -> int {
      String k = "\"" + String(key) + "\":";
      int pos = body.indexOf(k);
      if (pos < 0) return def;
      pos += k.length();
      while (pos < (int)body.length() && (body[pos] == ' ')) pos++;
      int end = pos;
      while (end < (int)body.length() && body[end] != ',' && body[end] != '}') end++;
      return body.substring(pos, end).toInt();
    };
    auto getBool = [&](const char* key, bool def) -> bool {
      String k = "\"" + String(key) + "\":";
      int pos = body.indexOf(k);
      if (pos < 0) return def;
      pos += k.length();
      while (pos < (int)body.length() && (body[pos] == ' ')) pos++;
      return body.substring(pos, pos + 4) == "true";
    };

    bool changed = false;
    int q = getInt("quality", -1);
    if (q >= 4 && q <= 63 && q != cfg.quality) { cfg.quality = q; changed = true; }
    int iv = getInt("interval_ms", -1);
    if (iv >= 200 && iv <= 30000 && (uint32_t)iv != cfg.interval_ms) { cfg.interval_ms = iv; changed = true; }
    int lb = getInt("led_bright", -1);
    if (lb >= 0 && lb <= 255 && lb != cfg.led_bright) { cfg.led_bright = lb; changed = true; }
    if (body.indexOf("\"led_auto\"") >= 0) {
      bool la = getBool("led_auto", cfg.led_auto);
      if (la != cfg.led_auto) { cfg.led_auto = la; changed = true; }
    }
    if (body.indexOf("\"h_mirror\"") >= 0) {
      bool hm = getBool("h_mirror", cfg.h_mirror);
      if (hm != cfg.h_mirror) { cfg.h_mirror = hm; changed = true; }
    }
    if (body.indexOf("\"v_flip\"") >= 0) {
      bool vf = getBool("v_flip", cfg.v_flip);
      if (vf != cfg.v_flip) { cfg.v_flip = vf; changed = true; }
    }

    if (changed) {
      saveCamConfig();
      applyCameraSettings();
      Serial.println("[CONFIG] ✓ Config actualizada desde servidor");
    }
  } else {
    http.end();
    if (code != 0) {
      Serial.printf("[CONFIG] Server response: %d\n", code);
    }
  }
}

void announceDevice() {
  static unsigned long lastAnnounceAttempt = 0;
  if (announced && millis() - lastAnnounceAttempt < 300000) return;

  lastAnnounceAttempt = millis();

  HTTPClient http;
  String url = String(serverUrl) + "/devices/announce";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(5000);

  String ip = WiFi.localIP().toString();
  String ts = getTimestamp();
  String payload = "{\"camera_id\":\"" + String(cameraID) +
                   "\",\"firmware_id\":\"" + String(cameraID) +
                   "\",\"ip\":\"" + ip +
                   "\",\"local_config_url\":\"http://" + ip + ":81\"" +
                   ",\"firmware\":\"v6.1\"" +
                   ",\"rssi\":" + String(WiFi.RSSI()) +
                   ",\"uptime\":" + String(millis() / 1000) +
                   ",\"timestamp\":\"" + ts + "\"}";

  int code = http.POST(payload);
  http.end();

  if (code >= 200 && code < 300) {
    Serial.println("[ANNOUNCE] ✓ Registrado");
    announced = true;
  } else {
    Serial.printf("[ANNOUNCE] ✗ HTTP %d (reintentando)\n", code);
    announced = false;
  }
}

bool sendFrame(uint8_t* buf, size_t len) {
  String urlStr = String(serverUrl);
  bool isHttps  = urlStr.startsWith("https://");
  String hostPort = urlStr.substring(isHttps ? 8 : 7);
  int slash = hostPort.indexOf('/');
  if (slash > 0) hostPort = hostPort.substring(0, slash);

  String host = hostPort;
  int port = isHttps ? 443 : 80;
  int colon = hostPort.indexOf(':');
  if (colon > 0) {
    host = hostPort.substring(0, colon);
    port = hostPort.substring(colon + 1).toInt();
  }

  WiFiClientSecure secureClient;
  WiFiClient plainClient;
  WiFiClient* client = isHttps ? &secureClient : &plainClient;

  if (isHttps) {
    secureClient.setInsecure();
  }

  if (!client->connect(host.c_str(), port)) {
    Serial.printf("[TX] ✗ No connection to %s:%d\n", host.c_str(), port);
    return false;
  }

  if (currentSessionId.length() == 0) {
    currentSessionId = String(cameraID) + "-" + String(millis());
  }

  String ts = getTimestamp();
  String path = "/ingest/frame";

  client->printf(
    "POST %s HTTP/1.1\r\n"
    "Host: %s\r\n"
    "X-Camera-Id: %s\r\n"
    "X-Session-Id: %s\r\n"
    "X-Timestamp: %s\r\n"
    "X-Firmware: v6.1\r\n"
    "X-Quality: %d\r\n"
    "X-Framesize: %d\r\n"
    "X-Led: %d\r\n"
    "X-Uptime: %lu\r\n"
    "Content-Type: image/jpeg\r\n"
    "Content-Length: %u\r\n"
    "Connection: close\r\n\r\n",
    path.c_str(), host.c_str(),
    cameraID, currentSessionId.c_str(),
    ts.c_str(),
    cfg.quality, cfg.framesize, ledCurrent,
    millis() / 1000,
    len
  );

  size_t sent = client->write(buf, len);

  unsigned long t = millis();
  while (!client->available() && millis() - t < 5000) delay(10);

  String resp = "";
  if (client->available()) {
    resp = client->readStringUntil('\n');
    while (client->available()) client->read();
  }
  client->stop();

  bool ok = (sent == len && resp.startsWith("HTTP/1.1 2"));
  if (ok) {
    Serial.printf("[TX] ✓ %u bytes | %s\n", len, ts.c_str());
  } else {
    Serial.printf("[TX] ✗ sent=%u/%u | resp: %s\n", sent, len, resp.c_str());
  }
  return ok;
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  analogWrite(LED_PIN, 0);
  Serial.begin(115200);
  delay(500);
  Serial.println("\n═══ OjoIA ESP32-CAM v6.1 ═══");

  esp_task_wdt_init(WATCHDOG_SECS, true);
  esp_task_wdt_add(NULL);

  generateID();
  loadCamConfig();
  configured = loadConfig();

  if (configured) {
    if (connectWiFi()) {
      syncNTP();
      cameraOK = initCamera();
      if (!cameraOK) {
        Serial.println("[CAM] ✗ Fallo al inicializar");
      } else {
        Serial.println("[CAM] ✓ Lista");
        setupConfigServer();
      }
    } else {
      Serial.println("[WiFi] ✗ Fallo, iniciando AP...");
      startAP();
    }
  } else {
    Serial.println("[CONFIG] Sin configuración, iniciando AP...");
    startAP();
  }

  Serial.printf("[BOOT] ID: %s | URL: %s | Intervalo: %dms\n",
                cameraID, serverUrl, cfg.interval_ms);
}

void loop() {
  esp_task_wdt_reset();

  if (!configured) {
    portalServer.handleClient();
    dns.processNextRequest();
    delay(5);
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    if (wifiLostAt == 0) wifiLostAt = millis();
    if (millis() - wifiLostAt > WIFI_RECONNECT_MS) {
      WiFi.reconnect();
      delay(1000);
      if (WiFi.status() == WL_CONNECTED) {
        wifiLostAt = 0;
        announced  = false;
        Serial.println("[WiFi] ✓ Reconectado");
      }
    }
    if (wifiLostAt > 0 && millis() - wifiLostAt > WIFI_RESTART_MS) {
      Serial.println("[WiFi] Timeout → reiniciando");
      ESP.restart();
    }
    delay(100);
    return;
  }
  wifiLostAt = 0;

  configServer.handleClient();

  if (!announced) {
    announceDevice();
    return;
  }

  if (millis() - lastConfigPoll > CONFIG_POLL_MS) {
    lastConfigPoll = millis();
    pollServerConfig();
  }

  if (!cameraOK) { delay(500); return; }

  if (millis() - lastSend < cfg.interval_ms) {
    delay(20);
    return;
  }

  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[CAM] ✗ Fallo captura");
    delay(100);
    return;
  }

  autoLED(fb);
  bool ok = sendFrame(fb->buf, fb->len);
  esp_camera_fb_return(fb);

  if (ok) {
    lastSend = millis();
  } else {
    delay(500);
  }
}
