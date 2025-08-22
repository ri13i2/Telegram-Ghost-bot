# bot.py — USDT(TRC20) 자동결제 확인 + 고객/운영자 알림
# (텍스트 수량 입력 / 뒤로가기만, 주문 영구 저장 + 미지정 입금 알림 + 디버깅 강화 + 허용오차 환경변수)

import os
import asyncio
import logging
import json
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
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "pending_state.json"

# 결제 완료된 사용자 저장소
paid_users = set()

load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN이 설정되지 않았습니다.")

PAYMENT_ADDRESS = (os.getenv("PAYMENT_ADDRESS") or "").strip()
if not PAYMENT_ADDRESS:
    raise RuntimeError("PAYMENT_ADDRESS가 설정되지 않았습니다.")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")

# TRON USDT(TRC20) 공식 컨트랙트 (메인넷)
USDT_CONTRACT = (os.getenv("USDT_CONTRACT") or "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").strip()

# ★ 변경: 안전한 Decimal 파서 + 허용오차 환경변수 지원
def _dec(v, q="0.01", default="0.00"):
    try:
        return Decimal(str(v)).quantize(Decimal(q))
    except Exception:
        return Decimal(default).quantize(Decimal(q))

try:
    PER_100_PRICE = _dec(os.getenv("PER_100_PRICE", "7.21"))
except InvalidOperation:
    PER_100_PRICE = _dec("7.21")
# 허용오차(매칭) 기본값 0.01 USDT
AMOUNT_TOLERANCE = _dec(os.getenv("AMOUNT_TOLERANCE", "0.01"))

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
log.info("🔧 CONFIG | token=%s admin=%s addr=%s contract=%s per100=%s tol=±%s log=%s",
         masked_token, ADMIN_CHAT_ID, PAYMENT_ADDRESS, USDT_CONTRACT, PER_100_PRICE, AMOUNT_TOLERANCE, LOG_LEVEL)

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
# 상태 저장 (주문/처리TX) — 파일 영구화
# ─────────────────────────────────────────────
pending_orders: dict[str, dict] = {}
processed_txs: set[str] = set()
completed_orders: dict[str, dict] = {}   # ✅ 결제 완료자

def _save_state():
    try:
        data = {
            "pending_orders": {
                str(uid): {
                    "qty": v["qty"],
                    "amount": str(v["amount"]),
                    "chat_id": v["chat_id"],
                } for uid, v in pending_orders.items()
            },
            "processed_txs": list(processed_txs)[-2000:],
        }
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.debug("[STATE] saved pending=%s processed=%s", len(pending_orders), len(processed_txs))
    except Exception as e:
        log.error("[STATE_SAVE_ERROR] %s", e)

def _load_state():
    global pending_orders, processed_txs
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        po = {}
        for uid, v in (data.get("pending_orders") or {}).items():
            try:
                po[str(uid)] = {
                    "qty": int(v["qty"]),
                    "amount": _dec(v["amount"]),
                    "chat_id": int(v["chat_id"]),
                }
            except Exception:
                continue
        pending_orders = po
        processed_txs = set(data.get("processed_txs") or [])
        log.info("[STATE] loaded pending=%s processed=%s", len(pending_orders), len(processed_txs))
    except Exception as e:
        log.error("[STATE_LOAD_ERROR] %s", e)

_load_state()

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

def back_only_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ 메뉴로 돌아가기", callback_data="back:main")]])

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

    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    pending_orders[user_id] = {"qty": qty, "amount": amount, "chat_id": chat_id}
    _save_state()

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
# TRC20 USDT 전송 확인 + 주문 매칭 & 알림
# ─────────────────────────────────────────────
TRONSCAN_URL = "https://apilist.tronscanapi.com/api/token_trc20/transfers"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaymentChecker/1.0)"}


def _to_decimal_amount(raw, token_decimals: int):
    if raw is None:
        return None
    try:
        s = str(raw)
        if s.startswith("0x"):  # ✅ HEX 문자열 처리
            val = int(s, 16)
            return (Decimal(val) / (Decimal(10) ** token_decimals)).quantize(Decimal("0.000000"))
        if s.isdigit():  # amountUInt64 같은 정수형
            return (Decimal(s) / (Decimal(10) ** token_decimals)).quantize(Decimal("0.000000"))
        return Decimal(s)  # 일반 소수 문자열
    except (InvalidOperation, ValueError):
        return None


def _extract_amount(tx: dict):
    """여러 케이스에서 안전하게 amount_raw 추출"""
    return (
        tx.get("amount")
        or tx.get("amount_str")
        or tx.get("amountUInt64")
        or tx.get("quant")
        or tx.get("value")
        or tx.get("tokenValue")
        or tx.get("raw_data", {}).get("contract", [{}])[0].get("parameter", {}).get("value", {}).get("amount")
    )


def _nearest_pending(amount: Decimal, top_k=3):
    """운영자 안전모드용 — 결제금액과 가장 가까운 주문 후보 찾기"""
    diffs = []
    for uid, order in pending_orders.items():
        exp = order["amount"]
        diffs.append((abs(amount - exp), uid, order))
    diffs.sort(key=lambda x: x[0])
    return diffs[:top_k]


