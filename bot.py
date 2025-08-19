# bot.py
import os
import asyncio
import aiohttp
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ─────────────────────────────────────────────
# ENV 로드
# ─────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN이 설정되지 않았습니다.")

# ─────────────────────────────────────────────
# 상수
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

PER_100_PRICE = Decimal("7.21")  # 100명당 가격
PAYMENT_ADDRESS = "TPhHDf6YZo7kAG8VxqWKK2TKC9wU2MrowH"

# 결제 대기 주문 저장소
pending_orders = {}  # {user_id: {"qty": int, "amount": Decimal, "chat_id": int}}

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
            InlineKeyboardButton("문의하기", url="https://t.me/YourSellerID"),
        ],
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
        kb = [
            [InlineKeyboardButton("100명 - 7.21$", callback_data="ghost:100")],
            [InlineKeyboardButton("500명 - 36.06$", callback_data="ghost:500")],
            [InlineKeyboardButton("1,000명 - 72.11$", callback_data="ghost:1000")],
            [InlineKeyboardButton("⬅️ 뒤로가기", callback_data="back:main")]
        ]
        await q.edit_message_text("🔹 인원수를 선택하세요", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data.startswith("ghost:"):
        base = int(q.data.split(":")[1])
        context.user_data["awaiting_ghost_qty"] = True
        context.user_data["ghost_base"] = base
        await q.edit_message_text(
            f"💫 {base:,}명을 선택하셨습니다!\n"
            f"📌 몇 개를 구매하시겠습니까?\n\n"
            f"※ 100단위로만 입력 가능합니다. (예: 600, 1000, 3000)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ 뒤로가기", callback_data="menu:ghost")],
                [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
            ])
        )

    elif q.data == "back:main":
        await q.edit_message_text(WELCOME_TEXT, reply_markup=main_menu_kb())

    else:
        await q.answer("준비 중입니다.", show_alert=True)

async def qty_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_ghost_qty"):
        return

    text = update.message.text.strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("수량은 숫자만 입력해주세요. (예: 600, 1000)")
        return

    qty = int(text)
    if qty < 100 or qty % 100 != 0:
        await update.message.reply_text("❌ 100단위로 입력해주세요. (예: 600, 1000, 3000)")
        return

    context.user_data["awaiting_ghost_qty"] = False
    context.user_data["ghost_qty"] = qty

    blocks = qty // 100
    total = (PER_100_PRICE * Decimal(blocks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    context.user_data["ghost_amount"] = total
    context.user_data["ghost_qty"] = qty

    await update.message.reply_text(
        f"💵 예상 결제금액: {total} USD (100명당 {PER_100_PRICE} USD 기준)\n\n"
        "💳 결제 수단을 선택하세요.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("TRON", callback_data="pay:TRX")],
            [InlineKeyboardButton("TRON Tether (USDT-TRC20)", callback_data="pay:USDT")],
            [InlineKeyboardButton("⬅️ 뒤로가기", callback_data="menu:ghost")]
        ])
    )

async def pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    method = q.data.split(":")[1]

    qty = context.user_data.get("ghost_qty")
    amount = context.user_data.get("ghost_amount")
    chat_id = q.message.chat_id
    user_id = q.from_user.id

    if not qty or not amount:
        await q.answer("먼저 수량을 선택해주세요.", show_alert=True)
        return

    pending_orders[user_id] = {"qty": qty, "amount": amount, "chat_id": chat_id}

    await q.edit_message_text(
        f"🧾 주문 요약\n"
        f"- 유령인원: {qty:,}명\n"
        f"- 결제수단: {method}\n"
        f"- 결제주소: `{PAYMENT_ADDRESS}`\n"
        f"- 결제금액: {amount} USDT\n\n"
        f"결제가 완료되면 자동 확인됩니다 ✅",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
        ])
    )

# ─────────────────────────────────────────────
# Tron 결제 확인 로직
# ─────────────────────────────────────────────
async def check_tron_payments(app):
    url = f"https://apilist.tronscanapi.com/api/transaction?sort=-timestamp&count=true&limit=20&start=0&address={PAYMENT_ADDRESS}"

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        txs = data.get("data", [])
                        for user_id, order in list(pending_orders.items()):
                            expected_amount = float(order["amount"])
                            for tx in txs:
                                if tx.get("contractType") == 1:  # TransferContract
                                    amount = tx.get("amount", 0) / 1e6
                                    if abs(amount - expected_amount) < 0.01:  # 금액 매칭
                                        chat_id = order["chat_id"]
                                        await app.bot.send_message(
                                            chat_id=chat_id,
                                            text=f"✅ 결제가 확인되었습니다!\n- 금액: {amount} USDT\n- 주문 수량: {order['qty']:,}명"
                                        )
                                        del pending_orders[user_id]
                                        break
        except Exception as e:
            print("결제 확인 에러:", e)

        await asyncio.sleep(30)  # 20초마다 확인

# ─────────────────────────────────────────────
# 앱 구동
# ─────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^(menu:ghost|ghost:\d+|back:main)$"))
    app.add_handler(CallbackQueryHandler(pay_handler, pattern=r"^pay:(TRX|USDT)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, qty_handler))

    # Tron 결제 감시 태스크 시작
    asyncio.get_event_loop().create_task(check_tron_payments(app))

    print("✅ 유령 자판기 실행 중... (polling)")
    app.run_polling()

if __name__ == "__main__":
    main()
