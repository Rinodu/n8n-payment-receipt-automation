# Bukti verifikasi v2 — 29 Oktober 2026

Lingkungan: Windows, Python 3.12.10, cryptography 50.0.1, Node 24.19.0, n8n 2.45.5 dan Caddy 2.11.7. Semua pembayaran/penerima dalam pengujian bersifat fiktif. Email eksternal terkirim: **0**.

## Pengujian Python

`python -m unittest -q` — **43 tests, OK**, 10,906 detik pada run verifikasi setelah perbaikan respons HTTP Windows.

| Area | Bukti yang dijalankan |
|---|---|
| Input dan uang | Payload salah, amount bool/string/negatif/pecahan, email injection, ID/status/currency/timestamp salah ditolak |
| Deduplikasi | ID sama setelah restart, request bersamaan, konflik isi, dan ID berbeda |
| Antrean | Persistensi, retry/backoff, delapan kegagalan menjadi dead, attention_required |
| Lintas proses | Empat proses Python bersaing; hanya satu attempt dan satu receipt |
| Proses mati | OS lock lepas setelah pemilik dibunuh; transaksi pending dilanjutkan |
| Email uncertain | Timeout/retry memakai header dan body sama; proses diputus setelah fixture provider menerima, lalu retry memakai identitas yang sama |
| Batas idempotensi | Email uncertain melewati 23 jam ditahan untuk pemeriksaan manual |
| Signature gateway | SHA512 valid, palsu ditolak sebelum status API, status pending/fraud/mata uang/identitas/nominal salah ditolak |
| Pesanan tepercaya | Email dari order lokal; email/status tambahan pada notification tidak dipercaya; order tak dikenal ditolak |
| Enkripsi | Email tidak ditemukan sebagai plaintext dalam file SQLite atau backup terenkripsi |
| Backup/restore | Snapshot dipulihkan, deduplikasi/outbox tetap ada, corrupt backup ditolak, overwrite ditolak |
| Rotasi | Data dapat dirotasi ke key baru; key lama sendiri tidak membuka payload hasil rotasi; fingerprint tetap bekerja |
| Retensi | Payload/message/outbox/order selesai dibersihkan; fingerprint masih mencegah pemrosesan ulang; Email Airtable dikosongkan lewat saved record ID pada fixture |
| Migrasi v1 | Sumber plaintext tetap utuh; target terenkripsi; sent tetap duplicate; sending lama ditahan manual |
| HTTP | Auth, ukuran, JSON malformed/key ganda, live route bypass, rate limiting dan security headers; 24 penolakan berulang dengan body belum dibaca tetap mengembalikan 401/413 tanpa reset koneksi |
| Service nyata | Subprocess processor.py menerima HTTP, worker memproses, backup otomatis dibuat dan metrics menampilkan waktunya |
| Audit | Log tidak memuat email/payment_id mentah/key; referensi transaksi memakai HMAC |
| Publikasi | File privat dan database tidak masuk ZIP, key umum/literal env ditolak, sampel nyata dan credential workflow ditolak |

Tes transport provider membuktikan bentuk request dan penanganan kegagalan, **bukan** keberhasilan akun Airtable/Midtrans/Resend asli.

## n8n asli: delapan kasus lulus

Kedua file workflow diimpor dan dipublikasikan pada instance n8n lokal yang diisolasi. Request HTTP benar-benar melewati Webhook → HTTP Request → Respond to Webhook, kemudian layanan processor dan worker lokal.

| Kasus | HTTP | Hasil |
|---|---|---|
| Header auth salah | 403 | Ditolak |
| Payload demo valid | 202 | queued |
| Pengiriman ulang | 200 | duplicate |
| ID sama dengan nominal berbeda | 409 | Ditolak |
| Nominal tidak valid | 422 | Ditolak |
| Midtrans signature palsu | 422 | Ditolak |
| Midtrans signature fixture valid | 200 | queued |
| Pengulangan notification gateway | 200 | duplicate |

