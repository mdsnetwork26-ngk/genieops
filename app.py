#!/usr/bin/env python3
# ============================================================================
#  GenieOps — aplikasi pendamping GenieACS
#  ---------------------------------------------------------------------------
#  Mengatasi 3 masalah operasional GenieACS:
#    1. ONU terpantau OFFLINE padahal ONLINE  -> watchdog otomatis mengirim
#       connection request ke ONU yang statusnya basi (diluar sana PPPoE-nya
#       aktif = ONU jelas hidup), tanpa limit 80 perangkat.
#    2. Pencarian ONU berat                   -> seluruh perangkat di-index di
#       SQLite lokal: cari per serial / IP / user PPPoE / nama pelanggan
#       (gabungan ke TagiNet) dalam sekejap.
#    3. Refresh/reboot satu per satu          -> aksi massal untuk hasil filter
#       apa pun, dengan concurrency terkendali + log.
#
#  Sumber data:
#    - GenieACS NBI  (REST, port 7557) : daftar perangkat + _lastInform + tugas
#    - Mikrotik (RouterOS API 8728)    : /ppp/active/print = kebenaran sesi
#    - TagiNet (SQLite, opsional, ro)  : nama pelanggan + penagih per ONU
#
#  Murni pustaka standar Python. Jalankan:  python app.py   (PORT=3000)
# ============================================================================
import json
import math
import os
import re
import secrets as _secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import routeros_api

APP_VERSION = '1.7'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
DB_PATH = os.path.join(DATA_DIR, 'genieops.db')
PORT = int(os.environ.get('PORT', '3000'))
SESSION_DAYS = 30

# ------------------------------------------------------------------ database

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  id TEXT PRIMARY KEY,
  serial TEXT DEFAULT '',
  model TEXT DEFAULT '',
  ip TEXT DEFAULT '',
  pppoe_user TEXT DEFAULT '',
  pppoe_state TEXT DEFAULT '',
  pppoe_addr TEXT DEFAULT '',
  pppoe_uptime TEXT DEFAULT '',
  last_inform TEXT DEFAULT '',
  last_seen TEXT DEFAULT '',
  rx TEXT DEFAULT '',
  rx_path TEXT DEFAULT '',
  wifi TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dev_inform ON devices(last_inform);
CREATE INDEX IF NOT EXISTS idx_dev_ip ON devices(ip);
CREATE INDEX IF NOT EXISTS idx_dev_user ON devices(pppoe_user);
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, device_id TEXT, device_label TEXT,
  action TEXT, status TEXT, msg TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULTS = {
    # alamat GenieACS
    'acs_nbi_url': os.environ.get('GENIEACS_URL', 'http://127.0.0.1:7557'),
    'acs_ui_url': os.environ.get('ACS_UI_URL', 'http://127.0.0.1:3000'),
    # daftar mikrotik: JSON [ {name,host,port,user,pass}, ... ]
    'mikrotik_list': json.dumps(
        ([{'name': 'mikrotik', 'host': os.environ['MIKROTIK_HOST'],
           'port': int(os.environ.get('MIKROTIK_PORT', '8728')),
           'user': os.environ.get('MIKROTIK_USER', 'admin'),
           'pass': os.environ.get('MIKROTIK_PASS', '')}]
         if os.environ.get('MIKROTIK_HOST') else [])),
    # status & sinkronisasi
    'inform_interval': '300',        # detik: interval PeriodicInform yang seharusnya
    'stale_minutes': '15',           # lebih lama dari ini tanpa inform = basi/offline
    'sync_interval': '60',           # detik: tarik data ACS + PPPoE
    # watchdog
    'watchdog_enabled': '1',
    'watchdog_interval': '300',      # detik
    'watchdog_max': '150',           # maks ONU diperiksa per putaran
    'watchdog_scope': 'pppoe_aktif', # pppoe_aktif | semua_stale
    # aksi massal
    'bulk_concurrency': '8',
    # gabungan TagiNet (path db, baca-saja)
    'taginet_db': os.environ.get('TAGINET_DB', ''),
    # login
    'admin_pass': os.environ.get('GENIEOPS_PASSWORD', 'admin123'),
}

DB_LOCK = threading.RLock()
_TLS = threading.local()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = db()
    with DB_LOCK:
        conn.executescript(SCHEMA)
        # migrasi halus utk database lama (v1.0/1.1)
        for kol in ('rx', 'rx_path', 'wifi', 'root'):
            try:
                conn.execute(f'ALTER TABLE devices ADD COLUMN {kol} TEXT DEFAULT \'\'')
            except sqlite3.OperationalError:
                pass
        for k, v in DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, str(v)))
        conn.commit()
    conn.close()


def get_setting(conn, key, default=None):
    r = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if r is None:
        return DEFAULTS.get(key, '') if default is None else default
    return r['value']


def set_setting(conn, key, value):
    conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    conn.commit()


def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def audit(conn, device_id, label, action, status, msg='', source='sistem'):
    conn.execute('INSERT INTO actions(ts,device_id,device_label,action,status,msg,source) '
                 'VALUES(?,?,?,?,?,?,?)',
                 (now_utc(), device_id or '', label or '', action, status, str(msg)[:300], source))


# ------------------------------------------------------------------ klien NBI GenieACS

def nbi_base():
    return get_setting(_db_cached(), 'acs_nbi_url').rstrip('/')


def _http_json(url, method='GET', body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode('utf-8', 'replace')
        return r.status, (json.loads(raw) if raw else None)


def nbi_devices():
    """Daftar seluruh perangkat dari NBI (hanya kolom yang dibutuhkan)."""
    proj = ('_id,_lastInform,'
            'InternetGatewayDevice.ManagementServer.ConnectionRequestURL,'
            'Device.ManagementServer.ConnectionRequestURL,'
            'InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username,'
            'InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ExternalIPAddress,'
            + ','.join(RX_KANDIDAT))
    url = nbi_base() + '/devices/?' + urllib.parse.urlencode({'projection': proj})
    status, data = _http_json(url)
    return data or []


def nbi_delete(device_id):
    """Hapus perangkat dari GenieACS (DELETE /devices/<id>)."""
    did = urllib.parse.quote(device_id, safe='')
    url = nbi_base() + f'/devices/{did}'
    req = urllib.request.Request(url, method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, ''
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode('utf-8', 'replace')).get('message', '')
        except Exception:  # noqa: BLE001
            msg = ''
        return e.code, msg or e.reason


def nbi_task(device_id, task, connection_request=True):
    """Kirim tugas ke perangkat. 200=langsung dieksekusi, 202=menunggu inform."""
    qs = '?connection_request' if connection_request else ''
    did = urllib.parse.quote(device_id, safe='')
    url = nbi_base() + f'/devices/{did}/tasks{qs}'
    try:
        status, data = _http_json(url, 'POST', task, timeout=25)
        return status, ''
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode('utf-8', 'replace')).get('message', '')
        except Exception:
            msg = ''
        return e.code, msg or e.reason


def nbi_pending_tasks(device_id):
    """Antrean tugas yang belum selesai untuk satu perangkat."""
    did = urllib.parse.quote(device_id, safe='')
    try:
        status, data = _http_json(nbi_base() + f'/devices/{did}/tasks', timeout=15)
        return data or []
    except Exception:  # noqa: BLE001
        return None      # gagal membaca antrean -> jangan menghalangi pengiriman


