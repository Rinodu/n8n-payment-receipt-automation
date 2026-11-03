# Operasi dan integrasi akun

## Rahasia dan akses lokal

Windows: install requirements, jalankan `configure.ps1 -Mode demo` atau `-Mode live`, lalu `start-private.ps1`. Script membatasi ACL folder `.private` dan `data` ke pengguna saat ini. Script tidak mengubah akun sistem atau konfigurasi proyek lain. DPAPI mengikat settings.xml ke pengguna/PC ini; file itu **bukan** backup key yang portabel.

Untuk perintah admin yang membutuhkan key dari settings, muat pada terminal privat:

```powershell
$settings = Import-Clixml .private/settings.xml
foreach ($name in $settings.Keys) {
    [Environment]::SetEnvironmentVariable($name, [Net.NetworkCredential]::new('', $settings[$name]).Password, 'Process')
}
python admin.py status
```

Setelah selesai, tutup terminal atau hapus semua environment bernama pada settings. Jangan menjalankan Get-ChildItem Env: atau mencetak `$settings` pada rekaman. Bila clipboard diperlukan untuk mengisi credential n8n milikmu, ambil hanya satu key melalui terminal privat, paste ke credential manager, lalu kosongkan clipboard. Jangan memasukkan key ke parameter node/source/chat. Windows clipboard history bisa menyimpan key; matikan history pada saat penanganan rahasia atau gunakan pengelola password.

Simpan DATA_KEYS dan INDEX_KEY di password manager terpisah, beserta MODE, base ID dan identitas provider. PROCESSOR_KEYS dan N8N_ENCRYPTION_KEY juga perlu dipulihkan. Backup database tanpa key tidak dapat dibuka. Jangan mengemas key bersama backup. Pada Linux gunakan secret manager/file berizin 0600 milik user service. Environment melindungi dari Git, tetapi tetap dapat dibaca proses/admin berizin; ini bukan perlindungan terhadap komputer yang sudah dikuasai penyerang.

## Airtable

Buat base demo dan tabel Transactions. Field harus persis:

| Nama | Tipe |
|---|---|
| Payment ID | Single line text, primary |
| Order ID | Single line text |
| Email | Email |
| Amount | Number, integer rupiah |
| Currency | Single line text |
| Paid At | Date dengan time |
| Status | Single line text |

Buat PAT hanya untuk base demo dengan `data.records:read` dan `data.records:write`. Simpan AIRTABLE_TOKEN, AIRTABLE_BASE_ID (`app...`) dan AIRTABLE_TABLE_ID (`tbl...`) privat. Processor menggunakan upsert Payment ID; Airtable bukan ledger utama pencegahan duplikasi. Jangan membuat duplikat manual dan jangan mengubah base/table ketika masih ada pekerjaan tertunda. Untuk pindah base, lakukan migrasi/review tersendiri.

## Resend dan penerima uji

Siapkan akun Resend, API key pengiriman, dan sender/domain terverifikasi sesuai akun. Atur RESEND_API_KEY, EMAIL_FROM dan TEST_RECIPIENT. Penerima harus alamat uji milikmu yang kamu setujui menerima receipt. Semua alamat lain ditolak. Jangan memasukkan alamat pelanggan atau menghapus allowlist untuk uji coba.

Status sent adalah API acceptance. Cek inbox dan dashboard untuk delivery/bounce; callback delivery/bounce belum diterapkan. Atur retensi data di akun provider; purge lokal tidak menghapus salinan email di provider/inbox. Quota dan harga bukan diasumsikan gratis, periksa akun masing-masing.

## Midtrans Sandbox dan pesanan tepercaya

1. Buka dashboard sandbox Midtrans dan ambil Server Key sandbox dari pengaturan access keys, simpan sebagai MIDTRANS_SERVER_KEY. Jangan memakai production key.
2. Buat pesanan sandbox milikmu melalui alur resmi Midtrans. Catat order_id/amount/currency/email yang sesuai dalam file privat; jangan mengubah sample publik menjadi data pribadi.
3. Pada terminal dengan environment privat, `python admin.py register-order path-ke-order-privat.json`. Perintah menolak perubahan pesanan dengan ID sama. Registrasi hanya lokal/tepercaya, tidak ada endpoint publik untuk menimpa pesanan.
4. Jalankan layanan MODE=live. Impor workflow.midtrans, isi credential internal X-Processor-Key, lalu publish. Atur Notification URL di dashboard sandbox ke Production URL workflow HTTPS tersebut.
5. Selesaikan simulasi pembayaran sandbox. Signature diperiksa, status diambil langsung dari `api.sandbox.midtrans.com`, lalu cocokkan record, queue dan inbox uji.
6. Kirim ulang notifikasi melalui fasilitas sandbox; satu transaksi dan satu email. Simulasikan nominal salah/pending pada fixture lokal, bukan mengubah transaksi nyata.

