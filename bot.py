# bot.py — USDT(TRC20) 자동결제 확인 + 고객/운영자 알림 (패키지 선택 포함 버전)
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

USDT_CONTRACT = (os.getenv("USDT_CONTRACT") or "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj").strip()

try:
    PER_100_PRICE = Decimal(os.getenv("PER_100_PRICE", "7.21"))
except InvalidOperation:
    PER_100_PRICE = Decimal("7.21")
PER_100_PRICE = PER_100_PRICE.quantize(Decimal("0.01"))

# ─────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("paybot")

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
    "➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
    "• 버튼 반응 없을시 → 메뉴로 돌아가기 클릭 필수\n\n"
    "• 유령 인입 과정이 완료되기까지 그룹/채널 설정 금지\n"
    "• 작업 완료 시간은 약 10~20분 소요\n"
    "• 1개의 주소만 진행 가능합니다.\n"
    "• 결제창 제한시간은 15분이며, 경과 시 처음부터 다시 결제 필요\n\n"
    "• 자판기 이용법을 위반하여 발생하는 불상사는 책임지지 않습니다.\n\n"
    "자판기 운영 취지:\n"
    "① 잦은 계정 터짐 방지\n"
    "② 본인 계정 노출 방지 (안전)\n"
    "봇/대량 구매시 문의 바랍니다.\n"
    "➖➖➖➖➖➖➖➖➖➖➖➖➖"
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
        [
            InlineKeyboardButton("유령인원", callback_data="menu:ghost"),
            InlineKeyboardButton("텔프유령인원", callback_data="menu:telf_ghost"),
        ],
        [
            InlineKeyboardButton("조회수", callback_data="menu:views"),
            InlineKeyboardButton("게시글 반응", callback_data="menu:reactions"),
        ],
        [
            InlineKeyboardButton("숙지사항/가이드", callback_data="menu:notice"),
            InlineKeyboardButton("문의하기", url="https://t.me/ghostsalesbot1"),
        ],
    ])

def pkg_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"100명 - {PER_100_PRICE:.2f} USDT", callback_data="pkg:100")],
        [InlineKeyboardButton(f"500명 - {(PER_100_PRICE * Decimal(5)).quantize(Decimal('0.01')):.2f} USDT", callback_data="pkg:500")],
        [InlineKeyboardButton(f"1,000명 - {(PER_100_PRICE * Decimal(10)).quantize(Decimal('0.01')):.2f} USDT", callback_data="pkg:1000")],
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
        await q.edit_message_text("📦 인원수를 선택하세요", reply_markup=pkg_menu_kb())
        return

    if q.data.startswith("pkg:"):
        # 패키지 수량 선택
        try:
            qty = int(q.data.split(":")[1])
        except ValueError:
            await q.answer("수량을 확인할 수 없습니다.", show_alert=True)
            return

        if qty < 100 or qty % 100 != 0:
            await q.answer("100 단위로만 선택 가능합니다.", show_alert=True)
            return

        blocks = qty // 100
        amount = (PER_100_PRICE * Decimal(blocks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # 이후 결제 단계에서 사용될 값 저장
        context.user_data["ghost_qty"] = qty
        context.user_data["ghost_amount"] = amount

        await q.edit_message_text(
            "🧾 주문 요약\n"
            f"- 유령인원: {qty:,}명\n"
            f"- 결제수단: USDT(TRC20)\n"
            f"- 결제주소: `{PAYMENT_ADDRESS}`\n"
            f"- 결제금액: {amount} USDT\n\n"
            "⚠️ 반드시 위 **정확한 금액(소수점 포함)** 으로 송금해주세요.\n"
            "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 USDT(TRC20) 결제", callback_data="pay:USDT")],
                [InlineKeyboardButton("⬅️ 수량 다시 선택", callback_data="menu:ghost")],
                [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
            ])
        )
        return

    if q.data == "menu:notice":
        await q.edit_message_text(NOTICE_TEXT, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
        ]))
        return

    if q.data == "back:main":
        await q.edit_message_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        return

    await q.answer("준비 중입니다.", show_alert=True)