def tugas_menganggur(device_id, task):
    """True kalau tugas serupa masih menganggur di ACS — anti penumpukan antrean.

    Akar masalah beban server produksi: watchdog/pindai mengirim tugas baru
    setiap putaran ke ONU yang tak pernah menyelesaikan tugasnya, hingga antrean
    membengkak puluhan ribu dan genieacs-cwmp disibukkan terus-menerus."""
    for t in (nbi_pending_tasks(device_id) or []):
        if (t.get('name') == task.get('name')
                and t.get('objectName') == task.get('objectName')
                and t.get('parameterValues') == task.get('parameterValues')):
            return True
    return False


# path redaman (dBm) yang umum — ZTE, Huawei, dan standar; path eksotis ditemukan
# otomatis lewat pindai penuh (telusuri_optik) saat detail ONU dibuka / pindai massal
RX_KANDIDAT = [
    'InternetGatewayDevice.WANDevice.1.X_ZTE-COM_WANPONInterfaceConfig.RXPower',
    'InternetGatewayDevice.WANDevice.1.X_ZTE-COMGPONOPTICAL.RxPower',
    'InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower',
    'InternetGatewayDevice.WANDevice.1.WANPONInterfaceConfig.RXPower',
]


def normalisasi_rx(nilai):
    """Normalisasi nilai redaman ke dBm.

    Beberapa firmware melaporkan dalam satuan lain:
      '-213.4'  (deci-dBm)  -> -21.34
      '8'       (mikrowatt) -> -20.97
      '0.008'   (miliwatt)  -> -20.97
      '-21.34 dBm' (dgn satuan) -> -21.34
    Hasil di luar rentang wajar PON (-45..+5 dBm) dikembalikan apa adanya."""
    if nilai is None or nilai == '':
        return ''
    try:
        v = float(nilai)
    except (TypeError, ValueError):
        m = re.match(r'^\s*(-?\d+(?:\.\d+)?)', str(nilai))
        if not m:
            return ''
        v = float(m.group(1))
    if -60 <= v <= 0:
        return f'{v:g}'  # sudah dBm (redaman PON praktis selalu negatif)
    if -450 <= v < -60:
        return f'{round(v / 10, 2):g}'  # deci-dBm
    if 0 < v < 1:  # miliwatt
        dBm = round(10 * math.log10(v), 2)
        return f'{dBm:g}' if -45 <= dBm <= 5 else f'{v:g}'
    if 1 <= v <= 20000:  # mikrowatt
        dBm = round(10 * math.log10(v / 1000), 2)
        return f'{dBm:g}' if -45 <= dBm <= 5 else f'{v:g}'
    return f'{v:g}'


def model_pendek(model):
    """Nama model utk tampilan: potong sebelum '%' dan '_'.
    'HSGQ%2DX130W' -> 'HSGQ', 'TOTOLINK_N355RT' -> 'TOTOLINK', 'F663NV3A' -> 'F663NV3A'."""
    m = (model or '').split('%')[0].split('_')[0].strip()
    return m or (model or '')


def model_utuh(model):
    """Nama model lengkap utk detail: %2D dikembalikan jadi strip. 'ZL%2D2113X' -> 'ZL-2113X'."""
    return (model or '').replace('%2D', '-').replace('%2d', '-')


def rx_dari_kandidat(doc):
    for p in RX_KANDIDAT:
        v = param_value(doc, p)
        if v not in (None, ''):
            return p, normalisasi_rx(v)
    return '', ''


TASK_REFRESH = {'name': 'refreshObject', 'objectName': ''}
TASK_REBOOT = {'name': 'reboot'}
def ping_task(root=''):
    """Tugas ringan untuk memaksa inform: refresh object DeviceInfo saja.

    Path menyesuaikan model data ONU — perangkat lama memakai
    InternetGatewayDevice.*, ONU TR-181 memakai Device.*. Path yang salah
    membuat tugas fault "Invalid parameter path" lalu dicoba ulang berkali-kali
    oleh ACS (badai fault di produksi)."""
    if root == 'tr181':
        return {'name': 'refreshObject', 'objectName': 'Device.DeviceInfo.'}
    return {'name': 'refreshObject', 'objectName': 'InternetGatewayDevice.DeviceInfo.'}


def set_inform_task(root, detik):
    """Tugas set PeriodicInformInterval (detik) — path menyesuaikan model data ONU."""
    path = ('Device' if root == 'tr181' else 'InternetGatewayDevice') \
        + '.ManagementServer.PeriodicInformInterval'
    return {'name': 'setParameterValues',
            'parameterValues': [[path, int(detik), 'xsd:unsignedInt']]}


def root_untuk(conn, device_id):
    r = conn.execute('SELECT root FROM devices WHERE id=?', (device_id,)).fetchone()
    return r["root"] if r else ""


def param_value(doc, dotted):
    """Ambil nilai parameter dari dokumen perangkat GenieACS (pohon bersarang)."""
    # bentuk bersarang: a.b.c -> doc[a][b][c]['_value']
    cur = doc
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            break
        cur = cur[part]
    else:
        if isinstance(cur, dict) and '_value' in cur:
            return cur['_value']
    # bentuk datar (kalau-kalau NBI mengembalikan kunci bertitik)
    flat = doc.get(dotted)
    if isinstance(flat, dict):
        return flat.get('_value')
    return None


def parse_last_inform(s):
    """Normalisasi berbagai format tanggal dari GenieACS -> 'YYYY-MM-DD HH:MM:SS' UTC.

    Format yang didukung:
      '2026-10-06 07:55:23 +0000'   (format dokumentasi NBI)
      '2026-10-06T07:55:23.409Z'    (ISO 8601 keluaran MongoDB)
      '2026-10-06T07:55:23+07:00'   (ISO dengan offset zona waktu)
      1760000000000                 (epoch milidetik)
    """
    if s is None or s == '':
        return ''
    if isinstance(s, (int, float)):
        dt = datetime.fromtimestamp(s / 1000.0, tz=timezone.utc)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(s, dict):
        s = s.get('$date') or s.get('date') or ''
        if not s:
            return ''
    s = str(s).strip()
    m = re.match(r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?'
                 r'(?:\s*(Z|[+-]\d{2}:?\d{2})?)?$', s)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + ' ' + m.group(2), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return ''
        off = m.group(3)
        if off and off != 'Z':
            tanda = 1 if off[0] == '+' else -1
            angka = off.strip('+-').replace(':', '')
            try:
                dt -= tanda * timedelta(hours=int(angka[:2]), minutes=int(angka[2:4] or 0))
            except ValueError:
                pass
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    return ''  # format tak dikenal


