# bot.py
import os
import re
import json
import logging
import sqlite3
from io import BytesIO

import pandas as pd
import pdfplumber
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
TOKEN = "6467703195:AAFo7I8cSpI2swiIlI8iYxB8gwjP08kh4mM"  # <-- ضع توكن البوت هنا
DB_PATH = "quizbot.db"
DOWNLOADS = "downloads"
os.makedirs(DOWNLOADS, exist_ok=True)

# ---------------- DB (SQLite) ----------------
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

def insert_question_db(qtext, options, correct=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO questions (qtext, options_json, correct_letter) VALUES (?, ?, ?)",
              (qtext, json.dumps(options, ensure_ascii=False), (correct.upper() if correct else None)))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return rid

def get_pending_questions_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, qtext, options_json, correct_letter FROM questions WHERE status='pending' ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return [{"db_id": r[0], "qtext": r[1], "options": json.loads(r[2]), "correct": r[3]} for r in rows]

def get_question_db_by_index(idx):
    rows = get_pending_questions_db()
    if 0 <= idx < len(rows):
        row = rows[idx]
        row["index"] = idx
        row["total"] = len(rows)
        return row
    return None

def update_question_db(db_id, qtext=None, options=None, correct=None):
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

def delete_question_db(db_id):
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

def mark_published_db(db_id):
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

# ---------------- Parsing helpers ----------------
CHOICE_PATTERN = re.compile(r'([A-E])\s*[-\.\)]\s*(.*?)(?=(?:[A-E]\s*[-\.\)]|$))', re.I | re.S)

def split_choices_from_line(line):
    matches = list(CHOICE_PATTERN.finditer(line))
    if matches and len(matches) > 1:
        opts = []
        for m in matches:
            text = m.group(2).strip()
            opts.append(text)
        return opts
    return None

def clean_option_line(line):
    m = re.match(r'^\s*[A-Ea-e]\s*[-\.\)]?\s*(.*)', line)
    if m:
        return m.group(1).strip()
    return line.strip()

def clean_question_text(q):
    if not q:
        return q
    q = re.sub(r'\bANSWERS\b', ' ', q, flags=re.I)
    q = re.sub(r'\bHernia PE\b', ' ', q, flags=re.I)
    q = re.sub(r'(\b\d{1,3}[\s\-\:]?){4,}', ' ', q)
    q = re.sub(r'\b\d{2,}\b', ' ', q)
    q = re.sub(r'\b([A-E]{2,})\b', ' ', q)
    q = re.sub(r'[_\*\=\~]{2,}', ' ', q)
    q = re.sub(r'\s{2,}', ' ', q).strip()
    return q

# ---------------- File parsing ----------------
def parse_questions_from_file(file_path):
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
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = [l.rstrip("\n") for l in f if l.strip()]
            except UnicodeDecodeError:
                with open(file_path, "r", encoding="latin1") as f:
                    lines = [l.rstrip("\n") for l in f if l.strip()]
        elif ext == ".docx":
            doc = Document(file_path)
            for p in doc.paragraphs:
                if p.text.strip():
                    lines.append(p.text.strip())
        elif ext == ".pdf":
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        for l in text.splitlines():
                            if l.strip():
                                lines.append(l.strip())
        else:
            return None
    except Exception as e:
        logger.exception("file read error")
        return None

    questions = []
    current_q = None

    for line in lines:
        if re.match(r'^\s*\d+\s*[\.\-\)\:]', line):
            if current_q:
                processed = []
                for opt in current_q["options"]:
                    multi = split_choices_from_line(opt)
                    if multi:
                        processed.extend(multi)
                    else:
                        processed.append(clean_option_line(opt))
                current_q["options"] = [o for o in processed if o]
                current_q["question"] = clean_question_text(current_q["question"])
                questions.append(current_q)
            qtxt = re.sub(r'^\s*\d+\s*[\.\-\)\:]\s*', '', line).strip()
            current_q = {"question": qtxt, "options": []}
        elif re.match(r'^\s*[A-Ea-e]\s*[\.\-\)]?', line):
            if current_q is None:
                continue
            multi = split_choices_from_line(line)
            if multi:
                for m in multi:
                    current_q["options"].append(m)
            else:
                current_q["options"].append(clean_option_line(line))
        else:
            if current_q:
                current_q["question"] += " " + line.strip()
            else:
                continue

    if current_q:
        processed = []
        for opt in current_q["options"]:
            multi = split_choices_from_line(opt)
            if multi:
                processed.extend(multi)
            else:
                processed.append(clean_option_line(opt))
        current_q["options"] = [o for o in processed if o]
        current_q["question"] = clean_question_text(current_q["question"])
        questions.append(current_q)

    final = []
    for q in questions:
        final.append({"qtext": q["question"].strip(), "options": [o.strip() for o in q.get("options", []) if o and o.strip()]})
    return final