async def pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    qty = context.user_data.get("ghost_qty")
    amount = context.user_data.get("ghost_amount")
    chat_id = q.message.chat.id
    user_id = q.from_user.id

    if not qty or not amount:
        await q.answer("먼저 수량을 선택해주세요.", show_alert=True)
        return

    pending_orders[user_id] = {"qty": qty, "amount": amount, "chat_id": chat_id}

    await q.edit_message_text(
        "🧾 주문 요약\n"
        f"- 유령인원: {qty:,}명\n"
        f"- 결제수단: USDT(TRC20)\n"
        f"- 결제주소: `{PAYMENT_ADDRESS}`\n"
        f"- 결제금액: {amount} USDT\n\n"
        "⚠️ 반드시 위 **정확한 금액(소수점 포함)** 으로 송금해주세요.\n"
        "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
        ])
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
    params = {
        "sort": "-timestamp",
        "limit": "40",
        "start": "0",
        "address": PAYMENT_ADDRESS
    }

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(TRONSCAN_URL, params=params, headers=HEADERS, timeout=30) as resp:
                    if resp.status != 200:
                        log.warning("[Tronscan] HTTP %s, 잠시 후 재시도", resp.status)
                        await asyncio.sleep(15)
                        continue

                    data = await resp.json()
                    txs = data.get("token_transfers", []) or []

                    if not pending_orders:
                        await asyncio.sleep(20)
                        continue

                    for tx in txs:
                        try:
                            txid = tx.get("transaction_id") or tx.get("hash")
                            if not txid or txid in processed_txs:
                                continue

                            symbol = (tx.get("tokenAbbr") or tx.get("symbol") or "").upper()
                            contract = (tx.get("contract_address") or "").strip()
                            to_addr = (tx.get("to_address") or "").strip()
                            from_addr = (tx.get("from_address") or "").strip()

                            token_decimals = int(tx.get("tokenDecimal", 6))
                            raw = tx.get("amount") or tx.get("amount_str") or tx.get("amountUInt64")
                            amount = _to_decimal_amount(raw, token_decimals)
                            if amount is None:
                                continue

                            # 필터: USDT / 공식 컨트랙트 / 내 주소 수취
                            if symbol != "USDT":
                                continue
                            if contract != USDT_CONTRACT:
                                continue
                            if to_addr != PAYMENT_ADDRESS:
                                continue

                            # 대기 주문과 금액 매칭 (±0.001 허용)
                            for uid, order in list(pending_orders.items()):
                                expected: Decimal = order["amount"]
                                if abs(amount - expected) <= Decimal("0.001"):
                                    chat_id = order["chat_id"]
                                    qty = order["qty"]

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
                                    except Exception as ee:
                                        log.error("고객 알림 실패: %s", ee)

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
                                        except Exception as ee:
                                            log.error("운영자 알림 실패: %s", ee)

                                    processed_txs.add(txid)
                                    del pending_orders[uid]
                                    break  # 이 TX 처리 완료

                        except Exception as tx_err:
                            log.exception("TX 처리 중 오류: %s", tx_err)

            except Exception as e:
                log.exception("결제 확인 루프 오류: %s", e)

            await asyncio.sleep(15)

# ─────────────────────────────────────────────
# 앱 구동
# ─────────────────────────────────────────────
async def on_startup(app):
    asyncio.create_task(check_tron_payments(app))
    log.info("🔄 TRC20 결제 확인 태스크 시작: %s", PAYMENT_ADDRESS)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))

    # 패키지 포함: menu:ghost, pkg:\d+, 메뉴/공지/뒤로가기
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^(menu:ghost|pkg:\d+|menu:notice|back:main)$"))
    app.add_handler(CallbackQueryHandler(pay_handler, pattern=r"^pay:USDT$"))

    # (옵션) 텍스트 입력 핸들러는 현재 사용 안 함. 남겨둬도 무해.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda *_: None))

    log.info("✅ 유령 자판기 실행중...")
    app.run_polling()

if __name__ == "__main__":
    main()