async def check_tron_payments(app):
    params = {"sort": "-timestamp", "limit": "50", "start": "0", "address": PAYMENT_ADDRESS}

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.debug("[LOOP] pending=%s processed=%s", len(pending_orders), len(processed_txs))

                async with session.get(TRONSCAN_URL, params=params, headers=HEADERS, timeout=30) as resp:
                    if resp.status != 200:
                        log.warning("[Tronscan] HTTP %s, 잠시 후 재시도", resp.status)
                        await asyncio.sleep(10)
                        continue

                    data = await resp.json()
                    txs = data.get("token_transfers", []) or []
                    log.debug("[FETCH] txs=%s", len(txs))

                    if not txs:
                        await asyncio.sleep(10)
                        continue

                    for tx in txs:
                        try:
                            txid = tx.get("transaction_id") or tx.get("hash")
                            to_addr = (tx.get("to_address") or "").strip()
                            from_addr = (tx.get("from_address") or "").strip()
                            token_decimals = int(tx.get("tokenDecimal", 6))

                            raw = _extract_amount(tx)
                            amount = _to_decimal_amount(raw, token_decimals)

                            log.debug("[TX] id=%s to=%s raw=%s -> %s", txid, to_addr, raw, amount)

                            if not txid:
                                continue
                            if txid in processed_txs:
                                continue
                            if amount is None:
                                continue
                            if to_addr.lower() != PAYMENT_ADDRESS.lower():
                                continue

                            # 주문 매칭
                            matched_uid = None
                            for uid, order in list(pending_orders.items()):
                                if abs(order["amount"] - amount) <= AMOUNT_TOLERANCE:
                                    matched_uid = uid
                                    paid_users.add(matched_uid)
                                    break

                            if matched_uid:
                                order = pending_orders.pop(matched_uid)
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
                                                "📨 전달 주소를 전달해주세요. (그룹방/채널 등)"
                                            )
                                        )
                                        log.info("[NOTIFY_USER_OK] uid=%s chat_id=%s", matched_uid, chat_id)
                                    except Exception as ee:
                                        log.error("[NOTIFY_USER_FAIL] uid=%s err=%s", matched_uid, ee)

                                 # 운영자 알림 (항상 실행)
                                 if ADMIN_CHAT_ID:
                                     try:
                                         log.debug("[MATCH_OK] uid=%s amount=%s", matched_uid, amount)

                                         await app.bot.send_message(
                                             chat_id=ADMIN_CHAT_ID,
                                             text=(
                                                 "🟢 [결제 확인]\n"
                                                 f"- TXID: {txid}\n"
                                                 f"- From: {from_addr}\n"
                                                 f"- To  : {to_addr}\n"
                                                 f"- 금액: {amount:.6f} USDT\n"
                                                 f"- 주문자(UserID): {matched_uid}\n"
                                                 f"- 수량: {qty:,}\n"
                                            )
                                        )
                                    except Exception as e:
                                        log.error("[ADMIN_NOTIFY_ERROR] %s", e)

                                processed_txs.add(txid)
                                _save_state()
                            else:
                                # 매칭 실패 → 운영자에게 후보 보여주기
                                if ADMIN_CHAT_ID:
                                    nearest = _nearest_pending(amount)
                                    cand = "\n".join([f"• UID={uid} 기대금액={order['amount']}" for _, uid, order in nearest])
                                    msg = (
                                        f"⚠️ [미매칭 결제 감지]\n"
                                        f"- TXID: `{txid}`\n"
                                        f"- From: `{from_addr}`\n"
                                        f"- To  : `{to_addr}`\n"
                                        f"- 금액: {amount:.6f} USDT\n\n"
                                        f"📌 후보 주문:\n{cand if cand else '없음'}"
                                    )
                                    await app.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="Markdown")

                                processed_txs.add(txid)

                        except Exception as e:
                            log.error("[ERROR] tx parse failed: %s", e)
                            continue

            except Exception as e:
                log.error("[ERROR] tron payment check failed: %s", e)

            await asyncio.sleep(5)

# ─────────────────────────────────────────────
# 주소 핸들러 (결제 완료자만 가능)
# ─────────────────────────────────────────────
# 주소 입력 핸들러 (명령어가 아닌 일반 텍스트일 때만)
async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_qty"):  
        return  # 아직 수량 입력 단계면 무시

    user_id = str(update.effective_user.id)
    if user_id not in paid_users:
        return  # 결제 안 된 유저는 무시
    
    user_channel_link = update.message.text.strip()
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            "📩 [주소 전달]\n"
            f"- 주문자: @{update.effective_user.username or 'N/A'} (ID: {user_id})\n"
            f"- 전달 주소: {user_channel_link}"
        )
    )
    await update.message.reply_text("✅ 주소가 정상적으로 접수되었습니다.")

# ─────────────────────────────────────────────
# 메인 실행부
# ─────────────────────────────────────────────
async def on_startup(app):
    # 결제체커 루프 실행
    app.create_task(check_tron_payments(app))

def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()

    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        print("❌ BOT_TOKEN이 .env에 설정되지 않았습니다.")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    # 기본 핸들러
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler))
    app.add_handler(MessageHandler(filters.Regex(r"^\d+$"), qty_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^\d+$"), handle_address))

    # v20.6에서는 run_polling에 on_startup 못 넣음 → post_init 사용
    app.post_init = on_startup  

    print("✅ 유령 자판기 봇 실행 중...")
    app.run_polling()

# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()
