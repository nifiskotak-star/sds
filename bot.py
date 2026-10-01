import asyncio
import json
import urllib.request
import urllib.parse
import os

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8705350376:AAHwmgyNaoFgQfWPmb0_ZftRGAALz6t-qMU")
API_BASE = os.environ.get("API_BASE", "https://sds-s5j9.onrender.com")
ADMIN_ID = "341311229"

last_update_id = 0


def tg_api(method, data=None):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    if data:
        data = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=data)
    else:
        req = urllib.request.Request(url)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode())
    except Exception as e:
        print(f"tg_api {method} failed:", e)
        return {"ok": False}


async def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    if text.startswith("/start"):
        tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": "Load Tester Bot\n\n"
                    "/test https://site.com — запустить тест (5 USDT)\n"
                    "/status — статус\n"
                    "/help — помощь"
        })
        return

    if text.startswith("/help"):
        tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": "Оплата 5 USDT через панель.\nТестируем только свои сайты."
        })
        return

    if text.startswith("/test "):
        url = text[6:].strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": f"Тест: {url}\n\nОткрой панель, оплати 5 USDT, тест запустится автоматически."
        })
        return

    if text.startswith("/status"):
        tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": "Статус в веб-панели."
        })
        return

    tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": "Неизвестная команда. /help"
    })


async def poll():
    global last_update_id
    print("Bot started, polling...")
    while True:
        try:
            res = tg_api("getUpdates", {
                "offset": last_update_id + 1,
                "timeout": 30
            })
            if res.get("ok"):
                for upd in res.get("result", []):
                    last_update_id = upd["update_id"]
                    if "message" in upd:
                        await handle_message(upd["message"])
        except Exception as e:
            print("poll error:", e)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(poll())