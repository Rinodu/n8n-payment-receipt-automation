# Batas keamanan proyek demo

Proyek ini menerima notifikasi pembayaran dan memiliki kewenangan menyimpan transaksi serta mengirim receipt ke satu penerima uji. Tujuannya menjaga asal pembayaran, mencegah efek ganda saat retry, melindungi data pribadi, dan memungkinkan pemulihan tanpa menebak status email.

## Kontrol yang diterapkan

1. Endpoint demo /payments ditolak pada mode live. Endpoint Midtrans memverifikasi SHA512 resmi, mengambil status langsung dari host sandbox tetap, memeriksa status lunas/fraud/currency/amount/order, dan mengambil email dari order lokal tepercaya. Redirect API keluar ditolak.
2. Payment ID unik, insert atomik, fingerprint HMAC dan lock OS lintas proses. Payload wajib yang berubah pada ID sama ditolak. Lock tidak menggunakan lease yang dapat habis ketika panggilan eksternal masih berlangsung.
3. Tahap disimpan sebelum efek berikutnya. Retry Airtable memakai upsert; retry email memakai body beku dan kunci idempotensi stabil. Email uncertain yang lebih tua dari 23 jam ditahan untuk rekonsiliasi.
4. Payload, pesanan dan pesan terenkripsi Fernet; key live berasal dari secret environment terpisah. INDEX_KEY yang stabil memberi fingerprint privat. Backup terenkripsi memakai snapshot konsisten dan restore non-overwrite dengan pemeriksaan integritas.
5. Live recipient allowlist, auth internal dengan perbandingan konstan dan dukungan dua key saat rotasi; secret tidak masuk workflow importable.
6. Batas body, penolakan JSON ambiguous, timeout, cap handler, rate limit, cache-control no-store dan metrics privat. Container processor tanpa root/kapabilitas tambahan; endpoint admin hanya CLI lokal.
7. Retensi payload selesai dan email Airtable, backup lokal terjadwal, audit dengan referensi HMAC tanpa payload dan indikator pekerjaan gagal.
8. Publikasi allowlist dan heuristic secret scanning; sampel harus fiktif dan workflow tidak membawa credential.

## Risiko dan batas yang masih berlaku

- Tidak menjamin exactly-once universal: SQLite, Airtable dan Resend tidak berbagi transaksi atomik. Gangguan setelah provider menerima email perlu idempotensi provider atau rekonsiliasi. Keterlambatan/kehilangan ledger atau restore lama dapat melewati jendela provider.
- Penguncian hanya untuk **satu host dengan filesystem lokal**. Multi-process lokal didukung; multi-host, network filesystem dan HA cluster tidak didukung. Semua proses yang berbagi ledger harus memakai konfigurasi key/base/provider yang sama. Serial dispatcher membatasi throughput.
- Mode live berarti Midtrans sandbox dan provider Airtable/email asli. Dukungan gateway produksi atau gateway lain tidak diasumsikan; tidak ada charging, refund atau pemenuhan barang.
- Enkripsi bukan perlindungan terhadap administrator atau proses yang sudah menguasai host dan key. Metadata ID/status/timestamp tidak dienkripsi. Demo menyimpan key lokal di folder data untuk kenyamanan; mode live wajib memisahkan key. Gunakan enkripsi volume host untuk metadata.
- DPAPI hanya portable pada user/PC yang sama. Kehilangan key tanpa backup terpisah menyebabkan data tidak dapat dipulihkan. Jangan menghapus INDEX_KEY atau mengganti base ketika ada pekerjaan tertunda.
- TLS/routing publik/CDN/firewall perlu diverifikasi di host tujuan. Rate limiter ini per instance, bukan filter DDoS terdistribusi. Library HTTP Python dibatasi dan diletakkan di belakang proxy; bukan server internet mandiri.
- Belum ada webhook bounce/delivery. sent berarti provider menerima request, bukan inbox delivery. Retensi provider/email inbox dan order belum dibayar harus diatur operator.
- Pemeriksaan rahasia hanya mengenali pola umum; tidak membuktikan semua rahasia/data pribadi hilang. Jangan merekam dashboard/clipboard/environment privat.
- Menghapus payload lokal tidak menjamin secure erase terhadap snapshot disk lama, backup off-host, atau log provider. Retensi salinan tersebut perlu pengaturan masing-masing.
- Backup otomatis lokal bukan disaster recovery off-host. Restore harus diuji dan direkonsiliasi sebelum worker diaktifkan.
- Integrasi akun asli, TLS publik dan deployment tidak boleh diklaim lulus berdasarkan mock tests saja; lihat TESTING untuk bukti yang benar-benar dijalankan.

## Jika ada indikasi kebocoran

Hentikan ingress/worker sesuai kebutuhan, pertahankan ledger dan bukti privat, cabut key terkait di provider, perbarui konfigurasi privat, lalu rekonsiliasi transaksi pending/sending. Jangan menghapus ledger untuk menyembunyikan error. Jangan memasukkan rahasia atau data pelanggan dalam issue publik; laporkan secara privat kepada pengelola proyek.

Rujukan: [OWASP Webhook Security](https://cheatsheetseries.owasp.org/cheatsheets/Webhook_Security_Cheat_Sheet.html), [Midtrans notifications](https://docs.midtrans.com/docs/https-notification-webhooks), [Resend idempotency](https://resend.com/docs/dashboard/emails/idempotency-keys), [Fernet](https://cryptography.io/en/latest/fernet/).