def age_seconds(last_inform):
    if not last_inform:
        return None
    try:
        t = datetime.strptime(last_inform, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
    except ValueError:
        return None


# ------------------------------------------------------------------ status & verdict

def stale_seconds():
    try:
        conn = _db_cached()
        return max(60, int(get_setting(conn, 'stale_minutes', '15') or 15) * 60)
    except Exception:  # belum init / tabel belum ada -> pakai bawaan
        return max(60, int(DEFAULTS['stale_minutes']) * 60)


def verdict_of(age, pppoe_state):
    """Kesimpulan status ONU:
    ok    = ACS segar + PPPoE aktif
    basi  = ACS basi/offline TAPI PPPoE aktif  -> ONU HIDUP, status ACS salah!
    idle  = ACS segar tapi PPPoE tidak aktif   -> ONU hidup, pelanggan tidak dial
    down  = ACS basi + PPPoE tidak aktif       -> kemungkinan benar-benar mati
    """
    segar = age is not None and age <= stale_seconds()
    aktif = pppoe_state == 'aktif'
    if segar and aktif:
        return 'ok'
    if aktif:
        return 'basi'
    if segar:
        return 'idle'
    return 'down'


VERDICT_LABEL = {
    'ok': '✅ Normal', 'basi': '🟡 ACS basi (ONU hidup)',
    'idle': '🔔 PPPoE idle', 'down': '🔴 Down',
}


# ------------------------------------------------------------------ sinkronisasi

STATE = {
    'acs_sync': {'ts': '', 'n': 0, 'err': ''},
    'pppoe_sync': {'ts': '', 'n': 0, 'routers': []},
    'watchdog': {'ts': '', 'target': 0, 'ok': 0, 'fail': 0, 'err': ''},
    'probe': {'running': False, 'done': 0, 'total': 0, 'err': ''},
    'diag': {'contoh': [], 'pppoe': [], 'raw_contoh': []},
}
STATE_LOCK = threading.Lock()

def _db_cached():
    """Satu koneksi SQLite per thread (aman untuk banyak thread + WAL)."""
    c = getattr(_TLS, 'conn', None)
    if c is None:
        c = db()
        _TLS.conn = c
    return c


def job_sync():
    """Tarik perangkat dari ACS + sesi PPPoE dari mikrotik, cocokkan, simpan."""
    # --- ACS ---
    err = ''
    devs = []
    try:
        raw = nbi_devices()
        for doc in raw:
            did = doc.get('_id', '')
            if not did:
                continue
            parts = did.split('-')
            cr = (param_value(doc, 'InternetGatewayDevice.ManagementServer.ConnectionRequestURL')
                  or param_value(doc, 'Device.ManagementServer.ConnectionRequestURL') or '')
            ip = ''
            m = re.match(r'https?://([^:/]+)', cr or '')
            if m:
                ip = m.group(1)
            devs.append((did, parts[-1], parts[1] if len(parts) >= 3 else '', ip,
                         parse_last_inform(doc.get('_lastInform')),
                         param_value(doc, 'InternetGatewayDevice.WANDevice.1.'
                                          'WANConnectionDevice.1.WANPPPConnection.1.Username') or '',
                         param_value(doc, 'InternetGatewayDevice.WANDevice.1.'
                                          'WANConnectionDevice.1.WANPPPConnection.1.ExternalIPAddress') or '',
                         *rx_dari_kandidat(doc),
                         ('igd' if 'InternetGatewayDevice' in doc
                          else ('tr181' if 'Device' in doc else ''))))
    except Exception as e:  # noqa: BLE001
        err = f'{type(e).__name__}: {e}'
    with STATE_LOCK:
        STATE['acs_sync'] = {'ts': now_utc(), 'n': len(devs), 'err': err}
    if err:
        return

    # --- PPPoE (kebenaran sesi) ---
    sessions = []
    routers = []
    conn = _db_cached()
    try:
        mt_list = json.loads(get_setting(conn, 'mikrotik_list') or '[]')
    except ValueError:
        mt_list = []
    for mt in mt_list:
        rstat = {'name': mt.get('name') or mt.get('host', '?'), 'ok': False, 'n': 0, 'err': ''}
        try:
            api = routeros_api.connect(mt['host'], mt['user'], mt['pass'],
                                       port=int(mt.get('port', 8728)))
            try:
                rows = api.raw_cmd('/ppp/active/print')
            finally:
                api.close()
            for r in rows:
                sessions.append({'name': r.get('name', ''), 'address': r.get('address', ''),
                                 'uptime': r.get('uptime', '')})
            rstat['ok'] = True
            rstat['n'] = len(rows)
        except Exception as e:  # noqa: BLE001
            rstat['err'] = f'{type(e).__name__}: {e}'
        routers.append(rstat)
    with STATE_LOCK:
        STATE['pppoe_sync'] = {'ts': now_utc(), 'n': len(sessions), 'routers': routers}

    by_name = {s['name']: s for s in sessions if s['name']}
    by_addr = {s['address']: s for s in sessions if s['address']}

    diag_contoh = []
    for did, serial, model, ip, li, user_pohon, ip_pohon, _rxp, _rxv, _root in devs[:3]:
        ses = (by_name.get(user_pohon) if user_pohon else None) or by_addr.get(ip) or by_addr.get(ip_pohon)
        diag_contoh.append({'id': did, 'cr_ip': ip, 'user_pohon': user_pohon,
                            'ip_pohon': ip_pohon, 'last_inform': li,
                            'umur_detik': age_seconds(li),
                            'cocok_pppoe': (ses['name'] if ses else None),
                            'alamat_sesi': (ses['address'] if ses else None)})
    with STATE_LOCK:
        STATE['diag'] = {'contoh': diag_contoh,
                         'pppoe': sessions[:3],
                         'raw_contoh': [d.get('_lastInform') for d in raw[:3]]}

    # --- gabungkan & simpan ---
    with DB_LOCK:
        ts = now_utc()
        cur_ids = set()
        for did, serial, model, ip, li, user_pohon, ip_pohon, rxp, rxv, root in devs:
            cur_ids.add(did)
            lama = conn.execute('SELECT pppoe_user FROM devices WHERE id=?', (did,)).fetchone()
            user = (lama['pppoe_user'] if lama else '') or (user_pohon or '')
            ses = (by_name.get(user) if user else None) or by_addr.get(ip) or by_addr.get(ip_pohon)
            if ses:
                user = user or ses['name']
                conn.execute(
                    'INSERT INTO devices(id,serial,model,ip,pppoe_user,pppoe_state,'
                    'pppoe_addr,pppoe_uptime,last_inform,last_seen,rx,rx_path,root) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET serial=excluded.serial, model=excluded.model,'
                    'ip=excluded.ip, pppoe_user=excluded.pppoe_user,'
                    'pppoe_state=excluded.pppoe_state, pppoe_addr=excluded.pppoe_addr,'
                    'pppoe_uptime=excluded.pppoe_uptime, last_inform=excluded.last_inform,'
                    'last_seen=excluded.last_seen,'
                    'rx=CASE WHEN excluded.rx != \'\' THEN excluded.rx ELSE devices.rx END,'
                    'rx_path=CASE WHEN excluded.rx_path != \'\' THEN excluded.rx_path ELSE devices.rx_path END, root=excluded.root',
                    (did, serial, model, ip, user, 'aktif', ses['address'], ses['uptime'],
                     li, ts, rxv, rxp, root))
            else:
                conn.execute(
                    'INSERT INTO devices(id,serial,model,ip,pppoe_user,pppoe_state,'
                    'pppoe_addr,pppoe_uptime,last_inform,last_seen,rx,rx_path,root) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET serial=excluded.serial, model=excluded.model,'
                    'ip=excluded.ip, pppoe_state=excluded.pppoe_state,'
                    'pppoe_addr=excluded.pppoe_addr, pppoe_uptime=excluded.pppoe_uptime,'
                    'last_inform=excluded.last_inform, last_seen=excluded.last_seen,'
                    'rx=CASE WHEN excluded.rx != \'\' THEN excluded.rx ELSE devices.rx END,'
                    'rx_path=CASE WHEN excluded.rx_path != \'\' THEN excluded.rx_path ELSE devices.rx_path END, root=excluded.root',
                    (did, serial, model, ip, user, '', '', '', li, ts, rxv, rxp, root))
        # perangkat yang sudah tidak ada di ACS -> tandai (jangan hapus, biar berjejak)
        for r in conn.execute("SELECT id FROM devices WHERE last_seen < ?", (ts,)):
            if r['id'] not in cur_ids:
                conn.execute("UPDATE devices SET pppoe_state='' WHERE id=?", (r['id'],))
        conn.commit()
    mungkin_probe_otomatis(conn)


# ------------------------------------------------------------------ TagiNet (gabungan nama pelanggan)

_TAGINET_CACHE = {'ts': 0.0, 'map': {}}
_TAGINET_LOCK = threading.Lock()


def taginet_map():
    path = get_setting(_db_cached(), 'taginet_db')
    if not path or not os.path.exists(path):
        return {}
    with _TAGINET_LOCK:
        if time.time() - _TAGINET_CACHE['ts'] < 300:
            return _TAGINET_CACHE['map']
        out = {}
        try:
            c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            try:
                rows = c.execute(
                    'SELECT c.pppoe, c.name, u.name FROM customers c '
                    'LEFT JOIN users u ON u.id=c.collector_id').fetchall()
                for ppp, nm, kol in rows:
                    if ppp and nm:
                        base = str(ppp).split(':')[0].strip().lower()
                        if base:
                            out[base] = {'nama': nm, 'kolektor': kol or ''}
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            return {}
        _TAGINET_CACHE['map'] = out
        _TAGINET_CACHE['ts'] = time.time()
        return out


# ------------------------------------------------------------------ aksi: kirim tugas ke ACS

def hapus_perangkat(device_id, source):
    """Hapus ONU dari GenieACS + baris lokal. Jejak tetap ada di log aksi."""
    conn = _db_cached()
    r = conn.execute('SELECT serial, pppoe_user, model FROM devices WHERE id=?',
                     (device_id,)).fetchone()
    label = (r['pppoe_user'] or r['serial'] or device_id) if r else device_id
    try:
        status, msg = nbi_delete(device_id)
        ok = 200 <= status < 300
        with DB_LOCK:
            audit(conn, device_id, label, 'hapusPerangkat',
                  'ok' if ok else f'HTTP {status}', msg, source)
            if ok:
                conn.execute('DELETE FROM devices WHERE id=?', (device_id,))
            conn.commit()
        return ok
    except Exception as e:  # noqa: BLE001
        with DB_LOCK:
            audit(conn, device_id, label, 'hapusPerangkat', 'gagal', str(e), source)
            conn.commit()
        return False


def kirim_tugas(device_id, task, source):
    """Kirim tugas + connection request. Catat hasilnya ke log aksi."""
    conn = _db_cached()
    r = conn.execute('SELECT serial, pppoe_user, model FROM devices WHERE id=?',
                     (device_id,)).fetchone()
    label = (r['pppoe_user'] or r['serial'] or device_id) if r else device_id
    try:
        if tugas_menganggur(device_id, task):
            with DB_LOCK:
                audit(conn, device_id, label, task['name'], 'dilewati',
                      'tugas serupa masih menganggur di ACS — tidak dikirim ulang', source)
                conn.commit()
            return True
        status, msg = nbi_task(device_id, task)
        ok = status in (200, 202)
        with DB_LOCK:
            audit(conn, device_id, label, task['name'],
                  'ok' if ok else f'HTTP {status}', msg, source)
            conn.commit()
        return ok
    except Exception as e:  # noqa: BLE001
        with DB_LOCK:
            audit(conn, device_id, label, task['name'], 'gagal', str(e), source)
            conn.commit()
        return False


# ------------------------------------------------------------------ penjelajah pohon data ONU

RX_RE = re.compile(r'rx\s*power|optical.*signal|redaman', re.I)
TX_RE = re.compile(r'tx\s*power', re.I)
RX_BUANG = re.compile(r'upper|lower|threshold|alarm|warning|lost|worst|average', re.I)


def nbi_device_full(device_id):
    """Ambil pohon data LENGKAP satu perangkat dari NBI (tanpa proyeksi).

    NBI tidak menyediakan GET /devices/<id> (405 Method Not Allowed) —
    cara resminya lewat endpoint daftar dengan filter query _id."""
    url = nbi_base() + '/devices/?' + urllib.parse.urlencode(
        {'query': json.dumps({'_id': device_id})})
    status, data = _http_json(url, timeout=45)
    return (data or [{}])[0]


def telusuri_optik(doc):
    """Cari parameter redaman (dBm) di pohon lengkap -> (path, nilai)."""
    temu = []

    def jalan(node, path):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            if k.startswith('_') or not isinstance(v, dict):
                continue
            p = f'{path}.{k}' if path else k
            if '_value' in v:
                if (RX_RE.search(k) and not TX_RE.search(k) and not RX_BUANG.search(k)):
                    val = v['_value']
                    try:
                        angka = float(val)
                    except (TypeError, ValueError):
                        continue
                    if 'OpticalSignalLevel' in k:
                        # Sagemcom dkk: nilai mentah mikrowatt -> dBm
                        if angka > 0:
                            temu.append((p, round(10 * math.log10(angka / 10000), 2)))
                        continue
                    nrm = normalisasi_rx(v['_value'])
                    if nrm != '':
                        temu.append((p, nrm))
            jalan(v, p)

    jalan(doc, '')
    temu.sort(key=lambda x: len(x[0]))
    return temu[0] if temu else ('', '')


def _wifi_igw(igw):
    """SSID + password dari InternetGatewayDevice.LANDevice.*.WLANConfiguration.*"""
    hasil = []
    lan = igw.get('LANDevice') or {}
    if not isinstance(lan, dict):
        return hasil
    for ld_k, ld in lan.items():
        if not isinstance(ld, dict):
            continue
        wcfg = ld.get('WLANConfiguration') or {}
        if not isinstance(wcfg, dict):
            continue
        for wc_k, wc in wcfg.items():
            if not isinstance(wc, dict):
                continue
            ssid = wc.get('SSID')
            if not (isinstance(ssid, dict) and '_value' in ssid):
                continue
            dasar = f'InternetGatewayDevice.LANDevice.{ld_k}.WLANConfiguration.{wc_k}'
            keys, kini = [], ''
            for k, v in wc.items():
                if k == 'KeyPassphrase' and isinstance(v, dict) and '_value' in v:
                    keys.append(f'{dasar}.KeyPassphrase')
                    kini = kini or str(v['_value'])
                elif 'PreSharedKey' in k and isinstance(v, dict):
                    # struktur: PreSharedKey.<n>.PreSharedKey (kadang bertingkat)
                    for pk, pv in v.items():
                        if not isinstance(pv, dict):
                            continue
                        for pk2, pv2 in pv.items():
                            if pk2 == 'PreSharedKey' and isinstance(pv2, dict) and '_value' in pv2:
                                keys.append(f'{dasar}.{k}.{pk}.{pk2}')
                                kini = kini or str(pv2['_value'])
                            elif 'PreSharedKey' in pk2 and isinstance(pv2, dict):
                                for pk3, pv3 in pv2.items():
                                    if pk3 == 'PreSharedKey' and isinstance(pv3, dict) and '_value' in pv3:
                                        keys.append(f'{dasar}.{k}.{pk}.{pk2}.{pk3}')
                                        kini = kini or str(pv3['_value'])
            hasil.append({'ssid_path': f'{dasar}.SSID', 'ssid': str(ssid['_value']),
                          'key_paths': keys, 'key': kini,
                          'band': '2.4GHz' if wc_k in ('1', '1.0') else
                                  ('5GHz' if wc_k in ('2', '2.0') else '')})
    return hasil


def _wifi_tr181(dev):
    """SSID + password dari Device.Wifi (TR-181): SSID.i dipasangkan dgn AccessPoint.i."""
    hasil = []
    wifi = dev.get('Wifi') or {}
    if not isinstance(wifi, dict):
        return hasil
    ssid_n, ap_n = wifi.get('SSID') or {}, wifi.get('AccessPoint') or {}
    if not isinstance(ssid_n, dict) or not isinstance(ap_n, dict):
        return hasil
    for i, sv in ssid_n.items():
        if not (isinstance(sv, dict) and isinstance(sv.get('SSID'), dict)
                and '_value' in sv['SSID']):
            continue
        ap = ap_n.get(i) or {}
        sec = (ap.get('Security') or {}) if isinstance(ap, dict) else {}
        keys, kini = [], ''
        if isinstance(sec, dict):
            kp = sec.get('KeyPassphrase')
            if isinstance(kp, dict) and '_value' in kp:
                keys.append(f'Device.Wifi.AccessPoint.{i}.Security.KeyPassphrase')
                kini = str(kp['_value'])
            psk = sec.get('PreSharedKey')
            if isinstance(psk, dict):
                for pk, pv in psk.items():
                    if isinstance(pv, dict) and '_value' in pv:
                        keys.append(f'Device.Wifi.AccessPoint.{i}.Security.PreSharedKey.{pk}')
                        kini = kini or str(pv['_value'])
        hasil.append({'ssid_path': f'Device.Wifi.SSID.{i}.SSID', 'ssid': str(sv['SSID']['_value']),
                      'key_paths': keys, 'key': kini, 'band': ''})
    return hasil


def telusuri_wifi(doc):
    """Kumpulkan daftar SSID + path password dari pohon data lengkap."""
    hasil = _wifi_igw(doc.get('InternetGatewayDevice') or {})
    hasil += _wifi_tr181(doc.get('Device') or {})
    return [h for h in hasil if h['ssid']]


def perkaya_wifi(did, doc):
    """Pastikan parameter WiFi ada di pohon ACS.

    GenieACS hanya menyimpan parameter yang pernah dibaca preset-nya. Kalau WiFi
    belum ada di pohon, kirim refreshObject utk sub-pohon LAN/WiFi (+ connection
    request) lalu tunggu sampai datanya masuk (maks ~9 detik)."""
    if telusuri_wifi(doc):
        return doc
    if 'InternetGatewayDevice' in doc:
        akar = 'InternetGatewayDevice.LANDevice.'
    elif 'Device' in doc:
        akar = 'Device.Wifi.'
    else:
        return doc
    try:
        if not tugas_menganggur(did, {'name': 'refreshObject', 'objectName': akar}):
            nbi_task(did, {'name': 'refreshObject', 'objectName': akar})
    except Exception:  # noqa: BLE001
        return doc
    for _ in range(6):
        time.sleep(1.5)
        try:
            doc2 = nbi_device_full(did)
        except Exception:  # noqa: BLE001
            break
        if telusuri_wifi(doc2):
            return doc2
    return doc


def simpan_temuan(did, rx_path, rx, wifi):
    conn = _db_cached()
    with DB_LOCK:
        conn.execute('UPDATE devices SET rx=?, rx_path=?, wifi=? WHERE id=?',
                     (rx, rx_path, json.dumps(wifi, ensure_ascii=False), did))
        conn.commit()


# ------------------------------------------------------------------ pindai otomatis

_PROBE_OTOMATIS = {'jalan': False}


def mungkin_probe_otomatis(conn):
    """Di tiap sinkron: pindai beberapa ONU yang belum punya redaman/WiFi.

    Ringan (maks 8 per putaran, hanya yang belum pernah berhasil dipindai) —
    seiring waktu semua ONU terisi tanpa perlu aksi manual."""
    if get_setting(conn, 'probe_otomatis', '1') != '1':
        return
    if STATE['probe'].get('running') or _PROBE_OTOMATIS['jalan']:
        return
    ids = [r['id'] for r in conn.execute(
        "SELECT id FROM devices WHERE (rx_path='' OR rx_path IS NULL"
        " OR wifi='' OR wifi IS NULL)"
        " ORDER BY pppoe_state='aktif' DESC, last_inform DESC LIMIT 8").fetchall()]
    if not ids:
        return

    def jalankan():
        _PROBE_OTOMATIS['jalan'] = True
        try:
            for did in ids:
                try:
                    doc = perkaya_wifi(did, nbi_device_full(did))
                    p, v = telusuri_optik(doc)
                    w = telusuri_wifi(doc)
                    if p or w:
                        simpan_temuan(did, p or '(tidak ada)', v, w)
                    elif doc:  # pohon terbaca tapi memang tak ada datanya
                        simpan_temuan(did, '(tidak ada)', '', [])
                except Exception:  # noqa: BLE001
                    pass  # gagal sementara -> dicoba lagi di putaran berikutnya
        finally:
            _PROBE_OTOMATIS['jalan'] = False

    threading.Thread(target=jalankan, daemon=True).start()


# ------------------------------------------------------------------ pindai massal

def jalankan_probe(ids):
    """Pindai massal: pohon lengkap + baca ulang WiFi bila perlu (4 paralel)."""
    total = len(ids)
    with STATE_LOCK:
        STATE['probe'] = {'running': True, 'done': 0, 'total': total, 'err': '', 'ketemu': 0}
    kunci = threading.Lock()
    hitung = {'n': 0, 'ok': 0}
    err = ['']

    def satu(did):
        try:
            doc = perkaya_wifi(did, nbi_device_full(did))
            p, v = telusuri_optik(doc)
            w = telusuri_wifi(doc)
            if not p:
                p = '(tidak ada)'  # penanda: sudah dipindai, redaman tak ditemukan
            if p or w:
                simpan_temuan(did, p, v, w)
                hitung['ok'] += 1
        except Exception as e:  # noqa: BLE001
            err[0] = f'{did[:24]}: {e}'
        finally:
            with kunci:
                hitung['n'] += 1
                with STATE_LOCK:
                    STATE['probe']['done'] = hitung['n']

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(satu, ids))
    with STATE_LOCK:
        STATE['probe'] = {'running': False, 'done': hitung['n'], 'total': total,
                          'err': err[0], 'ketemu': hitung['ok']}


