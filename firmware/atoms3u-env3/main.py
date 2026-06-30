################################################################################
# AtomS3U ENV-III — compact IoT firmware
#  - UDP discovery (JSON)
#  - HTTP API with Basic Auth: "/", "/sensors", "/config"
#  - Persistent tags (NVS with file fallback)
#  - High-res timestamps, NTP sync + daily resync (jitter & backoff)
################################################################################

import json, time, gc, socket, select, network, _thread
from machine import I2C, Pin
from unit import ENVUnit
import M5
from M5 import *
from hardware import *

# ------------------------------ Imports (compat) ------------------------------
try:    import uerrno as errno
except: import errno

try:    import ntptime
except: ntptime = None

try:
    import esp32
    _nvs = esp32.NVS("app")
except Exception:
    _nvs = None

try:    import urandom as _rand
except: _rand = None

# ----------------------------- Configuration ----------------------------------
try:
    import secrets as device_secrets
except Exception:
    device_secrets = None

def _secret(name, default=""):
    try:
        return getattr(device_secrets, name)
    except Exception:
        return default

WIFI_SSID     = _secret("WIFI_SSID")
WIFI_PASSWORD = _secret("WIFI_PASSWORD")
AUTH_USERNAME = _secret("AUTH_USERNAME", "admin")
AUTH_PASSWORD = _secret("AUTH_PASSWORD")

HTTP_PORT, DISCOVERY_PORT = 80, 12343
DEVICE_TYPE, MODEL = "M5AtomS3U", "AtomS3U-ENV3"
DEVICE_ID = _secret("DEVICE_ID")       # Set per device. Leave "" to use MAC fallback.

RESYNC_INTERVAL_MS = 24*60*60*1000     # ~24h
CHECK_INTERVAL_MS  = 60*1000           # check every 60s
MAX_JITTER_MS      = 5*60*1000         # up to 5 min jitter
CONFIG_PATH        = "/config.json"    # file fallback for tags

DEBUG = False

# ----------------------------- Globals / Hardware -----------------------------
M5.begin()
rgb = RGB(io=35, n=1, type="WS2812")
i2c0 = I2C(0, scl=Pin(1), sda=Pin(2), freq=100000)
env_unit = ENVUnit(i2c=i2c0, type=3)
WLAN = network.WLAN(network.STA_IF)

wifi_connected = False
server_socket  = None
device_tags    = {}         # persisted KVP

# High-res time base derived from initial NTP
_epoch_base_s  = None       # UTC seconds at sync
_ticks_base_ms = None       # monotonic ms at sync

# ----------------------------- Small Utilities --------------------------------
def log(*a):
    if DEBUG: print(*a)

def set_led(color):
    colors = {'red':0xff0000,'green':0x00ff00,'blue':0x0000ff,'yellow':0xffff00,
              'purple':0xff00ff,'cyan':0x00ffff,'white':0xffffff,'off':0}
    rgb.fill_color(colors.get(color,0))

if hasattr(time, "ticks_add"):
    ticks_add = time.ticks_add
else:
    def ticks_add(t, delta): return (t + delta) & 0x7fffffff

def _rand_jitter_ms():
    try:
        if _rand and hasattr(_rand, "getrandbits"):
            return int(_rand.getrandbits(16) % (MAX_JITTER_MS+1))
        return int(time.ticks_us() % (MAX_JITTER_MS+1))
    except Exception:
        return 0

# ----------------------------- Persistence: tags ------------------------------
def load_tags():
    # Try NVS
    if _nvs:
        try:
            raw = _nvs.get_str("tags")
            if raw:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    device_tags.clear()
                    for k,v in obj.items(): device_tags[str(k)] = str(v)
                    print("Tags: loaded from NVS")
                    return
        except Exception:
            pass
    # Try file
    try:
        with open(CONFIG_PATH,"r") as f:
            data = json.loads(f.read() or "{}")
        if isinstance(data, dict) and isinstance(data.get("tags"), dict):
            device_tags.clear()
            for k,v in data["tags"].items(): device_tags[str(k)] = str(v)
            print("Tags: loaded from file")
            return
    except Exception:
        pass
    print("Tags: using defaults", device_tags)

