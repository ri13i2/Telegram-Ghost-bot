import os
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

load_dotenv()  # 로컬에서만 적용. Railway에선 환경변수 사용

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Railway에서 제공하는 공개 URL을 WEBHOOK_URL로 넣어주세요. 예) https://your-app.up.railway.app
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # 배포 시 필수
PORT = int(os.getenv("PORT", "8080"))       # Railway가 자동 할당

WELCOME_TEXT = (
"➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
"▫️[텔레그램 유령 자판기]에 오신 것을 환영합니다!\n"
"▫️텔레그램 가라인원 구매 24h OK\n"
"▫️하단 메뉴 또는 /start 로 지금 시작하세요!\n"
"▫️가격은 유동적이며, 대량 구매는 판매자에게!\n"
"▫️숙지사항 꼭 확인하세요!\n"
"➖➖➖➖➖➖➖➖➖➖➖➖➖"
)

PER_100_PRICE = Decimal("7.21")  # 100명당 가격(총액 표시용)

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("유령인원", callback_data="menu:ghost")],
        [InlineKeyboardButton("텔프 유령인원", callback_data="menu:telf_ghost")],
        [InlineKeyboardButton("조회수", callback_data="menu:views")],
        [InlineKeyboardButton("게시글 반응", callback_data="menu:reactions")],
        [InlineKeyboardButton("숙지사항 및 사용법", callback_data="menu:notice")],
        [InlineKeyboardButton("판매자 문의하기", url="https://t.me/YourSellerID")]  # ← 실제 링크로 교체
    ])

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
        base = int(q.data.split(":")[1])  # 100 / 500 / 1000
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
        await q.answer("먼저 수량을 선택해주세요.", show_alert=True); return

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

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^(menu:ghost|ghost:\d+|back:main)$"))
    app.add_handler(CallbackQueryHandler(pay_handler, pattern=r"^ghost:pay$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, qty_handler))
    return app

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    app = build_app()

    if WEBHOOK_URL:
        # Railway: 웹훅 서버 기동 (aiohttp 내장)
        # webhook URL은 WEBHOOK_URL + /<token> 으로 권장(고유 경로)
        url_path = BOT_TOKEN
        full_url = WEBHOOK_URL.rstrip("/") + f"/{url_path}"
        print(f"[webhook] starting on 0.0.0.0:{PORT}, set URL={full_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=full_url,
        )
    else:
        # 로컬: 폴링 모드
        print("[polling] starting")
        app.run_polling()