# ------------------------------------------------------------------ watchdog

def watchdog_targets(conn):
    scope = get_setting(conn, 'watchdog_scope', 'pppoe_aktif')
    maks = max(10, int(get_setting(conn, 'watchdog_max', '150') or 150))
    ambang = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - stale_seconds()))
    q = ("SELECT id, pppoe_state, root FROM devices WHERE last_inform < ? AND last_inform != '' "
         "AND pppoe_state != 'hapus'")
    args = [ambang]
    if scope == 'pppoe_aktif':
        q += " AND pppoe_state = 'aktif'"
    q += ' ORDER BY last_inform ASC LIMIT ?'
    args.append(maks)
    return conn.execute(q, args).fetchall()


def job_watchdog(source='watchdog'):
    """ONU yang statusnya basi di ACS (padahal PPPoE aktif) -> paksa inform."""
    conn = _db_cached()
    if get_setting(conn, 'watchdog_enabled', '1') != '1' and source != 'manual':
        return
    targets = watchdog_targets(conn)
    if not targets:
        with STATE_LOCK:
            STATE['watchdog'] = {'ts': now_utc(), 'target': 0, 'ok': 0, 'fail': 0, 'err': ''}
        return
    ok = fail = 0
    try:
        conc = max(1, int(get_setting(conn, 'bulk_concurrency', '8') or 8))
        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = [ex.submit(kirim_tugas, r['id'], ping_task(r['root']), source) for r in targets]
            for f in as_completed(futs):
                if f.result():
                    ok += 1
                else:
                    fail += 1
    except Exception as e:  # noqa: BLE001
        with STATE_LOCK:
            STATE['watchdog']['err'] = str(e)
        return
    with STATE_LOCK:
        STATE['watchdog'] = {'ts': now_utc(), 'target': len(targets), 'ok': ok,
                             'fail': fail, 'err': ''}