def save_tags():
    # NVS preferred
    if _nvs:
        try:
            _nvs.set_str("tags", json.dumps(device_tags))
            try: _nvs.commit()
            except Exception: pass
            log("Tags saved to NVS:", device_tags); return True
        except Exception:
            pass
    # File fallback
    try:
        with open(CONFIG_PATH,"w") as f: f.write(json.dumps({"tags":device_tags}))
        log("Tags saved to file:", device_tags); return True
    except Exception:
        print("WARNING: failed to persist tags")
        return False

def tags_set_many(d):
    changed = False
    for k,v in (d or {}).items():
        k = (str(k) if k is not None else "").strip()
        v = (str(v) if v is not None else "").strip()
        if not k: continue
        if len(k)>64: k=k[:64]
        if len(v)>256: v=v[:256]
        if device_tags.get(k)!=v:
            device_tags[k]=v; changed=True
    if changed: save_tags()
    return changed

def tags_delete(keys):
    changed=False
    for k in keys or []:
        k=(str(k) if k is not None else "").strip()
        if k in device_tags: device_tags.pop(k); changed=True
    if changed: save_tags()
    return changed

def tags_clear():
    device_tags.clear(); save_tags()

# ----------------------------- Timebase (NTP + monotonic) ---------------------
def ntp_sync():
    global _epoch_base_s, _ticks_base_ms
    if ntptime is None:
        print("NTP unavailable; using monotonic timestamps.")
        return False
    try:
        ntptime.settime()
        _epoch_base_s  = int(time.time())
        _ticks_base_ms = time.ticks_ms()
        print("NTP synced @", _epoch_base_s)
        return True
    except Exception as e:
        print("NTP sync failed:", e)
        return False

def now_epoch_ms():
    if _epoch_base_s is not None and _ticks_base_ms is not None:
        return _epoch_base_s*1000 + int(time.ticks_diff(time.ticks_ms(), _ticks_base_ms))
    return int(time.ticks_ms())  # monotonic since boot

def now_iso8601_ms():
    ms = now_epoch_ms(); s, ms = ms//1000, ms%1000
    t = time.gmtime(s)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ"%(t[0],t[1],t[2],t[3],t[4],t[5],ms)

