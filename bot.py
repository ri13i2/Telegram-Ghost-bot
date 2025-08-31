# bot.py — USDT(TRC20) 자동결제 확인 + 고객/운영자 알림
# (텍스트 수량 입력 / 뒤로가기만, 주문 영구 저장 + 미지정 입금 알림 + 디버깅 강화 + 허용오차 환경변수)

import os
import asyncio
import logging
import json
import re
import random
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from datetime import datetime, timedelta
from telegram.helpers import escape_markdown

import aiohttp

# ─────────────────────────────────────────────
# 안전한 MarkdownV2 이스케이프 함수
# ─────────────────────────────────────────────
def safe_md(text: str) -> str:
    if not text:
        return ""
    escape_chars = r"\_*[]()~`>#+-=|{}.!<>"
    for ch in escape_chars:
        text = text.replace(ch, "\\" + ch)
    return text

# ─────────────────────────────────────────────
# 환경 변수 로드
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "pending_state.json"

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
    PER_100_PRICE = _dec(os.getenv("PER_100_PRICE", "3.61"))
    PER_100_PRICE_TELF = _dec(os.getenv("PER_100_PRICE_TELF", "5.05"))
    PER_100_PRICE_VIEWS = _dec(os.getenv("PER_100_PRICE_VIEWS", "1.44"))
    PER_100_PRICE_REACTS = _dec(os.getenv("PER_100_PRICE_REACTS", "1.44"))

except InvalidOperation:
    PER_100_PRICE = _dec("3.61")
    PER_100_PRICE_TELF = _dec("5.05")
    PER_100_PRICE_VIEWS = _dec("1.44")
    PER_100_PRICE_REACTS = Decimal("1.44")

# 허용오차(매칭) 기본값 0.10 USDT
AMOUNT_TOLERANCE = _dec(os.getenv("AMOUNT_TOLERANCE", "0.10"))

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
log.info(
    "🔧 CONFIG | token=%s admin=%s addr=%s contract=%s per100=%s tol=±%s log=%s",
    masked_token, ADMIN_CHAT_ID, PAYMENT_ADDRESS, USDT_CONTRACT,
    PER_100_PRICE, AMOUNT_TOLERANCE, LOG_LEVEL
)

if not ADMIN_CHAT_ID:
    log.warning("⚠️ ADMIN_CHAT_ID가 설정되지 않아 운영자 알림이 전송되지 않습니다. .env에 본인 chat_id를 넣어주세요.")


log.info("🔑 TRON_API_KEY=%s", os.getenv("TRON_API_KEY"))

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
    "• 비공개로 설정시 진행은 불가하며 공개주소로 전달부탁드립니다.\n"
    "• 결제창 제한시간은 15분이며, 경과 시 처음부터 다시 결제 필요\n\n"
    "• 자판기 이용법을 위반하여 발생하는 불상사는 책임지지 않습니다.\n\n"
    "자판기 운영 취지:\n"
    "① 잦은 계정 터짐 방지\n"
    "② 본인 계정 노출 방지 (안전)\n"
    "봇/대량 구매시 문의 바랍니다.\n"
    "➖➖➖➖➖➖➖➖➖➖➖➖➖"
)

# ─────────────────────────────────────────────
# 상태 저장 (주문/처리TX)
# ─────────────────────────────────────────────
pending_orders: dict[str, dict] = {}
processed_txs: set[str] = set()
last_seen_ts: float = 0.0   # ★ 추가

def _save_state():
    try:
        data = {
            "pending_orders": {
                str(uid): {
                    "qty": v["qty"],
                    "amount": str(v["amount"]),
                    "chat_id": v["chat_id"],
                    "created_at": v.get("created_at", datetime.utcnow().timestamp())
                }
                for uid, v in pending_orders.items()
            },
            "processed_txs": list(processed_txs)[-2000:],
            "last_seen_ts": last_seen_ts,
            "seen_txids": list(seen_txids)[-2000:],  # 최근 본 TXID 저장
        }
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.debug("[STATE] saved pending=%s processed=%s last_seen=%s",
                  len(pending_orders), len(processed_txs), last_seen_ts)
    except Exception as e:
        log.error("[STATE_SAVE_ERROR] %s", e)