# ------------------------------------------------------------------ aksi massal

BULKS = {}
BULK_LOCK = threading.Lock()
_bulk_seq = 0


def mulai_bulk(action, ids, source='manual', interval=3600):
    global _bulk_seq
    if not ids:
        return None
    with BULK_LOCK:
        _bulk_seq += 1
        bid = f'b{_bulk_seq}'
        BULKS[bid] = {'id': bid, 'action': action, 'total': len(ids), 'done': 0,
                      'ok': 0, 'fail': 0, 'running': True, 'ts': now_utc(), 'source': source}

    def jalankan():
        conn = _db_cached()
        conc = max(1, int(get_setting(conn, 'bulk_concurrency', '8') or 8))

        def kerja(d):
            if action == 'hapus':
                return hapus_perangkat(d, 'bulk-hapus')
            if action == 'set_inform':
                return kirim_tugas(d, set_inform_task(root_untuk(conn, d), interval),
                                   f'bulk-{action}')
            task = TASK_REBOOT if action == 'reboot' else ping_task(root_untuk(conn, d))
            if action == 'refresh':
                task = TASK_REFRESH
            return kirim_tugas(d, dict(task), f'bulk-{action}')

        with ThreadPoolExecutor(max_workers=conc) as ex:
            futs = {ex.submit(kerja, d): d for d in ids}
            for f in as_completed(futs):
                with BULK_LOCK:
                    b = BULKS[bid]
                    b['done'] += 1
                    if f.result():
                        b['ok'] += 1
                    else:
                        b['fail'] += 1
        with BULK_LOCK:
            BULKS[bid]['running'] = False

    threading.Thread(target=jalankan, daemon=True).start()
    return bid