Koneksi provider tidak dibuktikan oleh transport mock atau file konfigurasi. Hanya tandai lulus setelah pengecekan akun dan hasil API/inbox asli dilakukan. n8n Cloud membutuhkan service reachable lewat HTTPS dengan credential internal; tidak dapat menjangkau localhost PC.

## HTTPS dan deployment

compose.yaml menyediakan processor tanpa port publik dan editor n8n hanya `127.0.0.1:5678`. Gunakan password owner n8n yang kuat dan MFA bila tersedia. `.env` harus diisi privat; tidak otomatis dibaca ketika menjalankan Python langsung.

Untuk server publik: set RECEIPT_DOMAIN ke domain milikmu yang DNS-nya menuju host, WEBHOOK_URL ke `https://domainmu/`, dan buat key N8N_ENCRYPTION_KEY terpisah. Jalankan `docker compose --profile public up -d --build`. Caddy hanya mempublikasikan `/webhook/portfolio-midtrans-sandbox`, menerapkan HTTPS dan batas body 16 KiB. Editor, processor, metrics, dan admin tidak dirutekan publik. Port 80/443 dan penerbitan sertifikat harus diizinkan host. Jangan mengaktifkan profil publik memakai domain contoh atau domain yang bukan milikmu.

Caddy ini tidak membawa plugin rate limiting: pembatasan di processor per menit/per instance masih berlaku, dan reverse proxy/CDN perlu pembatasan per sumber untuk skala internet. Jangan mempercayai X-Forwarded-For tanpa konfigurasi proxy tepercaya. Set log akses proxy agar tidak menyimpan request body/auth header.

Docker/host/TLS membutuhkan verifikasi di lingkungan tujuan; adanya compose bukan bukti deploy sukses. Gunakan volume persisten, user tanpa root, backup off-host terenkripsi dan pembaruan image/dependency. Pin versi setelah uji upgrade, jangan mengganti versi hanya untuk menghilangkan warning dependency.

## Antrean, retry, dan daftar gagal

Tahap pembayaran: received → stored → sending → sent. Queue: pending/retry/done/dead/manual.

```powershell
python admin.py status
```

Metrics berisi jumlah pekerjaan, umur pending tertua, status backup, dan attention_required. Endpoint `/metrics` memerlukan key internal; `/health` tidak memuat data pelanggan. Audit memakai referensi HMAC dan event pendek; tidak mencatat key, email atau payload. Hubungkan metrics ke monitoring milikmu; threshold yang disarankan untuk demo: dead/manual >0 atau pending tertua >5 menit. Sistem tidak mengirim pesan/alert ke orang lain otomatis.

Worker memeriksa tiap 0,5 detik. Setelah gagal, jeda 2^attempt detik (batas 1 jam) dengan jitter, maksimal 8 percobaan lalu dead. Perbaiki konfigurasi/provider dahulu. Untuk pekerjaan yang belum sent dan masih dalam jendela email:

```powershell
python admin.py retry pay_demo_001 --evidence ticket-internal-123
```

Jangan memberi payment_id baru untuk memaksa kirim ulang. Kunci Resend hanya bertahan 24 jam, sehingga layanan menghentikan retry email pada usia 23 jam. Setelah itu cek dashboard provider. Bila ada bukti email sudah diterima API:

```powershell
python admin.py reconcile-sent pay_demo_001 --provider-id id-yang-terverifikasi --evidence referensi-provider-123
```

Perintah tidak boleh diisi bukti rekaan. CLI menyimpan hash referensi bukti, bukan isi sensitifnya. Bila status belum pasti, tahan di manual. Reset otomatis melewati batas 23 jam sengaja dilarang. Jika terbukti tidak pernah dikirim, rancang resend terpisah yang direview; jangan edit ledger secara sembarang.

