"""VK <-> Telegram notification bridge.

Слушает VK User Long Poll и пересылает входящие сообщения в Telegram-бота.
Ответ (reply) на уведомление в Telegram отправляется обратно в VK тому же собеседнику.
Нужен, потому что Apple отозвала push-токены приложений VK на iOS (июнь 2026).
"""

import html
import logging
import os
import random
import re
import threading
import time
from collections import OrderedDict

import requests
from dotenv import load_dotenv

load_dotenv()


def _to_bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


VK_TOKEN = os.getenv("VK_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
ONLY_PRIVATE = _to_bool(os.getenv("ONLY_PRIVATE"), False)
FORWARD_TEXT = _to_bool(os.getenv("FORWARD_TEXT"), True)
# skip = не уведомлять о заглушенных в VK чатах; silent = слать в TG без звука
MUTED_MODE = os.getenv("MUTED_MODE", "skip").strip().lower()
IGNORE_PEER_IDS = {
    int(p) for p in os.getenv("IGNORE_PEER_IDS", "").split(",") if p.strip()
}

if not VK_TOKEN or not TG_BOT_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("Fill VK_TOKEN / TG_BOT_TOKEN / TG_CHAT_ID in .env")

VK_API = "https://api.vk.com/method/"
VK_V = "5.199"
CHAT_PEER_BASE = 2_000_000_000
FLAG_OUTBOX = 2
VK_AUTH_ERROR_CODES = {5}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("vk-notify")

_name_cache = {}
_mute_cache = {}
MUTE_CACHE_TTL = 600

# tg message_id уведомления -> peer_id в VK, чтобы reply в TG уходил нужному собеседнику
_reply_map = OrderedDict()
_reply_lock = threading.Lock()
REPLY_MAP_MAX = 2000


class VkApiError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f"[{code}] {msg}")
        self.code = code


def vk(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_V)
    r = requests.post(VK_API + method, data=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise VkApiError(err.get("error_code", 0), err.get("error_msg", "unknown"))
    return data["response"]


def sender_name(from_id):
    if from_id in _name_cache:
        return _name_cache[from_id]
    try:
        if from_id > 0:
            u = vk("users.get", user_ids=from_id)[0]
            name = f"{u['first_name']} {u['last_name']}"
        else:
            g = vk("groups.getById", group_id=-from_id)["groups"][0]
            name = g["name"]
    except Exception as e:
        logger.warning("Name lookup failed for %s: %s", from_id, e)
        return f"id{from_id}"
    _name_cache[from_id] = name
    return name


def chat_title(peer_id):
    if peer_id in _name_cache:
        return _name_cache[peer_id]
    try:
        conv = vk("messages.getConversationsById", peer_ids=peer_id)["items"][0]
        title = conv.get("chat_settings", {}).get("title", f"чат {peer_id}")
    except Exception:
        title = f"чат {peer_id - CHAT_PEER_BASE}"
    _name_cache[peer_id] = title
    return title


def is_muted(peer_id):
    now = time.time()
    cached = _mute_cache.get(peer_id)
    if cached and cached[1] > now:
        return cached[0]
    muted = False
    try:
        items = vk("messages.getConversationsById", peer_ids=peer_id)["items"]
        if items:
            ps = items[0].get("push_settings", {})
            until = ps.get("disabled_until", 0)
            muted = bool(ps.get("disabled_forever")) or until == -1 or until > now
    except Exception as e:
        logger.warning("Mute lookup failed for %s: %s", peer_id, e)
    _mute_cache[peer_id] = (muted, now + MUTE_CACHE_TTL)
    return muted


# --- Telegram ---

def tg_api(method, **payload):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=40)
        except Exception as e:
            logger.warning("Telegram %s request failed: %s", method, e)
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
            continue
        data = r.json()
        if not data.get("ok"):
            logger.error("Telegram %s error: %s", method, str(data)[:300])
            return None
        return data["result"]
    return None


def tg_send(text, silent=False):
    return tg_api(
        "sendMessage",
        chat_id=TG_CHAT_ID,
        text=text[:4000],
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=silent,
    )


def tg_reply(to_message_id, text):
    return tg_api(
        "sendMessage",
        chat_id=TG_CHAT_ID,
        text=text[:4000],
        parse_mode="HTML",
        reply_to_message_id=to_message_id,
        allow_sending_without_reply=True,
    )


def remember_reply_target(tg_message_id, peer_id):
    with _reply_lock:
        _reply_map[tg_message_id] = peer_id
        while len(_reply_map) > REPLY_MAP_MAX:
            _reply_map.popitem(last=False)


def reply_target(tg_message_id):
    with _reply_lock:
        return _reply_map.get(tg_message_id)


# --- VK -> TG ---