def resolve_ids_from_filter(conn, verdict=None, q='', model=''):
    where, args = ['1=1'], []
    if model:
        where.append('model LIKE ?')
        args.append(model + '%')
    ambang = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - stale_seconds()))
    if verdict == 'ok':
        where.append("last_inform >= ? AND pppoe_state='aktif'")
        args.append(ambang)
    elif verdict == 'basi':
        where.append("(last_inform < ? OR last_inform='') AND pppoe_state='aktif'")
        args.append(ambang)
    elif verdict == 'idle':
        where.append("last_inform >= ? AND pppoe_state != 'aktif'")
        args.append(ambang)
    elif verdict == 'down':
        where.append("(last_inform < ? OR last_inform='') AND pppoe_state != 'aktif'")
        args.append(ambang)
    if q:
        like = f'%{q.lower()}%'
        where.append('(LOWER(serial) LIKE ? OR LOWER(ip) LIKE ? OR LOWER(pppoe_user) LIKE ? '
                     'OR LOWER(model) LIKE ?)')
        args += [like, like, like, like]
    rows = conn.execute(f"SELECT id FROM devices WHERE {' AND '.join(where)}", args).fetchall()
    return [r['id'] for r in rows]


# ------------------------------------------------------------------ API

class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


SESSIONS = {}


def cek_session(h):
    tok = None
    for part in (h.headers.get('Cookie') or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k == 'gops_session':
            tok = v
    if not tok or SESSIONS.get(tok, 0) < time.time():
        raise ApiError(401, 'Sesi berakhir. Silakan login lagi.')


def daftar_devices(conn, q='', verdict='', model='', page=1, per=50, sort='verdict'):
    tmap = taginet_map()
    where, args = ['1=1'], []
    if model:
        where.append('model LIKE ?')
        args.append(model + '%')
    ambang = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - stale_seconds()))
    if verdict == 'ok':
        where.append("last_inform >= ? AND pppoe_state='aktif'")
        args.append(ambang)
    elif verdict == 'basi':
        where.append("(last_inform < ? OR last_inform='') AND pppoe_state='aktif'")
        args.append(ambang)
    elif verdict == 'idle':
        where.append("last_inform >= ? AND pppoe_state != 'aktif'")
        args.append(ambang)
    elif verdict == 'down':
        where.append("(last_inform < ? OR last_inform='') AND pppoe_state != 'aktif'")
        args.append(ambang)
    if q:
        like = f'%{q.lower()}%'
        where.append('(LOWER(serial) LIKE ? OR LOWER(ip) LIKE ? OR LOWER(pppoe_user) LIKE ? '
                     'OR LOWER(model) LIKE ?)')
        args += [like, like, like, like]
    w = ' AND '.join(where)
    total = conn.execute(f'SELECT COUNT(*) c FROM devices WHERE {w}', args).fetchone()['c']
    _a = ambang  # timestamp ambang basi (aman di-inline, hasil time.strftime)
    order = {'verdict': ("CASE WHEN pppoe_state='aktif' AND last_inform < '" + _a + "' THEN 0"
                          " WHEN pppoe_state!='aktif' AND last_inform < '" + _a + "' THEN 1"
                          " WHEN pppoe_state!='aktif' THEN 2 ELSE 3 END, last_inform, serial"),
             'inform_asc': "last_inform ASC, serial",
             'serial': 'serial', 'model': 'model, serial'}.get(sort, 'serial')
    page = max(1, page)
    per = min(200, max(10, per))
    rows = conn.execute(
        f'SELECT * FROM devices WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?',
        (*args, per, (page - 1) * per)).fetchall()
    out = []
    for r in rows:
        age = age_seconds(r['last_inform'])
        v = verdict_of(age, r['pppoe_state'])
        tg = tmap.get((r['pppoe_user'] or '').lower()) or {}
        d = dict(r)
        d.pop('wifi', None)
        out.append({**d, 'age': age, 'verdict': v,
                    'verdict_label': VERDICT_LABEL[v],
                    'pelanggan': tg.get('nama', ''), 'kolektor': tg.get('kolektor', '')})
    return {'total': total, 'page': page, 'per': per, 'rows': out}


