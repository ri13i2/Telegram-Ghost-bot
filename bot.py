import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    ContextTypes, CallbackQueryHandler
)

# 환경변수 로드
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

# ✅ 더미 DB
user_orders = {}  # user_id: {"amount": 7.21, "address": "...", "status": "pending"}

# -----------------------------
# /start
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("숙지사항/가이드", callback_data="guide")],
        [InlineKeyboardButton("메인으로", callback_data="main")]
    ]
    await update.message.reply_text(
        "✅ 노숙자 자판기 봇에 오신걸 환영합니다!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# -----------------------------
# 숙지사항/가이드 버튼
# -----------------------------
async def guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "▌ 유령 자판기 이용법 🚩\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "• 버튼 반응 없을시- 메뉴로돌아가기 클릭필수\n\n"
        "• 유령 인입 과정이 완료 되기까지 그룹/채널 설정금지입니다.\n\n"
        "• 작업 완료 시간까지 10~20분입니다.\n\n"
        "• 결제창 제한시간은 15분 이며, 경과시 처음부터 다시 결제해야 합니다.\n\n"
        "• 1개의 주소만 진행 가능합니다.\n"
        "• 자판기 이용법을 위반하여 일어나는 불상사는 책임지지 않습니다.\n\n"
        "자판기 운영 취지는\n"
        "① 잦은 계정 터짐\n"
        "② 계정 구매 할곳을 몰라서 본 폰번호 계정을 사용하는 사람들이 생각보다 많이 있어 시작하게 되었습니다.\n"
        "대량구매는 자제해 주세요.\n"
        "대량구매 필요시 개별문의 바랍니다.\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖➖"
    )

# -----------------------------
# 주문 요약 (예시)
# -----------------------------
async def order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount = 7.21
    address = "TPhHDf6YZo7kAG8VxqWKK2TKC9wU2MrowH"

    # 가상 주문 저장
    user_orders[user_id] = {
        "amount": amount,
        "address": address,
        "status": "pending"
    }

    await update.message.reply_text(
        f"📋 주문 요약\n"
        f"- 유령인원: 100명\n"
        f"- 결제수단: USDT\n"
        f"- 결제주소: {address}\n"
        f"- 결제금액: {amount} USDT\n\n"
        f"결제가 완료되면 자동 확인됩니다 ✅",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 메인으로", callback_data="main")]]
        )
    )

# -----------------------------
# 결제 확인 체크 (비동기 루프에서 계속 돌면서 확인)
# -----------------------------
async def check_tron_payments(app):
    while True:
        for user_id, order in list(user_orders.items()):
            if order["status"] == "pending":
                # TODO: 여기서 Tron API 로직으로 결제 완료 여부 확인해야 함
                # 지금은 테스트용으로 그냥 자동 결제 완료 처리
                logging.info(f"✅ 결제 확인됨 (user={user_id})")
                order["status"] = "paid"

                # ⭕️ 결제가 확인되었습니다!
                await app.bot.send_message(
                    chat_id=user_id,
                    text="⭕️ 결제가 확인되었습니다!"
                )

                # 🎁 유령을 받을 주소를 신중히 입력하세요!
                await app.bot.send_message(
                    chat_id=user_id,
                    text="🎁 유령을 받을 주소를 신중히 입력하세요!"
                )

        await asyncio.sleep(10)  # 10초마다 확인

# -----------------------------
# 콜백 핸들러 (메뉴 이동 등)
# -----------------------------
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🏠 메인 메뉴로 돌아왔습니다.")

# -----------------------------
# 메인 실행
# -----------------------------
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("order", order_summary))
    app.add_handler(CallbackQueryHandler(guide_callback, pattern="guide"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="main"))

    # ✅ 비동기 결제 확인 루프 시작
    async def on_startup(app):
        asyncio.create_task(check_tron_payments(app))

    app.post_init = on_startup

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
