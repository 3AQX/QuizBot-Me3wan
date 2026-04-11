# bot.py — النسخة المحدثة: بتاريخ 11 ابريل 2026
import os
import re
import json
import logging
import sqlite3
from typing import List

import pdfplumber
import pandas as pd
from docx import Document
from dotenv import load_dotenv
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll, CallbackQuery
from telegram.error import TimedOut, TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- إعداد ----------
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # يُقرأ من .env
DB_PATH = "quizbot.db"
DOWNLOADS = "downloads"
os.makedirs(DOWNLOADS, exist_ok=True)

# Publish tuning (configurable via environment variables)
PUBLISH_RETRY = int(os.getenv("PUBLISH_RETRY", "3"))
PUBLISH_DELAY = 0  # No delay between questions in a batch
PUBLISH_RETRY_BACKOFF = float(os.getenv("PUBLISH_RETRY_BACKOFF", "1.0"))  # multiplier for retry backoff
PUBLISH_MIN_DELAY = 0  # No minimum delay between questions
PUBLISH_BATCH_SIZE = 10  # send this many questions before pause
PUBLISH_BATCH_PAUSE = 3.0  # 3 second pause between batches

# ---------- قاعدة البيانات ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        qtext TEXT NOT NULL,
        options_json TEXT NOT NULL,
        correct_letter TEXT,
        status TEXT DEFAULT 'pending'
    );
    """)
    conn.commit()
    conn.close()

def insert_question_db(qtext: str, options: List[str], correct: str = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO questions (qtext, options_json, correct_letter) VALUES (?, ?, ?)",
        (qtext, json.dumps(options, ensure_ascii=False), (correct.upper() if correct else None))
    )
    conn.commit()
    conn.close()

def get_pending_questions_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, qtext, options_json, correct_letter FROM questions WHERE status='pending' ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return [{"db_id": r[0], "qtext": r[1], "options": json.loads(r[2]), "correct": r[3]} for r in rows]

def get_flagged_questions_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, qtext, options_json, correct_letter FROM questions WHERE status='pending' ORDER BY id")
    rows = c.fetchall()
    conn.close()
    flagged = []
    for r in rows:
        db_id, qtext, options_json, correct = r
        opts = json.loads(options_json)
        reasons = []
        if len(qtext) > 300:
            reasons.append(f"طول السؤال ({len(qtext)} حرف) أطول من المسموح 300")
        if len(opts) > 10:
            reasons.append(f"عدد الخيارات ({len(opts)}) يتجاوز 10")
        for i, opt in enumerate(opts):
            if len(opt) > 100:
                reasons.append(f"الخيار {chr(65+i)} أطول من المسموح 100")
                break
        if reasons:
            flagged.append({"db_id": db_id, "qtext": qtext, "options": opts, "correct": correct, "reasons": reasons})
    return flagged

def get_unanswered_questions_db():
    """إرجاع الأسئلة المعلقة التي لا تحتوي على إجابة صحيحة."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, qtext FROM questions WHERE status='pending' AND (correct_letter IS NULL OR correct_letter='')"
    )
    rows = c.fetchall()
    conn.close()
    return [{"db_id": r[0], "qtext": r[1]} for r in rows]

def get_question_db_by_index(idx: int, flagged_only: bool = False):
    rows = get_flagged_questions_db() if flagged_only else get_pending_questions_db()
    if 0 <= idx < len(rows):
        row = rows[idx]
        row["index"] = idx
        row["total"] = len(rows)
        return row
    return None

