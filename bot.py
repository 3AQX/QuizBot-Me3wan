# bot.py — النسخة المحدثة: تعديل نفس الرسالة عند التنقل بين الأسئلة
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll, CallbackQuery
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- إعداد ----------
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "quizbot.db"
DOWNLOADS = "downloads"
os.makedirs(DOWNLOADS, exist_ok=True)

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

def get_question_db_by_index(idx: int):
    rows = get_pending_questions_db()
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
CHOICE_PATTERN = re.compile(r'([A-E])\s*[-\.\)]\s*(.*?)(?=(?:[A-E]\s*[-\.\)]|$))', re.I | re.S)

def split_choices_from_line(line: str):
    matches = list(CHOICE_PATTERN.finditer(line))
    if matches and len(matches) > 1:
        return [m.group(2).strip() for m in matches]
    return None

def clean_option_line(line: str) -> str:
    """
    يحذف بادئة (A- أو B. أو C) فقط إذا كانت بداية السطر.
    لا يمس أول حرف من الكلمات مثل 'Appendix'
    """
    line = line.strip()
    cleaned = re.sub(r'^[A-Ea-e]\s*[-\.\)]\s*', '', line)
    return cleaned

def clean_question_text(q: str) -> str:
    if not q:
        return q
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
                    for l in text.splitlines():
                        if l.strip():
                            lines.append(l.strip())
    except Exception:
        logger.exception("خطأ أثناء قراءة صفحات PDF")
    return lines

def parse_questions_from_file(file_path: str, pdf_pages: List[int] = None):
    ext = os.path.splitext(file_path)[1].lower()
    lines = []
    try:
        if ext in [".xlsx", ".xls"]:
            df = pd.read_excel(file_path, header=None)
            for row in df.values:
                line = " ".join([str(x) for x in row if str(x) != 'nan'])
                if line.strip():
                    lines.append(line.strip())
        elif ext in [".csv", ".txt"]:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [l.rstrip("\n") for l in f if l.strip()]
        elif ext == ".docx":
            doc = Document(file_path)
            for p in doc.paragraphs:
                if p.text.strip():
                    lines.append(p.text.strip())
        elif ext == ".pdf":
            if pdf_pages:
                lines = parse_pdf_pages(file_path, pdf_pages)
            else:
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            for l in text.splitlines():
                                if l.strip():
                                    lines.append(l.strip())
        else:
            return None
    except Exception:
        logger.exception("file read error")
        return None

    questions = []
    current_q = None
    for line in lines:
        if re.match(r'^\s*\d+\s*[\.\-\)\:]', line):
            if current_q:
                questions.append(current_q)
            qtxt = re.sub(r'^\s*\d+\s*[\.\-\)\:]\s*', '', line).strip()
            current_q = {"question": qtxt, "options": []}
        elif re.match(r'^\s*[A-Ea-e]\s*[\.\-\)]?', line):
            if current_q is None:
                continue
            multi = split_choices_from_line(line)
            if multi:
                for m in multi:
                    current_q["options"].append(clean_option_line(m))
            else:
                current_q["options"].append(clean_option_line(line))
        else:
            if current_q:
                current_q["question"] += " " + line.strip()
            else:
                continue

    if current_q:
        questions.append(current_q)

    final = []
    for q in questions:
        opts = [o.strip() for o in q.get("options", []) if o and o.strip()]
        final.append({"qtext": clean_question_text(q["question"]), "options": opts})
    return final

# ---------- حالة المستخدم ----------
USER_STATE = {}  # user_id -> dict(action, step, tmp, ...)

# ---------- أزرار الواجهة ----------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 تحميل ملف", callback_data="upload")],
        [InlineKeyboardButton("✍️ إضافة سؤال يدوي", callback_data="add_manual")],
        [InlineKeyboardButton("🧾 مراجعة الأسئلة", callback_data="review")],
        [InlineKeyboardButton("🅰️ (إدخال الإجابات (دفعة واحدة", callback_data="bulk_answers")],
        [InlineKeyboardButton("📤 نشر جميع الأسئلة هنا", callback_data="publish_all_here")],
        [InlineKeyboardButton("🗑️ حذف جميع الأسئلة", callback_data="delete_all")]
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="main")]])

# ---------- معالجة رفع الملفات ----------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ أرسل ملفاً صالحاً.", reply_markup=main_menu_kb())
        return
    file = await document.get_file()
    filename = document.file_name
    path = os.path.join(DOWNLOADS, filename)
    await file.download_to_drive(path)

    ext = os.path.splitext(filename)[1].lower()
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
    else:
        await process_file_and_insert(update, context, path, pdf_pages=None)

async def process_file_and_insert(update_or_query, context: ContextTypes.DEFAULT_TYPE, path: str, pdf_pages: List[int] = None):
    parsed = parse_questions_from_file(path, pdf_pages=pdf_pages)
    is_query = hasattr(update_or_query, "callback_query")
    if not parsed:
        if is_query:
            await update_or_query.edit_message_text("❌ لم يتم العثور على أسئلة في الملف.", reply_markup=main_menu_kb())
        else:
            await update_or_query.message.reply_text("❌ لم يتم العثور على أسئلة في الملف.", reply_markup=main_menu_kb())
        return
    inserted = 0
    for q in parsed:
        opts = q.get("options", []) or []
        if len(opts) == 1:
            opts.append("خيار فارغ")
        insert_question_db(q["qtext"], opts)
        inserted += 1
    if is_query:
        await update_or_query.edit_message_text(f"✅ تم استخراج وحفظ {inserted} سؤال.", reply_markup=main_menu_kb())
    else:
        await update_or_query.message.reply_text(f"✅ تم استخراج وحفظ {inserted} سؤال.", reply_markup=main_menu_kb())