Hasil: tepat **dua** receipt simulasi untuk dua transaksi, tanpa email eksternal. Bukti JSON: `n8n-test-result.json`. GET status Midtrans dan provider penyimpanan/email memakai fixture. Rahasia instance, credential impor dan database runtime hanya berada di folder kerja privat, tidak masuk paket publik.

Pada versi n8n ini publikasi dilakukan melalui layanan instance yang berjalan agar registrasi webhook ikut terbentuk. Flag CLI import activeState=fromJson tidak mendukung mode regular; status active pada database saja bukan bukti webhook sudah bekerja. Pengujian di atas memverifikasi request nyata setelah publikasi.

Runtime lokal memberi peringatan Python task runner internal tidak tersedia; workflow ini tidak menggunakan Python/Code node n8n. Layanan Python pendamping berjalan terpisah dan diuji. Deployment container/task runner tujuan belum diuji di host produksi.

## HTTPS dan routing lokal

Caddyfile yang didistribusikan berhasil diparse oleh Caddy 2.11.7. Konfigurasi hasil adaptasi dipakai untuk tes HTTPS lokal, dengan perubahan bind/upstream dan sertifikat uji saja.

- Client memverifikasi sertifikat localhost menggunakan trust anchor eksplisit; verifikasi TLS tidak dimatikan.
- `/`, `/rest/settings`, `/metrics`, `/payments` melalui proxy: 404.
- Hanya `/webhook/portfolio-midtrans-sandbox` diteruskan: 200 dari fixture upstream.
- Header no-store dan nosniff diterapkan.
- Body >16 KB: 413.

Bukti JSON: `deployment-test-result.json`. Ini membuktikan TLS/routing lokal, **bukan** DNS publik, ACME certificate issuance, firewall host, Docker image atau deploy server.

## Windows dan rahasia

- Parser PowerShell: semua script tanpa syntax error.
- configure.ps1 mode demo benar-benar dijalankan di folder kerja terisolasi: settings disimpan sebagai SecureString dengan DPAPI, ACL folder dibatasi, tidak ada key yang dicetak.
- start-private.ps1 -Status benar-benar membuka konfigurasi dan menjalankan admin status dengan sukses.
- Pengujian dilakukan memakai Windows PowerShell dan PowerShell 7. Tidak memakai credential akun nyata.

## Demo portabel dan paket

`python run_demo.py` lulus: HTTP queued → duplicate, satu receipt simulasi. Lihat `demo-result.json`.

`python publish.py` membuat ZIP dari daftar file eksplisit, memeriksa pola key dan menyertakan MANIFEST.json berisi SHA256 setiap file publik. Pengujian publikasi menyertakan file privat jebakan dan memastikan file tersebut tidak masuk ZIP. Heuristic scanning bukan jaminan tidak ada rahasia yang tidak dikenali.

## Integrasi asli di luar penyelesaian demo lokal

Pengguna memilih menyelesaikan demo lokal dahulu tanpa akun provider. Pemeriksaan berikut ditunda untuk tahap integrasi asli; bukan syarat menjalankan atau menuntaskan demo lokal ini.

| Pemeriksaan | Status |
|---|---|
| PAT, schema dan upsert di base Airtable asli | Belum: akun belum terhubung |
| Notifikasi dan GET status dari transaksi Midtrans Sandbox asli | Belum: server key/account sandbox belum dikonfigurasi |
| Sender/Resend API acceptance dan inbox uji milik pengguna | Belum: akun/sender/penerima uji belum dikonfigurasi dan disetujui |
| Docker compose pada host tujuan | Belum: runtime Docker tidak tersedia pada pemeriksaan awal |
| DNS domain, sertifikat publik dan firewall | Belum: domain/host tujuan belum ditentukan |
| Backup off-host dan recovery pada host lain | Belum: tujuan backup/host belum ditentukan |

Jangan mengubah status gerbang ini menjadi lulus berdasarkan unit test atau konfigurasi saja. Panduan konkret tersedia di OPERATIONS.md. Tidak ada email ke pihak nyata atau deployment publik yang dilakukan dalam verifikasi ini.