def update_question_db(db_id: int, qtext: str = None, options: List[str] = None, correct: str = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if qtext is not None:
        c.execute("UPDATE questions SET qtext=? WHERE id=?", (qtext, db_id))
    if options is not None:
        c.execute("UPDATE questions SET options_json=? WHERE id=?", (json.dumps(options, ensure_ascii=False), db_id))
    if correct is not None:
        c.execute("UPDATE questions SET correct_letter=? WHERE id=?", ((correct.upper() if correct else None), db_id))
    conn.commit()
    conn.close()

def delete_question_db(db_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM questions WHERE id=?", (db_id,))
    conn.commit()
    conn.close()

def get_question_db(db_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, qtext, options_json, correct_letter FROM questions WHERE id=?", (db_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"db_id": row[0], "qtext": row[1], "options": json.loads(row[2]), "correct": row[3]}
    return None

def delete_all_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM questions")
    conn.commit()
    conn.close()

def mark_published_db(db_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE questions SET status='published' WHERE id=?", (db_id,))
    conn.commit()
    conn.close()

def pending_count_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM questions WHERE status='pending'")
    cnt = c.fetchone()[0]
    conn.close()
    return cnt

# ---------- تحليل النص و تنظيف الاختيارات ----------
# دعم حتى 10 خيارات (A-J أو أ-ي)
CHOICE_PATTERN = re.compile(r'([A-Ja-jأ-ي])\s*[-\.\)]\s*(.*?)(?=(?:[A-Ja-jأ-ي]\s*[-\.\)]|$))', re.I | re.S)

# أنماط الأسطر التي يجب تجاهلها لمنع تلوث نص السؤال
IGNORE_LINE_PATTERNS = [
    re.compile(r'^\s*(Answer[s]?|Correct|Solution|Note[s]?|Reference[s]?|Source|Figure|Table|الإجابة|الحل|ملاحظة)\s*[\:\-]', re.I),
    re.compile(r'^\s*[\*\-_=]{3,}\s*$'),
]

# نمط السؤال بصيغة Q: أو Question: أو س: أو سؤال:
Q_PREFIX_PATTERN = re.compile(r'^\s*(?:Q\.?\s*\d*|Question\.?\s*\d*|سؤال\.?\s*\d*|س\.?\s*\d*)\s*[\:\-]\s*(.+)', re.I)

def is_ignored_line(line: str) -> bool:
    for pat in IGNORE_LINE_PATTERNS:
        if pat.match(line):
            return True
    return False

def split_choices_from_line(line: str):
    matches = list(CHOICE_PATTERN.finditer(line))
    if matches and len(matches) > 1:
        options = []
        for m in matches:
            opt = m.group(2).strip()
            if "Answers" in opt:  # If we find "Answers", stop processing more options
                break
            options.append(opt)
        return options if options else None
    return None

def clean_option_line(line: str) -> str:
    """
    يحذف بادئة (A- أو B. أو C) فقط إذا كانت بداية السطر.
    يدعم الآن حتى J (10 خيارات).
    """
    line = line.strip()
    cleaned = re.sub(r'^[A-Ja-jأ-ي\d]\s*[-\.\)]\s*', '', line)
    return cleaned

def clean_question_text(q: str) -> str:
    if not q:
        return q
    # القطع عند آخر علامة استفهام وليس الأولى (إصلاح: أسئلة بأكثر من ?)
    if '?' in q:
        idx = q.rfind('?')
        q = q[:idx + 1]
    q = re.sub(r'\s{2,}', ' ', q).strip()
    return q

# ---------- استخراج من الملفات ----------
def parse_pdf_pages(file_path: str, selected_pages: List[int]) -> List[str]:
    lines = []
    try:
        with pdfplumber.open(file_path) as pdf:
            pages = pdf.pages
            selected = [p - 1 for p in selected_pages if 1 <= p <= len(pages)]
            for i in selected:
                text = pages[i].extract_text()
                if text:
                    for line in text.splitlines():
                        if line.strip():
                            lines.append(line.strip())
    except Exception:
        logger.exception("خطأ أثناء قراءة صفحات PDF")
    return lines


def _parse_questions_from_lines(lines: List[str]) -> List[dict]:
    """المحلل الأساسي: يبحث عن أرقام أو Q: كبداية لكل سؤال."""
    questions = []
    current_q = None
    for line in lines:
        line_s = line.strip()
        if not line_s or is_ignored_line(line_s):
            continue
        # نمط Q: / Question:
        qm = Q_PREFIX_PATTERN.match(line_s)
        if qm:
            if current_q:
                questions.append(current_q)
            current_q = {"question": qm.group(1).strip(), "options": []}
            continue
        # نمط رقمي: 1. أو 1- أو 1) أو 1:
        if re.match(r'^\s*[\d٠-٩]+\s*[\.\-\)\:]', line_s):
            if current_q:
                questions.append(current_q)
            qtxt = re.sub(r'^\s*[\d٠-٩]+\s*[\.\-\)\:]\s*', '', line_s).strip()
            current_q = {"question": qtxt, "options": []}
            continue
        # نمط اختيار: A- أو A. أو A) أو أ- أو أ.
        if re.match(r'^\s*([A-Ja-jأ-ي])\s*[\.\-\)]', line_s):
            if current_q is None:
                continue
            multi = split_choices_from_line(line_s)
            if multi:
                for m in multi:
                    current_q["options"].append(clean_option_line(m))
            else:
                current_q["options"].append(clean_option_line(line_s))
            continue
        # سطر عادي — يضاف لنص السؤال مع حد أقصى 1000 حرف ليسمح بحفظه وإعطاء تنبيه للمستخدم لاحقاً
        if current_q and len(current_q["question"]) < 1000:
            current_q["question"] += " " + line_s
    if current_q:
        questions.append(current_q)
    return questions

def _fallback_parser(lines: List[str]) -> List[dict]:
    """محلل بديل مرن: يبحث عن سطر (غير خيار) يليه مباشرة سطر يبدأ بحرف كخيار."""
    questions = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or is_ignored_line(line):
            i += 1
            continue
        next_is_opt = (
            i + 1 < len(lines)
            and re.match(r'^\s*([A-Ja-jأ-ي])\s*[\.\-\)]', lines[i + 1].strip())
        )
        if next_is_opt and len(line) < 1000 and not re.match(r'^\s*([A-Ja-jأ-ي])\s*[\.\-\)]', line):
            current_q = {"question": line, "options": []}
            i += 1
            while i < len(lines):
                opt_line = lines[i].strip()
                if not opt_line:
                    i += 1
                    break
                if re.match(r'^\s*([A-Ja-jأ-ي])\s*[\.\-\)]', opt_line):
                    multi = split_choices_from_line(opt_line)
                    if multi:
                        for m in multi:
                            current_q["options"].append(clean_option_line(m))
                    else:
                        current_q["options"].append(clean_option_line(opt_line))
                    i += 1
                else:
                    break
            if current_q["options"]:
                questions.append(current_q)
        else:
            i += 1
    return questions

def parse_questions_from_file(file_path: str, pdf_pages: List[int] = None):
    ext = os.path.splitext(file_path)[1].lower()
    lines = []
    try:
        if ext in [".xlsx", ".xls"]:
            engine = "openpyxl" if ext == ".xlsx" else "xlrd"
            df = pd.read_excel(file_path, header=None, engine=engine)
            for row in df.values:
                line = " ".join([str(x) for x in row if str(x) not in ('nan', 'None', '')])
                if line.strip():
                    lines.append(line.strip())
        elif ext == ".csv":
            loaded = False
            for enc in ["utf-8-sig", "utf-8", "cp1256", "latin-1"]:
                try:
                    df = pd.read_csv(file_path, header=None, encoding=enc, dtype=str)
                    for row in df.values:
                        line = " ".join([str(x) for x in row if str(x) not in ('nan', 'None', '')])
                        if line.strip():
                            lines.append(line.strip())
                    loaded = True
                    break
                except Exception:
                    continue
            if not loaded:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [line.rstrip("\n") for line in f if line.strip()]
        elif ext == ".txt":
            # دعم عدة ترميزات مع أولوية UTF-8-SIG للملفات العربية
            lines_loaded = False
            for enc in ["utf-8-sig", "utf-8", "cp1256", "latin-1"]:
                try:
                    with open(file_path, "r", encoding=enc, errors="strict") as f:
                        lines = [line.rstrip("\n") for line in f if line.strip()]
                    lines_loaded = True
                    break
                except Exception:
                    continue
            if not lines_loaded:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = [line.rstrip("\n") for line in f if line.strip()]
        elif ext == ".docx":
            doc = Document(file_path)
            auto_counter = 0
            NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            for p in doc.paragraphs:
                if not p.text.strip():
                    continue
                text = p.text.strip()
                has_auto_num = p._p.find(f'{NS}numPr') is not None
                already_numbered = bool(re.match(r'^\s*[\d٠-٩]+', text))
                is_option = bool(re.match(r'^\s*[A-Ja-j]\s*[\.\-\)]', text))
                if has_auto_num and not already_numbered and not is_option:
                    auto_counter += 1
                    lines.append(f"{auto_counter}. {text}")
                else:
                    lines.append(text)
        elif ext == ".pdf":
            if pdf_pages:
                lines = parse_pdf_pages(file_path, pdf_pages)
            else:
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            for line in text.splitlines():
                                if line.strip():
                                    lines.append(line.strip())
        else:
            return None
    except Exception:
        logger.exception("file read error")
        return None

    # المحاولة الأولى: المحلل الأساسي
    questions = _parse_questions_from_lines(lines)
    # المحاولة الثانية: إذا فشل، جرب المحلل البديل المرن
    if not questions and lines:
        logger.info("المحلل الأساسي لم يجد أسئلة، جاري تجربة المحلل البديل...")
        questions = _fallback_parser(lines)

    final = []
    for q in questions:
        opts = [o.strip() for o in q.get("options", []) if o and o.strip()]
        final.append({"qtext": clean_question_text(q["question"]), "options": opts})
    return final if final else None

# ---------- حالة المستخدم ----------
USER_STATE = {}  # user_id -> dict(action, step, tmp, ...)

# ---------- أزرار الواجهة ----------
def main_menu_kb():
    flagged_count = len(get_flagged_questions_db())
    flagged_btn = [[InlineKeyboardButton(f"⚠️ مراجعة المرفوضة ({flagged_count})", callback_data="review_flagged")]] if flagged_count > 0 else []
    
    kb = [
        [InlineKeyboardButton("📄 تحميل ملف", callback_data="upload")],
        [InlineKeyboardButton("✍️ إضافة سؤال يدوي", callback_data="add_manual")],
        [InlineKeyboardButton("🧾 مراجعة الأسئلة", callback_data="review")]
    ] + flagged_btn + [
        [InlineKeyboardButton("🅰️ (إدخال الإجابات (دفعة واحدة", callback_data="bulk_answers")],
        [InlineKeyboardButton("📤 نشر جميع الأسئلة هنا", callback_data="publish_all_here")],
        [InlineKeyboardButton("📤 إرسال الأسئلة إلى شات آخر", callback_data="send_to_id")],
        [InlineKeyboardButton("🆔 معرفة ID الجروب", callback_data="get_chat_id")],
        [InlineKeyboardButton("🗑️ حذف جميع الأسئلة", callback_data="delete_all")]
    ]
    return InlineKeyboardMarkup(kb)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="main")]])

