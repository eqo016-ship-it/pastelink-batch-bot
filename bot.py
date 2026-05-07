import asyncio
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
PASTELINK_API_KEY = (os.getenv("PASTELINK_API_KEY") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()
PORT = int((os.getenv("PORT") or "8080").strip())
DEFAULT_PRIMARY_DOMAIN = (os.getenv("DEFAULT_PRIMARY_DOMAIN") or "vixoly.de").strip()
DEFAULT_BACKUP_DOMAIN = (os.getenv("DEFAULT_BACKUP_DOMAIN") or "vidvsy.de").strip()

ADMIN_IDS: set[int] = set()
if ADMIN_IDS_RAW:
    for part in ADMIN_IDS_RAW.split(","):
        s = part.strip()
        if s.isdigit():
            ADMIN_IDS.add(int(s))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

VIDOY_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?([^\s/]+)/(f|d|e)/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
BACKUP_HINT_PATTERN = re.compile(r"(backup|alternatif)", re.IGNORECASE)
PRIMARY_SECTION_HINT_PATTERN = re.compile(
    r"(video|utama|bonus|asupan|join|konten|judul)",
    re.IGNORECASE,
)


def _normalize_domain(domain: str) -> str:
    s = (domain or "").strip().lower()
    s = s.replace("https://", "").replace("http://", "").strip("/")
    if s.startswith("www."):
        s = s[4:]
    return s


def _replace_specific_old_domain(text: str, old_domain: str, new_domain: str) -> str:
    old_norm = _normalize_domain(old_domain)
    new_norm = _normalize_domain(new_domain)
    if not old_norm or not new_norm:
        return text

    def repl(m):
        dom = _normalize_domain(m.group(1))
        if dom != old_norm:
            return m.group(0)
        return f"https://{new_norm}/{m.group(2)}/{m.group(3)}"

    return VIDOY_LINK_PATTERN.sub(repl, text)


def _replace_all_vidoy_domains(text: str, new_domain: str) -> str:
    new_norm = _normalize_domain(new_domain)
    if not new_norm:
        return text

    def repl(m):
        return f"https://{new_norm}/{m.group(2)}/{m.group(3)}"

    return VIDOY_LINK_PATTERN.sub(repl, text)


def _replace_line_with_domain(line: str, target_domain: str, old_domain: str | None) -> tuple[str, list[tuple[str, str]]]:
    target_norm = _normalize_domain(target_domain)
    old_norm = _normalize_domain(old_domain or "")
    pairs: list[tuple[str, str]] = []

    def repl(m):
        src_dom = _normalize_domain(m.group(1))
        src = f"https://{src_dom}/{m.group(2)}/{m.group(3)}"
        if old_norm and src_dom != old_norm:
            return m.group(0)
        dst = f"https://{target_norm}/{m.group(2)}/{m.group(3)}"
        if src != dst:
            pairs.append((src, dst))
        return dst

    new_line = VIDOY_LINK_PATTERN.sub(repl, line)
    return new_line, pairs


def _replace_vidoy_domains_by_context(
    body: str,
    primary_domain: str,
    backup_domain: str,
    old_domain: str | None = None,
) -> tuple[str, list[tuple[str, str]], int, int]:
    lines = (body or "").splitlines()
    out_lines: list[str] = []
    changed_pairs: list[tuple[str, str]] = []
    changed_primary = 0
    changed_backup = 0
    in_backup_block = False

    for raw in lines:
        line = raw
        trimmed = line.strip()
        if not trimmed:
            in_backup_block = False
            out_lines.append(line)
            continue

        if BACKUP_HINT_PATTERN.search(trimmed):
            in_backup_block = True
            out_lines.append(line)
            continue

        if PRIMARY_SECTION_HINT_PATTERN.search(trimmed):
            in_backup_block = False

        if VIDOY_LINK_PATTERN.search(line):
            target = backup_domain if in_backup_block else primary_domain
            new_line, pairs = _replace_line_with_domain(line, target, old_domain)
            if pairs:
                if in_backup_block:
                    changed_backup += len(pairs)
                else:
                    changed_primary += len(pairs)
                changed_pairs.extend(pairs)
            out_lines.append(new_line)
        else:
            out_lines.append(line)

    return "\n".join(out_lines), changed_pairs, changed_primary, changed_backup


def _extract_vidoy_links(text: str) -> list[str]:
    out: list[str] = []
    for m in VIDOY_LINK_PATTERN.finditer(text or ""):
        dom = _normalize_domain(m.group(1))
        out.append(f"https://{dom}/{m.group(2)}/{m.group(3)}")
    return out


def _collect_changed_pairs(before_text: str, after_text: str) -> list[tuple[str, str]]:
    before_links = _extract_vidoy_links(before_text)
    after_links = _extract_vidoy_links(after_text)
    pairs: list[tuple[str, str]] = []
    for i in range(min(len(before_links), len(after_links))):
        if before_links[i] != after_links[i]:
            pairs.append((before_links[i], after_links[i]))
    return pairs


def _api_call(endpoint: str, params: dict, method: str = "GET") -> dict:
    url = "https://api.pastelink.net/" + endpoint.strip().lstrip("/")
    payload = urlencode(params or {})
    try:
        if method.upper() == "GET":
            req = Request(
                url + ("?" + payload if payload else ""),
                method="GET",
                headers={"Accept": "application/json"},
            )
        else:
            req = Request(
                url,
                data=payload.encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
        with urlopen(req, timeout=60) as resp:
            raw = (resp.read() or b"").decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {"response_code": 400, "error": "invalid json"}
    except Exception as e:
        return {"response_code": 400, "error": str(e)}


def _list_paste_urls(limit: int) -> tuple[list[str], str | None]:
    urls: list[str] = []
    page = 1
    while len(urls) < limit:
        chunk = min(1000, limit - len(urls))
        res = _api_call(
            "get-pastes",
            {"api_key": PASTELINK_API_KEY, "deleted": "0", "page": page, "limit": chunk},
            method="GET",
        )
        if res.get("response_code") != 200:
            return [], str(res.get("error") or "get-pastes error")
        arr = res.get("paste_list") or res.get("pastes") or []
        if not isinstance(arr, list) or not arr:
            break
        for p in arr:
            if isinstance(p, dict) and p.get("url"):
                urls.append(str(p.get("url")).strip())
                if len(urls) >= limit:
                    break
        total_pages = int(res.get("total_pages") or page)
        if page >= total_pages:
            break
        page += 1
    return urls, None


async def _require_admin(update: Update) -> bool:
    if not ADMIN_IDS:
        return True
    u = update.effective_user
    return bool(u and u.id in ADMIN_IDS)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        await update.message.reply_text("⛔ Tidak diizinkan.")
        return
    await update.message.reply_text(
        "🛠 <b>Pastelink Batch Domain Bot</b>\n\n"
        "Perintah:\n"
        "• <code>/list_pastes [page] [limit]</code>\n"
        "• <code>/replace_domain old.com new.com [apply] [limit]</code>\n"
        "• <code>/replace_all_domain new.com [apply] [limit]</code>\n\n"
        "UI rapih:\n"
        "• <code>/change</code>\n\n"
        "Tanpa <code>apply</code> = dry-run.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_list_pastes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        await update.message.reply_text("⛔ Tidak diizinkan.")
        return
    page = 1
    limit = 20
    if context.args:
        if len(context.args) >= 1 and str(context.args[0]).isdigit():
            page = max(1, int(context.args[0]))
        if len(context.args) >= 2 and str(context.args[1]).isdigit():
            limit = max(1, min(int(context.args[1]), 100))
    res = _api_call(
        "get-pastes",
        {"api_key": PASTELINK_API_KEY, "deleted": "0", "page": page, "limit": limit},
        method="GET",
    )
    if res.get("response_code") != 200:
        await update.message.reply_text(f"❌ Gagal list: {res.get('error')}")
        return
    arr = res.get("paste_list") or []
    if not arr:
        await update.message.reply_text("ℹ️ Tidak ada paste.")
        return
    lines = [f"📄 Page {res.get('current_page')} / {res.get('total_pages')}"]
    for i, p in enumerate(arr, start=1):
        url = (p.get("url") or "").strip()
        ttl = (p.get("title") or "-").strip()
        lines.append(f"{i}. {ttl}\nhttps://pastelink.net/{url}")
    await update.message.reply_text("\n\n".join(lines)[:3800], disable_web_page_preview=True)


def _change_keyboard(is_input_mode: bool = False) -> InlineKeyboardMarkup:
    if is_input_mode:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Kembali", callback_data="chg_back")], [InlineKeyboardButton("❌ Batal", callback_data="chg_cancel")]]
        )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐 Ganti link baru", callback_data="chg_set_primary")],
            [InlineKeyboardButton("🛟 Ganti link backup", callback_data="chg_set_backup")],
            [InlineKeyboardButton("📦 Limit", callback_data="chg_limit")],
            [InlineKeyboardButton("▶️ START", callback_data="chg_start"), InlineKeyboardButton("❌ Batal", callback_data="chg_cancel")],
        ]
    )