ATTACH_LABELS = {
    "photo": "📷 фото",
    "video": "🎬 видео",
    "audio": "🎵 музыка",
    "audio_message": "🎤 голосовое",
    "doc": "📄 файл",
    "sticker": "😊 стикер",
    "wall": "📰 репост",
    "wall_reply": "💬 комментарий",
    "link": "🔗 ссылка",
    "gift": "🎁 подарок",
    "graffiti": "🖌 граффити",
    "money_transfer": "💸 перевод",
    "money_request": "💸 запрос денег",
    "poll": "📊 опрос",
    "call": "📞 звонок",
    "story": "📸 история",
}


def attach_summary(attachments, skip_media=False):
    parts = []
    i = 1
    while f"attach{i}_type" in attachments:
        t = attachments[f"attach{i}_type"]
        if not (skip_media and t in MEDIA_TYPES):
            parts.append(ATTACH_LABELS.get(t, "📎 вложение"))
        i += 1
    if "fwd" in attachments:
        parts.append("↪️ пересланное")
    if "geo" in attachments or "geo_provider" in attachments:
        parts.append("📍 геометка")
    return ", ".join(parts)


# типы вложений, которые пересылаем содержимым, а не текстовой пометкой
# (голосовые в Long Poll приходят с типом doc — разбираются через messages.getById)
MEDIA_TYPES = {"photo", "sticker", "audio_message", "doc"}


def has_media(attachments):
    i = 1
    while f"attach{i}_type" in attachments:
        if attachments[f"attach{i}_type"] in MEDIA_TYPES:
            return True
        i += 1
    return False


def fetch_full_attachments(msg_id):
    try:
        items = vk("messages.getById", message_ids=msg_id)["items"]
        return items[0].get("attachments", []) if items else []
    except Exception as e:
        logger.warning("messages.getById failed for %s: %s", msg_id, e)
        return []


def biggest_photo_url(photo):
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    return max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0)).get("url")


def send_media(msg_id, caption, silent):
    """Пересылает фото/стикеры/голосовые содержимым. Возвращает сообщение TG или None."""
    photos, voices, docs = [], [], []
    for a in fetch_full_attachments(msg_id):
        t = a.get("type")
        if t == "photo":
            url = biggest_photo_url(a["photo"])
            if url:
                photos.append(url)
        elif t == "sticker":
            images = a["sticker"].get("images") or []
            if images:
                photos.append(images[-1].get("url"))
        elif t == "audio_message":
            am = a["audio_message"]
            url = am.get("link_ogg") or am.get("link_mp3")
            if url:
                voices.append(url)
        elif t == "doc":
            d = a["doc"]
            if d.get("url"):
                docs.append(d["url"])

    cap = caption[:1000]
    sent = None
    if len(photos) == 1:
        sent = tg_api("sendPhoto", chat_id=TG_CHAT_ID, photo=photos[0],
                      caption=cap, parse_mode="HTML", disable_notification=silent)
    elif len(photos) > 1:
        media = [{"type": "photo", "media": u} for u in photos[:10]]
        media[0].update(caption=cap, parse_mode="HTML")
        res = tg_api("sendMediaGroup", chat_id=TG_CHAT_ID, media=media,
                     disable_notification=silent)
        sent = res[0] if res else None
    for url in voices:
        kwargs = dict(chat_id=TG_CHAT_ID, voice=url,
                      disable_notification=silent or bool(sent))
        if not sent:
            kwargs.update(caption=cap, parse_mode="HTML")
        v = tg_api("sendVoice", **kwargs)
        sent = sent or v
    for url in docs:
        kwargs = dict(chat_id=TG_CHAT_ID, document=url,
                      disable_notification=silent or bool(sent))
        if not sent:
            kwargs.update(caption=cap, parse_mode="HTML")
        d = tg_api("sendDocument", **kwargs)
        sent = sent or d
    return sent


def decode_lp_text(text):
    return html.unescape(text.replace("<br>", "\n"))


def handle_message(update):
    # LP v3, code 4: [4, msg_id, flags, peer_id, ts, text, extra, attachments]
    flags, peer_id = update[2], update[3]
    text = decode_lp_text(update[5]) if len(update) > 5 else ""
    extra = update[6] if len(update) > 6 and isinstance(update[6], dict) else {}
    attachments = update[7] if len(update) > 7 and isinstance(update[7], dict) else {}

    if flags & FLAG_OUTBOX:
        return
    if peer_id in IGNORE_PEER_IDS:
        return

    is_chat = peer_id >= CHAT_PEER_BASE
    if is_chat and ONLY_PRIVATE:
        return

    muted = is_muted(peer_id)
    if muted and MUTED_MODE != "silent":
        logger.info("Skipped muted peer %s", peer_id)
        return

    from_id = int(extra.get("from", peer_id)) if is_chat else peer_id
    name = sender_name(from_id)
    header = f"💬 <b>{html.escape(name)}</b>"
    if is_chat:
        header += f" в «{html.escape(chat_title(peer_id))}»"

    base = html.escape(text) if FORWARD_TEXT and text else ""

    def build_text(skip_media):
        b = base
        line = attach_summary(attachments, skip_media=skip_media)
        if line:
            b = (b + "\n" + line).strip()
        if not FORWARD_TEXT:
            b = "новое сообщение"
        return f"{header}\n{b}" if b else header

    sent = None
    if FORWARD_TEXT and has_media(attachments):
        sent = send_media(update[1], build_text(skip_media=True), muted)
    if sent is None:
        sent = tg_send(build_text(skip_media=False), silent=muted)
    if sent:
        remember_reply_target(sent["message_id"], peer_id)
    logger.info("Forwarded message from %s (peer %s, muted=%s)", from_id, peer_id, muted)