# ---------- معالجة رفع الملفات ----------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ أرسل ملفاً صالحاً.", reply_markup=main_menu_kb())
        return
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ عذراً، حجم الملف يجب أن لا يتجاوز 20 ميجابايت.", reply_markup=main_menu_kb())
        return
    file = await document.get_file()
    filename = document.file_name
    path = os.path.join(DOWNLOADS, filename)
    await file.download_to_drive(path)
    USER_STATE.pop(user_id, None)

    ext = os.path.splitext(filename)[1].lower()
    supported = [".pdf", ".docx", ".txt", ".csv", ".xlsx", ".xls"]
    if ext == ".pdf":
        try:
            with pdfplumber.open(path) as pdf:
                pages = len(pdf.pages)
            USER_STATE[user_id] = {"action": "pdf_page_select", "file_path": path, "total": pages}
            await update.message.reply_text(
                f"📘 الملف يحتوي على {pages} صفحة.\n\nاكتب رقم/نطاق الصفحات المطلوب مثل:\n`10-20` أو `1,5,9` أو اكتب `all` لاستخراج الكل.",
                reply_markup=back_kb(),
                parse_mode="Markdown"
            )
        except Exception:
            await update.message.reply_text("❌ خطأ في قراءة PDF.", reply_markup=main_menu_kb())
            USER_STATE.pop(user_id, None)
    elif ext in supported:
        await process_file_and_insert(update, context, path, pdf_pages=None)
    else:
        await update.message.reply_text(
            f"❌ صيغة الملف `{ext}` غير مدعومة.\n\n✅ الصيغ المدعومة: PDF, DOCX, TXT, CSV, XLSX",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown"
        )

async def process_file_and_insert(update_or_query, context: ContextTypes.DEFAULT_TYPE, path: str, pdf_pages: List[int] = None):
    try:
        parsed = parse_questions_from_file(path, pdf_pages=pdf_pages)
        is_query = isinstance(update_or_query, CallbackQuery)
        if not parsed:
            if is_query:
                await update_or_query.edit_message_text("❌ لم يتم العثور على أسئلة في الملف.", reply_markup=main_menu_kb())
            else:
                await update_or_query.message.reply_text("❌ لم يتم العثور على أسئلة في الملف.", reply_markup=main_menu_kb())
            return
        inserted = 0
        flagged = 0
        for q in parsed:
            opts = q.get("options", []) or []
            if len(opts) == 1:
                opts.append("خيار فارغ")
            if len(q["qtext"]) > 300 or len(opts) > 10:
                flagged += 1
            insert_question_db(q["qtext"], opts)
            inserted += 1
        msg = f"✅ تم استخراج وحفظ {inserted} سؤال."
        if flagged > 0:
            msg += f"\n\n⚠️ انتبه: تم اكتشاف {flagged} أسئلة تتجاوز 300 حرف لطول السؤال أو يبلغ عدد خياراتها أكثر من 10.\nيرجى تعديلها يدوياً من قسم المراجعة للتمكن من نشرها في تيليجرام."
        if is_query:
            await update_or_query.edit_message_text(msg, reply_markup=main_menu_kb())
        else:
            await update_or_query.message.reply_text(msg, reply_markup=main_menu_kb())
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.error(f"Error removing file {path}: {e}")

