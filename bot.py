# bot.py
import os
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ─────────────────────────────────────────────────────────
# ENV 로드
# ─────────────────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
)

print("[env] BOT_TOKEN set?:", bool(BOT_TOKEN))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN이 설정되지 않았습니다 (Railway Service → Variables에 BOT_TOKEN 추가 후 재배포).")

# ─────────────────────────────────────────────────────────
# 상수/문구
# ─────────────────────────────────────────────────────────
WELCOME_TEXT = (
"➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
"◼️[텔레그램 유령 자판기]에 오신 것을 환영합니다!\n"
"◼️텔레그램 유령인원 구매 24h OK\n"
"◼️하단 메뉴 또는 /start 로 지금 시작하세요!\n"
"◼️가격은 유동적이며, 대량 구매는 판매자에게!\n"
"◼️숙지사항 꼭 확인하세요!\n"
"➖➖➖➖➖➖➖➖➖➖➖➖➖"
)

PER_100_PRICE = Decimal("7.21")  # 100명당 가격

# ─────────────────────────────────────────────────────────
# 키보드
# ─────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────
# 핸들러들
# ─────────────────────────────────────────────────────────
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
        base = int(q.data.split(":")[1])  # 100/500/1000
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
        try:
            # 기존 메시지 삭제
            await q.delete_message()
        except Exception:
            pass
        # 새 메시지 전송
        await q.message.chat.send_message(
            WELCOME_TEXT,
            reply_markup=main_menu_kb()
        )

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

    total_msg = ""
    if PER_100_PRICE:
        blocks = qty // 100
        total = (PER_100_PRICE * Decimal(blocks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_msg = f"\n💵 예상 결제금액: {total} USD (100명당 {PER_100_PRICE} USD 기준)"

    await update.message.reply_text(
        f"✅ 선택 수량: {qty:,}명 확인되었습니다.{total_msg}\n\n"
        f"다음 단계로 진행하시려면 아래 버튼을 눌러주세요.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧾 결제 안내 받기", callback_data="ghost:pay")],
            [InlineKeyboardButton("⬅️ 다시 선택", callback_data="menu:ghost")],
            [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
        ])
    )

async def pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    qty = context.user_data.get("ghost_qty")
    if not qty:
        await q.answer("먼저 수량을 선택해주세요.", show_alert=True)
        return

    await q.edit_message_text(
        f"🧾 주문 요약\n"
        f"- 유령인원: {qty:,}명\n"
        f"- 결제 단계로 진행합니다.\n\n"
        f"※ 실제 결제(주소/고유금액/링크)는 추후 연동하세요.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ 뒤로가기", callback_data="menu:ghost")],
            [InlineKeyboardButton("🏠 메인으로", callback_data="back:main")]
        ])
    )

# ─────────────────────────────────────────────────────────
# 앱 구동
# ─────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^(menu:ghost|ghost:\d+|back:main)$"))
    app.add_handler(CallbackQueryHandler(pay_handler, pattern=r"^ghost:pay$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, qty_handler))
    print("✅ 유령 자판기 실행 중... (polling)")
    app.run_polling()

if __name__ == "__main__":
    main()