def ringkasan(conn):
    tmap = taginet_map()
    rows = conn.execute('SELECT id, last_inform, pppoe_state, pppoe_user, model FROM devices').fetchall()
    hit = {'ok': 0, 'basi': 0, 'idle': 0, 'down': 0}
    models = {}
    for r in rows:
        v = verdict_of(age_seconds(r['last_inform']), r['pppoe_state'])
        hit[v] = hit.get(v, 0) + 1
        m = model_pendek(r['model']) or '(tanpa model)'
        models[m] = models.get(m, 0) + 1
    ada_taginet = bool(tmap) if get_setting(conn, 'taginet_db') else False
    return {
        'versi': APP_VERSION,
        'n_devices': len(rows),
        'verdict': hit,
        'models': sorted(models.items(), key=lambda x: -x[1]),
        'acs_sync': STATE['acs_sync'], 'pppoe_sync': STATE['pppoe_sync'],
        'probe': STATE['probe'],
        'watchdog': STATE['watchdog'],
        'watchdog_enabled': get_setting(conn, 'watchdog_enabled') == '1',
        'watchdog_interval': int(get_setting(conn, 'watchdog_interval', '300') or 300),
        'sync_interval': int(get_setting(conn, 'sync_interval', '60') or 60),
        'stale_minutes': int(get_setting(conn, 'stale_minutes', '15') or 15),
        'taginet_aktif': ada_taginet,
        'acs_ui_url': get_setting(conn, 'acs_ui_url'),
        'n_bulks_aktif': sum(1 for b in BULKS.values() if b['running']),
    }