# ---------- معالجة النص (state machine) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.message.from_user.id
    text = (update.message.text or "").strip()
    state = USER_STATE.get(user_id)
    if not state or state.get("action") == "await_file":
        if state and state.get("action") == "await_file":
            USER_STATE.pop(user_id, None)
        # إضافة الميزة الجديدة: تحليل الرسائل النصية المنسقة وكأنها ملف TXT
        lines = text.splitlines()
        questions = _parse_questions_from_lines(lines)
        if not questions:
            questions = _fallback_parser(lines)
            
        final = []
        for q in questions:
            opts = [o.strip() for o in q.get("options", []) if o and o.strip()]
            final.append({"qtext": clean_question_text(q["question"]), "options": opts})
            
        if final:
            inserted = 0
            flagged = 0
            for q in final:
                opts = q.get("options", []) or []
                if len(opts) == 1:
                    opts.append("خيار فارغ")
                if len(q["qtext"]) > 300 or len(opts) > 10:
                    flagged += 1
                insert_question_db(q["qtext"], opts)
                inserted += 1
            msg = f"✅ تم استخراج وحفظ {inserted} سؤال من رسالتك."
            if flagged > 0:
                msg += f"\n\n⚠️ انتبه: تم اكتشاف {flagged} أسئلة تتجاوز 300 حرف لطول السؤال أو يبلغ عدد خياراتها أكثر من 10.\nيرجى تعديلها يدوياً من قسم المراجعة للتمكن من نشرها في تيليجرام."
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        else:
            # رد توضيحي في حال لم يتم العثور على أسئلة
            await update.message.reply_text(
                "⚠️ لم يتم العثور على أسئلة في رسالتك بتنسيق معروف.\n\n"
                "تأكد من استخدام تنسيق مثل:\n"
                "1. ما هي عاصمة فرنسا؟\n"
                "أ- باريس\n"
                "ب- لندن\n\n"
                "أو أرسل ملفاً ليتم تحليله.",
                reply_markup=main_menu_kb()
            )
        return

    # PDF pages selection
    if state.get("action") == "pdf_page_select":
        total = state.get("total")
        path = state.get("file_path")
        if text.lower() == "all":
            pages = list(range(1, total + 1))
        else:
            try:
                pages = []
                parts = [p.strip() for p in text.split(",") if p.strip()]
                for part in parts:
                    if "-" in part:
                        a, b = map(int, part.split("-"))
                        pages.extend(range(a, b + 1))
                    else:
                        pages.append(int(part))
                pages = sorted(set([p for p in pages if 1 <= p <= total]))
                if not pages:
                    raise ValueError()
            except Exception:
                await update.message.reply_text("❌ صيغة غير صحيحة، أعد المحاولة مثل: `10-20` أو `1,5,9` أو `all`.", reply_markup=back_kb())
                return
        USER_STATE[user_id] = {"action": "pdf_page_confirm", "file_path": path, "pages": pages}
        if len(pages) == 1:
            pr = f"الصفحة {pages[0]}"
        else:
            pr = f"من {pages[0]} إلى {pages[-1]} (مجموع صفحات: {len(pages)})"
        await update.message.reply_text(
            f"سيتم استخراج الأسئلة {pr}.\nهل تريد المتابعة؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، استخرج", callback_data="pdf_confirm")],
                [InlineKeyboardButton("↩️ إلغاء", callback_data="main")]
            ])
        )
        return

    # Manual add
    if state.get("action") == "manual_add":
        step = state.get("step", 1)
        tmp = state.get("tmp", {})
        if step == 1:
            tmp["question"] = text
            USER_STATE[user_id] = {"action": "manual_add", "step": 2, "tmp": tmp}
            await update.message.reply_text("✍️ أرسل الآن الاختيارات — كل اختيار في سطر واحد، أو ارسلهما بصيغة A-.. B-..", reply_markup=back_kb())
            return
        elif step == 2:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            joined = " ".join(lines)
            multi = split_choices_from_line(joined)
            if multi:
                opts = [clean_option_line(m) for m in multi]
            else:
                opts = [clean_option_line(line) for line in lines if line.strip()]
            tmp["options"] = opts
            USER_STATE[user_id] = {"action": "manual_add", "step": 3, "tmp": tmp}
            await update.message.reply_text("✅ اكتب رقم الإجابة الصحيحة (1= A, 2= B, ...) أو اكتب '-' إذا لا توجد إجابة صحيحة.", reply_markup=back_kb())
            return
        elif step == 3:
            if text == "-":
                correct = None
            else:
                try:
                    idx = int(text) - 1
                    if 0 <= idx < len(state["tmp"]["options"]):
                        correct = chr(65 + idx)
                    else:
                        correct = None
                except Exception:
                    correct = None
            qtxt = state["tmp"]["question"]
            opts = state["tmp"]["options"]
            if len(opts) == 1:
                opts.append("خيار فارغ")
            insert_question_db(qtxt, opts, correct=correct)
            USER_STATE.pop(user_id, None)
            await update.message.reply_text("✅ تم إضافة السؤال يدوياً.", reply_markup=main_menu_kb())
            return

    # Bulk answers
    if state.get("action") == "bulk_answers":
        cleaned = re.sub(r'[^A-Za-z\-\s]', ' ', text)
        parts = cleaned.strip().split()
        if len(parts) == 1 and len(parts[0]) > 1 and all(ch.isalpha() or ch == '-' for ch in parts[0]):
            seq = [ch for ch in re.sub(r'[^A-Za-z\-]', '', parts[0])]
        else:
            seq = []
            for p in parts:
                if p == '-':
                    seq.append('-')
                else:
                    m = re.search(r'[A-Za-z\-]', p)
                    if m:
                        seq.append(m.group(0))
                    else:
                        seq.append('-')
        rows = get_pending_questions_db()
        applied = 0
        skipped = 0
        for i, q in enumerate(rows):
            if i >= len(seq):
                break
            letter = seq[i].upper()
            if letter == '-':
                update_question_db(q["db_id"], correct=None)
                skipped += 1
                continue
            idx = ord(letter) - ord('A')
            if 0 <= idx < len(q.get("options", [])):
                update_question_db(q["db_id"], correct=letter)
                applied += 1
            else:
                update_question_db(q["db_id"], correct=None)
                skipped += 1
        USER_STATE.pop(user_id, None)
        await update.message.reply_text(f"✅ تم تطبيق الإجابات. مُطبق: {applied}, بدون إجابة/مهمل: {skipped}", reply_markup=main_menu_kb())
        return

    # Goto (user typed number)
    if state.get("action") == "goto":
        try:
            idx = int(text) - 1
            # هنا نستدعي show_review_question بتمرير CallbackQuery-like object غير متاح
            # لأن المستخدم كتب رقم في رسالة، نرسل عرض كس_REPLY (سيكون رسالة جديدة)
            await show_review_question(update, context, idx=idx)
        except Exception:
            await update.message.reply_text("❌ رقم غير صحيح.", reply_markup=back_kb())
        USER_STATE.pop(user_id, None)
        return

    # choose edit option letter
    if state.get("action") == "choose_edit_option":
        if len(text) != 1 or not text.isalpha():
            await update.message.reply_text("❌ أدخل حرفًا واحدًا فقط (A–E).", reply_markup=back_kb())
            return
        USER_STATE[user_id] = {"action": "edit_one_text", "db_id": state.get("db_id"), "letter": text.upper()}
        await update.message.reply_text(f"أرسل النص الجديد للاختيار {text.upper()}:", reply_markup=back_kb())
        return

    if state.get("action") == "edit_one_text":
        db_id = state.get("db_id")
        letter = state.get("letter")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT options_json FROM questions WHERE id=?", (db_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await update.message.reply_text("❌ السؤال غير موجود.", reply_markup=main_menu_kb())
            USER_STATE.pop(user_id, None)
            return
        opts = json.loads(row[0])
        idx = ord(letter) - ord('A')
        if 0 <= idx < len(opts):
            opts[idx] = text
            update_question_db(db_id, options=opts)
            await update.message.reply_text(f"✅ تم تعديل الاختيار {letter}.", reply_markup=main_menu_kb())
        else:
            await update.message.reply_text("❌ رقم اختيار غير صالح.", reply_markup=main_menu_kb())
        USER_STATE.pop(user_id, None)
        return
    # ======= استقبال نصوص التعديل =======

    # تعديل نص السؤال
    if state.get("action") == "edit_text":
        db_id = state.get("db_id")
        update_question_db(db_id, qtext=text)
        USER_STATE.pop(user_id, None)
        await update.message.reply_text("✅ تم تعديل نص السؤال بنجاح.", reply_markup=main_menu_kb())
        return
        
    # إضافة اختيار جديد
    if state.get("action") == "add_opt":
        db_id = state.get("db_id")
        row = get_question_db(db_id)
        if not row:
            await update.message.reply_text("❌ السؤال غير موجود.", reply_markup=main_menu_kb())
            USER_STATE.pop(user_id, None)
            return
            
        opts = row["options"] if row["options"] else []
        if len(opts) >= 10:
            await update.message.reply_text("❌ لا يمكن إضافة المزيد من الاختيارات (الحد الأقصى 10).", reply_markup=main_menu_kb())
            USER_STATE.pop(user_id, None)
            return
            
        opts.append(clean_option_line(text))
        update_question_db(db_id, options=opts)
        await update.message.reply_text(f"✅ تم إضافة الاختيار {chr(65+len(opts)-1)}.", reply_markup=main_menu_kb())
        USER_STATE.pop(user_id, None)
        return

    # تعديل جميع الاختيارات دفعة واحدة
    if state.get("action") == "edit_all_opts":
        db_id = state.get("db_id")
        lines = [clean_option_line(line) for line in text.splitlines() if line.strip()]
        if not lines:
            await update.message.reply_text("❌ لم يتم العثور على أي اختيارات.", reply_markup=main_menu_kb())
            USER_STATE.pop(user_id, None)
            return
        update_question_db(db_id, options=lines)
        USER_STATE.pop(user_id, None)
        await update.message.reply_text("✅ تم تعديل جميع الاختيارات بنجاح.", reply_markup=main_menu_kb())
        return

    # حذف اختيار معيّن
    if state.get("action") == "delete_opt":
        db_id = state.get("db_id")
        letter = text.strip().upper()
        if not letter.isalpha() or not ('A' <= letter <= 'J'):
            await update.message.reply_text("❌ أدخل حرفًا صحيحًا من A إلى J.", reply_markup=back_kb())
            return
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT options_json FROM questions WHERE id=?", (db_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await update.message.reply_text("❌ لم يتم العثور على السؤال.", reply_markup=main_menu_kb())
            USER_STATE.pop(user_id, None)
            return
        opts = json.loads(row[0])
        idx = ord(letter) - ord('A')
        if 0 <= idx < len(opts):
            del opts[idx]
            update_question_db(db_id, options=opts)
            await update.message.reply_text(f"🗑️ تم حذف الاختيار {letter}.", reply_markup=main_menu_kb())
        else:
            await update.message.reply_text("❌ رقم اختيار غير صالح.", reply_markup=main_menu_kb())
        USER_STATE.pop(user_id, None)
        return
    # ====== إرسال الأسئلة إلى ID آخر ======
    if state.get("action") == "await_target_id":
        target_id = text.strip()
        try:
            target_int = int(target_id)
        except ValueError:
            await update.message.reply_text("❌ الـ ID غير صحيح، أعد المحاولة.", reply_markup=back_kb())
            return
        unanswered = get_unanswered_questions_db()
        if unanswered:
            q_list = "\n".join(
                [f"• {q['qtext'][:70]}{'...' if len(q['qtext'])>70 else ''}" for q in unanswered[:10]]
            )
            more = f"\n... و{len(unanswered)-10} غيرها" if len(unanswered) > 10 else ""
            USER_STATE[user_id] = {"action": "pending_publish", "chat_id": target_int, "is_same_chat": False, "progress_chat_id": update.message.chat_id}
            await update.message.reply_text(
                f"⚠️ يوجد *{len(unanswered)}* سؤال بدون إجابة صحيحة:\n\n{q_list}{more}\n\nماذا تريد أن تفعل بها؟",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 نشرها كاستطلاع رأي (بدون إجابة)", callback_data="publish_unanswered_as_poll")],
                    [InlineKeyboardButton("⏩ نشر الجميع (المجهولة الإجابة A تلقائياً)", callback_data="publish_all_force")],
                    [InlineKeyboardButton("🔙 رجوع لتحديد الإجابات", callback_data="main")]
                ])
            )
        else:
            USER_STATE.pop(user_id, None)
            await update.message.reply_text(f"📤 جاري إرسال الأسئلة إلى الشات ID: `{target_id}` ...", parse_mode="Markdown")
            try:
                await publish_all_to_chat(target_int, context, is_same_chat=False, progress_chat_id=update.message.chat_id)
            except Exception as e:
                await update.message.reply_text(f"❌ فشل الإرسال.\nتأكد أن البوت عضو في الشات.\n\n`{e}`", parse_mode="Markdown", reply_markup=main_menu_kb())
        return