def _load_state():
    global pending_orders, processed_txs, last_seen_ts, seen_txids
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
                    "created_at": float(v.get("created_at", datetime.utcnow().timestamp())),
                }
            except Exception:
                continue
        pending_orders = po
        processed_txs = set(data.get("processed_txs") or [])
        last_seen_ts = data.get("last_seen_ts", 0)
        seen_txids = set(data.get("seen_txids") or [])
        log.info("[STATE] loaded pending=%s processed=%s last_seen=%s",
                 len(pending_orders), len(processed_txs), last_seen_ts)
    except Exception as e:
        log.error("[STATE_LOAD_ERROR] %s", e)

def _load_state():
    global pending_orders, processed_txs, last_seen_ts, seen_txids
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
                    "created_at": float(v.get("created_at", datetime.utcnow().timestamp())),
                }
            except Exception:
                continue
        pending_orders = po
        processed_txs = set(data.get("processed_txs") or [])
        last_seen_ts = float(data.get("last_seen_ts", 0))
        seen_txids = set(data.get("seen_txids") or [])
        log.info("[STATE] loaded pending=%s processed=%s", len(pending_orders), len(processed_txs))
    except Exception as e:
        log.error("[STATE_LOAD_ERROR] %s", e)

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
            "유령인원 수량을 입력해주세요\n"
            "예: 100, 500, 1000  100단위만 가능합니다.\n"
            f"100명당 {PER_100_PRICE} USDT",
            reply_markup=back_only_kb()
        )
        return

    if q.data == "menu:telf_ghost":
        context.user_data["awaiting_qty_telf"] = True
        log.info("[MENU] user=%s → awaiting_qty_telf=True", q.from_user.id)
        await q.edit_message_text(
            "텔프유령인원 수량을 입력해주세요\n"
            "예: 100, 500, 1000  100단위만 가능합니다.\n"
            f"100명당 {PER_100_PRICE_TELF} USDT",
            reply_markup=back_only_kb()
        )
        return

    if q.data == "menu:views":
        context.user_data["awaiting_qty_views"] = True
        log.info("[MENU] user=%s → awaiting_qty_views=True", q.from_user.id)
        await q.edit_message_text(
            "조회수 수량을 입력해주세요\n"
            "예: 100, 500, 1000  (100단위만 가능)\n"
            f"100회 조회수 = {PER_100_PRICE_VIEWS} USDT",
            reply_markup=back_only_kb()
        )
        return

    if q.data == "menu:reactions":
        context.user_data["awaiting_qty_reacts"] = True
        log.info("[MENU] user=%s → awaiting_qty_reacts=True", q.from_user.id)
        await q.edit_message_text(
            "게시글 반응 수량을 입력해주세요\n"
            "예: 100, 500, 1000  (100단위만 가능)\n"
            f"100회 반응 = {PER_100_PRICE_REACTS} USDT",
            reply_markup=back_only_kb()
        )
        return

    if q.data == "menu:notice":
        await q.edit_message_text(NOTICE_TEXT, reply_markup=back_only_kb())
        return

    if q.data == "back:main":
        for key in [
            "awaiting_qty", "awaiting_target",
            "awaiting_qty_telf", "awaiting_target_telf",
            "awaiting_qty_views", "awaiting_post_count_views", "awaiting_link_views",
            "awaiting_qty_reacts", "awaiting_post_count_reacts", "awaiting_link_reacts"
        ]:
            context.user_data.pop(key, None)
        await q.edit_message_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        return

    await q.answer("준비 중입니다.", show_alert=True)