def handle_api(h, path, params, body):
    conn = _db_cached()
    pa = path[len('/api/'):]

    if path == '/api/login':
        pw = str(body.get('password', ''))
        if pw and pw == get_setting(conn, 'admin_pass'):
            tok = _secrets.token_hex(24)
            SESSIONS[tok] = time.time() + SESSION_DAYS * 86400
            return {'ok': True, 'token': tok}
        raise ApiError(401, 'Password salah.')

    if path == '/api/logout':
        cek_session(h)
        tok = None
        for part in (h.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'gops_session':
                tok = v
        if tok:
            SESSIONS.pop(tok, None)
        return {'ok': True}

    if path == '/api/state':
        cek_session(h)
        return ringkasan(conn)

    if path == '/api/devices':
        cek_session(h)
        return daftar_devices(
            conn, q=params.get('q', [''])[0], verdict=params.get('verdict', [''])[0],
            model=params.get('model', [''])[0],
            page=int(params.get('page', ['1'])[0] or 1),
            per=int(params.get('per', ['50'])[0] or 50),
            sort=params.get('sort', ['verdict'])[0])

    # detail satu ONU: ambil pohon lengkap -> redaman + SSID + password wifi
    m = re.match(r'^device/([^/]+)$', pa)
    if m:
        cek_session(h)
        did = urllib.parse.unquote(m.group(1))
        row = conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone()
        if not row:
            raise ApiError(404, 'Perangkat tidak ditemukan.')
        out = dict(row)
        try:
            doc = perkaya_wifi(did, nbi_device_full(did))
            p, v = telusuri_optik(doc)
            w = telusuri_wifi(doc)
            if p or w:
                simpan_temuan(did, p or row['rx_path'], v or row['rx'], w)
                out['rx'], out['rx_path'] = v or row['rx'], p or row['rx_path']
                out['wifi'] = w
        except Exception as e:  # noqa: BLE001
            out['err_detail'] = f'gagal ambil pohon data: {e}'
        if isinstance(out.get('wifi'), str):
            out['wifi'] = json.loads(out['wifi'] or '[]')
        age = age_seconds(out.get('last_inform', ''))
        out['age'] = age
        out['verdict'] = verdict_of(age, out.get('pppoe_state', ''))
        return out

    # ganti password wifi: setParameterValues ke semua path kunci SSID tsb
    if path == '/api/wifi_pass':
        cek_session(h)
        did = str(body.get('device_id', ''))
        ssid_path = str(body.get('ssid_path', ''))
        baru = str(body.get('pass', ''))
        if not (8 <= len(baru) <= 63):
            raise ApiError(400, 'Password WiFi harus 8-63 karakter.')
        row = conn.execute('SELECT wifi, rx, rx_path FROM devices WHERE id=?', (did,)).fetchone()
        if not row:
            raise ApiError(404, 'Perangkat tidak ditemukan.')
        wifi = json.loads(row['wifi'] or '[]')
        item = next((w for w in wifi if w.get('ssid_path') == ssid_path), None)
        if not (item and item.get('key_paths')):
            # coba ambil segar dari ACS (mungkin belum pernah dipindai)
            doc = perkaya_wifi(did, nbi_device_full(did))
            w_baru = telusuri_wifi(doc)
            p, v = telusuri_optik(doc)
            if p or w_baru:
                simpan_temuan(did, p, v, w_baru)
            item = next((w for w in w_baru if w.get('ssid_path') == ssid_path), None)
        if not (item and item.get('key_paths')):
            raise ApiError(400, 'Path password WiFi utk SSID ini tidak dikenali.')
        task = {'name': 'setParameterValues',
                'parameterValues': [[p, baru, 'xsd:string'] for p in item['key_paths']]}
        ok = kirim_tugas(did, task, 'wifi')
        return {'ok': ok, 'key_paths': item['key_paths']}

    # pindai massal: isi redaman + wifi utk ONU yang belum punya
    if path == '/api/probe':
        cek_session(h)
        with STATE_LOCK:
            if STATE['probe'].get('running'):
                raise ApiError(409, 'Pindai sedang berjalan.')
        if body.get('semua'):
            ids = [r['id'] for r in conn.execute(
                "SELECT id FROM devices WHERE ((rx='' AND (rx_path='' OR rx_path IS NULL))"
                " OR (wifi='' OR wifi IS NULL)) ORDER BY serial").fetchall()]
        else:
            ids = body.get('ids') or resolve_ids_from_filter(
                conn, verdict=body.get('verdict', ''), q=body.get('q', ''),
                model=body.get('model', ''))
        if not ids:
            raise ApiError(400, 'Tidak ada ONU yang perlu dipindai.')
        if len(ids) > 5000:
            raise ApiError(400, 'Terlalu banyak (maks 5000).')
        threading.Thread(target=jalankan_probe, args=(ids,), daemon=True).start()
        return {'ok': True, 'total': len(ids)}

    if path == '/api/action':
        cek_session(h)
        did = body.get('device_id', '')
        action = body.get('action', '')
        if action not in ('refresh', 'reboot', 'ping', 'hapus'):
            raise ApiError(400, 'Aksi tidak dikenal.')
        if action == 'hapus':
            ok = hapus_perangkat(did, 'manual')
            return {'ok': ok}
        if action == 'ping':
            task = ping_task(root_untuk(conn, did))
        else:
            task = {'refresh': TASK_REFRESH, 'reboot': TASK_REBOOT}[action]
        ok = kirim_tugas(did, dict(task), 'manual')
        return {'ok': ok}

    if path == '/api/bulk':
        cek_session(h)
        action = body.get('action', '')
        if action not in ('refresh', 'reboot', 'ping', 'hapus', 'set_inform'):
            raise ApiError(400, 'Aksi tidak dikenal.')
        interval = 3600
        if action == 'set_inform':
            try:
                interval = int(body.get('interval', 3600))
            except (TypeError, ValueError):
                raise ApiError(400, 'Interval tidak valid.')
            if not (60 <= interval <= 86400):
                raise ApiError(400, 'Interval harus 60-86400 detik.')
        # hapus massal TIDAK boleh tanpa filter (jangan sambar seluruh ACS)
        if (action == 'hapus' and not (body.get('ids') or body.get('verdict')
                                       or body.get('q') or body.get('model'))):
            raise ApiError(400, 'Hapus massal wajib dengan filter — mis. status Down.')
        ids = body.get('ids') or []
        if not ids:
            ids = resolve_ids_from_filter(conn, verdict=body.get('verdict', ''),
                                          q=body.get('q', ''), model=body.get('model', ''))
        if not ids:
            raise ApiError(400, 'Tidak ada ONU yang cocok dengan filter.')
        if len(ids) > 5000:
            raise ApiError(400, 'Terlalu banyak (maks 5000). Persempit filter.')
        bid = mulai_bulk(action, ids, interval=interval)
        return {'ok': True, 'bulk_id': bid, 'total': len(ids)}

    m = re.match(r'^/api/bulk/([^/]+)$', pa) or re.match(r'^bulk/([^/]+)$', path[5:])
    if m:
        cek_session(h)
        with BULK_LOCK:
            b = BULKS.get(m.group(1))
        if not b:
            raise ApiError(404, 'Bulk tidak ditemukan.')
        return dict(b)

    if path == '/api/watchdog/run':
        cek_session(h)
        threading.Thread(target=job_watchdog, kwargs={'source': 'manual'}, daemon=True).start()
        return {'ok': True, 'msg': 'Watchdog dijalankan sekarang.'}

    if path == '/api/diag':
        cek_session(h)
        rx_contoh = [{'id': r['id'], 'rx': r['rx'], 'rx_path': r['rx_path']}
                     for r in conn.execute(
                         "SELECT id, rx, rx_path FROM devices WHERE rx != '' LIMIT 5")]
        return {'versi': APP_VERSION, **STATE['diag'], 'rx_contoh': rx_contoh,
                'stale_minutes': get_setting(conn, 'stale_minutes'),
                'routers': STATE['pppoe_sync'].get('routers', []),
                'pppoe_sync_ts': STATE['pppoe_sync'].get('ts'),
                'mikrotik': json.loads(get_setting(conn, 'mikrotik_list') or '[]') and
                            [m.get('host') for m in json.loads(get_setting(conn, 'mikrotik_list'))]}

    if path == '/api/actions':
        cek_session(h)
        page = max(1, int(params.get('page', ['1'])[0] or 1))
        rows = conn.execute('SELECT * FROM actions ORDER BY id DESC LIMIT 100 OFFSET ?',
                            ((page - 1) * 100,)).fetchall()
        return {'rows': [dict(r) for r in rows]}

    if path == '/api/settings':
        cek_session(h)
        if h.command == 'POST':
            boleh = {'acs_nbi_url', 'acs_ui_url', 'mikrotik_list', 'inform_interval',
                     'stale_minutes', 'sync_interval', 'watchdog_enabled',
                     'watchdog_interval', 'watchdog_max', 'watchdog_scope',
                     'bulk_concurrency', 'taginet_db'}
            for k, v in (body or {}).items():
                if k not in boleh:
                    continue
                if k == 'mikrotik_list':
                    json.loads(v)  # validasi JSON
                set_setting(conn, k, v)
            if body.get('password_baru'):
                set_setting(conn, 'admin_pass', str(body['password_baru']))
            return {'ok': True}
        out = {k: get_setting(conn, k) for k in DEFAULTS if k != 'admin_pass'}
        out['mikrotik_list'] = json.loads(out['mikrotik_list'])
        return out

    raise ApiError(404, 'Endpoint tidak ditemukan.')


# ------------------------------------------------------------------ server HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _kirim(self, code, data=None, raw=None, ctype='application/json', cookie=None):
        body = raw if raw is not None else json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', f'gops_session={cookie}; Path=/; Max-Age=2592000; HttpOnly')
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if path == '/':
            path = '/index.html'
        fp = os.path.normpath(os.path.join(PUBLIC_DIR, path.lstrip('/')))
        if not fp.startswith(PUBLIC_DIR) or not os.path.isfile(fp):
            self._kirim(404, {'error': 'tidak ada'})
            return
        ctype = {'html': 'text/html', 'js': 'application/javascript',
                 'css': 'text/css', 'png': 'image/png', 'svg': 'image/svg+xml',
                 'json': 'application/json'}.get(fp.rsplit('.', 1)[-1], 'application/octet-stream')
        with open(fp, 'rb') as f:
            self._kirim(200, raw=f.read(), ctype=ctype)

    def _handle(self, is_post):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        body = None
        if is_post:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(min(n, 1024 * 1024))
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return self._kirim(400, {'error': 'JSON tidak valid'})
        if path.startswith('/api/'):
            try:
                out = handle_api(self, path, query, body or {})
                if path == '/api/login' and out.get('token'):
                    return self._kirim(200, out, cookie=out['token'])
                return self._kirim(200, out)
            except ApiError as e:
                return self._kirim(e.code, {'error': e.msg})
            except Exception as e:  # noqa: BLE001
                return self._kirim(500, {'error': f'{type(e).__name__}: {e}'})
        if is_post:
            return self._kirim(404, {'error': 'tidak ada'})
        self._static(path)

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)


# ------------------------------------------------------------------ loop latar

def loop(nama_fn, interval_key, default_interval):
    while True:
        try:
            nama_fn()
        except Exception as e:  # noqa: BLE001
            with STATE_LOCK:
                STATE['acs_sync']['err'] = f'loop {nama_fn.__name__}: {e}'
        conn = _db_cached()
        try:
            iv = max(10, int(get_setting(conn, interval_key, default_interval)))
        except ValueError:
            iv = int(default_interval)
        time.sleep(iv)


def main():
    init_db()
    print(f'✅ GenieOps v{APP_VERSION} berjalan di http://0.0.0.0:{PORT}  (data: {DB_PATH})')
    threading.Thread(target=loop, args=(job_sync, 'sync_interval', '60'),
                     kwargs={}, daemon=True).start()
    threading.Thread(target=_loop_watchdog, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


def _loop_watchdog():
    while True:
        conn = _db_cached()
        enabled = get_setting(conn, 'watchdog_enabled', '1') == '1'
        try:
            iv = max(60, int(get_setting(conn, 'watchdog_interval', '300') or 300))
        except ValueError:
            iv = 300
        if enabled:
            try:
                job_watchdog()
            except Exception as e:  # noqa: BLE001
                with STATE_LOCK:
                    STATE['watchdog']['err'] = str(e)
        time.sleep(iv)


if __name__ == '__main__':
    main()