# ---------- عرض قوائم وحذف ونشر ----------
async def show_delete_list(query: CallbackQuery, context, start=0, page_size=10):
    rows = get_pending_questions_db()
    if not rows:
        await query.edit_message_text("لا توجد أسئلة.", reply_markup=main_menu_kb())
        return
    end = min(start + page_size, len(rows))
    text_lines = []
    buttons = []
    for i in range(start, end):
        q = rows[i]
        txt = q["qtext"][:80] + ("..." if len(q["qtext"]) > 80 else "")
        text_lines.append(f"{i+1}. {txt}")
        buttons.append([InlineKeyboardButton(f"حذف {i+1}", callback_data=f"del_db:{q['db_id']}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"del_page:{max(0, start-page_size)}"))
    if end < len(rows):
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"del_page:{start+page_size}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("↩️ رجوع", callback_data="main")])
    text = "اختر سؤال للحذف:\n\n" + "\n".join(text_lines)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

# (removed duplicate/buggy `show_goto_menu` - the improved version appears later)

async def show_review_question(query, context, idx=0, flagged_only=False):
    row = get_question_db_by_index(idx, flagged_only=flagged_only)
    is_query = isinstance(query, CallbackQuery)
    if not row:
        if is_query:
            await query.edit_message_text("لا يوجد سؤال بهذا الرقم.", reply_markup=main_menu_kb())
        else:
            await query.message.reply_text("لا يوجد سؤال بهذا الرقم.", reply_markup=main_menu_kb())
        return

    opts = row["options"]
    opts_text = "\n".join([f"{chr(65+i)}) {opt}" for i, opt in enumerate(opts)]) if opts else "(لا توجد اختيارات)"
    corr = row["correct"] if row["correct"] else "-"
    
    # Count options
    opt_count = len(opts) if opts else 0
    can_add_option = opt_count < 10  # تليجرام يدعم حتى 10 خيارات
    
    text = f"السؤال {idx+1}/{row['total']}:\n\n{row['qtext']}\n\n{opts_text}\n\nالإجابة الصحيحة: {corr}"
    if flagged_only and "reasons" in row:
        reasons_text = "\n".join([f"• {r}" for r in row["reasons"]])
        text = f"🚨 **مرفوض للأسباب التالية:** 🚨\n{reasons_text}\n\n" + text

    buttons = []
    nav = []
    prefix = "review_flagged_idx" if flagged_only else "review_idx"
    if idx > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{prefix}:{idx-1}"))
    if idx + 1 < row["total"]:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{prefix}:{idx+1}"))
    if nav:
        buttons.append(nav)
        
    if not flagged_only:
        buttons.append([InlineKeyboardButton("🔢 الانتقال إلى سؤال معين", callback_data="goto_question")])

    buttons.append([
        InlineKeyboardButton("✏️ تعديل اختيار", callback_data=f"edit_one:{row['db_id']}"),
        InlineKeyboardButton("✏️ تعديل نص السؤال", callback_data=f"edit_text:{row['db_id']}")     
    ])

    buttons.append([
        InlineKeyboardButton("✏️ تعديل كل الاختيارات", callback_data=f"edit_all_opts:{row['db_id']}"),
        InlineKeyboardButton("🗑️ حذف اختيار", callback_data=f"delete_opt:{row['db_id']}")
    ])
    
    # Add the "Add option" button if we have room for more options
    if can_add_option:
        buttons.append([
            InlineKeyboardButton("➕ إضافة اختيار", callback_data=f"add_opt:{row['db_id']}")
        ])


    if opts:
        setrow = []
        for i in range(len(opts)):
            letter = chr(65+i)
            setrow.append(InlineKeyboardButton(letter, callback_data=f"set_correct:{row['db_id']}:{letter}"))
        buttons.append(setrow)

    buttons.append([
        InlineKeyboardButton("📤 نشر", callback_data=f"publish:{row['db_id']}"),
        InlineKeyboardButton("🗑️ حذف", callback_data=f"del_one:{row['db_id']}")
    ])
    buttons.append([InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="main")])

    if is_query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def show_goto_menu(query, start=0):
    rows = get_pending_questions_db()
    if not rows:
        await query.edit_message_text("❌ لا توجد أسئلة.", reply_markup=main_menu_kb())
        return

    total = len(rows)
    end = min(start + 10, total)
    btns = []

    # عرض أرقام الأسئلة (كل 10 أرقام في صفحة)
    for i in range(start, end):
        btns.append([InlineKeyboardButton(f"{i+1}", callback_data=f"review_idx:{i}")])

    # أزرار التنقل بين صفحات الأرقام
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"goto_page:{start-10}"))
    if end < total:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"goto_page:{end}"))
    if nav:
        btns.append(nav)

    # زر الرجوع للمراجعة
    btns.append([InlineKeyboardButton("↩️ رجوع", callback_data="review_idx:0")])

    await query.edit_message_text(
        f"اختر رقم السؤال للانتقال إليه (إجمالي {total} سؤال):",
        reply_markup=InlineKeyboardMarkup(btns)
    )