# --- 단일 입력 핸들러 ---
async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) 수량 입력 대기 상태일 때
    if context.user_data.get("awaiting_qty"):
        text = update.message.text.strip().replace(",", "")
        if not text.isdigit():
            await update.message.reply_text("❌ 수량은 숫자만 입력해주세요. 예) 600, 1000", reply_markup=back_only_kb())
            return

        qty = int(text)
        if qty < 100 or qty % 100 != 0:
            await update.message.reply_text("❌ 100단위로만 입력 가능합니다. 예) 600, 1000, 3000", reply_markup=back_only_kb())
            return

        # 금액 계산
        blocks = qty // 100
        base_amount = (PER_100_PRICE * Decimal(blocks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # 0.001 ~ 0.009 USDT 랜덤 오프셋
        unique_offset = Decimal(str(random.randint(1, 9))) / Decimal("1000")

        # 최종 금액
        amount = (base_amount + unique_offset).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        # 상태 업데이트
        context.user_data["awaiting_qty"] = False
        context.user_data["awaiting_target"] = True
        context.user_data["ghost_qty"] = qty
        context.user_data["ghost_amount"] = amount

        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        pending_orders[user_id] = {"qty": qty, "amount": amount, "chat_id": chat_id, "created_at": datetime.utcnow().timestamp()}
        _save_state()
        log.info("[STATE] 주문 저장됨 uid=%s qty=%s amount=%s", user_id, qty, amount)

        await update.message.reply_text(
            f"✅ 유령인원 {qty:,}명 주문이 확인되었습니다.\n"
            "다음 단계로, 인원을 투입할 그룹/채널 주소(@username 또는 초대링크)를 입력해주세요.",
            reply_markup=back_only_kb()
        )
        return

    # --- 텔프유령인원 수량 입력 ---
    if context.user_data.get("awaiting_qty_telf"):
        text = update.message.text.strip().replace(",", "")
        if not text.isdigit():
            await update.message.reply_text("❌ 수량은 숫자만 입력해주세요. 예) 600, 1000", reply_markup=back_only_kb())
            return

        qty = int(text)
        if qty < 100 or qty % 100 != 0:
            await update.message.reply_text("❌ 100단위로만 입력 가능합니다. 예) 600, 1000, 3000", reply_markup=back_only_kb())
            return

        # 금액 계산 (텔프 단가)
        blocks = qty // 100
        base_amount = (PER_100_PRICE_TELF * Decimal(blocks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # 0.001 ~ 0.009 USDT 랜덤 오프셋
        unique_offset = Decimal(str(random.randint(1, 9))) / Decimal("1000")

        # 최종 금액
        amount = (base_amount + unique_offset).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

        # 상태 업데이트
        context.user_data["awaiting_qty_telf"] = False
        context.user_data["awaiting_target_telf"] = True
        context.user_data["telf_qty"] = qty
        context.user_data["telf_amount"] = amount

        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        pending_orders[user_id] = {"qty": qty, "amount": amount, "chat_id": chat_id, "type": "telf", "created_at": datetime.utcnow().timestamp()}
        _save_state()

        await update.message.reply_text(
            f"✅ 텔프유령인원 {qty:,}명 주문 확인되었습니다.\n"
            "다음 단계로, 인원을 투입할 그룹/채널 주소(@username 또는 초대링크)를 입력해주세요.",
            reply_markup=back_only_kb()
        )
        return

    # --- 조회수 수량 입력 ---
    if context.user_data.get("awaiting_qty_views"):
        text = update.message.text.strip().replace(",", "")
        if not text.isdigit():
            await update.message.reply_text("❌ 수량은 숫자만 입력해주세요.", reply_markup=back_only_kb())
            return
        qty = int(text)
        if qty < 100 or qty % 100 != 0:
            await update.message.reply_text("❌ 100단위로만 입력 가능합니다. 예) 600, 1000", reply_markup=back_only_kb())
            return

        context.user_data["views_qty"] = qty
        context.user_data["awaiting_qty_views"] = False
        context.user_data["awaiting_post_count_views"] = True

        await update.message.reply_text(
            f"✅ 조회수 {qty:,}개 주문 확인되었습니다.\n"
            "📌 원하시는 게시글 수량을 입력해주세요\n"
            "예: 1, 3, 5",
            reply_markup=back_only_kb()
        )
        return

    # --- 반응 수량 입력 ---
    if context.user_data.get("awaiting_qty_reacts"):
        text = update.message.text.strip().replace(",", "")
        if not text.isdigit():
            await update.message.reply_text("❌ 수량은 숫자만 입력해주세요.", reply_markup=back_only_kb())
            return
        qty = int(text)
        if qty < 100 or qty % 100 != 0:
            await update.message.reply_text("❌ 100단위로만 입력 가능합니다. 예) 600, 1000", reply_markup=back_only_kb())
            return

        context.user_data["reacts_qty"] = qty
        context.user_data["awaiting_qty_reacts"] = False
        context.user_data["awaiting_post_count_reacts"] = True

        await update.message.reply_text(
            f"✅ 반응 {qty:,}개 주문 확인되었습니다.\n"
            "📌 원하시는 게시글 수량을 입력해주세요\n"
            "예: 1, 3, 5",
            reply_markup=back_only_kb()
        )
        return

    # --- 조회수 게시글 개수 입력 ---
    if context.user_data.get("awaiting_post_count_views"):
        try:
            post_count = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ 숫자만 입력해주세요.")
            return

        context.user_data["views_post_count"] = post_count
        context.user_data["views_links"] = []              # ✅ 리스트 초기화
        context.user_data["awaiting_post_count_views"] = False
        context.user_data["awaiting_link_views"] = True    # ✅ 다음 단계 플래그 세팅

        await update.message.reply_text(
            f"📌 진행할 게시글 링크 {post_count}개를 순서대로 입력해주세요.",
            reply_markup=back_only_kb()
        )
        return

    # --- 반응 게시글 개수 입력 ---
    if context.user_data.get("awaiting_post_count_reacts"):
        try:
            post_count = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ 숫자만 입력해주세요.")
            return

        context.user_data["reacts_post_count"] = post_count
        context.user_data["reacts_links"] = []              # ✅ 리스트 초기화
        context.user_data["awaiting_post_count_reacts"] = False
        context.user_data["awaiting_link_reacts"] = True    # ✅ 다음 단계 플래그 세팅

        await update.message.reply_text(
            f"📌 진행할 게시글 링크 {post_count}개를 순서대로 입력해주세요.",
            reply_markup=back_only_kb()
        )
        return

    # --- 유령인원 주소 입력 ---
    if context.user_data.get("awaiting_target"):
        target = update.message.text.strip()
        context.user_data["awaiting_target"] = False
        context.user_data["ghost_target"] = target

        user_id = str(update.effective_user.id)
        if user_id in pending_orders:
            pending_orders[user_id]["target"] = target
            _save_state()

        qty = context.user_data["ghost_qty"]
        amount = context.user_data["ghost_amount"]

        await update.message.reply_text(
            "🧾 <b>최종 주문 요약</b>\n"
            f"- 유령인원: <b>{qty:,}명</b>\n"
            f"- 대상주소: <code>{target}</code>\n"
            f"- 결제수단: <b>USDT(TRC20)</b>\n"
            f"- 결제주소: <code>{PAYMENT_ADDRESS}</code>\n"
            f"- 결제금액: <b>{amount} USDT</b>\n\n"
            "⚠️ 반드시 위 <b>정확한 금액(소수점 포함)</b> 으로 송금해주세요.\n"
            "15분이내로 결제가 이루어지지 않을시 <b>자동취소</b>됩니다.\n"
            "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
            parse_mode="HTML",
            reply_markup=back_only_kb()
        )
        return

    # --- 텔프유령인원 주소 입력 ---
    if context.user_data.get("awaiting_target_telf"):
        target = update.message.text.strip()
        context.user_data["awaiting_target_telf"] = False
        context.user_data["ghost_target_telf"] = target

        user_id = str(update.effective_user.id)
        if user_id in pending_orders:
            pending_orders[user_id]["target_telf"] = target
            _save_state()

        qty = context.user_data["telf_qty"]
        amount = context.user_data["telf_amount"]

        await update.message.reply_text(
            "🧾 <b>최종 주문 요약</b>\n"
            f"- 텔프유령인원: <b>{qty:,}명</b>\n"
            f"- 대상주소: <code>{target}</code>\n"
            f"- 결제수단: <b>USDT(TRC20)</b>\n"
            f"- 결제주소: <code>{PAYMENT_ADDRESS}</code>\n"
            f"- 결제금액: <b>{amount} USDT</b>\n\n"
            "⚠️ 반드시 위 <b>정확한 금액(소수점 포함)</b> 으로 송금해주세요.\n"
            "15분이내로 결제가 이루어지지 않을시 <b>자동취소</b>됩니다.\n"
            "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
            parse_mode="HTML",
            reply_markup=back_only_kb()
        )
        return

    # --- 조회수 게시글 링크 입력 ---
    if context.user_data.get("awaiting_link_views"):
        link = update.message.text.strip()
        context.user_data["views_links"].append(link)

        links = context.user_data["views_links"]
        count = context.user_data["views_post_count"]

        if len(links) < count:
            # 중간 안내
            safe_links = [safe_md(l) for l in links]
            await update.message.reply_text(
                f"✅ {len(links)}개 게시글 입력 완료.\n"
                f"👉 나머지 {count - len(links)}개 링크를 입력해주세요.\n\n"
                "📌 현재까지 입력된 링크:\n" + "\n".join([f"{i+1}. {l}" for i, l in enumerate(safe_links, 1)]),
                reply_markup=back_only_kb()
            )
            return

        elif len(links) == count:
            # 최종 주문 요약
            context.user_data["awaiting_link_views"] = False
            qty = context.user_data["views_qty"]
            blocks = qty // 100
            base_amount = PER_100_PRICE_VIEWS * Decimal(blocks)
            total_amount = (base_amount * Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            context.user_data["views_amount"] = total_amount

            user_id = str(update.effective_user.id)
            chat_id = update.effective_chat.id
            pending_orders[user_id] = {
                "qty": qty * count,
                "amount": total_amount,
                "chat_id": chat_id,
                "type": "views",
                "created_at": datetime.utcnow().timestamp()
            }
            _save_state()

            safe_links = [safe_md(l) for l in links[:count]]
            await update.message.reply_text(
                "🧾 <b>최종 주문 요약</b>\n"
                f"- 조회수: {qty:,}개 × {count}개 게시글\n"
                f"- 총 주문량: {qty * count:,}개\n"
                f"- 게시글 링크:\n" + "\n".join([f"{i+1}. {l}" for i, l in enumerate(safe_links, 1)]) + "\n\n"
                f"- 결제수단: USDT(TRC20)\n"
                f"- 결제주소: <code>{PAYMENT_ADDRESS}</code>\n"
                f"- 결제금액: <b>{total_amount} USDT</b>\n\n"
                "⚠️ 반드시 위 <b>정확한 금액(소수점 포함)</b> 으로 송금해주세요.\n"
                "15분이내로 결제가 이루어지지 않으면 자동취소됩니다.\n"
                "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
                parse_mode="HTML",
                reply_markup=back_only_kb()
            )
            return

    # --- 반응 게시글 링크 입력 ---
    if context.user_data.get("awaiting_link_reacts"):
        link = update.message.text.strip()
        context.user_data["reacts_links"].append(link)

        links = context.user_data["reacts_links"]
        count = context.user_data["reacts_post_count"]

        if len(links) < count:
            # 중간 안내
            safe_links = [safe_md(l) for l in links]
            await update.message.reply_text(
                f"✅ {len(links)}개 게시글 입력 완료.\n"
                f"👉 나머지 {count - len(links)}개 링크를 입력해주세요.\n\n"
                "📌 현재까지 입력된 링크:\n" + "\n".join([f"{i+1}. {l}" for i, l in enumerate(safe_links, 1)]),
                reply_markup=back_only_kb()
            )
            return

        elif len(links) == count:
            # 최종 주문 요약
            context.user_data["awaiting_link_reacts"] = False
            qty = context.user_data["reacts_qty"]
            blocks = qty // 100
            base_amount = PER_100_PRICE_REACTS * Decimal(blocks)
            total_amount = (base_amount * Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            context.user_data["reacts_amount"] = total_amount

            user_id = str(update.effective_user.id)
            chat_id = update.effective_chat.id
            pending_orders[user_id] = {
                "qty": qty * count,
                "amount": total_amount,
                "chat_id": chat_id,
                "type": "reacts",
                "created_at": datetime.utcnow().timestamp()
            }
            _save_state()

            safe_links = [safe_md(l) for l in links[:count]]
            await update.message.reply_text(
                "🧾 <b>최종 주문 요약</b>\n"
                f"- 반응: {qty:,}개 × {count}개 게시글\n"
                f"- 총 주문량: {qty * count:,}개\n"
                f"- 게시글 링크:\n" + "\n".join([f"{i+1}. {l}" for i, l in enumerate(safe_links, 1)]) + "\n\n"
                f"- 결제수단: USDT(TRC20)\n"
                f"- 결제주소: <code>{PAYMENT_ADDRESS}</code>\n"
                f"- 결제금액: <b>{total_amount} USDT</b>\n\n"
                "⚠️ 반드시 위 <b>정확한 금액(소수점 포함)</b> 으로 송금해주세요.\n"
                "15분이내로 결제가 이루어지지 않으면 자동취소됩니다.\n"
                "결제가 확인되면 자동으로 메시지가 전송됩니다 ✅",
                parse_mode="HTML",
                reply_markup=back_only_kb()
            )
            return

# ─────────────────────────────────────────────
# 트론스캔 API 관련 유틸
# ─────────────────────────────────────────────
TRONGRID_URL = (
    f"https://api.trongrid.io/v1/accounts/{PAYMENT_ADDRESS}/transactions/trc20"
    f"?contract_address={USDT_CONTRACT}&only_to=true"
)

HEADERS = {
    "accept": "application/json",
    "TRON-PRO-API-KEY": os.getenv("TRON_API_KEY")  # <- TronGrid에서 발급받은 키
}
def _extract_amount(tx: dict):
    return (
        tx.get("amount") or
        tx.get("amount_str") or
        tx.get("amountUInt64") or
        tx.get("quant") or
        tx.get("value") or
        tx.get("tokenValue") or
        tx.get("raw_data", {}).get("contract", [{}])[0].get("parameter", {}).get("value", {}).get("amount")
    )

def _to_decimal_amount(raw, token_decimals: int):
    if raw is None:
        return None
    try:
        s = str(raw)
        if s.startswith("0x"):  # HEX 값
            val = int(s, 16)
            return (Decimal(val) / (Decimal(10) ** token_decimals)).quantize(Decimal("0.000000"))
        if s.isdigit():  # 정수 문자열
            return (Decimal(s) / (Decimal(10) ** token_decimals)).quantize(Decimal("0.000000"))
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None

def _nearest_pending(amount, n=3):
    """가장 가까운 금액 순으로 n개 pending order 반환"""
    try:
        diffs = []
        for uid, order in pending_orders.items():
            diff = abs(order["amount"] - amount)
            diffs.append((diff, uid, order))
        return sorted(diffs, key=lambda x: x[0])[:n]
    except Exception:
        return []

# ─────────────────────────────
# TronGrid / TronScan API 공통 조회 함수
# ─────────────────────────────
async def fetch_txs(session, url, headers=None):
    try:
        async with session.get(url, headers=headers, timeout=30) as resp:
            if resp.status != 200:
                log.warning("[API_FAIL] %s HTTP %s", url, resp.status)
                return []
            data = await resp.json()

            # TronGrid 응답 구조
            if "data" in data:
                return data["data"]
            # TronScan 응답 구조
            if "token_transfers" in data:
                return data["token_transfers"]
            if "trc20_transfers" in data:
                return data["trc20_transfers"]

            return []
    except Exception as e:
        log.error("[API_ERROR] url=%s err=%s", url, e)
        return []

# ─────────────────────────────
# 결제 감지 & 매칭 루프
# ─────────────────────────────
last_seen_ts = 0
seen_txids = set()   # 같은 타임스탬프라도 TXID 단위로 중복 처리 방지

async def check_tron_payments(app):
    global last_seen_ts, seen_txids

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # 1) TronGrid 기본 transactions/trc20
                txs = await fetch_txs(session, TRONGRID_URL, HEADERS)

                # 2) TronGrid events fallback
                if not txs:
                    alt_url = f"https://api.trongrid.io/v1/contracts/{USDT_CONTRACT}/events?event_name=Transfer&limit=20"
                    txs = await fetch_txs(session, alt_url, HEADERS)

                # 3) TronScan fallback
                if not txs:
                    txs = await fetch_txs(session, TRONSCAN_URL)

                # 최초 실행 시 타임스탬프 초기화
                if last_seen_ts == 0 and txs:
                    last_seen_ts = max(tx.get("block_timestamp") or tx.get("timestamp") or 0 for tx in txs)
                    log.info("[INIT] last_seen_ts 초기화=%s", last_seen_ts)
                    await asyncio.sleep(5)
                    continue

                log.debug("[FETCH] txs=%s", len(txs))
                all_txids = [t.get("transaction_id") or t.get("hash") or t.get("transactionHash") for t in txs]
                log.debug("[FETCH_TXIDS] %s", all_txids)

                for tx in txs:
                    ts = tx.get("block_timestamp") or tx.get("timestamp") or 0
                    txid = tx.get("transaction_id") or tx.get("hash") or tx.get("transactionHash")

                    # 중복 방지
                    if not txid or txid in processed_txs or txid in seen_txids:
                        continue
                    if ts < last_seen_ts:
                        continue

                    last_seen_ts = max(last_seen_ts, ts)
                    _save_state()
                    seen_txids.add(txid)

                    log.debug("[RAW_TX] %s", json.dumps(tx, ensure_ascii=False))

                    try:
                        to_addr = (tx.get("to_address") or tx.get("to") or tx.get("toAddress") or "").strip()
                        from_addr = (tx.get("from_address") or tx.get("from") or tx.get("fromAddress") or "").strip()

                        try:
                            token_decimals = int(tx.get("tokenDecimal", 6))
                        except Exception:
                            token_decimals = 6

                        raw = _extract_amount(tx)
                        amount = _to_decimal_amount(raw, token_decimals)
                        log.debug("[TX] id=%s to=%s raw=%s -> %s", txid, to_addr, raw, amount)

                        if amount is None:
                            continue

                        # ── 매칭 체크 ──
                        for uid, order in list(pending_orders.items()):
                            expected = order["amount"].quantize(Decimal("0.01"))
                            actual = amount.quantize(Decimal("0.01"))
                            log.debug("[MATCH_CHECK] uid=%s expected=%s actual=%s tol=%s", uid, expected, actual, AMOUNT_TOLERANCE)

                            if abs(expected - actual) <= AMOUNT_TOLERANCE:
                                matched_uid = uid
                                log.info("[MATCH_SUCCESS] uid=%s txid=%s 금액=%s", uid, txid, actual)

                                # 고객 알림
                                chat_id = order["chat_id"]
                                qty = order["qty"]
                                addr = order.get("target", "❌ 주소 미입력")
                                amount_expected = order["amount"]

                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=(f"✅ 결제가 확인되었습니다!\n"
                                          f"- 금액: {order['amount']:.2f} USDT\n"
                                          f"- 주문 수량: {qty:,}\n\n"
                                          "15분 내로 인원이 들어갑니다.")
                                )

                                # 운영자 알림
                                if ADMIN_CHAT_ID:
                                    user = await app.bot.get_chat(chat_id)
                                    username = f"@{user.username}" if user.username else f"ID:{matched_uid}"
                                    await app.bot.send_message(
                                        chat_id=ADMIN_CHAT_ID,
                                        text=(f"🟢 [결제 확인]\n"
                                              f"- 주문자: {username}\n"
                                              f"- 수량: {qty:,}\n"
                                              f"- 주소: {addr}\n"
                                              f"- 금액: {amount_expected} USDT\n"
                                              f"- TXID: {txid}")
                                    )

                                processed_txs.add(txid)
                                pending_orders.pop(matched_uid)
                                _save_state()
                                break
                        else:
                            # 매칭 실패 처리
                            if pending_orders:
                                log.warning("[MATCH_FAIL] txid=%s 금액=%s → 매칭 실패", txid, amount)
                                if ADMIN_CHAT_ID:
                                    await app.bot.send_message(
                                        ADMIN_CHAT_ID,
                                        f"⚠️ [미매칭 결제 감지]\n"
                                        f"- TXID: {txid}\n"
                                        f"- From: {from_addr}\n"
                                        f"- To: {to_addr}\n"
                                        f"- 금액: {amount:.6f} USDT\n"
                                        f"- 현재 보류 주문 수: {len(pending_orders)}개"
                                    )
                            else:
                                # 주문이 전혀 없는 상태에서 결제 들어옴
                                log.warning("[NO_ORDER_PAYMENT] txid=%s 금액=%s", txid, amount)
                                if ADMIN_CHAT_ID:
                                    await app.bot.send_message(
                                        ADMIN_CHAT_ID,
                                        f"⚠️ [주문 없는 결제 감지]\n"
                                        f"- TXID: {txid}\n"
                                        f"- From: {from_addr}\n"
                                        f"- To: {to_addr}\n"
                                        f"- 금액: {amount:.6f} USDT\n"
                                        "👉 주문 데이터가 없어 자동 처리 불가합니다."
                                    )
                            processed_txs.add(txid)
                            _save_state()

                    except Exception as e:
                        log.error("[ERROR] tx parse failed: %s", e)
                        continue

                # ── 주문 만료(15분 초과) 체크 ──
                now = datetime.utcnow().timestamp()
                expired = []
                for uid, order in list(pending_orders.items()):
                    if now - order.get("created_at", now) > 900:  # 900초 = 15분
                        expired.append((uid, order))

                for uid, order in expired:
                    chat_id = order["chat_id"]
                    try:
                        # 고객 알림
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text="⏰ 결제 제한시간(15분)이 초과되어 주문이 자동 취소되었습니다.\n"
                                 "다시 주문을 진행해주세요."
                        )
                        # 운영자 알림
                        if ADMIN_CHAT_ID:
                            await app.bot.send_message(
                                ADMIN_CHAT_ID,
                                f"❌ [주문 취소됨 - 시간초과]\n"
                                f"- UID: {uid}\n"
                                f"- 수량: {order['qty']:,}\n"
                                f"- 금액: {order['amount']} USDT"
                            )
                    except Exception as e:
                        log.error("[EXPIRE_NOTIFY_ERROR] uid=%s err=%s", uid, e)

                    pending_orders.pop(uid, None)
                    _save_state()

            except Exception as e:
                log.error("[ERROR] tron payment check failed: %s", e)

            await asyncio.sleep(5)

# ─────────────────────────────────────────────
# 메인 실행부
# ─────────────────────────────────────────────
async def on_startup(app):
    app.create_task(check_tron_payments(app))

def main():
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        print("❌ BOT_TOKEN이 .env에 설정되지 않았습니다.")
        return

    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

    # 핸들러 추가 (start, 메뉴, 입력)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    print("✅ 유령 자판기 봇 실행 중...")
    app.run_polling()

if __name__ == "__main__":
    main()