# ---------------- User state ----------------
USER_STATE = {}  # user_id -> dict(action, step, tmp, etc.)

# ---------------- UI ----------------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 تحميل ملف", callback_data="upload")],
        [InlineKeyboardButton("✍️ إضافة سؤال يدوي", callback_data="add_manual")],
        [InlineKeyboardButton("🅰️ إضافة الإجابات (دفعة واحدة)", callback_data="bulk_answers")],
        [InlineKeyboardButton("🧾 مراجعة الأسئلة", callback_data="review")],
        [InlineKeyboardButton("🗑️ حذف سؤال", callback_data="del_menu")],
        [InlineKeyboardButton("🚀 إرسال الامتحان (Quiz + Anonymous)", callback_data="publish_menu")],
        [InlineKeyboardButton("🗑️ حذف جميع الأسئلة", callback_data="delete_all")]
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="main")]])

# ---------------- Handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("اختر إجراء:", reply_markup=main_menu_kb())

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    # UPLOAD
    if data == "upload":
        USER_STATE[uid] = {"action": "await_file"}
        await query.edit_message_text("📂 ابعت الملف الآن (docx/pdf/txt/csv/xlsx).", reply_markup=back_kb())
        return

    # ADD MANUAL
    if data == "add_manual":
        USER_STATE[uid] = {"action": "manual", "step": 1, "tmp": {}}
        await query.edit_message_text("✏️ إضافة سؤال يدوي — اكتب نص السؤال الآن.", reply_markup=back_kb())
        return

    # BULK ANSWERS
    if data == "bulk_answers":
        USER_STATE[uid] = {"action": "await_bulk_answers"}
        await query.edit_message_text("✳️ ابعت سلسلة الحروف بالترتيب (مثال: `B A D C` أو `BAD C A`).\nاكتب '-' لسؤال بدون إجابة.", reply_markup=back_kb())
        return

    # REVIEW
    if data == "review":
        if pending_count_db() == 0:
            await query.edit_message_text("لا توجد أسئلة محفوظة حالياً.", reply_markup=main_menu_kb())
            return
        await show_review_question(query, context, idx=0)
        return

    # DELETE MENU
    if data == "del_menu":
        rows = get_pending_questions_db()
        if not rows:
            await query.edit_message_text("لا توجد أسئلة للحذف.", reply_markup=main_menu_kb())
            return
        await show_delete_list(query, context, start=0)
        return

    # PUBLISH MENU
    if data == "publish_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 نشر كل الأسئلة هنا (Quiz, Anonymous)", callback_data="publish_all_here")],
            [InlineKeyboardButton("📤 نشر إلى chat_id آخر", callback_data="publish_enter_id")],
            [InlineKeyboardButton("↩️ رجوع", callback_data="main")]
        ])
        await query.edit_message_text("اختر طريقة النشر:", reply_markup=kb)
        return

    if data == "publish_all_here":
        chat_id = query.message.chat_id
        await publish_all_to_chat(chat_id, context)
        await query.edit_message_text("✅ تم نشر كل الأسئلة هنا.", reply_markup=main_menu_kb())
        return

    if data == "publish_enter_id":
        USER_STATE[uid] = {"action": "enter_chat_id_publish"}
        await query.edit_message_text("📤 ابعت chat_id (رقم) حيث تريد أن أرسل الأسئلة إليه. مثال: -1001234567890", reply_markup=back_kb())
        return

    # DELETE ALL
    if data == "delete_all":
        delete_all_db()
        await query.edit_message_text("✅ تم حذف جميع الأسئلة من القاعدة.", reply_markup=main_menu_kb())
        return

    if data == "main":
        await query.edit_message_text("القائمة الرئيسية:", reply_markup=main_menu_kb())
        return

    # Review navigation and actions
    if data.startswith("review_idx:"):
        idx = int(data.split(":")[1])
        await show_review_question(query, context, idx=idx)
        return

    if data.startswith("edit_q:"):
        db_id = int(data.split(":")[1])
        USER_STATE[uid] = {"action": "edit_q_text", "db_id": db_id}
        await query.edit_message_text("✏️ ابعت النص الجديد للسؤال الآن.", reply_markup=back_kb())
        return

    if data.startswith("edit_opts:"):
        db_id = int(data.split(":")[1])
        USER_STATE[uid] = {"action": "edit_q_opts", "db_id": db_id}
        await query.edit_message_text("✏️ ابعت الاختيارات الجديدة — كل اختيار في سطر واحد أو بصيغة A-.. B-..", reply_markup=back_kb())
        return

    if data.startswith("set_correct:"):
        parts = data.split(":")
        db_id = int(parts[1]); letter = parts[2].upper()
        update_question_db(db_id, correct=letter)
        await query.edit_message_text(f"✅ تم تعيين الإجابة الصحيحة: {letter}", reply_markup=main_menu_kb())
        return

    if data.startswith("del_db:"):
        db_id = int(data.split(":")[1])
        delete_question_db(db_id)
        await query.edit_message_text("🗑️ تم حذف السؤال.", reply_markup=main_menu_kb())
        return

    # delete list pagination
    if data.startswith("del_page:"):
        start = int(data.split(":")[1])
        await show_delete_list(query, context, start=start)
        return

    # publish single question
    if data.startswith("publish_q:"):
        db_id = int(data.split(":")[1])
        await publish_one_db(query, context, db_id=db_id)
        return