def long_poll_loop():
    lp = vk("messages.getLongPollServer", lp_version=3)
    server, key, ts = lp["server"], lp["key"], lp["ts"]
    logger.info("Long Poll connected: %s", server)

    while True:
        try:
            r = requests.get(
                f"https://{server}",
                params={"act": "a_check", "key": key, "ts": ts, "wait": 25,
                        "mode": 2, "version": 3},
                timeout=40,
            )
            data = r.json()
        except Exception as e:
            logger.warning("Long Poll request failed: %s", e)
            time.sleep(5)
            continue

        failed = data.get("failed")
        if failed == 1:
            ts = data["ts"]
            continue
        if failed in (2, 3):
            lp = vk("messages.getLongPollServer", lp_version=3)
            server, key, ts = lp["server"], lp["key"], lp["ts"]
            continue

        ts = data.get("ts", ts)
        for update in data.get("updates", []):
            if update and update[0] == 4:
                try:
                    handle_message(update)
                except Exception:
                    logger.exception("Failed to handle update: %s", update)


# --- TG -> VK (ответы) ---

def confirm_sent(tg_message_id):
    ok = tg_api(
        "setMessageReaction",
        chat_id=TG_CHAT_ID,
        message_id=tg_message_id,
        reaction=[{"type": "emoji", "emoji": "👍"}],
    )
    if ok is None:
        tg_reply(tg_message_id, "✅ отправлено")


def handle_tg_message(msg):
    text = (msg.get("text") or "").strip()
    message_id = msg["message_id"]
    reply_to = msg.get("reply_to_message")

    if not reply_to:
        if not text.startswith("/start"):
            tg_reply(message_id,
                     "Чтобы ответить в VK — отправь текст реплаем (reply) на уведомление.")
        return

    peer_id = reply_target(reply_to["message_id"])
    if peer_id is None:
        tg_reply(message_id,
                 "⚠️ Не знаю, кому это отправить: мост перезапускался и потерял привязку. "
                 "Ответь на уведомление, пришедшее после перезапуска.")
        return
    if not text:
        tg_reply(message_id, "⚠️ В VK могу отправить только текст.")
        return

    try:
        vk("messages.send", peer_id=peer_id, message=text,
           random_id=random.randint(1, 2**31))
    except Exception as e:
        tg_reply(message_id, f"⚠️ VK не принял сообщение: {html.escape(str(e))}")
        logger.warning("Reply to peer %s failed: %s", peer_id, e)
        return
    confirm_sent(message_id)
    logger.info("Replied to peer %s from Telegram", peer_id)


def tg_updates_loop():
    offset = 0
    while True:
        updates = tg_api("getUpdates", offset=offset, timeout=25,
                         allowed_updates=["message"])
        if updates is None:
            time.sleep(5)
            continue
        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            msg = upd.get("message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id")) != str(TG_CHAT_ID):
                continue
            try:
                handle_tg_message(msg)
            except Exception:
                logger.exception("Failed to handle TG message")


TOKEN_DEAD_ALERT = (
    "⚠️ <b>Токен VK перестал работать</b> (авторизация отклонена — обычно после смены "
    "пароля или «завершить все сеансы»).\n\nПолучи новый на vkhost.github.io (Kate Mobile), "
    "обнови VK_TOKEN в /opt/vk-notify/.env и выполни: systemctl restart vk-notify.\n"
    "Проверяю снова каждые 10 минут."
)


def main():
    threading.Thread(target=tg_updates_loop, daemon=True).start()
    auth_alerted = False
    started = False
    while True:
        try:
            me = vk("users.get")[0]
            logger.info("Started as %s %s (id=%s)", me["first_name"], me["last_name"], me["id"])
            if not started:
                tg_send("✅ vk-notify запущен: уведомления из VK будут приходить сюда. "
                        "Reply на уведомление = ответ в VK.")
                started = True
            if auth_alerted:
                tg_send("✅ Токен VK снова работает.")
                auth_alerted = False
            long_poll_loop()
        except VkApiError as e:
            if e.code in VK_AUTH_ERROR_CODES:
                logger.error("VK auth failed: %s", e)
                if not auth_alerted:
                    tg_send(TOKEN_DEAD_ALERT)
                    auth_alerted = True
                time.sleep(600)
            else:
                logger.exception("VK API error, restarting in 15s")
                time.sleep(15)
        except Exception:
            logger.exception("Long Poll loop crashed, restarting in 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()