# ---------- معالجة النص (state machine) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = (update.message.text or "").strip()
    state = USER_STATE.get(user_id)
    if not state:
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
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            joined = " ".join(lines)
            multi = split_choices_from_line(joined)
            if multi:
                opts = [clean_option_line(m) for m in multi]
            else:
                opts = [clean_option_line(l) for l in lines if l.strip()]
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
                except:
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

    # تعديل جميع الاختيارات دفعة واحدة
    if state.get("action") == "edit_all_opts":
        db_id = state.get("db_id")
        lines = [clean_option_line(l) for l in text.splitlines() if l.strip()]
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
        if not letter.isalpha() or not ('A' <= letter <= 'E'):
            await update.message.reply_text("❌ أدخل حرفًا صحيحًا من A إلى E.", reply_markup=back_kb())
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

async def show_goto_menu(query: CallbackQuery, start=0):
    rows = get_pending_questions_db()
    if not rows:
        await query.edit_message_text("لا توجد أسئلة.", reply_markup=main_menu_kb())
        return
    end = min(start + 10, len(rows))
    btns = []
    for i in range(start, end):
        btns.append([InlineKeyboardButton(f"{i+1}", callback_data=f"review:{i}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"goto_page:{max(0, start-10)}"))
    if end < len(rows):
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"goto_page:{start+10}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("↩️ رجوع", callback_data="review")])
    await query.edit_message_text("اختر رقم السؤال:", reply_markup=InlineKeyboardMarkup(btnns := btns))  # small py trick

async def show_review_question(query, context, idx=0):
    row = get_question_db_by_index(idx)
    if not row:
        await query.edit_message_text("لا يوجد سؤال بهذا الرقم.", reply_markup=main_menu_kb())
        return

    opts = row["options"]
    opts_text = "\n".join([f"{chr(65+i)}) {opt}" for i, opt in enumerate(opts)]) if opts else "(لا توجد اختيارات)"
    corr = row["correct"] if row["correct"] else "-"
    text = f"السؤال {idx+1}/{row['total']}:\n\n{row['qtext']}\n\n{opts_text}\n\nالإجابة الصحيحة: {corr}"

    buttons = []
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"review_idx:{idx-1}"))
    if idx + 1 < row["total"]:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"review_idx:{idx+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔢 الانتقال إلى سؤال معين", callback_data="goto_question")])

    buttons.append([
        InlineKeyboardButton("✏️ تعديل اختيار", callback_data=f"edit_one:{row['db_id']}"),
        InlineKeyboardButton("✏️ تعديل نص السؤال", callback_data=f"edit_text:{row['db_id']}")     
    ])

    buttons.append([
        InlineKeyboardButton("✏️ تعديل كل الاختيارات", callback_data=f"edit_all_opts:{row['db_id']}"),
        InlineKeyboardButton("🗑️ حذف اختيار", callback_data=f"delete_opt:{row['db_id']}")
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

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

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
async def publish_one_db(chat_id, context: ContextTypes.DEFAULT_TYPE, db_id: int):
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
    if correct_index is None:
        correct_index = 0
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

async def publish_all_to_chat(chat_id, context: ContextTypes.DEFAULT_TYPE):
    rows = get_pending_questions_db()
    total = len(rows)
    if total == 0:
        await context.bot.send_message(chat_id, "❌ لا توجد أسئلة لإرسالها.", reply_markup=main_menu_kb())
        return

    sent = 0
    for r in rows:
        ok = await publish_one_db(chat_id, context, r["db_id"])
        if ok:
            sent += 1

    remaining = pending_count_db()

    if remaining == 0:
        msg = f"✅ تم إرسال جميع الأسئلة ({sent}/{total}) بنجاح."
    else:
        msg = f"✅ تم إرسال {sent} من أصل {total} سؤال.\n📚 المتبقي: {remaining} سؤال لم يتم إرساله بعد."

    # إرسال رسالة النتيجة + الرجوع للقائمة الرئيسية
    await context.bot.send_message(chat_id, msg, reply_markup=main_menu_kb())

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
        await show_review_question(query, context, idx=0)
        return

    if data == "delete_all":
        delete_all_db()
        await query.edit_message_text("✅ تم حذف جميع الأسئلة من القاعدة.", reply_markup=main_menu_kb())
        return

    if data == "publish_all_here":
        await publish_all_to_chat(query.message.chat_id, context)
        await query.edit_message_text("✅ تم نشر كل الأسئلة هنا.", reply_markup=main_menu_kb())
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
        await query.edit_message_text("🗑️ اكتب الحرف (A–E) للاختيار الذي تريد حذفه:", reply_markup=back_kb())
        return


    if data.startswith("set_correct:"):
        parts = data.split(":")
        db_id = int(parts[1]); letter = parts[2].upper()
        update_question_db(db_id, correct=letter)
        await query.edit_message_text(f"✅ تم تعيين الإجابة الصحيحة: {letter}", reply_markup=main_menu_kb())
        return

    if data.startswith("publish:"):
        db_id = int(data.split(":")[1])
        await publish_one_db(query.message.chat_id, context, db_id)
        await query.edit_message_text("✅ تم نشر السؤال هنا.", reply_markup=main_menu_kb())
        return
    if data == "goto_question":
        await show_goto_menu(query)
        return

    if data.startswith("goto_page:"):
        start = int(data.split(":")[1])
        await show_goto_menu(query, start=start)
        return


    # fallback
    await query.edit_message_text("تم الضغط على زر غير معروف أو انتهت صلاحية الرسالة. ارجع إلى القائمة.", reply_markup=main_menu_kb())

# ---------- أوامر ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً أيها المعواني — اختر إجراء:", reply_markup=main_menu_kb())

# ---------- التشغيل ----------
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
