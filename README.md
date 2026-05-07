# Pastelink Batch Domain Bot

Bot Telegram terpisah, khusus untuk:
- menampilkan list paste dari akun Pastelink,
- ganti domain link secara massal (batch/all/global),
- mode panel tombol `/change` (1 pesan, user-friendly),
- mode aman `dry-run` dan mode eksekusi `apply`.

Bot ini sengaja fokus 1 tugas, supaya tidak bentrok dengan bot upload Vidoy utama.

## 1) Setup lokal

1. Masuk folder:
   - `cd pastelink-batch-bot`
2. Install dependency:
   - `pip install -r requirements.txt`
3. Buat `.env` dari `.env.example`, lalu isi:
   - `TELEGRAM_BOT_TOKEN`
   - `PASTELINK_API_KEY`
   - `ADMIN_IDS` (opsional, pisah koma)
   - `DEFAULT_PRIMARY_DOMAIN` (opsional, default `vixoly.de`)
   - `DEFAULT_BACKUP_DOMAIN` (opsional, default `vidvsy.de`)
4. Jalankan:
   - `python bot.py`

## 2) Command bot

- `/start`
- `/list_pastes [page] [limit]`
  - Contoh: `/list_pastes 1 20`
- `/replace_domain old.com new.com [apply] [limit]`
  - Contoh dry-run: `/replace_domain acelimg.com vixoly.de`
  - Contoh apply: `/replace_domain acelimg.com vixoly.de apply 2000`
- `/replace_all_domain new.com [apply] [limit]`
  - Mengganti semua domain link Vidoy (`/f/`, `/d/`, `/e/`) ke domain baru.
  - Contoh dry-run: `/replace_all_domain vixoly.de`
  - Contoh apply: `/replace_all_domain vixoly.de apply 3000`
- `/change`
  - Menampilkan panel tombol 1 pesan:
    - ganti link baru (utama),
    - ganti link backup/alternatif,
    - limit 50/ALL,
    - start apply,
    - batal.

## 3) Catatan edit manual web

Benar, pola URL edit manual Pastelink seperti ini:
- View: `https://pastelink.net/a8o6n16w`
- Edit: `https://pastelink.net/a8o6n16w?edit=true`

Contoh referensi:
- [Pastelink contoh view](https://pastelink.net/a8o6n16w)
- [Pastelink contoh edit mode](https://pastelink.net/a8o6n16w?edit=true)

## 4) Deploy Koyeb

Gunakan folder ini sebagai root service di Koyeb:
- Build memakai `Dockerfile`
- Set environment variables:
  - `TELEGRAM_BOT_TOKEN`
  - `PASTELINK_API_KEY`
  - `ADMIN_IDS` (opsional)
  - `PORT=8080` (opsional, default sudah 8080)

Health check endpoint:
- `/health`

## 5) Keamanan

- Sangat disarankan isi `ADMIN_IDS` agar tidak bisa dipakai user lain.
- Jalankan dry-run dulu sebelum apply pada akun dengan paste banyak.
