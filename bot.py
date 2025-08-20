# bot.py — USDT(TRC20) 자동결제 확인 + 고객/운영자 알림
# (텍스트 수량 입력 / 뒤로가기만 유지, 디버깅 로그 강화판)

import os
import asyncio
import logging
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import aiohttp

# ─────────────────────────────────────────────
# 환경 변수 로드
# ─────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN이 설정되지 않았습니다.")

PAYMENT_ADDRESS = (os.getenv("PAYMENT_ADDRESS") or "").strip()
if not PAYMENT_ADDRESS:
    raise RuntimeError("PAYMENT_ADDRESS가 설정되지 않았습니다.")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")

# TRON USDT(TRC20) 공식 컨트랙트 (메인넷)
USDT_CONTRACT = (os.getenv("USDT_CONTRACT") or "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").strip()

try:
    PER_100_PRICE = Decimal(os.getenv("PER_100_PRICE", "7.21"))
except InvalidOperation:
    PER_100_PRICE = Decimal("7.21")
PER_100_PRICE = PER_100_PRICE.quantize(Decimal("0.01"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()

# ─────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("paybot")

masked_token = BOT_TOKEN[:10] + "..." if BOT_TOKEN else "N/A"
log.info("🔧 CONFIG | token=%s admin=%s addr=%s contract=%s per100=%s log=%s",
         masked_token, ADMIN_CHAT_ID, PAYMENT_ADDRESS, USDT_CONTRACT, PER_100_PRICE, LOG_LEVEL)

# ─────────────────────────────────────────────
# 안내 텍스트
# ─────────────────────────────────────────────
WELCOME_TEXT = (
    "➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
    "▫️[텔레그램 유령 자판기]에 오신 것을 환영합니다!\n"
    "▫️텔레그램 유령인원 구매 24h OK\n"
    "▫️하단 메뉴 또는 /start 로 지금 시작하세요!\n"
    "▫️가격은 유동적이며, 대량 구매는 판매자에게!\n"
    "▫️숙지사항 꼭 확인하세요!\n"
    "➖➖➖➖➖➖➖➖➖➖➖➖➖"
)

NOTICE_TEXT = (
    " 유령 자판기 이용법 🚩\n"
    "• 버튼 반응 없을시 → 메뉴로 돌아가기 클릭 필수\n"
    "• 유령 인입 완료 전까지 그룹/채널 설정 금지\n"
    "• 작업 완료 시간: 약 10~20분\n"
    "• 1개의 주소만 진행 가능\n"
    "• 결제창 제한 15분 경과 시 최초부터 재결제 필요\n"
    "• 위반으로 발생하는 불상사는 책임지지 않습니다.\n"
)

# ─────────────────────────────────────────────
# 간단 저장소
# {user_id: {"qty": int, "amount": Decimal, "chat_id": int}}
# ─────────────────────────────────────────────
pending_orders = {}
processed_txs = set()  # 중복 처리 방지

# ─────────────────────────────────────────────
# 키보드
# ─────────────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("유령인원", callback_data="menu:ghost")],
        [InlineKeyboardButton("숙지사항/가이드", callback_data="menu:notice")],
    ])

def back_only_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ 뒤로가기", callback_data="back:main")]
    ])

# ─────────────────────────────────────────────
# 핸들러들
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_kb())

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "menu:ghost":
        context.user_data["awaiting_qty"] = True
        log.info("[MENU] user=%s → awaiting_qty=True", q.from_user.id)
        await q.edit_message_text(
            "인원수를 말씀해주세요\n"
            "예: 100, 500, 1000  100단위만 가능합니다.\n"
            f"100명당 {PER_100_PRICE} USDT",
            reply_markup=back_only_kb()
        )
        return

    if q.data == "menu:notice":
        await q.edit_message_text(NOTICE_TEXT, reply_markup=back_only_kb())
        return

    if q.data == "back:main":
        context.user_data.pop("awaiting_qty", None)
        await q.edit_message_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        return

    await q.answer("준비 중입니다.", show_alert=True)