def _change_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Ya, mulai", callback_data="chg_confirm_yes")],
            [InlineKeyboardButton("↩️ Kembali", callback_data="chg_confirm_no")],
        ]
    )


def _change_text(state: dict, status: str = "Siap") -> str:
    limit_txt = "ALL" if state.get("limit") == 0 else str(state.get("limit"))
    return (
        f"🛠 <b>Panel Change Domain</b>\n\n"
        f"Status: <b>{status}</b>\n\n"
        f"Link baru = <code>{state.get('primary_domain')}</code>\n"
        f"Link backup = <code>{state.get('backup_domain')}</code>\n"
        f"Limit = <code>{limit_txt}</code>\n\n"
        f"Gunakan tombol di bawah."
    )


def _count_links_in_text(text: str) -> int:
    return len(_extract_vidoy_links(text))


def _progress_bar(current: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "] 0%"
    ratio = max(0.0, min(1.0, current / total))
    done = int(ratio * width)
    bar = ("#" * done) + ("-" * (width - done))
    return f"[{bar}] {int(ratio * 100)}%"


def _scan_total_target_links(limit: int) -> tuple[int, int, str | None]:
    urls, err = _list_paste_urls(limit if limit > 0 else 5000)
    if err:
        return 0, 0, err
    total_links = 0
    for pu in urls:
        detail = _api_call("get-paste", {"api_key": PASTELINK_API_KEY, "url": pu}, method="GET")
        if detail.get("response_code") != 200:
            continue
        total_links += _count_links_in_text(str(detail.get("body") or ""))
    return len(urls), total_links, None


async def cmd_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        await update.message.reply_text("⛔ Tidak diizinkan.")
        return
    state = {
        "primary_domain": DEFAULT_PRIMARY_DOMAIN,
        "backup_domain": DEFAULT_BACKUP_DOMAIN,
        "limit": 50,
        "awaiting": None,
    }
    context.user_data["change_state"] = state
    sent = await update.message.reply_text(
        _change_text(state),
        parse_mode=ParseMode.HTML,
        reply_markup=_change_keyboard(),
        disable_web_page_preview=True,
    )
    context.user_data["change_panel_chat_id"] = sent.chat_id
    context.user_data["change_panel_msg_id"] = sent.message_id


async def _refresh_change_panel(context: ContextTypes.DEFAULT_TYPE, status: str = "Siap"):
    state = context.user_data.get("change_state") or {}
    chat_id = context.user_data.get("change_panel_chat_id")
    msg_id = context.user_data.get("change_panel_msg_id")
    if not chat_id or not msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_change_text(state, status),
            parse_mode=ParseMode.HTML,
            reply_markup=_change_keyboard(),
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def _safe_q_edit(q, text: str | None = None, **kwargs):
    if text is not None:
        kwargs["text"] = text
    try:
        await q.edit_message_text(**kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def callback_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except BadRequest:
        # Abaikan callback query yang kedaluwarsa agar bot tetap lanjut memproses klik berikutnya.
        pass
    if not await _require_admin(update):
        await _safe_q_edit(q, text="⛔ Tidak diizinkan.", disable_web_page_preview=True)
        return
    state = context.user_data.get("change_state")
    if not state:
        await _safe_q_edit(q, text="Session /change tidak ditemukan. Jalankan /change lagi.")
        return

    data = q.data or ""
    if data == "chg_set_primary":
        state["awaiting"] = "primary_domain"
        context.user_data["change_state"] = state
        await _safe_q_edit(q,
            "Silakan input domain tujuan untuk link baru.\nContoh: <code>vixoly.de</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_change_keyboard(is_input_mode=True),
        )
        return
    if data == "chg_set_backup":
        state["awaiting"] = "backup_domain"
        context.user_data["change_state"] = state
        await _safe_q_edit(q,
            "Silakan input domain tujuan untuk link backup/alternatif.\nContoh: <code>vidvsy.de</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_change_keyboard(is_input_mode=True),
        )
        return
    if data == "chg_limit":
        state["awaiting"] = "limit"
        context.user_data["change_state"] = state
        await _safe_q_edit(q,
            "Silakan input limit.\n"
            "- Isi angka (contoh: <code>50</code>, <code>200</code>)\n"
            "- Atau isi <code>all</code> untuk semua note",
            parse_mode=ParseMode.HTML,
            reply_markup=_change_keyboard(is_input_mode=True),
        )
        return
    if data == "chg_back":
        state["awaiting"] = None
        context.user_data["change_state"] = state
        await _safe_q_edit(q,
            _change_text(state, "Siap"),
            parse_mode=ParseMode.HTML,
            reply_markup=_change_keyboard(),
            disable_web_page_preview=True,
        )
        return
    if data == "chg_cancel":
        context.user_data.pop("change_state", None)
        await _safe_q_edit(q, text="❌ Proses dibatalkan.", disable_web_page_preview=True)
        return
    if data == "chg_start":
        state["awaiting"] = None
        context.user_data["change_state"] = state
        await _safe_q_edit(q,
            _change_text(state, "Konfirmasi sebelum apply"),
            parse_mode=ParseMode.HTML,
            reply_markup=_change_confirm_keyboard(),
            disable_web_page_preview=True,
        )
        return
    if data == "chg_confirm_no":
        await _safe_q_edit(q,
            _change_text(state, "Siap"),
            parse_mode=ParseMode.HTML,
            reply_markup=_change_keyboard(),
            disable_web_page_preview=True,
        )
        return
    if data == "chg_confirm_yes":
        await _safe_q_edit(q,
            _change_text(state, "Memulai proses APPLY..."),
            parse_mode=ParseMode.HTML,
            reply_markup=None,
            disable_web_page_preview=True,
        )
        await _run_replace_contextual(update, context, do_apply=True)
        return


async def msg_change_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    state = context.user_data.get("change_state")
    if not state:
        return
    awaiting = state.get("awaiting")
    if awaiting not in {"primary_domain", "backup_domain", "limit"}:
        return

    raw = (update.message.text or "").strip()
    if awaiting == "limit":
        if raw.lower() == "all":
            state["limit"] = 0
        elif raw.isdigit():
            n = int(raw)
            if n <= 0:
                await update.message.reply_text("Limit harus lebih dari 0, atau ketik <code>all</code>.", parse_mode=ParseMode.HTML)
                return
            state["limit"] = n
        else:
            await update.message.reply_text("Format limit tidak valid. Contoh: <code>50</code> atau <code>all</code>.", parse_mode=ParseMode.HTML)
            return
        state["awaiting"] = None
        context.user_data["change_state"] = state
        await _refresh_change_panel(context, "Limit diperbarui")
        return

    text = _normalize_domain(raw)
    if not text:
        await update.message.reply_text("Domain tidak valid. Contoh: <code>vixoly.de</code>", parse_mode=ParseMode.HTML)
        return
    state[awaiting] = text
    state["awaiting"] = None
    context.user_data["change_state"] = state
    await _refresh_change_panel(context, "Domain diperbarui")


async def _run_replace(update: Update, old_domain: str | None, new_domain: str, do_apply: bool, limit: int):
    msg = update.message
    urls, err = _list_paste_urls(limit)
    if err:
        await msg.reply_text(f"❌ get-pastes gagal: {err}")
        return
    if not urls:
        await msg.reply_text("ℹ️ Tidak ada paste yang ditemukan.")
        return

    await msg.reply_text(
        f"🔎 Mulai scan {len(urls)} note Pastelink...\n"
        f"Target domain baru: {new_domain}\n"
        "Saya akan kirim progres + contoh perubahan."
    )

    changed = 0
    applied = 0
    failed = 0
    detailed_sent = 0
    max_detailed = 10
    for i, pu in enumerate(urls, start=1):
        if i == 1 or i % 25 == 0:
            await msg.reply_text(f"⏳ Sedang scan {i}/{len(urls)}: https://pastelink.net/{pu}")

        detail = _api_call("get-paste", {"api_key": PASTELINK_API_KEY, "url": pu}, method="GET")
        if detail.get("response_code") != 200:
            failed += 1
            continue
        body = str(detail.get("body") or "")
        if old_domain:
            new_body = _replace_specific_old_domain(body, old_domain, new_domain)
        else:
            new_body = _replace_all_vidoy_domains(body, new_domain)
        changed_pairs = _collect_changed_pairs(body, new_body)
        if not changed_pairs:
            continue
        changed += 1
        if detailed_sent < max_detailed:
            examples = changed_pairs[:3]
            ex_lines = []
            for idx, pair in enumerate(examples, start=1):
                ex_lines.append(f"{idx}. {pair[0]} -> {pair[1]}")
            left = len(changed_pairs) - len(examples)
            if left > 0:
                ex_lines.append(f"... dan {left} link lainnya.")
            await msg.reply_text(
                f"📌 Note: https://pastelink.net/{pu}\n"
                f"Ditemukan {len(changed_pairs)} link /f|/d|/e/ yang diganti.\n"
                f"{'Mode APPLY' if do_apply else 'Mode DRY-RUN'}\n"
                + "\n".join(ex_lines)
            )
            detailed_sent += 1

        if do_apply:
            er = _api_call(
                "edit-paste",
                {"api_key": PASTELINK_API_KEY, "url": pu, "body": new_body},
                method="POST",
            )
            if er.get("response_code") == 200:
                applied += 1
            else:
                failed += 1
        if i % 100 == 0:
            await msg.reply_text(
                f"📊 Progress {i}/{len(urls)} | note berubah={changed} | applied={applied} | failed={failed}"
            )

    await msg.reply_text(
        f"✅ {'APPLY' if do_apply else 'DRY-RUN'} selesai\n"
        f"Scanned: {len(urls)}\n"
        f"Need update: {changed}\n"
        f"Applied: {applied if do_apply else '(dry-run)'}\n"
        f"Failed: {failed}"
    )


async def _run_replace_contextual(update: Update, context: ContextTypes.DEFAULT_TYPE, do_apply: bool):
    state = context.user_data.get("change_state") or {}
    limit = int(state.get("limit") or 50)
    primary_domain = _normalize_domain(state.get("primary_domain") or DEFAULT_PRIMARY_DOMAIN)
    backup_domain = _normalize_domain(state.get("backup_domain") or DEFAULT_BACKUP_DOMAIN)
    urls, err = _list_paste_urls(limit if limit > 0 else 5000)
    chat_id = context.user_data.get("change_panel_chat_id")
    msg_id = context.user_data.get("change_panel_msg_id")

    if err:
        if chat_id and msg_id:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"❌ Gagal get-pastes: {err}")
        return
    if not urls:
        if chat_id and msg_id:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="ℹ️ Tidak ada paste yang ditemukan.")
        return

    logger.info(
        "START scan note=%s limit=%s primary=%s backup=%s",
        len(urls),
        ("ALL" if limit == 0 else limit),
        primary_domain,
        backup_domain,
    )

    changed_notes = 0
    changed_primary = 0
    changed_backup = 0
    applied = 0
    failed = 0
    sample_done = False

    for i, pu in enumerate(urls, start=1):
        detail = _api_call("get-paste", {"api_key": PASTELINK_API_KEY, "url": pu}, method="GET")
        if detail.get("response_code") != 200:
            failed += 1
            continue
        body = str(detail.get("body") or "")
        new_body, pairs, cnt_primary, cnt_backup = _replace_vidoy_domains_by_context(
            body=body,
            primary_domain=primary_domain,
            backup_domain=backup_domain,
            old_domain=None,
        )
        if not pairs:
            if i % 25 == 0 and chat_id and msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=(
                        f"⏳ Scan {i}/{len(urls)}\n"
                        f"Note berubah: {changed_notes}\n"
                        f"Utama berubah: {changed_primary}\n"
                        f"Backup berubah: {changed_backup}\n"
                        f"Applied: {applied}\nFailed: {failed}"
                    ),
                    disable_web_page_preview=True,
                )
            continue

        changed_notes += 1
        changed_primary += cnt_primary
        changed_backup += cnt_backup

        if do_apply:
            er = _api_call(
                "edit-paste",
                {"api_key": PASTELINK_API_KEY, "url": pu, "body": new_body},
                method="POST",
            )
            if er.get("response_code") == 200:
                applied += 1
            else:
                failed += 1

        if not sample_done and chat_id and msg_id:
            sample_before = body[:700]
            sample_after = new_body[:700]
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    f"✅ Sedang proses note: https://pastelink.net/{pu}\n"
                    f"Ditemukan {len(pairs)} link /f|/d|/e/ yang diganti.\n"
                    f"Utama: {cnt_primary} | Backup: {cnt_backup}\n\n"
                    f"Contoh BEFORE:\n{sample_before}\n\n"
                    f"Contoh AFTER:\n{sample_after}\n\n"
                    f"(potongan contoh, bukan full note)"
                ),
                disable_web_page_preview=True,
            )
            sample_done = True

        if i % 25 == 0 and chat_id and msg_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    f"⏳ Scan {i}/{len(urls)}\n"
                    f"Note berubah: {changed_notes}\n"
                    f"Utama berubah: {changed_primary}\n"
                    f"Backup berubah: {changed_backup}\n"
                    f"Applied: {applied}\nFailed: {failed}"
                ),
                disable_web_page_preview=True,
            )
        if i == 1 or i % 10 == 0 or i == len(urls):
            logger.info(
                "SCAN %s %s changed_notes=%s primary=%s backup=%s applied=%s failed=%s",
                i,
                _progress_bar(i, len(urls)),
                changed_notes,
                changed_primary,
                changed_backup,
                applied,
                failed,
            )

    if chat_id and msg_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                f"✅ APPLY selesai\n"
                f"Scanned note: {len(urls)}\n"
                f"Note berubah: {changed_notes}\n"
                f"Link utama berubah: {changed_primary}\n"
                f"Link backup berubah: {changed_backup}\n"
                f"Applied note: {applied}\n"
                f"Failed: {failed}"
            ),
            disable_web_page_preview=True,
        )
    logger.info(
        "DONE %s changed_notes=%s primary=%s backup=%s applied=%s failed=%s",
        _progress_bar(len(urls), len(urls)),
        changed_notes,
        changed_primary,
        changed_backup,
        applied,
        failed,
    )


