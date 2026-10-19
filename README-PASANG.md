# 📡 GenieOps — Pendamping GenieACS untuk ISP

Aplikasi web **self-hosted** yang mengatasi 3 masalah operasional GenieACS:

| Masalah di GenieACS | Solusi GenieOps |
|---|---|
| ONU terpantau **offline padahal online** | **Watchdog otomatis**: ONU yang statusnya basi di ACS (padahal sesi PPPoE-nya aktif = ONU jelas hidup) dipaksa mengirim inform lewat *connection request* — tanpa menekan tombol refresh satu per satu, **tanpa limit 80 perangkat** |
| **Pencarian ONU berat** | Seluruh perangkat di-*index* di SQLite lokal — cari per **serial / IP / user PPPoE / nama pelanggan / penagih** dalam sekejap |
| Refresh/reboot harus satu-satu atau maks 80 | **Aksi massal** untuk hasil filter apa pun (semua / ACS basi / down / per model) dengan paralel terkendali + log lengkap |

**Kolom "Status" menjelaskan keadaan sebenarnya tiap ONU:**

| Verdict | Arti |
|---|---|
| ✅ Normal | ACS segar + PPPoE aktif |
| 🟡 ACS basi (ONU hidup) | ACS bilang offline, **padahal PPPoE aktif** → sasaran watchdog otomatis |
| 🔔 PPPoE idle | ONU hidup tapi pelanggan sedang tidak dial (mis. diisolir) |
| 🔴 Down | ACS basi + PPPoE mati → kemungkinan benar-benar mati, perlu cek lapangan |

Sumber data: **NBI GenieACS** (port 7557) untuk daftar perangkat & tugas,
**Mikrotik** (RouterOS API) untuk status sesi PPPoE pelanggan,
dan (opsional) **TagiNet** untuk nama pelanggan + penagih.

---

## 🚀 Cara Pasang (Docker / CasaOS)

1. Salin folder `genieops` ke server (mis. `/DATA/AppData/genieops/`):
   ```
   /DATA/AppData/genieops/app/app.py
   /DATA/AppData/genieops/app/routeros_api.py
   /DATA/AppData/genieops/app/public/…
   /DATA/AppData/genieops/data/          (kosong, db terbentuk sendiri)
   ```

2. Pasang lewat CasaOS → **Docker Compose**, salin isi `docker-compose.yml`
   (sesuaikan `GENIEACS_URL`, `MIKROTIK_HOST/USER/PASS`, dan password).
   Atau lewat SSH:
   ```bash
   cd /DATA/AppData/genieops
   mkdir -p data
   docker run -d --name genieops --restart unless-stopped \
     -p 3040:3000 -e TZ=Asia/Jakarta -e DATA_DIR=/data \
     -e GENIEOPS_PASSWORD=passwordku \
     -e GENIEACS_URL=http://IP-GENIEACS:7557 \
     -e ACS_UI_URL=http://IP-GENIEACS:3000 \
     -e MIKROTIK_HOST=IP-MIKROTIK -e MIKROTIK_USER=apisaya -e MIKROTIK_PASS=passwordapi \
     -v $PWD/app:/app -v $PWD/data:/data \
     -w /app python:3.13-alpine python app.py
   ```

3. Buka `http://IP-SERVER:3040` → login dengan password → buka menu
   **Pengaturan** → sesuaikan (URL ACS, daftar Mikrotik — bisa lebih dari satu,
   batas "basi", interval watchdog, path db TagiNet).

> **Prasyarat jaringan**: server GenieOps harus bisa (1) mengakses NBI GenieACS
> (7557), (2) mengakses API Mikrotik (8728), dan (3) **menjangkau IP ONU**
> (connection request dari ACS ke ONU — pastikan routing/firewall membolehkan,
> dan credential Connection Request ONU sesuai). Kalau (3) tidak terpenuhi,
> watchdog tetap mencatat ONU basi tapi tidak bisa memaksa inform.

## 🔧 Yang terjadi di belakang layar

- Tiap **±60 detik**: tarik seluruh perangkat dari NBI + sesi `/ppp/active`
  dari semua Mikrotik → cocokkan (user PPPoE / IP) → simpan ke SQLite.
- Tiap **±5 menit** (watchdog): cari ONU "🟡 ACS basi yang PPPoE-nya aktif"
  → kirim *connection request* + tugas ringan `refreshObject` (paralel 8)
  → ACS langsung dapat inform baru → status kembali benar.
- Semua aksi (watchdog/manual/massal) tercatat di menu **Log Aksi**.

## 💡 Saran tuning GenieACS (sekalian)

Di GenieACS → Admin → Presets, pastikan preset default **"inform"** aktif agar
semua ONU: PeriodicInformEnable=1, interval 300 detik, dengan *offset unik per
perangkat* (beban ACS merata) — ini mengurangi "offline palsu" dari sumbernya.

## Redaman PON, SSID & Ganti Password WiFi (v1.2+)