# delete list view
async def show_delete_list(query, context, start=0, page_size=10):
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

# review single question
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

    buttons.append([InlineKeyboardButton("✏️ تعديل نص", callback_data=f"edit_q:{row['db_id']}"),
                    InlineKeyboardButton("✏️ تعديل اختيارات", callback_data=f"edit_opts:{row['db_id']}")])

    # correct letter buttons
    if opts:
        setrow = []
        for i in range(len(opts)):
            letter = chr(65+i)
            setrow.append(InlineKeyboardButton(letter, callback_data=f"set_correct:{row['db_id']}:{letter}"))
        buttons.append(setrow)

    buttons.append([InlineKeyboardButton("📤 نشر سؤال", callback_data=f"publish_q:{row['db_id']}"),
                    InlineKeyboardButton("🗑 حذف", callback_data=f"del_db:{row['db_id']}")])
    buttons.append([InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

# publish helpers
async def publish_all_to_chat(chat_id, context: ContextTypes.DEFAULT_TYPE):
    rows = get_pending_questions_db()
    for r in rows:
        opts = r["options"]
        correct_index = None
        if r["correct"]:
            letter = r["correct"].upper()
            idx = ord(letter) - ord('A')
            if 0 <= idx < len(opts):
                correct_index = idx
        await context.bot.send_poll(
            chat_id=chat_id,
            question=r["qtext"],
            options=opts if opts else ["No options"],
            type=Poll.QUIZ if correct_index is not None else Poll.REGULAR,
            correct_option_id=correct_index if correct_index is not None else 0,
            is_anonymous=True
        )
        mark_published_db(r["db_id"])

async def publish_one_db(query, context: ContextTypes.DEFAULT_TYPE, db_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT qtext, options_json, correct_letter FROM questions WHERE id=?", (db_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await query.edit_message_text("❌ السؤال غير موجود.", reply_markup=main_menu_kb())
        return
    qtext, opts_json, correct = row[0], json.loads(row[1]), row[2]
    correct_index = None
    if correct:
        idx = ord(correct.upper()) - ord('A')
        if 0 <= idx < len(opts_json):
            correct_index = idx
    await context.bot.send_poll(
        chat_id=query.message.chat_id,
        question=qtext,
        options=opts_json if opts_json else ["No options"],
        type=Poll.QUIZ if correct_index is not None else Poll.REGULAR,
        correct_option_id=correct_index if correct_index is not None else 0,
        is_anonymous=True
    )
    mark_published_db(db_id)
    await query.edit_message_text("✅ تم نشر السؤال هنا.", reply_markup=main_menu_kb())

# ---------------- Handlers for files & text flows ----------------
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
    await update.message.reply_text("📂 جاري استخراج الأسئلة من الملف ...", reply_markup=back_kb())

    parsed = parse_questions_from_file(path)
    if parsed is None:
        await update.message.reply_text("❌ فشل في قراءة الملف. تأكد من الصيغة (docx/pdf/txt/csv/xlsx).", reply_markup=main_menu_kb())
        USER_STATE.pop(user_id, None)
        return

    inserted = 0
    for q in parsed:
        opts = q.get("options", []) or []
        # إذا خيار واحد فقط → أضف خيار وهمي
        if len(opts) == 1:
            opts.append("خيار فارغ")
        insert_question_db(q["qtext"], opts)
        inserted += 1

    USER_STATE.pop(user_id, None)
    await update.message.reply_text(f"✅ تم استخراج وحفظ {inserted} سؤال في القاعدة.", reply_markup=main_menu_kb())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    state = USER_STATE.get(user_id)
    if not state:
        return

    # manual add flow
    if state.get("action") == "manual":
        step = state.get("step", 1)
        tmp = state.get("tmp", {})
        if step == 1:
            tmp["question"] = text
            USER_STATE[user_id] = {"action": "manual", "step": 2, "tmp": tmp}
            await update.message.reply_text("✏️ الآن أرسل الاختيارات — كل اختيار في سطر واحد، أو ارسلهم مرة واحدة بصيغة A-.. B-..", reply_markup=back_kb())
            return
        elif step == 2:
            lines = text.splitlines()
            joined = " ".join(lines)
            multi = split_choices_from_line(joined)
            if multi:
                opts = multi
            else:
                opts = [clean_option_line(l) for l in lines if l.strip()]
            tmp["options"] = opts
            USER_STATE[user_id] = {"action": "manual", "step": 3, "tmp": tmp}
            await update.message.reply_text("✅ اكتب رقم الإجابة الصحيحة (1= A, 2= B, ...) أو اكتب '-' إذا لا توجد إجابة صحيحة.", reply_markup=back_kb())
            return
        elif step == 3:
            if text.strip() == "-":
                correct = None
            else:
                try:
                    idx = int(text.strip()) - 1
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

    # bulk answers flow
    if state.get("action") == "await_bulk_answers":
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
        applied = 0; skipped = 0
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
        await update.message.reply_text(f"✅ تم تطبيق الإجابات. مُطبق: {applied}, مُهمل/بدون إجابة: {skipped}", reply_markup=main_menu_kb())
        return

    # enter chat id for publish
    if state.get("action") == "enter_chat_id_publish":
        try:
            chat_id = int(text.strip())
        except:
            await update.message.reply_text("❌ chat_id غير صالح. أرسله كرقم مثل -1001234567890.", reply_markup=main_menu_kb())
            return
        USER_STATE.pop(user_id, None)
        await publish_all_to_chat(chat_id, context)
        await update.message.reply_text(f"✅ تم النشر إلى chat_id: {chat_id}", reply_markup=main_menu_kb())
        return

    # edit text
    if state.get("action") == "edit_q_text":
        db_id = state.get("db_id")
        update_question_db(db_id, qtext=text)
        USER_STATE.pop(user_id, None)
        await update.message.reply_text("✅ تم تحديث نص السؤال.", reply_markup=main_menu_kb())
        return

    # edit options
    if state.get("action") == "edit_q_opts":
        db_id = state.get("db_id")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        joined = " ".join(lines)
        multi = split_choices_from_line(joined)
        if multi:
            opts = multi
        else:
            opts = [clean_option_line(l) for l in lines]
        if len(opts) == 1:
            opts.append("خيار فارغ")
        update_question_db(db_id, options=opts)
        USER_STATE.pop(user_id, None)
        await update.message.reply_text("✅ تم تحديث الاختيارات.", reply_markup=main_menu_kb())
        return

# ---------------- Startup ----------------
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(button_router, pattern=r"^del_page:"))  # pagination reuse
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot started.")
    app.run_polling()

from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()


if __name__ == "__main__":
    keep_alive()
    main()