# ---------- نشر ----------
async def publish_one_db(chat_id, context: ContextTypes.DEFAULT_TYPE, db_id: int, as_regular_poll: bool = False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT qtext, options_json, correct_letter FROM questions WHERE id=?", (db_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    qtext, opts_json, correct = row[0], json.loads(row[1]), row[2]
    if not opts_json:
        opts_json = ["خيار افتراضي"]
    correct_index = None
    if correct:
        idx = ord(correct.upper()) - ord('A')
        if 0 <= idx < len(opts_json):
            correct_index = idx
    # إذا كان بدون إجابة صحيحة وطلب إرسالها كاستطلاع رأي
    send_as_regular = as_regular_poll or (correct_index is None)
    attempts = 0
    while attempts < PUBLISH_RETRY:
        try:
            if send_as_regular:
                await context.bot.send_poll(
                    chat_id=chat_id,
                    question=qtext,
                    options=opts_json,
                    type=Poll.REGULAR,
                    is_anonymous=True
                )
            else:
                await context.bot.send_poll(
                    chat_id=chat_id,
                    question=qtext,
                    options=opts_json,
                    type=Poll.QUIZ,
                    correct_option_id=correct_index,
                    is_anonymous=True
                )
            mark_published_db(db_id)
            return True
        except TimedOut:
            attempts += 1
            logger.warning("Timed out sending poll db_id=%s to chat=%s (attempt %s)", db_id, chat_id, attempts)
            await asyncio.sleep(PUBLISH_RETRY_BACKOFF * attempts)
            continue
        except TelegramError as e:
            logger.exception("Telegram error sending poll db_id=%s to chat=%s: %s", db_id, chat_id, e)
            return False
        except Exception as e:
            logger.exception("Unexpected error sending poll db_id=%s to chat=%s: %s", db_id, chat_id, e)
            return False
    logger.error("Failed to send poll db_id=%s to chat=%s after %s attempts", db_id, chat_id, attempts)
    return False

async def publish_all_to_chat(chat_id, context: ContextTypes.DEFAULT_TYPE, is_same_chat: bool = False, progress_chat_id: int = None, send_unanswered_as_poll: bool = True):
    rows = get_pending_questions_db()
    total = len(rows)
    if total == 0:
        await context.bot.send_message(progress_chat_id or chat_id, "❌ لا توجد أسئلة لإرسالها.", reply_markup=main_menu_kb())
        return

    sent = 0
    failed_ids = []
    
    # تحديد التأخير بناءً على مكان النشر
    current_delay = 0 if is_same_chat else PUBLISH_DELAY
    
    # إرسال رسالة التقدم في بداية العملية دائماً في محادثة البوت
    progress_msg = None
    try:
        progress_msg = await context.bot.send_message(
            chat_id=progress_chat_id or chat_id,  # استخدم محادثة البوت للتقدم
            text=f"🚀 جاري نشر {total} سؤال...\n{'إلى نفس المحادثة' if is_same_chat else f'إلى محادثة أخرى (ID: {chat_id})'}\nتم: 0/{total}"
        )
    except Exception:
        logger.warning("لم نتمكن من إرسال رسالة التقدم")
    
    if is_same_chat:
        logger.info("النشر في نفس المحادثة - سيتم الإرسال بدون تأخير")
    else:
        logger.info("النشر في محادثة أخرى - سيتم استخدام التأخير")
    
    
    # Process in batches of PUBLISH_BATCH_SIZE
    for batch_start in range(0, len(rows), PUBLISH_BATCH_SIZE):
        batch = rows[batch_start:batch_start + PUBLISH_BATCH_SIZE]
        batch_sent = 0
        
        # Send each question in the batch
        for r in batch:
            try:
                ok = await publish_one_db(chat_id, context, r["db_id"], as_regular_poll=(send_unanswered_as_poll and not r.get("correct")))
                if ok:
                    sent += 1
                    batch_sent += 1
                    logger.info("✓ تم إرسال السؤال %s (%d/%d)", r["db_id"], sent, total)
                else:
                    failed_ids.append(r["db_id"])
                    logger.warning("✗ فشل إرسال السؤال %s", r["db_id"])
            except TimedOut:
                logger.warning("⌛ تأخر إرسال السؤال %s", r["db_id"])
                failed_ids.append(r["db_id"])
            except TelegramError as e:
                if "Flood control exceeded" in str(e):
                    try:
                        wait_sec = float(str(e).split("Retry in ")[1].split(" ")[0])
                        current_delay = max(current_delay, wait_sec / 10)
                        logger.info("⚠️ تم تعديل وقت التأخير إلى %.1f ثانية", current_delay)
                        await asyncio.sleep(wait_sec)
                    except Exception:
                        current_delay = max(current_delay * 1.5, PUBLISH_MIN_DELAY)
                    failed_ids.append(r["db_id"])
                else:
                    logger.warning("❌ خطأ تيليجرام عند إرسال السؤال %s", r["db_id"])
                    failed_ids.append(r["db_id"])
            except Exception:
                logger.warning("❌ خطأ غير متوقع عند إرسال السؤال %s", r["db_id"])
                failed_ids.append(r["db_id"])

            # تحديث رسالة التقدم بعد كل سؤال
            if progress_msg:
                try:
                    status_msg = f"🚀 جاري نشر {total} سؤال...\n"
                    status_msg += f"✅ تم بنجاح: {sent}/{total}\n"
                    if failed_ids:
                        status_msg += f"❌ فشل إرسال: {len(failed_ids)}"
                    await context.bot.edit_message_text(
                        text=status_msg,
                        chat_id=progress_msg.chat_id,
                        message_id=progress_msg.message_id
                    )
                except Exception:
                    pass  # تجاهل أي خطأ في تحديث رسالة التقدم
            
        # After completing a batch, take a pause if more questions remain (only for different chat)
        if not is_same_chat and batch_start + len(batch) < total:
            logger.info("⏳ راحة %d ثواني بعد إرسال %d سؤال من الدفعة", PUBLISH_BATCH_PAUSE, batch_sent)
            
            if progress_msg:
                try:
                    pause_msg = f"🚀 جاري نشر {total} سؤال...\n"
                    pause_msg += f"✅ تم بنجاح: {sent}/{total}\n"
                    if failed_ids:
                        pause_msg += f"❌ فشل إرسال: {len(failed_ids)}\n"
                    pause_msg += f"⏳ راحة {PUBLISH_BATCH_PAUSE} ثواني..."
                    
                    await context.bot.edit_message_text(
                        text=pause_msg,
                        chat_id=progress_msg.chat_id,
                        message_id=progress_msg.message_id
                    )
                except Exception:
                    pass
            
            await asyncio.sleep(PUBLISH_BATCH_PAUSE)  # راحة بين الدفعات
    # إظهار النتيجة النهائية
    remaining = pending_count_db()
    
    # تحديث رسالة التقدم النهائية
    if progress_msg:
        try:
            final_msg = "✅ اكتملت العملية!\n\n"
            final_msg += "📊 إحصائيات النشر:\n"
            final_msg += f"• إجمالي الأسئلة: {total}\n"
            final_msg += f"• تم إرسال بنجاح: {sent}\n"
            if failed_ids:
                final_msg += f"• فشل إرسال: {len(failed_ids)}\n"
            if remaining > 0:
                final_msg += f"• متبقي في القاعدة: {remaining}\n"
            
            await context.bot.edit_message_text(
                text=final_msg,
                chat_id=progress_msg.chat_id,
                message_id=progress_msg.message_id
            )
        except Exception:
            pass
            
    # تحديث الرسالة النهائية
    final_msg = "✅ اكتملت العملية!\n\n"
    final_msg += "📊 إحصائيات النشر:\n"
    final_msg += f"• إجمالي الأسئلة: {total}\n"
    final_msg += f"• تم إرسال بنجاح: {sent}\n"
    if failed_ids:
        final_msg += f"• فشل إرسال: {len(failed_ids)}\n"
    if remaining > 0:
        final_msg += f"• متبقي في القاعدة: {remaining}\n"
    
    # إرسال رسالة جديدة بالإحصائيات النهائية والقائمة
    try:
        # أولاً، نحدث رسالة التقدم لتظهر أنه تم الانتهاء
        if progress_msg:
            await context.bot.edit_message_text(
                text=final_msg,
                chat_id=progress_msg.chat_id,
                message_id=progress_msg.message_id
            )
        
        # ثم نرسل رسالة جديدة مع القائمة
        await context.bot.send_message(
            chat_id=progress_msg.chat_id if progress_msg else chat_id,
            text="✅ تم نشر الأسئلة بنجاح!\nاختر إجراء من القائمة أدناه:",
            reply_markup=main_menu_kb()
        )
    except Exception as e:
        logger.warning(f"خطأ عند إرسال رسالة الإكمال: {e}")

# ---------- التعامل مع الأزرار ----------
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    if data == "main":
        await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_kb())
        return

    if data == "upload":
        USER_STATE[uid] = {"action": "await_file"}
        await query.edit_message_text("📂 ابعت الملف الآن (docx/pdf/txt/csv/xlsx).", reply_markup=back_kb())
        return

    if data == "add_manual":
        USER_STATE[uid] = {"action": "manual_add", "step": 1, "tmp": {}}
        await query.edit_message_text("✏️ إضافة سؤال يدوي — اكتب نص السؤال الآن.", reply_markup=back_kb())
        return

    if data == "bulk_answers":
        USER_STATE[uid] = {"action": "bulk_answers"}
        await query.edit_message_text("✳️ ابعت سلسلة الحروف بالترتيب (مثال: `B A D C` أو `BADC`). اكتب '-' لسؤال بدون إجابة.", reply_markup=back_kb(), parse_mode="Markdown")
        return

    if data == "review":
        if pending_count_db() == 0:
            await query.edit_message_text("لا توجد أسئلة محفوظة حالياً.", reply_markup=main_menu_kb())
            return
        # هنا نمرر whole callback query كي الدالة تعدّل نفس الرسالة
        await show_review_question(query, context, idx=0, flagged_only=False)
        return

    if data == "review_flagged":
        if len(get_flagged_questions_db()) == 0:
            await query.edit_message_text("لا توجد أسئلة مرفوضة حالياً.", reply_markup=main_menu_kb())
            return
        await show_review_question(query, context, idx=0, flagged_only=True)
        return

    if data.startswith("review_flagged_idx:"):
        idx = int(data.split(":")[1])
        await show_review_question(query, context, idx=idx, flagged_only=True)
        return

    if data == "delete_all":
        cnt = pending_count_db()
        if cnt == 0:
            await query.edit_message_text("❌ لا توجد أسئلة لحذفها.", reply_markup=main_menu_kb())
        else:
            await query.edit_message_text(
                f"⚠️ تنبيه! أنت على وشك حذف *{cnt}* سؤال بشكل نهائي.لا يمكن التراجع!\n\nهل أنت متأكد؟",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ نعم احذف الكل", callback_data="delete_all_confirm")],
                    [InlineKeyboardButton("❌ إلغاء", callback_data="main")]
                ])
            )
        return

    if data == "delete_all_confirm":
        delete_all_db()
        await query.edit_message_text("✅ تم حذف جميع الأسئلة من القاعدة.", reply_markup=main_menu_kb())
        return

    if data == "publish_all_here":
        unanswered = get_unanswered_questions_db()
        if unanswered:
            q_list = "\n".join(
                [f"• {q['qtext'][:70]}{'...' if len(q['qtext'])>70 else ''}" for q in unanswered[:10]]
            )
            more = f"\n... و{len(unanswered)-10} غيرها" if len(unanswered) > 10 else ""
            USER_STATE[uid] = {"action": "pending_publish", "chat_id": query.message.chat_id, "is_same_chat": True, "progress_chat_id": query.message.chat_id}
            await query.edit_message_text(
                f"⚠️ يوجد *{len(unanswered)}* سؤال بدون إجابة صحيحة:\n\n{q_list}{more}\n\nماذا تريد أن تفعل بها؟",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 نشرها كاستطلاع رأي (بدون إجابة)", callback_data="publish_unanswered_as_poll")],
                    [InlineKeyboardButton("⏩ نشر الجميع (المجهولة الإجابة A تلقائياً)", callback_data="publish_all_force")],
                    [InlineKeyboardButton("🔙 رجوع لتحديد الإجابات", callback_data="main")]
                ])
            )
        else:
            await publish_all_to_chat(query.message.chat_id, context, is_same_chat=True, progress_chat_id=query.message.chat_id)
        return

    if data == "publish_unanswered_as_poll":
        state = USER_STATE.pop(uid, {})
        chat_id = state.get("chat_id", query.message.chat_id)
        is_same = state.get("is_same_chat", True)
        prog_id = state.get("progress_chat_id", query.message.chat_id)
        await query.edit_message_text("🚀 جاري نشر الأسئلة (الأسئلة بدون إجابة ستُرسل كاستطلاع رأي)...")
        await publish_all_to_chat(chat_id, context, is_same_chat=is_same, progress_chat_id=prog_id, send_unanswered_as_poll=True)
        return

    if data == "publish_all_force":
        state = USER_STATE.pop(uid, {})
        chat_id = state.get("chat_id", query.message.chat_id)
        is_same = state.get("is_same_chat", True)
        prog_id = state.get("progress_chat_id", query.message.chat_id)
        await query.edit_message_text("🚀 جاري نشر جميع الأسئلة...")
        await publish_all_to_chat(chat_id, context, is_same_chat=is_same, progress_chat_id=prog_id, send_unanswered_as_poll=False)
        return

    if data == "pdf_confirm":
        state = USER_STATE.get(uid, {})
        path = state.get("file_path")
        pages = state.get("pages", [])
        if not path or not pages:
            await query.edit_message_text("❌ خطأ داخلي، حاول مرة أخرى.", reply_markup=main_menu_kb())
            USER_STATE.pop(uid, None)
            return
        await query.edit_message_text("📥 جاري استخراج الأسئلة من الصفحات المحددة ...")
        await process_file_and_insert(query, context, path, pdf_pages=pages)
        USER_STATE.pop(uid, None)
        return

    if data.startswith("del_page:"):
        start = int(data.split(":")[1])
        await show_delete_list(query, context, start=start)
        return

    if data.startswith("del_db:"):
        db_id = int(data.split(":")[1])
        delete_question_db(db_id)
        await query.edit_message_text("🗑️ تم حذف السؤال.", reply_markup=main_menu_kb())
        return

    if data.startswith("del_one:"):
        db_id = int(data.split(":")[1])
        delete_question_db(db_id)
        await query.edit_message_text("🗑️ تم حذف السؤال.", reply_markup=main_menu_kb())
        return

    if data.startswith("review_idx:"):
        idx = int(data.split(":")[1])
        await show_review_question(query, context, idx=idx)
        return

    if data == "goto_question":
        await show_goto_menu(query)
        return

    if data.startswith("goto_page:"):
        start = int(data.split(":")[1])
        await show_goto_menu(query, start=start)
        return

    # ======= تعديل السؤال والاختيارات =======
    if data.startswith("edit_text:"):
        db_id = int(data.split(":")[1])
        USER_STATE[uid] = {"action": "edit_text", "db_id": db_id}
        await query.edit_message_text("✏️ أرسل النص الجديد للسؤال:", reply_markup=back_kb())
        return

    if data.startswith("edit_one:"):
        db_id = int(data.split(":")[1])
        USER_STATE[uid] = {"action": "choose_edit_option", "db_id": db_id}
        await query.edit_message_text("اكتب الحرف (A,B,C,D,...) للاختيار الذي تريد تعديله:", reply_markup=back_kb())
        return

    if data.startswith("edit_all_opts:"):
        db_id = int(data.split(":")[1])
        USER_STATE[uid] = {"action": "edit_all_opts", "db_id": db_id}
        await query.edit_message_text(
            "✏️ أرسل كل الاختيارات الجديدة كل اختيار في سطر (مثلاً:\nA- Kidney \nB- Lung \nC- الكبLiver...)", 
            reply_markup=back_kb()
        )
        return

    if data.startswith("delete_opt:"):
        db_id = int(data.split(":")[1])
        USER_STATE[uid] = {"action": "delete_opt", "db_id": db_id}
        await query.edit_message_text("🗑️ اكتب الحرف (A–J) للاختيار الذي تريد حذفه:", reply_markup=back_kb())
        return
        
    if data.startswith("add_opt:"):
        db_id = int(data.split(":")[1])
        row = get_question_db(db_id)
        if row and (not row["options"] or len(row["options"]) < 10):
            USER_STATE[uid] = {"action": "add_opt", "db_id": db_id}
            await query.edit_message_text("✏️ أرسل نص الاختيار الجديد:", reply_markup=back_kb())
        else:
            await query.answer("لا يمكن إضافة المزيد من الاختيارات (الحد الأقصى 10)")
        return


    if data.startswith("set_correct:"):
        parts = data.split(":")
        db_id = int(parts[1])
        letter = parts[2].upper()
        update_question_db(db_id, correct=letter)
        await query.edit_message_text(f"✅ تم تعيين الإجابة الصحيحة: {letter}", reply_markup=main_menu_kb())
        return

    if data.startswith("publish:"):
        db_id = int(data.split(":")[1])
        # نشر السؤال الواحد
        success = await publish_one_db(query.message.chat_id, context, db_id)
        if success:
            await query.edit_message_text("✅ تم نشر السؤال بنجاح.")
        else:
            await query.edit_message_text("❌ حدث خطأ أثناء نشر السؤال.", reply_markup=main_menu_kb())
        return


    if data == "send_to_id":
        USER_STATE[uid] = {"action": "await_target_id"}
        await query.edit_message_text("📮 أرسل الآن الـ Chat ID للجروب أو القناة التي تريد إرسال الأسئلة إليها.\n\n📌 ملاحظة: تأكد أن البوت عضو في هذا الجروب أو القناة وله صلاحية إرسال الرسائل.", reply_markup=back_kb())
        return
    if data == "get_chat_id":
        chat = query.message.chat
        msg = (
            f"📍 *Chat Info:*\n"
            f"👤 Name: {chat.title or chat.first_name or '—'}\n"
            f"💬 Type: {chat.type}\n"
            f"🆔 ID: `{chat.id}`"
        )
        try:
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=main_menu_kb())
        except Exception as e:
            if "Message is not modified" in str(e):
                pass  # تجاهل الخطأ لو نفس الرسالة
            else:
                raise
        return
    # fallback

# ✅ يلتقط أي رسالة من قناة ويرسل الـ ID لصاحب البوت على الخاص
async def detect_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.channel_post:
        return

    chat = update.channel_post.chat

    msg = (
        f"📢 تم استقبال منشور من قناة:\n"
        f"📛 الاسم: {chat.title}\n"
        f"🆔 ID القناة: `{chat.id}`"
    )

    try:
        await context.bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")
        print(f"✅ تم إرسال ID القناة إليك على الخاص ({ADMIN_ID})")
    except Exception as e:
        print(f"⚠️ لم أستطع إرسال ID القناة إلى الخاص: {e}")

# removed stray top-level async send loop (leftover code)
# the publish/send logic is handled by `publish_one_db` and `publish_all_to_chat`

# ---------- أوامر ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً أيها المعواني — اختر إجراء:", reply_markup=main_menu_kb())

# ---------- التشغيل ----------
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("version", lambda u, c: u.message.reply_text("QuizBot Version: 1.0.2 (Railway Debug)")))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, detect_channel_post))


    print("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()