- **Kolom Redaman** di dashboard: nilai dBm otomatis dari parameter optik yang umum
  (ZTE `X_ZTE-COM_WANPONInterfaceConfig.RXPower`, Huawei `X_GponInterafceConfig.RXPower`, dll).
  Hijau ≥ -24 dBm, kuning -24 s/d -27, merah < -27 (fiber perlu diperiksa).
- **Klik baris ONU** -> modal detail: redaman, PPPoE, dan daftar SSID beserta password saat ini.
- **Ganti password WiFi** langsung dari modal (tombol 💾): kirim `setParameterValues`
  ke semua path kunci SSID tsb (PreSharedKey + KeyPassphrase) + connection request.
- Tombol **🔎 Pindai Redaman+WiFi**: sekali jalan mengambil pohon data lengkap tiap ONU
  yang belum punya data — menemukan path optik/WiFi merek apa pun (termasuk yang eksotis),
  lalu tersimpan dan diperbarui otomatis saat sinkron.
- Uji end-to-end: `python3 test_e2e.py` (46 tes, mock NBI + mock RouterOS).

## Hapus massal ONU mati/diganti (v1.4)

Untuk ONU yang sudah diganti unit baru tapi masih terdaftar di GenieACS:
filter **Status: 🔴 Down** → tombol **🗑 Hapus massal** → ketik `HAPUS` untuk konfirmasi.
Perangkat dihapus dari GenieACS (`DELETE /devices/<id>`) dan dari daftar GenieOps;
jejaknya tetap tersimpan di Log Aksi (`hapusPerangkat`).
Pengaman: hapus massal **wajib dengan filter** (status/pencarian/model) — tidak bisa
menghapus seluruh ACS sekaligus. Hapus satu ONU juga bisa dari tombol 🗑 di modal detail.

## Tampilan HP untuk teknisi (v1.5)

Di layar ≤ 760px, daftar ONU otomatis berubah dari tabel menjadi **kartu** yang enak
disentuh: status + serial + model, user PPPoE, redaman, umur inform, pelanggan, dan
tombol ↻/⏻ besar. Ketuk kartu → detail lengkap (modal bergaya lembar-bawah).
Kotak pencarian satu baris penuh, KPI grid 2×2, tombol massal selebar layar,
font input 16px (anti-zoom iOS). Ganti orientasi/putar layar → tampilan menyesuaikan otomatis.

## Redaman & nama model (v1.6)

- **Nilai redaman dinormalisasi ke dBm** — sebagian firmware melaporkan dalam satuan lain:
  `-251` (deci-dBm) → `-25.1`, `8` (µW) → `-21`, `0.008` (mW) → `-21`, `'-21.3 dBm'` (dgn satuan) → `-21.3`.
- **Pindai otomatis latar belakang**: tiap sinkron, maks 8 ONU yang belum punya data
  redaman/WiFi dipindai pelan-pelan — ONU baru terisi sendiri seiring waktu tanpa aksi manual
  (bisa dimatikan di Pengaturan). Tombol 🔎 Pindai tetap ada utk mengisi semua sekaligus.
- **Nama model dipendekkan** di tabel/kartu/filter: potong sebelum `%` dan `_`
  (`HSGQ%2DX130W` → `HSGQ`, `TOTOLINK_N355RT` → `TOTOLINK`); filter model mencocokkan awalan
  sehingga semua varian satu merek ikut terpilih. Di modal detail tertera model utuh
  (`ZL%2D2113X` → `ZL-2113X`).
- Modal detail menampilkan **sumber nilai redaman** (path parameter) — berguna kalau ada
  nilai yang dicurigai salah; `/api/diag` juga menyertakan contoh `rx_contoh`.

---

## 🔄 Cara Update ke Versi Baru

File aplikasi hanya `app.py` + `public/` (data tetap aman di `data/`):

```bash
cd /DATA/AppData/genieops/app
cp app.py app.py.bak            # cadangkan versi lama
# salin app.py dan isi folder public/ dari versi baru (scp/rsync), lalu:
docker restart genieops
```

Migrasi database (mis. kolom baru) berjalan otomatis saat aplikasi start.

## ⏱ Interval inform & kesehatan server ACS (v1.7)

Interval inform yang pendek (5 menit) pada ±1.900 ONU membuat `genieacs-cwmp`
jenuh terus-menerus — CPU 100%, server panas, antrean tugas membengkak. Rekomendasi:

1. Set interval inform ONU ke **30–60 menit** lewat tombol **⏱ Interval inform**
   di dashboard (aksi massal mengikuti hasil filter; path otomatis menyesuaikan
   ONU lama `InternetGatewayDevice.*` vs ONU TR-181 `Device.*`).
2. Pastikan **batas "basi"** (`stale_minutes`) LEBIH BESAR dari interval inform.
   Kalau lebih kecil, semua ONU terlihat basi dan watchdog menghajar tanpa henti —
   inilah sumber badai 50 ribu tugas di GenieACS.
3. Sejak v1.7, GenieOps **tidak mengirim tugas baru bila tugas serupa masih
   menganggur** di antrean ACS (anti penumpukan), dan tugas ping memakai path
   yang benar per ONU (menghilangkan fault "Invalid parameter path").