async def cmd_replace_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        await update.message.reply_text("⛔ Tidak diizinkan.")
        return
    args = list(context.args or [])
    if len(args) < 2:
        await update.message.reply_text(
            "Format:\n<code>/replace_domain old.com new.com [apply] [limit]</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    old_dom = _normalize_domain(args[0])
    new_dom = _normalize_domain(args[1])
    do_apply = any(str(a).lower() == "apply" for a in args[2:])
    limit = 1000
    for a in args[2:]:
        if str(a).isdigit():
            limit = max(1, min(int(a), 5000))
            break
    await update.message.reply_text(
        f"⏳ {'APPLY' if do_apply else 'DRY-RUN'}\nfrom: {old_dom}\nto: {new_dom}\nlimit: {limit}"
    )
    await _run_replace(update, old_dom, new_dom, do_apply, limit)


async def cmd_replace_all_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        await update.message.reply_text("⛔ Tidak diizinkan.")
        return
    args = list(context.args or [])
    if len(args) < 1:
        await update.message.reply_text(
            "Format:\n<code>/replace_all_domain new.com [apply] [limit]</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    new_dom = _normalize_domain(args[0])
    do_apply = any(str(a).lower() == "apply" for a in args[1:])
    limit = 1000
    for a in args[1:]:
        if str(a).isdigit():
            limit = max(1, min(int(a), 5000))
            break
    await update.message.reply_text(
        f"⏳ {'APPLY' if do_apply else 'DRY-RUN'} all Vidoy domains -> {new_dom}\nlimit: {limit}"
    )
    await _run_replace(update, None, new_dom, do_apply, limit)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def _run_health_server():
    try:
        srv = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
        logger.info("Health server listening on :%s", PORT)
        srv.serve_forever()
    except Exception as e:
        logger.warning("Health server error: %s", e)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN wajib diisi.")
        return
    if not PASTELINK_API_KEY:
        print("PASTELINK_API_KEY wajib diisi.")
        return

    threading.Thread(target=_run_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("list_pastes", cmd_list_pastes))
    app.add_handler(CommandHandler("replace_domain", cmd_replace_domain))
    app.add_handler(CommandHandler("replace_all_domain", cmd_replace_all_domain))
    app.add_handler(CommandHandler("change", cmd_change))
    app.add_handler(CallbackQueryHandler(callback_change, pattern=r"^chg_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_change_input))
    logger.info("Pastelink batch bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