## Backup, restore dan retensi

Saat layanan mulai dan tiap 24 jam, backup konsisten dibuat lewat SQLite backup API lalu dienkripsi. Backup otomatis berada di data/backups, ditahan 7 hari. Salin backup terenkripsi ke lokasi off-host privat sesuai kebutuhan. Key disimpan terpisah.

```powershell
python admin.py backup backup-privat/snapshot.fernet
python admin.py restore backup-privat/snapshot.fernet restore-baru.sqlite3
```

Restore menolak overwrite dan memeriksa integritas. Pulihkan ke file/folder baru, hentikan layanan, gunakan key yang sama, lalu verifikasi row count/deduplikasi sebelum memindahkan layanan ke ledger hasil restore. Jangan mengganti database hidup. Backup bisa lebih tua dari email yang sudah terkirim: **jangan langsung menjalankan worker pada restore lama**. Bandingkan interval sejak backup dengan provider dan ledger terakhir; tandai pengiriman yang sudah terjadi sebelum membuka worker. Tanpa rekonsiliasi, snapshot lama dapat membuat email lama terkirim lagi setelah jendela idempotensi provider habis.

Retensi default: payload/message lokal, outbox dan salinan Airtable demo dibersihkan 30 hari setelah transaksi selesai; email pada record Airtable live dibuat kosong berdasarkan record_id. Order yang telah selesai dan tidak lagi dirujuk payload aktif ikut dibuang. Order belum dibayar tidak dihapus otomatis; tentukan masa penyimpanan pesanan terpisah sesuai kebutuhan bisnis sebelum dipakai nyata. Metadata ID/fingerprint/status tetap disimpan untuk deduplikasi. Backup lama dapat membawa data hingga 7 hari tambahan. Salinan off-host dan provider harus mengikuti retensi tersendiri.

```powershell
python admin.py maintain
```

RETENTION_DAYS dan BACKUP_DAYS minimal 1. Maintenance yang gagal dicatat di metrics dan dicoba lagi; periksa ruang disk dan akses provider. Enkripsi kolom bukan enkripsi seluruh disk: ID/timestamp/status masih terlihat. Gunakan BitLocker atau enkripsi volume host untuk metadata dan backup key yang dilindungi.

## Rotasi key

- PROCESSOR_KEYS mendukung `baru,lama`; layanan memeriksa keduanya. Perbarui credential n8n ke baru, verifikasi request, baru hapus lama. Jangan menaruh key pada URL.
- DATA_KEYS mendukung `baru,lama`; key pertama dipakai menulis, semua key dipakai membuka. Hentikan layanan, backup, masukkan kedua key melalui secret manager, lalu `python admin.py rotate-data`. Jalankan kembali dan tes. Jangan mengubah INDEX_KEY saat rotasi: ia menjaga fingerprint transaksi lama.
- Simpan key lama sampai semua backup yang membutuhkan key tersebut sudah lewat retensi atau diuji dengan prosedur rotasi backup. Jangan menghapus key lama hanya karena database aktif sudah terbuka.
- Rotasi PAT/Resend/Midtrans dilakukan di akun provider, perbarui service privat lalu tes. Jangan mengubah EMAIL_FROM/base atau body receipt untuk pekerjaan yang sudah sending; body dan tujuan email lama dibekukan.
- N8N_ENCRYPTION_KEY menjaga credential n8n; jangan menggantinya tanpa prosedur migrasi resmi n8n.

## Migrasi paket v1

V2 menolak ledger plaintext lama. Hentikan v1 terlebih dahulu:

```powershell
python migrate_legacy.py path-mutlak/receipts-demo.sqlite3 data-v2
```

Sumber tetap utuh, target harus folder baru, backup legacy terenkripsi dibuat sebelum membaca rows. Transaksi sent tetap duplicate; sending lama masuk manual karena kunci idempotensi v1 berbeda. V1 tidak memiliki lock OS yang sama: operator wajib memastikan v1 berhenti. Jangan membagikan sumber plaintext. Setelah validasi migrasi dan backup, penghapusan sumber lama harus menjadi tindakan operator yang disengaja.