def ntp_resync_loop():
    due = ticks_add(time.ticks_ms(), RESYNC_INTERVAL_MS + _rand_jitter_ms())
    while True:
        time.sleep_ms(CHECK_INTERVAL_MS)
        if not wifi_connected: continue
        if time.ticks_diff(time.ticks_ms(), due) >= 0:
            print("NTP: resync…")
            if ntp_sync():
                due = ticks_add(time.ticks_ms(), RESYNC_INTERVAL_MS + _rand_jitter_ms())
                print("NTP: OK; next ~24h")
            else:
                due = ticks_add(time.ticks_ms(), 60*60*1000 + (MAX_JITTER_MS//6))
                print("NTP: FAIL; retry ~1h")

def start_ntp_thread():
    try: _thread.start_new_thread(ntp_resync_loop, ()); print("NTP thread started.")
    except Exception as e: print("NTP thread error:", e)

# ----------------------------- Networking: Wi-Fi ------------------------------
def wifi_connect(timeout_s=30):
    global wifi_connected
    WLAN.active(True)
    if WLAN.isconnected():
        try: WLAN.disconnect()
        except Exception: pass
        time.sleep(0.2)

    print("WiFi: connecting to '%s'…" % WIFI_SSID)
    WLAN.connect(WIFI_SSID, WIFI_PASSWORD)
    blink=False; t=timeout_s
    while not WLAN.isconnected() and t>0:
        set_led('blue' if blink else 'off'); blink=not blink
        time.sleep(0.5); t-=0.5
        st=WLAN.status()
        if st in (network.STAT_WRONG_PASSWORD, network.STAT_NO_AP_FOUND):
            print("WiFi error status:", st); set_led('red'); return None

    if WLAN.isconnected():
        wifi_connected=True
        ip,mask,gw,dns=WLAN.ifconfig()
        print("WiFi: %s  GW:%s  DNS:%s"%(ip,gw,dns)); set_led('green'); return ip
    print("WiFi: failed. status=", WLAN.status()); set_led('red'); return None

# ----------------------------- Discovery (UDP) --------------------------------
def get_device_id():
    if DEVICE_ID: return DEVICE_ID
    mac = WLAN.config('mac')
    return "ATOM3U-" + ''.join('{:02x}'.format(b) for b in mac)

def discovery_loop():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for opt,val in ((socket.SO_REUSEADDR,1),(socket.SO_BROADCAST,1)):
            try: sock.setsockopt(socket.SOL_SOCKET,opt,val)
            except Exception: pass
        sock.bind(('', DISCOVERY_PORT))
        sock.setblocking(False)
        poller = select.poll(); poller.register(sock, select.POLLIN)
        print("Discovery: UDP %d" % DISCOVERY_PORT)

        mac = WLAN.config('mac')
        mac_str = ':'.join('{:02x}'.format(b) for b in mac)
        dev_id = get_device_id()

        while True:
            if not poller.poll(1000): continue
            try: data, addr = sock.recvfrom(1024)
            except OSError as e:
                if getattr(e,'errno',None) in (errno.EAGAIN, errno.ETIMEDOUT): continue
                print("Discovery recv err", e); time.sleep(0.1); continue

            try:
                req = json.loads(data.decode('utf-8','ignore'))
            except Exception:
                continue
            if 'discover' not in req: continue

            ip_now,_,_,_ = WLAN.ifconfig()
            resp = {
                "type": DEVICE_TYPE,
                "model": MODEL,
                "mac": mac_str,
                "deviceId": dev_id,
                "on_time": time.ticks_ms()//1000,
                "on_time_ms": int(time.ticks_ms()),
                "timestamp_ms": now_epoch_ms(),
                "timestamp_iso": now_iso8601_ms(),
                "m5_device_type": "environmental_sensor",
                "m5_sensor_types": ["temperature","humidity","pressure"],
                "m5_api_port": HTTP_PORT,
                "m5_auth_required": True,
                "m5_endpoints": ["/","/sensors","/config"],
                "m5_ip": ip_now or "unknown",
                "m5_tags": device_tags,
            }
            try: sock.sendto(json.dumps(resp).encode('utf-8'), addr)
            except OSError as e:
                if getattr(e,'errno',None) not in (errno.EAGAIN, errno.ETIMEDOUT):
                    print("Discovery send err", e)
    except Exception as e:
        print("Discovery loop failed:", e)

def start_discovery_thread():
    try: _thread.start_new_thread(discovery_loop, ()); print("Discovery thread started.")
    except Exception as e: print("Discovery thread error:", e)

# ----------------------------- Sensors ----------------------------------------
def read_sensors():
    try:
        t = env_unit.read_temperature()
        h = env_unit.read_humidity()
        p = env_unit.read_pressure()
        return True, {
            "temperature_c": round(t,2),
            "temperature_f": round((t*9/5)+32,2),
            "humidity": round(h,2),
            "pressure_pa": round(p,2),
            "pressure_hpa": round(p/100.0,2),
        }
    except Exception as e:
        log("Sensor error:", e)
        return False, {"error": str(e)}

# ----------------------------- HTTP helpers -----------------------------------
def _recv_http_request(conn, max_bytes=8192, header_timeout_ms=2000):
    """
    Read until we have headers (CRLFCRLF), then read Content-Length bytes of body.
    Returns a UTF-8 request string "headers\\r\\n\\r\\n<body>".
    """
    conn.settimeout(2)
    buf = b""
    t0 = time.ticks_ms()

    # Read headers
    while b"\r\n\r\n" not in buf and len(buf) < max_bytes:
        try:
            chunk = conn.recv(512)
            if not chunk:
                break
            buf += chunk
        except OSError:
            time.sleep_ms(10)
        if time.ticks_diff(time.ticks_ms(), t0) > header_timeout_ms:
            break

    hdr_end = buf.find(b"\r\n\r\n")
    if hdr_end < 0:
        # No full headers; best-effort return
        return buf.decode("utf-8", "ignore")

    headers_bytes = buf[:hdr_end]
    body = buf[hdr_end + 4:]

    # Find Content-Length
    content_length = 0
    try:
        headers_text = headers_bytes.decode("utf-8", "ignore")
        for line in headers_text.split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
                break
    except Exception:
        content_length = 0

    # Read remaining body (if any)
    remaining = max(0, content_length - len(body))
    while remaining > 0 and (len(headers_bytes) + 4 + len(body)) < max_bytes:
        try:
            chunk = conn.recv(min(512, remaining))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        except OSError:
            time.sleep_ms(10)

    return headers_text + "\r\n\r\n" + body.decode("utf-8", "ignore")


def http_json(payload, code=200, reason="OK"):
    body = json.dumps(payload)
    hdrs = [
        "HTTP/1.1 %d %s"%(code,reason),
        "Content-Type: application/json",
        "Access-Control-Allow-Origin: *",
        "Connection: close",
        "Content-Length: %d"%len(body),
        "\r\n",
    ]
    return ("\r\n".join(hdrs)+body)

def http_unauthorized():
    body = json.dumps({"device":DEVICE_TYPE,"error":"Authentication required"})
    hdrs = [
        "HTTP/1.1 401 Unauthorized",
        'WWW-Authenticate: Basic realm="M5AtomS3U Sensor"',
        "Content-Type: application/json",
        "Connection: close",
        "Content-Length: %d"%len(body),
        "\r\n",
    ]
    return ("\r\n".join(hdrs)+body)

def parse_auth_ok(headers):
    try:
        auth = next((h for h in headers if h.lower().startswith("authorization:")), None)
        if not auth: return False
        parts = auth.split()
        if len(parts)<3 or parts[1].lower()!="basic": return False
        try: import ubinascii as b64
        except Exception: import binascii as b64
        user, pw = b64.a2b_base64(parts[2]).decode("utf-8").split(":",1)
        return user==AUTH_USERNAME and pw==AUTH_PASSWORD
    except Exception as e:
        log("Auth parse err:", e); return False

def url_decode(s):
    out=[]; i=0
    while i<len(s):
        c=s[i]
        if c=='+': out.append(' ')
        elif c=='%' and i+2<len(s):
            try: out.append(chr(int(s[i+1:i+3],16))); i+=2
            except: out.append('%')
        else: out.append(c)
        i+=1
    return ''.join(out)

def parse_query(path_with_qs):
    path,_,qs = path_with_qs.partition('?')
    params={}
    if qs:
        for pair in qs.split('&'):
            if not pair: continue
            k,_,v = pair.partition('=')
            k=url_decode(k); v=url_decode(v)
            if k in params:
                cur=params[k]
                params[k]=cur+[v] if isinstance(cur,list) else [cur,v]
            else:
                params[k]=v
    return path, params

def ensure_list(x): return x if isinstance(x,list) else [x]

# ----------------------------- HTTP server & handlers -------------------------
def handle_request(raw):
    try:
        print(raw)
        lines = raw.split("\r\n")
        if not lines: return http_json({"error":"Invalid request"},400,"Bad Request")
        method, target = lines[0].split(' ')[:2]
        path, query  = parse_query(target)
        headers = [l for l in lines[1:] if l and ':' in l]

        if not parse_auth_ok(headers):
            set_led('red'); time.sleep(0.05); set_led('green')
            return http_unauthorized()

        set_led('yellow'); time.sleep(0.02); set_led('green')

        # small body parse
        try: body = '\r\n'.join(lines[lines.index('')+1:])
        except ValueError: body = ""

        if method not in ("GET","POST"):
            return http_json({"error":"Only GET/POST supported"},405,"Method Not Allowed")

        dev_id = get_device_id()

        if path in ("","/"):
            ip,mask,gw,dns = WLAN.ifconfig()
            return http_json({
                "device": DEVICE_TYPE,
                "model": MODEL,
                "device_id": dev_id,
                "ip": ip,
                "wifi_connected": wifi_connected,
                "uptime_ms": time.ticks_ms(),
                "free_memory": gc.mem_free(),
                "timestamp_ms": now_epoch_ms(),
                "timestamp_iso": now_iso8601_ms(),
                "tags": device_tags,
                "endpoints": ["/","/sensors","/config"],
            })

        if path == "/sensors":
            ok, data = read_sensors()
            payload = {
                "device": DEVICE_TYPE, "model": MODEL, "device_id": dev_id,
                "timestamp_ms": now_epoch_ms(), "timestamp_iso": now_iso8601_ms(),
                "tags": device_tags,
            }
            payload.update(data)
            return http_json(payload, 200 if ok else 500, "OK" if ok else "Internal Server Error")

        if path == "/config":
            updated=False
            # query: tag=key:value (repeat), del=key[,key2], clear=true
            print(query)
            if "tag" in query:
                kv={}
                for item in ensure_list(query["tag"]):
                    print(item)
                    if ":" in item:
                        k,v = item.split(":",1); kv[k]=v
                updated |= tags_set_many(kv)
            if "del" in query:
                dels = query["del"].split(",") if isinstance(query["del"],str) else []
                updated |= tags_delete(dels)
            if str(query.get("clear","")).lower() in ("1","true","yes"):
                tags_clear(); updated=True
            # JSON body: {"tags":{...},"delete":[...],"clear":true}
            if body:
                try:
                    j=json.loads(body)
                    if isinstance(j,dict):
                        if isinstance(j.get("tags"),dict): updated |= tags_set_many(j["tags"])
                        if isinstance(j.get("delete"),list): updated |= tags_delete(j["delete"])
                        if j.get("clear") is True: tags_clear(); updated=True
                except Exception: pass

            return http_json({
                "device": DEVICE_TYPE, "model": MODEL, "device_id": dev_id,
                "status": "ok", "updated": updated,
                "timestamp_ms": now_epoch_ms(), "timestamp_iso": now_iso8601_ms(),
                "tags": device_tags, "persist": "nvs" if _nvs else "file"
            })

        return http_json({"error":"Endpoint not found","available":["/","/sensors","/config"]},404,"Not Found")

    except Exception as e:
        print("HTTP handler error:", e)
        set_led('red'); time.sleep(0.1); set_led('green')
        return http_json({"error":"Internal server error","details":str(e)},500,"Internal Server Error")

def start_http():
    global server_socket
    try:
        s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', HTTP_PORT)); s.listen(5); s.settimeout(1.0)
        server_socket=s; print("HTTP: :%d ? [/ /sensors /config]"%HTTP_PORT); return True
    except Exception as e:
        print("HTTP start error:", e); set_led('red'); return False

def http_loop():
    global server_socket
    while True:
        try:
            if server_socket:
                try:
                    c, addr = server_socket.accept()
                    req = _recv_http_request(c)
                    if req: c.send(handle_request(req).encode('utf-8'))
                    c.close(); gc.collect()
                except OSError:
                    pass
                except Exception as e:
                    print("HTTP client error:", e)
                    set_led('red'); time.sleep(0.05); set_led('green')

            M5.update()
            if BtnA.wasPressed():
                ok,data=read_sensors()
                if ok:
                    print("T %.2fC (%.2fF) H %.2f%% P %.2f hPa"%
                          (data["temperature_c"],data["temperature_f"],data["humidity"],data["pressure_hpa"]))
                    set_led('white'); time.sleep(0.1); set_led('green')
                else:
                    print("Sensor read failed:", data.get("error","?"))
                    set_led('red'); time.sleep(0.2); set_led('green')

            time.sleep(0.01)
        except KeyboardInterrupt:
            print("HTTP: stop"); set_led('off'); break
        except Exception as e:
            print("HTTP loop error:", e); set_led('red'); time.sleep(0.5)

# ----------------------------- Main -------------------------------------------
def main():
    print("%s %s API" % (DEVICE_TYPE, MODEL))
    print("Auth:", AUTH_USERNAME, "/","*"*len(AUTH_PASSWORD))
    set_led('off'); time.sleep(0.1)

    load_tags()

    ok,data = read_sensors()
    print("Sensor: OK" if ok else ("Sensor error: %s"%data.get("error","?")))
    set_led('green' if ok else 'red'); time.sleep(0.2); set_led('off')

    ip = wifi_connect()
    if not ip:
        print("No WiFi ? exit"); set_led('red'); return

    ntp_sync(); start_ntp_thread()
    start_discovery_thread()

    if start_http(): http_loop()
    else: print("HTTP failed to start"); set_led('red')

def cleanup():
    global server_socket
    try:
        if server_socket: server_socket.close()
    except Exception: pass
    set_led('off')

if __name__ == "__main__":
    try:    main()
    except Exception as e:
        print("Fatal:", e); set_led('red')
    finally:
        cleanup()