async def qty_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_qty"):
        return

    text = update.message.text.strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("❌ 수량은 숫자만 입력해주세요. 예) 600, 1000", reply_markup=back_only_kb())
        return

    qty = int(text)
    if qty < 100 or qty % 100 != 0:
        await update.message.reply_text("❌ 100단위로만 입력 가능합니다. 예) 600, 1000, 3000", reply_markup=back_only_kb())
        return

    context.user_data["awaiting_qty"] = False
    context.user_data["ghost_qty"] = qty

    blocks = qty // 100
    amount = (PER_100_PRICE * Decimal(blocks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    context.user_data["ghost_amount"] = amount

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    pending_orders[user_id] = {"qty": qty, "amount": amount, "chat_id": chat_id}

    log.info("[ORDER] uid=%s qty=%s amount=%s chat_id=%s pending=%s",
             user_id, qty, amount, chat_id, len(pending_orders))

    await update.message.reply_text(
        "🧾 주문 요약\n"
        f"- 유령인원: {qty:,}명\n"
        f"- 결제수단: USDT(TRC20)\n"
        f"- 결제주소: `{PAYMENT_ADDRESS}`\n"
        f"- 결제금액: {amount} USDT\n\n"
        "⚠️ 반드시 위 **정확한 금액(소수점 포함)** 으로 송금해주세요.\n"
        "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
        parse_mode="Markdown",
        reply_markup=back_only_kb()
    )

# ─────────────────────────────────────────────
# TRC20 USDT 전송 확인 (간편/안전)
# ─────────────────────────────────────────────
TRONSCAN_URL = "https://apilist.tronscanapi.com/api/token_trc20/transfers"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaymentChecker/1.0)"}

def _to_decimal_amount(raw, token_decimals: int):
    if raw is None:
        return None
    try:
        s = str(raw)
        if s.isdigit():  # amountUInt64 같은 정수형
            return (Decimal(s) / (Decimal(10) ** token_decimals)).quantize(Decimal("0.000000"))
        return Decimal(s)
    except InvalidOperation:
        return None

async def check_tron_payments(app):
    params = {"sort": "-timestamp", "limit": "50", "start": "0", "address": PAYMENT_ADDRESS}

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # 대기 건수/처리건수 체크
                log.debug("[LOOP] pending=%s processed=%s", len(pending_orders), len(processed_txs))

                async with session.get(TRONSCAN_URL, params=params, headers=HEADERS, timeout=30) as resp:
                    if resp.status != 200:
                        log.warning("[Tronscan] HTTP %s, 잠시 후 재시도", resp.status)
                        await asyncio.sleep(10)
                        continue

                    data = await resp.json()
                    txs = data.get("token_transfers", []) or []
                    log.debug("[FETCH] txs=%s", len(txs))

                    if not pending_orders:
                        await asyncio.sleep(10)
                        continue

                    for tx in txs:
                        try:
                            txid = tx.get("transaction_id") or tx.get("hash")
                            contract = (tx.get("contract_address") or "").strip()
                            to_addr = (tx.get("to_address") or "").strip()
                            from_addr = (tx.get("from_address") or "").strip()
                            token_decimals = int(tx.get("tokenDecimal", 6))
                            raw = tx.get("amount") or tx.get("amount_str") or tx.get("amountUInt64")
                            amount = _to_decimal_amount(raw, token_decimals)

                            log.debug("[TX] id=%s contract=%s to=%s amount_raw=%s -> %s",
                                      txid, contract, to_addr, raw, amount)

                            if not txid or txid in processed_txs:
                                if txid:
                                    log.debug("[SKIP_DUP] %s", txid)
                                continue

                            if amount is None:
                                log.debug("[SKIP_NO_AMOUNT] id=%s", txid)
                                continue

                            # 필터: 컨트랙트 & 수취주소 일치
                            if contract != USDT_CONTRACT:
                                log.debug("[SKIP_CONTRACT] id=%s api=%s env=%s", txid, contract, USDT_CONTRACT)
                                continue
                            if to_addr != PAYMENT_ADDRESS:
                                log.debug("[SKIP_TO_ADDR] id=%s api=%s env=%s", txid, to_addr, PAYMENT_ADDRESS)
                                continue

                            matched = False
                            for uid, order in list(pending_orders.items()):
                                expected: Decimal = order["amount"]
                                diff = (amount - expected)
                                if abs(diff) <= Decimal("0.001"):
                                    matched = True
                                    chat_id = order["chat_id"]
                                    qty = order["qty"]

                                    log.info("[MATCH] tx=%s uid=%s amount=%s qty=%s", txid, uid, amount, qty)

                                    # 고객 알림
                                    try:
                                        await app.bot.send_message(
                                            chat_id=chat_id,
                                            text=(
                                                "✅ 결제가 확인되었습니다!\n"
                                                f"- 금액: {amount:.2f} USDT\n"
                                                f"- 주문 수량: {qty:,}\n\n"
                                                "📨 전달 받을 정보를 회신해주세요. (이메일/링크 등)"
                                            )
                                        )
                                        log.info("[NOTIFY_USER_OK] uid=%s chat_id=%s", uid, chat_id)
                                    except Exception as ee:
                                        log.error("[NOTIFY_USER_FAIL] uid=%s err=%s", uid, ee)

                                    # 운영자 알림
                                    if ADMIN_CHAT_ID:
                                        try:
                                            await app.bot.send_message(
                                                chat_id=ADMIN_CHAT_ID,
                                                text=(
                                                    "🟢 [결제 확인]\n"
                                                    f"- TXID: `{txid}`\n"
                                                    f"- From: `{from_addr}`\n"
                                                    f"- To  : `{to_addr}`\n"
                                                    f"- 금액: {amount:.6f} USDT\n"
                                                    f"- 주문자(UserID): {uid}\n"
                                                    f"- 수량: {qty:,}"
                                                ),
                                                parse_mode="Markdown"
                                            )
                                            log.info("[NOTIFY_ADMIN_OK] uid=%s admin=%s", uid, ADMIN_CHAT_ID)
                                        except Exception as ee:
                                            log.error("[NOTIFY_ADMIN_FAIL] uid=%s err=%s", uid, ee)

                                    processed_txs.add(txid)
                                    del pending_orders[uid]
                                    break
                                else:
                                    log.debug("[MISS] id=%s uid=%s tx=%s expected=%s diff=%s",
                                              txid, uid, amount, expected, diff)

                            if not matched:
                                log.debug("[UNMATCHED] id=%s amount=%s (orders=%s)", txid, amount, len(pending_orders))

                        except Exception as tx_err:
                            log.exception("[TX_ERROR] %s", tx_err)

            except Exception as e:
                log.exception("[LOOP_ERROR] %s", e)

            await asyncio.sleep(10)

# ─────────────────────────────────────────────
# 앱 구동
# ─────────────────────────────────────────────
async def on_startup(app):
    asyncio.create_task(check_tron_payments(app))
    log.info("🔄 TRC20 결제 확인 태스크 시작: addr=%s contract=%s", PAYMENT_ADDRESS, USDT_CONTRACT)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^(menu:ghost|menu:notice|back:main)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, qty_handler))
    log.info("✅ 유령 자판기 실행중...")
    app.run_polling()

if __name__ == "__main__":
    main()
