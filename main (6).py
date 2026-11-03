import os
import sqlite3
import json
import re
import html
import uuid
import logging
import random
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, 
    KeyboardButton, KeyboardButtonPollType, ReplyKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent  
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler, PollAnswerHandler,
    InlineQueryHandler  
)
from telegram.error import NetworkError
from telegram.request import HTTPXRequest
from google import genai
from html import escape as html_escape

TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')
# Enable Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 🇮🇳 India Standard Time (IST) Timezone
IST = timezone(timedelta(hours=5, minutes=30))

# ... आपके कोड की शुरुआती इम्पोर्ट लाइन्स और load_dotenv() ऊपर रहेगा ...
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None

# 🔥 FIXED: .env se SUPPORT_GROUP_ID load karne ke liye ye line jodi hai
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID")) if os.getenv("SUPPORT_GROUP_ID") else None
# 📢 NEW: .env se SUPPORT_CHANNEL_ID load karne ke liye ye line jodi hai
SUPPORT_CHANNEL_ID = int(os.getenv("SUPPORT_CHANNEL_ID")) if os.getenv("SUPPORT_CHANNEL_ID") else None
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ====================================================================
# 🔒 NEW: ALLOWED USERS PARSING & SECURITY LOGIC
# ====================================================================
def get_allowed_ids():
    """Load and safely clean ALLOWED_USER_IDS from .env string to a list of integers"""
    raw_str = os.environ.get("ALLOWED_USER_IDS", "")
    # कोट्स या अनचाहे स्पेस को साफ़ करें
    clean_str = raw_str.replace('"', '').replace("'", "").strip()
    if not clean_str:
        return []
    
    allowed_list = []
    for x in clean_str.split(","):
        x_clean = x.strip()
        if x_clean.isdigit():
            allowed_list.append(int(x_clean))
        elif x_clean.startswith("-") and x_clean[1:].isdigit(): # नेगेटिव आईडी सपोर्ट
            allowed_list.append(int(x_clean))
            
    return allowed_list

def is_authorized(update: Update):
    """Check if the user is either the Owner or present in Allowed Users list (Safe for Messages & Buttons)"""
    # 🟢 FIXED: Check both message and callback_query safely
    if update.message:
        user_id = update.message.from_user.id
        chat_type = update.message.chat.type
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_type = update.callback_query.message.chat.type
    else:
        return False # Unknown update structure
        
    allowed_ids = get_allowed_ids()
    
    # अगर ग्रुप चैट है, तो ही सुरक्षा प्रतिबंध लागू होंगे
    if chat_type in ["group", "supergroup"]:
        if user_id != OWNER_ID and user_id not in allowed_ids:
            return False
    return True
# ====================================================================

# Initialize Gemini Client if Key exists
ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ... इसके नीचे आपका बाकी का पुराना कोड चलता रहेगा (DB_FILE = "quiz_bot.db" आदि) ...

DB_FILE = "quiz_bot.db"

# Global dictionary for active group games memory
GROUP_GAMES = {}

# In-memory map for autorun asyncio tasks: key = autorun_id, value = asyncio.Task
AUTORUN_TASKS = {}
AUTORUN_LIST_TASK = None
POLLING_LOOP_ACTIVE = True
# Global autorun serial lock — ensures autoruns run one-by-one in SUPPORT_GROUP_ID
AUTORUN_SERIAL_LOCK = asyncio.Lock()
# ====================================================================
# 🔥 FULLY OPERATIONAL GLOBAL CONVERSATION STATES (AUTOMATIC NO-OVERLAP SEQUENCE)
# ====================================================================
# New quiz build flow states (0 to 5)
(TOPIC, Q_COUNT, TITLE, DESCRIPTION, LANGUAGE,
 EXPLANATION, DIFFICULTY, OPTIONS_COUNT, TIME_LIMIT, NEGATIVE,
 QUESTIONS, PRE_MESSAGE, TIMER,
 EDIT_TITLE, EDIT_DESC, EDIT_TIMER, EDIT_NEGATIVE,
 EDIT_QUESTION_TEXT, EDIT_QUESTION_OPTIONS, EDIT_QUESTION_CORRECT, EDIT_QUESTION_EXPLANATION, EDIT_QUESTION_PRE_MESSAGE) = range(22)

def escape_markdown(text):
    """Escape special characters for Telegram Markdown"""
    if not text:
        return text
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_time(seconds):
    """Convert seconds to min:sec format (e.g., 1m 45s)"""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}m {secs}s"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                title TEXT,
                description TEXT,
                timer INTEGER DEFAULT 30,
                negative_value REAL DEFAULT 0.0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER,
                question_text TEXT,
                options TEXT,
                correct_answer INTEGER DEFAULT 0,
                explanation TEXT,
                pre_message TEXT,
                FOREIGN KEY(quiz_id) REFERENCES quizzes(quiz_id)
            )
        """)
        cursor.execute("CREATE TABLE IF NOT EXISTS broadcast_users (chat_id INTEGER PRIMARY KEY)")
        cursor.execute("CREATE TABLE IF NOT EXISTS broadcast_groups (chat_id INTEGER PRIMARY KEY)")

        # Create autoruns with schedule_time present
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS autoruns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL,
                interval_minutes INTEGER NOT NULL,
                schedule_time TEXT,
                next_run TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS autorun_custom_topic (
                key TEXT PRIMARY KEY,
                topic_name TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO autorun_custom_topic (key, topic_name) VALUES ('active_topic', '')")
        
        # 🆕 Tracking table for global continuous sequential polling
        cursor.execute("CREATE TABLE IF NOT EXISTS polling_state (key TEXT PRIMARY KEY, last_question_id INTEGER DEFAULT 0)")
        cursor.execute("INSERT OR IGNORE INTO polling_state (key, last_question_id) VALUES ('global_loop', 0)")
        conn.commit()

        # Ensure older DBs get the schedule_time column if missing
        cursor.execute("PRAGMA table_info('autoruns')")
        cols = [row[1] for row in cursor.fetchall()]
        if "schedule_time" not in cols:
            try:
                cursor.execute("ALTER TABLE autoruns ADD COLUMN schedule_time TEXT")
                conn.commit()
                logging.info("Added missing column 'schedule_time' to autoruns table")
            except Exception as ae:
                logging.warning(f"Could not add schedule_time column: {ae}")

        conn.close()
        logging.info("Database initialized successfully with correct_answer as INTEGER")
    except Exception as e:
        logging.error(f"Error initializing database: {e}")


def migrate_fix_correct_answer():
    """🟢 Fix existing questions: convert correct_answer text to index"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Get all questions with text-based correct_answer
        cursor.execute("SELECT id, options, correct_answer FROM questions")
        rows = cursor.fetchall()
        
        fixed_count = 0
        for q_id, options_json, correct_ans in rows:
            try:
                options = json.loads(options_json)
                
                # अगर correct_ans पहले से ही number है, तो skip करो
                try:
                    idx = int(correct_ans)
                    if 0 <= idx < len(options):
                        continue
                except (ValueError, TypeError):
                    pass
                
                # Text को index में convert करो
                try:
                    correct_idx = options.index(str(correct_ans))
                except ValueError:
                    # अगर exact match नहीं मिला, तो fuzzy search करो
                    correct_idx = next(
                        (i for i, opt in enumerate(options) 
                         if opt.strip().lower() == str(correct_ans).strip().lower()), 
                        0
                    )
                    logging.warning(f"Q{q_id}: Fuzzy matched '{correct_ans}' to index {correct_idx}")
                
                # Database को update करो
                cursor.execute("UPDATE questions SET correct_answer = ? WHERE id = ?", 
                             (correct_idx, q_id))
                fixed_count += 1
                
            except Exception as e:
                logging.error(f"Error fixing Q{q_id}: {e}")
        
        conn.commit()
        conn.close()
        if fixed_count > 0:
            logging.info(f"✅ Migration complete: Fixed {fixed_count} questions")
        
    except Exception as e:
        logging.error(f"❌ Migration failed: {e}")
        
def check_active_quiz_creation(user_id, context):
    """Check if user has an active quiz creation in progress"""
    return "quiz_build" in context.user_data and context.user_data["quiz_build"].get("title")
    
# --- CONVERSATION STATES ---
(TOPIC, BUTTON_TOPIC, Q_COUNT, TITLE, DESCRIPTION, LANGUAGE, 
 EXPLANATION, DIFFICULTY, OPTIONS_COUNT, TIME_LIMIT, NEGATIVE) = range(11)

# AI Question Generator helper
def generate_bulk_questions_ai(topic, count, lang, difficulty, options_cnt):
    if not ai_client:
        logging.warning("⚠️ AI CLIENT NOT INITIALIZED - Returning None")
        logging.warning(f"GEMINI_API_KEY present: {bool(GEMINI_API_KEY)}")
        return None
    
    # 🇮🇳 Dynamic Date-Time Handler (IST Timezone Base)
    # Isse har request me current month aur year dynamically prompt me jayega
    now_ist = datetime.now(tz=IST)
    current_date_str = now_ist.strftime("%B %Y")  # Output Example: "October 2026"
    current_year = now_ist.year
    
    # 🔎 EXAM DETECTION LOGIC: Check if topic contains popular exam keywords
    exam_keywords = ["ssc", "gd", "chsl", "cgl", "mts", "railway", "ntpc", "group d", "banking", "ibps", "upsc", "police", "pet"]
    is_exam_detected = any(keyword in topic.lower() for keyword in exam_keywords)
    
    # 🎯 DYNAMIC PROMPT EXTRACTION WITH CURRENT TIME CONTEXT
    if is_exam_detected:
        exam_instruction = f"""
CRITICAL CONTEXT FOR COMPETITIVE EXAMS (PYQs):
The user has specified an exam-related topic: "{topic}". 
You MUST strictly generate ONLY actual Previous Year Questions (PYQs) or highly accurate mock questions that perfectly match the official syllabus, standard, and weightage of that specific exam ({topic}). 
Do NOT generate generic questions. The questions must mimic the exact language, format, and difficulty level seen in real past papers of this exam. 
If the exam has a current affairs section, ensure the data is verified up to the current date: {current_date_str}.
"""
    else:
        exam_instruction = f"""
CRITICAL CONTEXT FOR CURRENT AFFAIRS & GENERAL KNOWLEDGE:
If the topic is "Current Affairs", "General Knowledge", "News", or related to recent events, you MUST focus on the latest national and international events, awards, sports, appointments, and developments up to the current date: {current_date_str} (Year {current_year}). 
Ensure all facts are updated, highly accurate, non-speculative, and reflect the real-world status as of {current_date_str}. Do NOT generate outdated historical current affairs.
"""

    # 📝 MAIN SYSTEM PROMPT (Embedded with dynamic date)
    prompt = f"""Generate exactly {count} unique quiz questions ONLY in {lang} language about "{topic}".
Current Evaluation Date Context: {current_date_str}

{exam_instruction}

Topic: {topic}
Language: {lang}
Difficulty: {difficulty}
Options per question: {options_cnt}

Return ONLY valid JSON array (no markdown, no extra text):
[
  {{
    "question": "What is..?", 
    "options": ["A", "B", "C", "D"], 
    "correct": 2, 
    "explanation": "Short explanation here"
  }}
]

CRITICAL RULES:
1. "correct" MUST be a 0-based index (0, 1, 2, 3, etc)
2. "correct" values should be RANDOMLY placed - use different positions for EACH question
3. Do NOT put correct answer always at position 0 or position 1
4. For {options_cnt} options: "correct" can be ANY value from 0 to {options_cnt-1}
5. Each question must have correct answer at DIFFERENT random position
6. Example: Q1 correct=1, Q2 correct=3, Q3 correct=0, Q4 correct=2 (varied!)
7. All options must be DIFFERENT and meaningful
8. Questions must strictly relate to {topic}
9. NO sample/dummy questions
10. Each item MUST include an "explanation" string (one or two sentences) explaining the correct answer
11. Return ONLY JSON array"""

    max_retries = 3
    retry_delay = 2  # सेकंड

    for attempt in range(max_retries):
        try:
            logging.info(f"🤖 Requesting AI for {count} questions on {topic} (Time Context: {current_date_str})...")
            
            response = ai_client.interactions.create(
                model='gemini-3.5-flash-lite',
                input=prompt,
            )
            
            if not response.output_text:
                logging.warning("⚠️ AI returned empty output_text")
                return None

            logging.info(f"📝 Response received: {response.output_text[:100]}...")
            clean_text = response.output_text.strip()
            
            # Clean markdown
            clean_text = clean_text.replace("```json", "").replace("```", "").strip()
            
            # Advanced Regex cleaning to extract only JSON array
            match = re.search(r'\[.*\]', clean_text, re.DOTALL)
            if match:
                clean_text = match.group(0)
                
            questions = json.loads(clean_text)
            break  # सफलता मिलने पर रीतराई लूप से बाहर निकलें

        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                if attempt < max_retries - 1:
                    logging.warning(f"🔄 AI बिजी है (503)। {retry_delay} सेकंड में दोबारा प्रयास कर रहे हैं...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # एक्सपोनेंशियल बैकऑफ़
                    continue
            
            logging.error(f"❌ AI Generation Error: {e}", exc_info=True)
            return None
            
    # Validation Loop
    valid_questions = []
    for idx, q in enumerate(questions):
        try:
            if not q.get("question") or not q.get("options"):
                logging.warning(f"Skipping invalid question: {q}")
                continue
            
            correct_idx = q.get("correct", 0)
            
            if not isinstance(correct_idx, int):
                try:
                    correct_idx = int(correct_idx)
                except (ValueError, TypeError):
                    correct_idx = 0
            
            if correct_idx < 0 or correct_idx >= len(q["options"]):
                logging.warning(f"Q{idx}: Invalid index {correct_idx}, using 0")
                correct_idx = 0
            
            q["correct"] = correct_idx
            
            if "explanation" not in q or not q["explanation"]:
                q["explanation"] = f"The correct answer is option {correct_idx + 1}."
                
            valid_questions.append(q)
            logging.info(f"✅ Q{len(valid_questions)}: '{q['question'][:40]}...' | Correct at index {correct_idx}")
            
        except Exception as q_err:
            logging.warning(f"Question parse error: {q_err}")
            continue
    
    min_required = min(10, count)
    
    if len(valid_questions) >= min_required:
        logging.info(f"✅ Minimum threshold met. Proceeding with {len(valid_questions)} questions.")
        return valid_questions[:count]
    else:
        logging.warning(f"⚠️ Only {len(valid_questions)} questions received. Less than minimum {min_required}. Cancelling.")
        return None
        
# --- BOT ROUTINES & HANDLERS ---
async def autoquiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    msg_obj = query.message if query else update.message
    user_id = query.from_user.id if query else update.message.from_user.id
    chat_id = msg_obj.chat_id
    chat_type = msg_obj.chat.type
    
    allowed_ids = get_allowed_ids()
    
    if chat_type in ["group", "supergroup"]:
        if SUPPORT_GROUP_ID and chat_id != SUPPORT_GROUP_ID:
            await msg_obj.reply_text("❌ <b>Security Error:</b> Yah command is group me allowed nahi hai.", parse_mode="HTML")
            return ConversationHandler.END
            
    if user_id != OWNER_ID and user_id not in allowed_ids:
        await msg_obj.reply_text("❌ <b>Sorry!</b> Yah command keval authorized users hi use kar sakte hain.", parse_mode="HTML")
        return ConversationHandler.END

    if query:
        await query.answer()

    context.user_data.clear()
    
    # 🌟 Raw dict structure to inject button colors (Blue & Green)
    mode_keyboard = InlineKeyboardMarkup([
        [
            {"text": "✍️ Text Mode (Type Topic)", "callback_data": "mode_text_input", "style": "primary"},
            {"text": "📂 Subject Mode (Buttons)", "callback_data": "mode_button_select", "style": "success"}
        ]
    ])
    
    welcome_text = (
        "<blockquote>🤖 <b>Welcome to AI Auto-Quiz Generator!</b></blockquote>\n\n"
        "<blockquote>🎯 <b>Choose Topic Input Mode:</b></blockquote>\n"
        "Aap quiz ka topic kis tarah se set karna chahte hain?\n\n"
        "📝 <b>Text Mode:</b> Aap chat me khud apna topic manually type karke bhejenge.\n"
        "🗂️ <b>Subject Mode:</b> Bot की pre-defined subject list (History, GK आदि) में से चुनेंगे।"
    )
    
    if query:
        await query.edit_message_text(text=welcome_text, reply_markup=mode_keyboard, parse_mode="HTML")
    else:
        await msg_obj.reply_text(text=welcome_text, reply_markup=mode_keyboard, parse_mode="HTML")
        
    return TOPIC

async def handle_topic_mode_routing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return TOPIC

    await query.answer()
    choice = query.data

    if choice == "mode_text_input":
        await query.edit_message_text(
            text="✍️ <b>Please type your Quiz Topic in the chat & send it:</b>\n\n<i>Example: Ancient Indian History, General Science Quiz etc.</i>",
            reply_markup=None,
            parse_mode="HTML"
        )
        return TOPIC

    elif choice == "mode_button_select" or choice == "back_to_subjects_nav":
        # 🌟 Injecting color to Subject Selection Buttons
        main_subject_keyboard = InlineKeyboardMarkup([
            [{"text": "📰 Current Affairs & GK 📰", "callback_data": "sub_current_gk", "style": "primary"}],
            [{"text": "📜 History (इतिहास)", "callback_data": "sub_history", "style": "primary"},
             {"text": "✨ Polity (राजव्यवस्था)", "callback_data": "sub_polity", "style": "primary"}],
            [{"text": "🌍 Geography (भूगोल)", "callback_data": "sub_geography", "style": "primary"},
             {"text": "🧬 General Science (विज्ञान)", "callback_data": "sub_science", "style": "primary"}],
            [{"text": "🤖 Languages & Grammar", "callback_data": "sub_languages", "style": "primary"}]
        ])
        
        await query.edit_message_text(
            text="<blockquote>🤖 <b>Welcome to AI Auto-Quiz Generator!</b></blockquote>\n\n<blockquote>📚 <b>Step 1 — Select Subject:</b></blockquote>\nNiche diye gaye buttons me se apna main <b>Subject</b> chunein, jiske baad uske specific topics open honge.",
            reply_markup=main_subject_keyboard,
            parse_mode="HTML"
        )
        return BUTTON_TOPIC
        
    return TOPIC

async def handle_subject_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles subject button clicks and updates keyboard with specific topics"""
    query = update.callback_query
    await query.answer()
    
    selected_sub = query.data
    topics_keyboard = []
    sub_title = ""

    if selected_sub == "sub_current_gk":
        sub_title = "📰 Current Affairs & GK"
        topics_keyboard = [
            [InlineKeyboardButton("📰 Current Affairs 2026", callback_data="set_topic_Current Affairs 2026")],
            [InlineKeyboardButton("🌐 International News", callback_data="set_topic_International Current Affairs")],
            [InlineKeyboardButton("🏆 Sports & Awards", callback_data="set_topic_Sports and Awards 2026")],
            [InlineKeyboardButton("💡 Static GK", callback_data="set_topic_Static General Knowledge")]
        ]
    elif selected_sub == "sub_history":
        sub_title = "History (इतिहास)"
        topics_keyboard = [
            # 🏺 प्राचीन भारत (Ancient India)
            [InlineKeyboardButton("🏺 सिंधु घाटी सभ्यता", callback_data="set_topic_Indus Valley Civilization"),
             InlineKeyboardButton("📖 वैदिक काल", callback_data="set_topic_Vedic Period History")],
            [InlineKeyboardButton("🕉️ बौद्ध और जैन धर्म", callback_data="set_topic_Buddhism and Jainism"),
             InlineKeyboardButton("👑 16 महाजनपद", callback_data="set_topic_16 Mahajanapadas")],
            [InlineKeyboardButton("🦁 मौर्य साम्राज्य", callback_data="set_topic_Mauryan Empire"),
             InlineKeyboardButton("📀 गुप्त साम्राज्य (स्वर्ण युग)", callback_data="set_topic_Gupta Empire History")],
             
            # ⚔️ मध्यकालीन भारत (Medieval India)
            [InlineKeyboardButton("🕌 दिल्ली सल्तनत", callback_data="set_topic_Delhi Sultanate Dynasty"),
             InlineKeyboardButton("🏯 मुगल साम्राज्य", callback_data="set_topic_Mughal Empire History")],
            [InlineKeyboardButton("🚩 मराठा और विजयनगर", callback_data="set_topic_Maratha and Vijayanagara Empire"),
             InlineKeyboardButton("✨ भक्ति और सूफी आंदोलन", callback_data="set_topic_Bhakti and Sufi Movement")],
             
            # 🇮🇳 आधुनिक भारत (Modern India)
            [InlineKeyboardButton("🚢 यूरोपीय शक्तियों का आगमन", callback_data="set_topic_Arrival of European Powers in India"),
             InlineKeyboardButton("💥 1857 का विद्रोह", callback_data="set_topic_Revolt of 1857 Indian History")],
            [InlineKeyboardButton("🎯 भारतीय राष्ट्रीय कांग्रेस", callback_data="set_topic_Indian National Congress INC History"),
             InlineKeyboardButton("🚶 गांधीवादी युग और आंदोलन", callback_data="set_topic_Mahatma Gandhi and Freedom Movements")],
            [InlineKeyboardButton("✊ क्रांतिकारी आंदोलन", callback_data="set_topic_Indian Revolutionary Freedom Fighters"),
             InlineKeyboardButton("📅 भारत का विभाजन और स्वतंत्रता", callback_data="set_topic_Indian Independence and Partition 1947")],
             
            # 🌍 विश्व इतिहास (World History)
            [InlineKeyboardButton("⚙️ औद्योगिक क्रांति", callback_data="set_topic_Industrial Revolution World History"),
             InlineKeyboardButton("🗼 फ्रांस और रूस की क्रांति", callback_data="set_topic_French and Russian Revolutions")],
            [InlineKeyboardButton("⚔️ प्रथम और द्वितीय विश्व युद्ध", callback_data="set_topic_World War 1 and World War 2 History"),
             InlineKeyboardButton("❄️ शीतयुद्ध का दौर", callback_data="set_topic_Cold War Era Geopolitics")]
        ]
    elif selected_sub == "sub_polity":
        sub_title = "Polity (राजव्यवस्था)"
        # 🟢 फिक्स: यहाँ callback_data को छोटा (Short) कर दिया गया है ताकि 64-byte limit न टूटे
        topics_keyboard = [
            [InlineKeyboardButton("✨ संविधान का निर्माण व स्रोत", callback_data="set_topic_pol_1"),
             InlineKeyboardButton("⚡ प्रस्तावना व अनुसूचियां", callback_data="set_topic_pol_2")],
            [InlineKeyboardButton("✨ संघ, राज्यक्षेत्र व नागरिकता", callback_data="set_topic_pol_3"),
             InlineKeyboardButton("💥 मौलिक अधिकार (Rights)", callback_data="set_topic_pol_4")],
            [InlineKeyboardButton("🌟 नीति निदेशक तत्व व कर्तव्य", callback_data="set_topic_pol_5"),
             InlineKeyboardButton("💫 राष्ट्रपति, उपराष्ट्रपति व PM", callback_data="set_topic_pol_6")],
            [InlineKeyboardButton("🎉 भारतीय संसद (लोक-राज्यसभा)", callback_data="set_topic_pol_7"),
             InlineKeyboardButton("❤️‍🔥 सर्वोच्च न्यायालय (Supreme Court)", callback_data="set_topic_pol_8")],
            [InlineKeyboardButton("❤️‍🩹 राज्यपाल, CM व विधानमंडल", callback_data="set_topic_pol_9"),
             InlineKeyboardButton("🌷 उच्च न्यायालय (High Court)", callback_data="set_topic_pol_10")],
            [InlineKeyboardButton("☘️ पंचायती राज व स्थानीय शासन", callback_data="set_topic_pol_11"),
             InlineKeyboardButton("🍀 निर्वाचन आयोग व चुनाव सुधार", callback_data="set_topic_pol_12")],
            [InlineKeyboardButton("🍃 UPSC, CAG और नीति आयोग", callback_data="set_topic_pol_13"),
             InlineKeyboardButton("🍁 प्रमुख संवैधानिक संशोधन", callback_data="set_topic_pol_14")],
            [InlineKeyboardButton("🍂 आपातकालीन प्रावधान (Emergency)", callback_data="set_topic_pol_15")]
        ]
    elif selected_sub == "sub_geography":
        sub_title = "Geography (भूगोल)"
        topics_keyboard = [
            # 🇮🇳 भारत का भूगोल (Indian Geography)
            [InlineKeyboardButton("🇮🇳 भारत का भौतिक स्वरूप", callback_data="set_topic_Physical Features of India"),
             InlineKeyboardButton("🌊 भारत की नदियां व जलतंत्र", callback_data="set_topic_Indian Rivers and Drainage System")],
            [InlineKeyboardButton("🌦️ भारत की जलवायु व मिट्टी", callback_data="set_topic_Indian Climate and Soil Types"),
             InlineKeyboardButton("🌳 वनस्पति व राष्ट्रीय उद्यान", callback_data="set_topic_Natural Vegetation and National Parks of India")],
            [InlineKeyboardButton("🌾 कृषि, फसलें और सिंचाई", callback_data="set_topic_Agriculture and Irrigation in India"),
             InlineKeyboardButton("🏔️ प्रमुख बांध व परियोजनाएं", callback_data="set_topic_Major Dams and River Valley Projects in India")],
            [InlineKeyboardButton("⛏️ खनिज व ऊर्जा संसाधन", callback_data="set_topic_Minerals and Energy Resources of India"),
             InlineKeyboardButton("🛣️ उद्योग, राजमार्ग व बंदरगाह", callback_data="set_topic_Industries Transport and Ports in India")],
            [InlineKeyboardButton("👥 जनगणना व प्रमुख जनजातियां", callback_data="set_topic_Indian Census and Tribes")],
             
            # 🌍 विश्व का भूगोल (World Geography)
            [InlineKeyboardButton("🗺️ महाद्वीप, पर्वत व मरुस्थल", callback_data="set_topic_Continents Mountains and Deserts of the World"),
             InlineKeyboardButton("🌏 विश्व की नदियां और झीलें", callback_data="set_topic_World Rivers Lakes and Waterfalls")],
            [InlineKeyboardButton("⚓ प्रमुख जलसंधियां व नहरें", callback_data="set_topic_Important Straits and Canals of the World")],
             
            # ☀️ भौतिक व सामान्य भूगोल (Physical Geography)
            [InlineKeyboardButton("🌌 सौरमंडल, अक्षांश व देशांतर", callback_data="set_topic_Solar System Latitudes and Longitudes"),
             InlineKeyboardButton("🌋 पृथ्वी की संरचना व ज्वालामुखी", callback_data="set_topic_Interior of Earth Volcanoes and Earthquakes")],
            [InlineKeyboardButton("🌀 वायुमंडल, पवनें व चक्रवात", callback_data="set_topic_Atmosphere Structure Winds and Cyclones"),
             InlineKeyboardButton("🌊 महासागरीय धाराएं व ज्वार", callback_data="set_topic_Ocean Currents and Tides")]
        ]
    elif selected_sub == "sub_science":
        sub_title = "General Science"
        topics_keyboard = [
            # 🧬 जीव विज्ञान (Biology)
            [InlineKeyboardButton("🧬 कोशिका विज्ञान (Cell Biology)", callback_data="set_topic_Cell Structure and Functions Biology"),
             InlineKeyboardButton("🩻 मानव शरीर के तंत्र (Systems)", callback_data="set_topic_Human Anatomy and Body Systems")],
            [InlineKeyboardButton("🍎 पोषण, विटामिन और रोग", callback_data="set_topic_Human Nutrition Vitamins and Diseases"),
             InlineKeyboardButton("🌿 पादप जगत (Plant Kingdom)", callback_data="set_topic_Plant Anatomy Physiology and Kingdom")],
            [InlineKeyboardButton("🧬 आनुवंशिकी व जैव विकास", callback_data="set_topic_Genetics and Biological Evolution")],
             
            # 🧪 रसायन विज्ञान (Chemistry)
            [InlineKeyboardButton("🧪 पदार्थ की अवस्थाएं व परमाणु", callback_data="set_topic_Matter States and Atomic Structure Chemistry"),
             InlineKeyboardButton("📊 आवर्त सारणी (Periodic Table)", callback_data="set_topic_Periodic Table and Elements Properties")],
            [InlineKeyboardButton("⚗️ अम्ल, क्षारक और लवण", callback_data="set_topic_Acids Bases Salts and pH Scale"),
             InlineKeyboardButton("💎 धातु, अधातु व मिश्रधातु", callback_data="set_topic_Metals NonMetals and Alloys Chemistry")],
            [InlineKeyboardButton("🔥 कार्बनिक रसायन व ईंधन", callback_data="set_topic_Organic Chemistry Carbon and Fuels")],
             
            # 🧲 भौतिक विज्ञान (Physics)
            [InlineKeyboardButton("📏 मात्रक, गति और बल", callback_data="set_topic_Units Measurements Motion and Force Physics"),
             InlineKeyboardButton("⚡ कार्य, ऊर्जा और शक्ति", callback_data="set_topic_Work Energy and Power Physics")],
            [InlineKeyboardButton("💡 प्रकाश व ध्वनि (Light/Sound)", callback_data="set_topic_Light Optics Sound and Waves Physics"),
             InlineKeyboardButton("🔌 विद्युत व चुंबकत्व", callback_data="set_topic_Electricity and Magnetism Physics")],
            [InlineKeyboardButton("🚀 आधुनिक भौतिकी व अंतरिक्ष", callback_data="set_topic_Modern Physics Nuclear Energy and Space")]
        ]
    elif selected_sub == "sub_languages":
        sub_title = "Languages & Grammar"
        # 🟢 फिक्स: यहाँ भी callback_data को छोटा कर दिया गया है
        topics_keyboard = [
            [InlineKeyboardButton("🍂 वर्णमाला, वर्तनी व संधि", callback_data="set_topic_lang_1"),
             InlineKeyboardButton("✨ शब्द भेद (संज्ञा, सर्वनाम, क्रिया)", callback_data="set_topic_lang_2")],
            [InlineKeyboardButton("⚡ समास, उपसर्ग व प्रत्यय", callback_data="set_topic_lang_3"),
             InlineKeyboardButton("👍 विलोम, पर्यायवाची व अनेकार्थी", callback_data="set_topic_lang_4")],
            [InlineKeyboardButton("💫 लिंग, वचन, कारक व काल", callback_data="set_topic_lang_5"),
             InlineKeyboardButton("🍃 मुहावरे, लोकोक्तियां व रस-छंद", callback_data="set_topic_lang_6")],
            [InlineKeyboardButton("☘️ Parts of Speech", callback_data="set_topic_lang_7"),
             InlineKeyboardButton("🌷 Tenses & Sentence Structure", callback_data="set_topic_lang_8")],
            [InlineKeyboardButton("❤️‍🔥 Active & Passive Voice", callback_data="set_topic_lang_9"),
             InlineKeyboardButton("🎉 Direct & Indirect Speech", callback_data="set_topic_lang_10")],
            [InlineKeyboardButton("💥 Subject-Verb Agreement", callback_data="set_topic_lang_11")],
            [InlineKeyboardButton("💫 Synonyms, Antonyms & Idioms", callback_data="set_topic_lang_12"),
             InlineKeyboardButton("✨ Prepositions & Articles", callback_data="set_topic_lang_13")]
        ]

    topics_keyboard.append([InlineKeyboardButton("Back to Subjects", callback_data="back_to_subjects_nav")])
    
    updated_text = (
        f"<blockquote>📂 Subject: <b>{sub_title}</b></blockquote>\n\n"
        "🎑 <b>topics me se koi ek select karein:</b>"
    )
    
    await query.edit_message_text(text=updated_text, reply_markup=InlineKeyboardMarkup(topics_keyboard), parse_mode="HTML")
    return BUTTON_TOPIC

async def handle_final_topic_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when user clicks a specific topic button. Saves it and routes to Q_COUNT"""
    query = update.callback_query
    await query.answer()
    
    topic_chosen = query.data.replace("set_topic_", "").strip()
    
    topic_map = {
        "pol_1": "Making and Sources of Indian Constitution",
        "pol_2": "Preamble and Schedules of Indian Constitution",
        "pol_3": "Union and its Territory and Citizenship",
        "pol_4": "Fundamental Rights of India",
        "pol_5": "Directive Principles of State Policy and Fundamental Duties",
        "pol_6": "President Vice President and Prime Minister of India",
        "pol_7": "Indian Parliament Lok Sabha and Rajya Sabha",
        "pol_8": "Supreme Court of India and Judicial Review",
        "pol_9": "Governor Chief Minister and State Legislature",
        "pol_10": "High Court and Subordinate Courts in India",
        "pol_11": "Panchayati Raj and Local Self Government",
        "pol_12": "Election Commission of India and Electoral Reforms",
        "pol_13": "Constitutional and Non Constitutional Bodies CAG NITI Aayog",
        "pol_14": "Important Constitutional Amendments of India",
        "pol_15": "Emergency Provisions in Indian Constitution",
        "lang_1": "Hindi Grammar Varnamala and Sandhi",
        "lang_2": "Hindi Grammar Sangya Sarvnam Kriya Avyay",
        "lang_3": "Hindi Grammar Samas Upsarg Pratyay",
        "lang_4": "Hindi Vocabulary Vilom Paryayvachi",
        "lang_5": "Hindi Grammar Ling Vachan Karak Kaal",
        "lang_6": "Hindi Muhavare Lokoktiyan Ras Chhand Alankar",
        "lang_7": "English Grammar Parts of Speech",
        "lang_8": "English Grammar Tenses and Sentence Structure",
        "lang_9": "English Grammar Active and Passive Voice",
        "lang_10": "English Grammar Direct and Indirect Narration",
        "lang_11": "English Grammar Subject Verb Agreement Errors",
        "lang_12": "English Vocabulary Synonyms Antonyms One Word Substitution",
        "lang_13": "English Grammar Prepositions and Articles"
    }
    
    if topic_chosen in topic_map:
        topic_chosen = topic_map[topic_chosen]
        
    context.user_data['topic'] = topic_chosen
    
    # 🟢 FIXED & COLORED: Raw payload format used to force Green (success) button colors
    count_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "10", "callback_data": "qcnt_10", "style": "success"},
            {"text": "20", "callback_data": "qcnt_20", "style": "success"},
            {"text": "50", "callback_data": "qcnt_50", "style": "success"},
            {"text": "70", "callback_data": "qcnt_70", "style": "success"}
        ]
    ])
    
    await query.edit_message_text(
        text=(
            f"<blockquote>✅ Topic Saved: <b>{context.user_data['topic']}</b></blockquote>\n\n"
            "<blockquote>✨ Step 2:</blockquote><b>How many questions do you want?</b>"
        ),
        parse_mode="HTML",
        reply_markup=count_inline_keyboard
    )
    return Q_COUNT

async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return TOPIC
    
    context.user_data['topic'] = update.message.text
    
    # 🟢 FIXED & COLORED: Raw payload format used to force Green (success) button colors
    count_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "10", "callback_data": "qcnt_10", "style": "success"},
            {"text": "20", "callback_data": "qcnt_20", "style": "success"},
            {"text": "50", "callback_data": "qcnt_50", "style": "success"},
            {"text": "70", "callback_data": "qcnt_70", "style": "success"}
        ]
    ])
    
    await update.message.reply_text(
        text=(
            f"<blockquote>✅ Topic Saved: <b>{context.user_data['topic']}</b></blockquote>\n\n"
            "<blockquote>✨ Step 2:</blockquote><b>How many questions do you want?</b>"
        ),
        parse_mode="HTML",
        reply_markup=count_inline_keyboard
    )
    return Q_COUNT

async def handle_q_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return Q_COUNT
    query = update.callback_query
    if not query:
        return Q_COUNT  
        
    await query.answer()
    user_text = query.data.replace("qcnt_", "").strip()
    context.user_data['q_count'] = int(user_text)
    saved_topic = context.user_data.get('topic', 'AI Quiz')
    
    # 🌟 Yes button gets Green (success), No button gets Blue (primary) for layout balance
    title_confirm_keyboard = InlineKeyboardMarkup([
        [
            {"text": "Yes ✅", "callback_data": "title_use_topic", "style": "success"},
            {"text": "No ❌", "callback_data": "title_custom_name", "style": "primary"}
        ]
    ])
    
    await query.edit_message_text(
        text=(
            f"<blockquote>✅ Questions Count: <b>{context.user_data['q_count']}</b></blockquote>\n\n"
            f"💫 <b>क्या आप क्विज़ का टाइटल वही रखना चाहते हैं जो टॉपिक का नाम है?</b>\n"
            f"📝 <i>टॉपिक नाम: {saved_topic}</i>"
        ),
        parse_mode="HTML",
        reply_markup=title_confirm_keyboard
    )
    return TITLE

async def handle_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return TITLE
    
    if "quiz_build" not in context.user_data:
        context.user_data["quiz_build"] = {"title": "", "description": "", "questions": []}
    
    query = update.callback_query
    
    # Case 1: अगर यूज़र ने इनलाइन बटन (Yes/No) पर क्लिक किया है
    if query:
        await query.answer()
        if query.data == "title_use_topic":
            topic_name = context.user_data.get('topic', 'AI Quiz')
            context.user_data['title'] = topic_name
            context.user_data["quiz_build"]["title"] = topic_name  
            
            desc_inline_keyboard = InlineKeyboardMarkup([
                [{"text": "Skip Description ⏭️", "callback_data": "desc_skip", "style": "primary"}]
            ])
            
            # मैसेज सेंड करके उसकी ID मेमोरी में सेव कर रहे हैं
            sent_msg = await query.edit_message_text(
                text=(
                    f"<blockquote>✅ Title Saved (Same as Topic): <b>{context.user_data['title']}</b></blockquote>\n\n"
                    "<blockquote>📝 <b>Step 4:</b> Send a Description for this quiz in chat.</blockquote>\n"
                    "<b>Or Skip Description button par click kare.</b>"
                ),
                parse_mode="HTML",
                reply_markup=desc_inline_keyboard
            )
            # 📌 यहाँ पुरानी मैसेज आईडी को स्टोर कर लिया
            context.user_data["last_desc_panel_id"] = sent_msg.message_id
            return DESCRIPTION
            
        elif query.data == "title_custom_name":
            await query.edit_message_text(
                "📝 <b>कृपया अपनी क्विज़ के लिए एक नया Title चैट में टाइप करके भेजें:</b>",
                parse_mode="HTML",
                reply_markup=None
            )
            return TITLE
            
    # Case 2: अगर यूज़र चैट में अपना कस्टम Title टाइप करके भेजता है
    if update.message and update.message.text:
        title_text = update.message.text.strip()
        if len(title_text) > 128:
            await update.message.reply_text("⚠️ This title is too long. Please send a new one, 128 characters max.")
            return TITLE
            
        context.user_data['title'] = title_text
        context.user_data["quiz_build"]["title"] = title_text
        
        # इसे अपने कोड में जहां भी desc_inline_keyboard है, वहां अपडेट कर लें (जैसे handle_title के अंदर):
        desc_inline_keyboard = InlineKeyboardMarkup([
            [{"text": "Skip Description ⏭️", "callback_data": "desc_skip", "style": "primary"}]
        ])
        
        sent_msg = await update.message.reply_text(
            f"<blockquote>✅ Title Saved: <b>{context.user_data['title']}</b></blockquote>\n\n"
            "<blockquote>📝 <b>Step 4:</b> Send a Description for this quiz in chat.</blockquote>\n"
            "<b>Or Skip Description button par click kare.</b>",
            parse_mode="HTML",
            reply_markup=desc_inline_keyboard
        )
        # 📌 यहाँ भी पुरानी मैसेज आईडी को स्टोर कर लिया
        context.user_data["last_desc_panel_id"] = sent_msg.message_id
        return DESCRIPTION

    return TITLE

async def handle_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return DESCRIPTION
    
    query = update.callback_query
    
    # 🟢 Case 1: अगर यूज़र ने इनलाइन 'Skip Description' बटन पर क्लिक किया है
    if query:
        await query.answer()
        context.user_data['description'] = "None"
        
    # 🟢 Case 2: अगर यूज़र ने चैट में डिस्क्रिप्शन टेक्स्ट टाइप करके भेजा है
    else:
        text = update.message.text.strip()
        context.user_data['description'] = "None" if text in ["/skipped", "Skip ⏭️", "/skip"] else text
        
        # 🔥 FIXED (FORCE CLOSE BUTTON LOGIC):
        # मेमोरी से स्टोर की गई मैसेज आईडी को निकालकर उस पूरे पुराने बटन वाले पैनल को डिलीट कर रहे हैं
        old_panel_id = context.user_data.pop("last_desc_panel_id", None)
        if old_panel_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=old_panel_id
                )
            except Exception:
                # सेफ्टी फॉलबैक: अगर डिलीट करने की परमिशन न हो, तो कम से कम बटन्स को क्लोज (रिमूव) कर दे
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=update.effective_chat.id,
                        message_id=old_panel_id,
                        reply_markup=None
                    )
                except Exception:
                    pass

    # 🟢 COLORED: Step 5 (Language) के लिए इनलाइन कीबोर्ड बटन्स को Green (success) कलर दिया गया है
    lang_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "Hindi", "callback_data": "lang_Hindi", "style": "success"},
            {"text": "English", "callback_data": "lang_English", "style": "primary"}
        ]
    ])
    
    # यदि बटन दबाया था तो उसी मेसेज को एडिट करेगा, यदि टेक्स्ट भेजा था तो नया फ्रेश मेसेज भेजेगा
    if query:
        await query.edit_message_text(
            text="<blockquote>🌐 Step 5 — Language</blockquote>\n<b>Choose quiz output layout language:</b>",
            reply_markup=lang_inline_keyboard,
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text="<blockquote>🌐 Step 5 — Language</blockquote>\n<b>Choose quiz output layout language:</b>",
            reply_markup=lang_inline_keyboard,
            parse_mode="HTML"
        )
        
    return LANGUAGE

async def handle_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return LANGUAGE
    query = update.callback_query
    if not query:
        return LANGUAGE
        
    await query.answer()
    user_text = query.data.replace("lang_", "").strip()
    context.user_data['language'] = user_text
    
    # 🌟 Language specific features custom colors
    expl_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "With Explanation", "callback_data": "expl_With Explanation", "style": "primary"},
            {"text": "No Explanation", "callback_data": "expl_No Explanation", "style": "success"}
        ]
    ])
    
    await query.edit_message_text(
        text="<blockquote>✨ Step 6 — Explanation</blockquote>\n<b>Do you want explanations?</b>",
        reply_markup=expl_inline_keyboard,
        parse_mode="HTML"
    )
    return EXPLANATION

async def handle_explanation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return EXPLANATION
    
    query = update.callback_query
    
    # 🟢 सिर्फ इनलाइन बटन्स स्वीकार करें, टेक्स्ट मैसेज पूरी तरह ब्लॉक
    if not query:
        return EXPLANATION
        
    await query.answer()
    user_text = query.data.replace("expl_", "").strip()
    context.user_data['explanation'] = user_text
    
    # इसे अपने मुख्य कोड में handle_explanation फ़ंक्शन के अंदर बदलें:
    diff_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "Easy", "callback_data": "diff_Easy", "style": "primary"},
            {"text": "Medium", "callback_data": "diff_Medium", "style": "success"},
            {"text": "Hard", "callback_data": "diff_Hard", "style": "primary"}
        ]
    ])
    
    await query.edit_message_text(
        text="<blockquote>⚡ Step 7 — Difficulty</blockquote>\n<b>Choose calculation difficulty:</b>",
        reply_markup=diff_inline_keyboard,
        parse_mode="HTML"
    )
    return DIFFICULTY

async def handle_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return DIFFICULTY
    
    query = update.callback_query
    
    # 🟢 सिर्फ इनलाइन बटन्स स्वीकार करें, टेक्स्ट मैसेज पूरी तरह ब्लॉक
    if not query:
        return DIFFICULTY
        
    await query.answer()
    user_text = query.data.replace("diff_", "").strip()
    context.user_data['difficulty'] = user_text
    
    # 🟢 COLORED: Step 8 (Option Count) के लिए इनलाइन कीबोर्ड बटन्स को Green (success) कलर दिया गया है
    opts_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "2 Options", "callback_data": "opts_2", "style": "success"},
            {"text": "3 Options", "callback_data": "opts_3", "style": "primary"},
            {"text": "4 Options", "callback_data": "opts_4", "style": "success"}
        ]
    ])
    
    await query.edit_message_text(
        text="<blockquote>🔥 Step 8 — Option Count</blockquote>\n<b>How many choices per card?</b>",
        reply_markup=opts_inline_keyboard,
        parse_mode="HTML"
    )
    return OPTIONS_COUNT

async def handle_options_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return OPTIONS_COUNT
    
    query = update.callback_query
    
    # 🟢 सिर्फ इनलाइन बटन्स स्वीकार करें, टेक्स्ट मैसेज पूरी तरह ब्लॉक
    if not query:
        return OPTIONS_COUNT
        
    await query.answer()
    user_text = query.data.replace("opts_", "").strip()
    context.user_data['options_count'] = int(user_text)
    
    # 🔵 COLORED: Step 9 (Time Limit) के लिए इनलाइन कीबोर्ड बटन्स को Blue (primary) कलर दिया गया है
    time_inline_keyboard = InlineKeyboardMarkup([
        [
            {"text": "10 sec", "callback_data": "time_10", "style": "primary"},
            {"text": "15 sec", "callback_data": "time_15", "style": "success"},
            {"text": "30 sec", "callback_data": "time_30", "style": "primary"}
        ]
    ])
    
    await query.edit_message_text(
        text="<blockquote>⏱ Step 9 — Time Limit</blockquote>\n<b>Set ticker duration:</b>",
        reply_markup=time_inline_keyboard,
        parse_mode="HTML"
    )
    return TIME_LIMIT

async def handle_time_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return TIME_LIMIT
    
    query = update.callback_query
    if not query:
        return TIME_LIMIT
        
    await query.answer()
    user_text = query.data.replace("time_", "").strip()
    context.user_data['time_limit'] = int(user_text)
    
    topic = context.user_data.get('topic', 'General Knowledge')
    count = context.user_data.get('q_count', 5)
    lang = context.user_data.get('language', 'English')
    difficulty = context.user_data.get('difficulty', 'Medium')
    options_cnt = context.user_data.get('options_count', 4)
    
    msg_target = query.message

    # 🚀 पहला पैनल: हमेशा दिखेगा
    generating_msg = await query.edit_message_text(
        text="<b>🚀 AI Quiz Generator</b>\n\n⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜\n🔎 <b>Researching your topic...</b>\n⏳ <b>please wait...</b>",
        parse_mode="HTML",
        reply_markup=None
    )
    
    try:
        task = asyncio.create_task(asyncio.to_thread(
            generate_bulk_questions_ai, topic, count, lang, difficulty, options_cnt
        ))
        
        try:
            # 🟢 पहला एनिमेशन: 3 सेकंड का अनिवार्य वेट
            await asyncio.sleep(4)
            await generating_msg.edit_text(
                "<b>🚀 AI Quiz Generator</b>\n\n🟪🟪🟪⬜⬜⬜⬜⬜⬜⬜\n🧠 <b>Crafting questions...</b>\n⏳ <b>please wait...</b>",
                parse_mode="HTML"
            )
                
            # 🟢 दूसरा एनिमेशन: अगला 4 सेकंड का अनिवार्य वेट
            await asyncio.sleep(4)
            await generating_msg.edit_text(
                "<b>🚀 AI Quiz Generator</b>\n\n🟪🟪🟪🟪🟪🟪⬜⬜⬜⬜\n✍️ <b>Writing options...</b>\n⏳ <b>please wait...</b>",
                parse_mode="HTML"
            )
                
            # 🟢 तीसरा एनिमेशन बदलाव: अगला 4 सेकंड का अनिवार्य वेट (कंडीशन हटा दी गई है, यह भी हर हाल में दिखेगा)
            await asyncio.sleep(4)
            await generating_msg.edit_text(
                "<b>🚀 AI Quiz Generator</b>\n\n🟪🟪🟪🟪🟪🟪🟪🟪🟪⬜\n📟 <b>Verifying answers...</b>\n⏳ <b>please wait...</b>",
                parse_mode="HTML"
            )
        except Exception as msg_err:
            logging.warning(f"Animation message sequence alert: {msg_err}")

        # 🔥 मुख्य बदलाव: तीनों एनिमेशन दिखने के बाद बोट यहाँ आकर AI टास्क के खत्म होने का इंतज़ार करेगा। 
        # अगर AI को 11 सेकंड से ज़्यादा का समय लग रहा है, तो स्क्रीन इसी तीसरे एनिमेशन पर रुकी रहेगी।
        ai_questions = await task
        
        if ai_questions is None or len(ai_questions) == 0:
            await generating_msg.edit_text(
                "❌ <b>AI Quiz Generator Error</b>\n\nServer par heavy load ya error ke karan quiz generate nahi ho paya. Kripya thodi der baad fir se koshish karein.",
                parse_mode="HTML"
            )
            context.user_data.clear()
            return ConversationHandler.END
            
        try:
            await generating_msg.edit_text(
                "<b>🚀 AI Quiz Generator</b>\n\n🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩\n"
                "<b>Verified Answer's</b> ✅\n💯 <b>Done generated...</b>",
                parse_mode="HTML"
            )
            await asyncio.sleep(3)
            await generating_msg.delete()
        except Exception:
            pass
        
        formatted_questions = []
        for q in ai_questions:
            correct_idx = q.get("correct", 0)
            options = q.get("options", [])
            
            if not isinstance(correct_idx, int):
                try: correct_idx = int(correct_idx)
                except: correct_idx = 0
            
            if correct_idx < 0 or correct_idx >= len(options):
                correct_idx = 0
            
            formatted_questions.append({
                "text": q.get("question", ""), "options": options, "correct": correct_idx,
                "explanation": q.get("explanation", ""), "pre_message": ""
            })
        
        actual_count = len(formatted_questions)
        alert_text = ""
        if actual_count < count:
            alert_text = f"\n\n⚠️ <b>नोट:</b> आपने {count} सवाल माँगे थे, लेकिन AI ने कुल <b>{actual_count}</b> सवाल ही जनरेट किए हैं।"
        
        context.user_data["ai_questions"] = formatted_questions
        context.user_data["quiz_build"] = {
            "title": context.user_data.get("title", "AI Quiz"), "description": context.user_data.get("description", ""),
            "timer": context.user_data.get("time_limit", 30), "questions": formatted_questions
        }
        
        context.user_data["quiz_build_creator_id"] = query.from_user.id
        
        neg_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ No Negative (0.0)", callback_data="neg_0.0"), InlineKeyboardButton("📉 1/4th (-0.25)", callback_data="neg_0.25")],
            [InlineKeyboardButton("📉 Half (-0.5)", callback_data="neg_0.5"), InlineKeyboardButton("📉 Single (-1.0)", callback_data="neg_1.0")],
            [InlineKeyboardButton("📉 Heavy (-1.5)", callback_data="neg_1.5")]
        ])
        
        await msg_target.chat.send_message(
            f"<blockquote>🛅 <b>Select Negative Marking Schema:</b>{alert_text}</blockquote>\n\n"
            "<blockquote>Aap is quiz ke liye kitni negative marking set karna chahte hain?</blockquote>",
            reply_markup=neg_keyboard,
            parse_mode="HTML"
        )
        return NEGATIVE
        
    except Exception as e:
        logging.error(f"Error in handle_time_limit: {e}", exc_info=True)
        
        if "429" in str(e) or "too_many_requests" in str(e):
            await msg_target.chat.send_message(
                "⚠️ <b>आज का फ्री लिमिट कोटा समाप्त हो चुका है!</b>\n\nAI मॉडल की प्रतिदिन की सीमा (20 रिक्वेस्ट) पूरी हो गई है। कृपया कुछ समय बाद या कल दोबारा प्रयास करें।", 
                parse_mode="HTML"
            )
        else:
            await msg_target.chat.send_message("aapka quiz genrate karne me error aa gaya tha esliye cancel ho gaya aap fir se quiz generate kare", parse_mode="HTML")
            
        context.user_data.clear()
        return ConversationHandler.END

# Final Summary aur Quiz Generation Confirmation
async def handle_negative_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle negative marking selection and save AI-generated quiz to DB"""
    try:
        query = update.callback_query
        await query.answer()
        
        neg_val = float(query.data.replace("neg_", "").strip())
        
        # Get quiz data from context
        quiz_build = context.user_data.get("quiz_build")
        if not quiz_build:
            quiz_build = {
                "title": context.user_data.get("title", "AI Quiz"),
                "description": context.user_data.get("description", "AI Generated Quiz"),
                "timer": context.user_data.get("time_limit", 30),
                "questions": context.user_data.get("ai_questions", [])
            }
        
        user_id = context.user_data.get("quiz_build_creator_id") or update.callback_query.from_user.id
        
        if not quiz_build or not quiz_build.get("title"):
            await query.message.reply_text("❌ Error: Quiz data missing. Start over with /newquiz or /autoquiz")
            return ConversationHandler.END

        # ✅ SAVE QUIZ TO DATABASE
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Insert into quizzes table
        cursor.execute(
            "INSERT INTO quizzes (creator_id, title, description, timer, negative_value) VALUES (?, ?, ?, ?, ?)", 
            (user_id, quiz_build["title"], quiz_build["description"], quiz_build.get("timer", 30), neg_val)
        )
        quiz_id = cursor.lastrowid
        
        # Insert questions
        questions = quiz_build.get("questions", [])
        for q_idx, q in enumerate(questions):
            if isinstance(q, dict):
                q_text = q.get("text") or q.get("question", "")
                options = q.get("options", [])
                correct = q.get("correct", 0)
                explanation = q.get("explanation", "")
                pre_message = q.get("pre_message", "")
                
                # 🟢 CRITICAL: Convert correct to INTEGER INDEX
                if isinstance(correct, str):
                    try:
                        correct_idx = int(correct)
                    except ValueError:
                        # अगर string option है, तो find करो
                        try:
                            correct_idx = options.index(str(correct))
                            logging.info(f"Q{q_idx}: Converted string '{correct}' to index {correct_idx}")
                        except (ValueError, IndexError):
                            correct_idx = 0
                            logging.warning(f"Q{q_idx}: Could not find '{correct}', using 0")
                else:
                    try:
                        correct_idx = int(correct)
                    except (ValueError, TypeError):
                        correct_idx = 0
                
                # Validate index
                if correct_idx < 0 or correct_idx >= len(options):
                    logging.warning(f"Q{q_idx}: Invalid index {correct_idx}, using 0")
                    correct_idx = 0
                
                # 🟢 Log करो database में क्या जा रहा है
                logging.info(f"Q{q_idx}: Saving correct_answer={correct_idx}, option='{options[correct_idx]}'")
                
                cursor.execute(
                    "INSERT INTO questions (quiz_id, question_text, options, correct_answer, explanation, pre_message) VALUES (?, ?, ?, ?, ?, ?)", 
                    (quiz_id, q_text, json.dumps(options), correct_idx, explanation, pre_message)
                )
        
        conn.commit()
        conn.close()
        
        # ✅ CLEAR TEMPORARY DATA
        context.user_data.pop("quiz_build", None)
        context.user_data.pop("quiz_build_creator_id", None)
        context.user_data.pop("title", None)
        context.user_data.pop("description", None)
        context.user_data.pop("time_limit", None)
        context.user_data.pop("ai_questions", None)
        context.user_data.pop("topic", None)
        context.user_data.pop("q_count", None)
        context.user_data.pop("language", None)
        context.user_data.pop("difficulty", None)
        context.user_data.pop("options_count", None)
        context.user_data.pop("shuffle", None)
        context.user_data.pop("explanation", None)
        
        # Remove callback buttons
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        
        # Show success message
        neg_display = "Disabled" if neg_val == 0.0 else f"-{neg_val} per wrong answer"
        await query.message.reply_text(
            f"✅ Quiz Created Successfully!\n⏱ Timer: {quiz_build.get('timer', 30)}s\n📉 Negative Marking: {neg_display}"
        )
        
        # ✅ SHOW SUMMARY PANEL
        await show_summary_panel_text(query, context, quiz_id)
        
        return ConversationHandler.END
        
    except Exception as e:
        logging.error(f"Error in handle_negative_and_finish: {e}", exc_info=True)
        return ConversationHandler.END
        
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Quiz setup processing setup abandoned.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def global_continuous_polling_worker(app: Application):
    global POLLING_LOOP_ACTIVE
    logging.info("Background Global Continuous Polling Worker started.")
    await asyncio.sleep(10) # Initial startup buffer delay
    
    while True:
        try:
            # Agar owner ne loop ko stop kiya hai, toh ye 10-10 second ruk kar check karega
            if not POLLING_LOOP_ACTIVE:
                await asyncio.sleep(10)
                continue

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # 1. Last sent question ID status read karein
            cursor.execute("SELECT last_question_id FROM polling_state WHERE key = 'global_loop'")
            state_row = cursor.fetchone()
            last_qid = state_row[0] if state_row else 0
            
            # 2. Next question fetch karein jo list me sequential ho
            cursor.execute(
                "SELECT id, question_text, options, correct_answer, explanation FROM questions WHERE id > ? ORDER BY id ASC LIMIT 1", 
                (last_qid,)
            )
            q_row = cursor.fetchone()
            
            # Agar last question tak pahunch gaye ho, toh loop zero se restart karein
            if not q_row:
                cursor.execute("SELECT id, question_text, options, correct_answer, explanation FROM questions ORDER BY id ASC LIMIT 1")
                q_row = cursor.fetchone()
            
            if q_row:
                q_id, q_text, options_json, correct_ans, explanation = q_row
                options = json.loads(options_json)
                
                # Correct index integer verification
                try:
                    correct_idx = int(correct_ans)
                except (ValueError, TypeError):
                    try:
                        correct_idx = options.index(str(correct_ans))
                    except ValueError:
                        correct_idx = 0
                
                if correct_idx < 0 or correct_idx >= len(options):
                    correct_idx = 0
                
                # 3. Target broadcast chats pull karein database se
                cursor.execute("SELECT chat_id FROM broadcast_groups")
                groups = cursor.fetchall()
                
                cursor.execute("SELECT chat_id FROM broadcast_users")
                users = cursor.fetchall()
                
                # Raw list mapping clean array sequence
                raw_chats = [r[0] for r in groups] + [r[0] for r in users]
                all_target_chats = list(set(raw_chats))
                
                # 🔥 CRITICAL FIXED: Owner ki private chat ID ko destination list se bahar (Filter) karein
                if OWNER_ID in all_target_chats:
                    all_target_chats.remove(OWNER_ID)
                    
                logging.info(f"Global Poll: Broadcasting Question ID {q_id} to {len(all_target_chats)} users/groups (Owner Chat Exempted).")
                
                # 4. SMART OPTIONS CHARACTER LIMIT CHECK (Telegram limit is 100 chars per option)
                is_any_option_too_long = any(len(str(opt)) > 100 for opt in options)
                
                # Explanation validation (Max 200 chars allowed by Telegram)
                clean_expl = explanation.strip() if explanation and len(explanation.strip()) <= 200 else None
                
                # 5. Build Dynamic Presentation Layouts (Removed ID display text completely)
                if is_any_option_too_long:
                    # Options bahut bade hain, isliye inko text me dikhana padega
                    text_message_payload = "📝 <b>Practice Question</b>\n\n"
                    text_message_payload += f"<b>{q_text}</b>\n\n"
                    text_message_payload += "<b>विकल्प (Options):</b>\n"
                    for i, opt in enumerate(options):
                        text_message_payload += f" {i+1}<b>. {opt}</b>\n"
                        
                    poll_question = "🫵 ऊपर दिए गए प्रश्न का सही उत्तर चुनें: (Select Correct Answer):"
                    poll_options = [f"Option {i+1}" for i in range(len(options))]
                else:
                    # Options limit ke andar hain, toh text me sirf question jayega aur options pure poll card me
                    text_message_payload = f"📝 <b>Practice Question</b>\n\n<b>{q_text}</b>"
                    
                    poll_question = "🫵 ऊपर दिए गए प्रश्न का सही उत्तर चुनें: (Select Correct Answer):"
                    poll_options = [str(opt) for opt in options]
                
                # 6. Target chats me messages dispatch karein
                for target_chat in all_target_chats:
                    if not POLLING_LOOP_ACTIVE:
                        break
                    try:
                        # Step A: Hamesha pehle text message me Question send hoga
                        await app.bot.send_message(
                            chat_id=target_chat,
                            text=text_message_payload,
                            parse_mode="HTML"
                        )
                        
                        # Chhota sa pause proper arrangement ke liye
                        await asyncio.sleep(0.5)
                        
                        # Step B: Uske theek niche functional poll card attach hoga (Without timer)
                        await app.bot.send_poll(
                            chat_id=target_chat,
                            question=poll_question,
                            options=poll_options,
                            type="quiz",
                            correct_option_id=correct_idx,
                            explanation=clean_expl,
                            is_anonymous=False
                        )
                        await asyncio.sleep(0.3) # API flood protection delay
                    except Exception as chat_err:
                        logging.warning(f"Failed sending global automated poll setup to chat {target_chat}: {chat_err}")
                
                # 7. State update karke save karein database me (sirf tab jab loop active bacha ho)
                if POLLING_LOOP_ACTIVE:
                    cursor.execute("UPDATE polling_state SET last_question_id = ? WHERE key = 'global_loop'", (q_id,))
                    conn.commit()
            else:
                logging.info("Global Polling Loop: No questions found in the database yet.")
                
            conn.close()
            
        except Exception as e:
            logging.error(f"Error inside global continuous polling worker loop: {e}", exc_info=True)
            
        # ⏱️ 30 Minutes interval timer (30 * 60 = 1800 seconds)
        await asyncio.sleep(1800)

async def start_global_polling_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only command to resume background global 30-min polling"""
    global POLLING_LOOP_ACTIVE
    # Strictly check if command caller is the authenticated bot owner
    if OWNER_ID is None or update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("❌ Unauthorized - Only bot owner can use this command.")
        return
        
    if POLLING_LOOP_ACTIVE:
        await update.message.reply_text("⚠️ Global automated 30-min polling is already running.")
    else:
        POLLING_LOOP_ACTIVE = True
        await update.message.reply_text("✅ <b>Global Automated Polling Resumed!</b>\n\nBot ab har 30 min me database se agla question automatic sabhi users aur groups me send karega (Owner chat blocked).", parse_mode="HTML")

async def stop_global_polling_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only command to pause background global 30-min polling"""
    global POLLING_LOOP_ACTIVE
    # Strictly check if command caller is the authenticated bot owner
    if OWNER_ID is None or update.message.from_user.id != OWNER_ID:
        await update.message.reply_text("❌ Unauthorized - Only bot owner can use this command.")
        return
        
    if not POLLING_LOOP_ACTIVE:
        await update.message.reply_text("⚠️ Global automated polling is already paused/stopped.")
    else:
        POLLING_LOOP_ACTIVE = False
        await update.message.reply_text("🛑 <b>Global Automated Polling Stopped!</b>\n\nAutomated questions distribution has been successfully paused.", parse_mode="HTML")

async def savetopic_custom_topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: Reply to a valid topic list message with /savetopic to overwrite active topics pool.
    Conditions applied: Minimum 5 topics, each starting on a new line.
    """
    try:
        message = update.message
        if not message:
            return

        if OWNER_ID is None or message.from_user.id != OWNER_ID:
            await message.reply_text("❌ Unauthorized - Only bot owner can use this command.")
            return

        if not message.reply_to_message:
            await message.reply_text(
                "⚠️ <b>How to use?</b>\n"
                "1. Bot की भेजी गई टॉपिक्स लिस्ट या अपने किसी लिस्ट मैसेज पर जाएँ।\n"
                "2. उस मैसेज का <b>Reply</b> करते हुए केवल `/savetopic` लिखकर भेजें.",
                parse_mode="HTML"
            )
            return

        target_message = message.reply_to_message
        raw_text = target_message.text or target_message.caption

        if not raw_text or not raw_text.strip():
            await message.reply_text("❌ Error: रिप्लाई किए गए मैसेज में कोई टेक्स्ट नहीं मिला।")
            return

        # 🟢 Condition 1 & Line Splitting Checker
        raw_lines = raw_text.split('\n')
        valid_topics = []

        for line in raw_lines:
            clean_line = line.strip()
            if not clean_line:
                continue
            # सिस्टम की अनचाही लाइन्स या हेडर्स को रिमूव करें
            if any(keyword in clean_line.lower() for keyword in ["list", "owner", "total", "schedule", "active", "⏱", "🎯", "🆔"]):
                continue
            valid_topics.append(clean_line)

        # 🟢 Condition 2: Minimum 5 topics verification
        if len(valid_topics) < 5:
            await message.reply_text(
                f"❌ <b>Validation Failed!</b>\n\n"
                f"⚠️ रिप्लाई की गई लिस्ट में कम से कम 5 वैलिड टॉपिक्स होने चाहिए।\n"
                f"📊 वर्तमान में केवल `{len(valid_topics)}` टॉपिक्स ही डिटेक्ट हुए हैं।\n"
                f"📝 सुनिश्चित करें कि हर टॉपिक एक नई लाइन से शुरू हो रहा हो।",
                parse_mode="HTML"
            )
            return

        final_payload = "\n".join(valid_topics)
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO autorun_custom_topic (key, topic_name, updated_at) VALUES ('active_topic', ?, datetime('now'))",
            (final_payload,)
        )
        conn.commit()
        conn.close()

        preview_text = f"1. {valid_topics[0]}\n2. {valid_topics[1]}"
        await message.reply_text(
            f"✅ *New Topic Pool Successfully Overwritten!*\n\n"
            f"🗑️ *Old Settings:* Automatically Deleted\n"
            f"🔢 *Detected Valid Topics:* `{len(valid_topics)}` Items\n\n"
            f"📋 *Saved Topics Preview:*\n`{preview_text}`\n`...and {len(valid_topics)-2} more topics stored.`",
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Error in save_custom_topic_command: {e}")
        await update.message.reply_text("❌ Error saving dynamic topic configuration validation rules.")

async def repeat_ready_button_color_worker(chat_id: int, message_id: int, quiz_id: int, context: ContextTypes.DEFAULT_TYPE):
    """बटन का रंग बदलता है और बाहरी फ़ोर्स क्लोज़ (Cancel) को सुरक्षित रूप से स्वीकार करता है।"""
    styles = ["primary", "success", "danger"]
    idx = 0
    
    try:
        while True:
            await asyncio.sleep(3) # हर 3 सेकंड में रंग बदलेगा
            
            # अगर क्विज़ शुरू हो चुकी है, तो लूप बंद करें
            if chat_id in GROUP_GAMES and GROUP_GAMES[chat_id].get("quiz_started"):
                break
                
            ready_count = 0
            if chat_id in GROUP_GAMES and "ready_users" in GROUP_GAMES[chat_id]:
                if isinstance(GROUP_GAMES[chat_id]["ready_users"], set):
                    ready_count = len(GROUP_GAMES[chat_id]["ready_users"])
            
            # अगर कम से कम 1 खिलाड़ी आ गया है, तो ब्लिंकिंग बंद करें
            if ready_count >= 1:
                break
            
            raw_button = {
                "text": "I am ready!",
                "callback_data": f"ready_{quiz_id}",
                "style": styles[idx]
            }
            
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=InlineKeyboardMarkup([[raw_button]])
                )
            except Exception as e:
                if "message is not modified" in str(e).lower() or "bad request" in str(e).lower():
                    pass
                else:
                    raise e
            
            idx = (idx + 1) % len(styles)
            
    except asyncio.CancelledError:
        # जब बॉट बाहर से इसे बंद करेगा, तो यह बिना एरर के यहाँ सुरक्षित समाप्त होगा
        logging.info(f"पुराने मैसेज {message_id} का ब्लिंकिंग टास्क बंद कर दिया गया।")
    except Exception as error:
        logging.debug(f"Silenced worker error: {error}")

# start handler 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Broadcast ke liye Chat ID aur Type database me save karein
        chat_id = update.message.chat.id
        chat_type = update.message.chat.type

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # 'private' string check
        is_private = str(chat_type) == "private" or (hasattr(chat_type, "value") and chat_type.value == "private")
        
        if is_private:
            cursor.execute("INSERT OR IGNORE INTO broadcast_users (chat_id) VALUES (?)", (chat_id,))
        else:
            cursor.execute("INSERT OR IGNORE INTO broadcast_groups (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        conn.close()

        # 🔥 SMART OLD BUTTONS CLEANUP (सुरक्षित अनुक्रम सुधार के साथ)
        if not is_private:
            if chat_id in GROUP_GAMES:
                game = GROUP_GAMES[chat_id]
                
                # 1. Purane Welcome Message ke buttons remove karein
                if "welcome_message_id" in game:
                    try: await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=game["welcome_message_id"], reply_markup=None)
                    except Exception: pass
                
                # 2. 🛑 मुख्य सुधार: बटन हाइड करने से पहले पुराने कलर चेंजिंग फंक्शन को क्लोज करें
                if "setup_message_id" in game:
                    try:
                        # स्टेप A: पुराने चल रहे ब्लिंकिंग लूप को तुरंत फ़ोर्स क्लोज़ (Cancel) करें
                        old_blink_task = game.get("blink_task")
                        if old_blink_task and not old_blink_task.done():
                            old_blink_task.cancel()
                            await asyncio.sleep(0.1) # पायथन को लूप बंद करने के लिए 100 मिलीसेकंड का समय दें
                            logging.info("पुराने पैनल का ब्लिंकिंग फंक्शन सफलतापूर्वक बंद हुआ।")

                        # स्टेप B: अब जब लूप पूरी तरह मर चुका है, तो सुरक्षित रूप से बटन हाइड करें
                        await context.bot.edit_message_reply_markup(
                            chat_id=chat_id, 
                            message_id=game["setup_message_id"], 
                            reply_markup=None
                        )
                    except Exception as e: 
                        logging.debug(f"Handling old panel clearance error: {e}")

                # 3. Agar koi purana pause message chal raha hai toh uske buttons bhee remove karein
                if "pause_message_id" in game:
                    try: await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=game["pause_message_id"], reply_markup=None)
                    except Exception: pass
        # ✅ FIXED: Extracting the first element [0] index to stop the List AttributeError
        if context.args and len(context.args) > 0:
            first_arg = context.args[0]  # 🛑 मुख्य सुधार: लिस्ट से असली स्ट्रिंग टेक्स्ट को बाहर निकाला!
            
            if first_arg.startswith("quiz_"):
                if not is_private and chat_id in GROUP_GAMES:
                    # यदि मेमोरी पूरी तरह साफ़ कर रहे हैं, तो पुराने टास्क को भी बंद करें
                    old_task = GROUP_GAMES[chat_id].get("blink_task")
                    if old_task and not old_task.done():
                        old_task.cancel()
                    GROUP_GAMES.pop(chat_id, None)

                parts = first_arg.split("_")
                if len(parts) < 2:
                    await update.message.reply_text("❌ Invalid quiz link.")
                    return
                
                try:
                    quiz_id = int(parts[1]) # 🛑 सुधारे गए पार्ट्स से आईडी पार्स करना
                except ValueError:
                    await update.message.reply_text("❌ Invalid quiz ID format.")
                    return
                
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT title, description, timer, negative_value FROM quizzes WHERE quiz_id = ?", (quiz_id,))
                quiz_data = cursor.fetchone()
                
                cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
                total_q_data = cursor.fetchone()
                total_q = total_q_data[0] if total_q_data else 0  # 🛑 टुपल से सही वैल्यू अनपैक करना
                conn.close()
                
                if not quiz_data:
                    await update.message.reply_text("❌ Quiz data not found.")
                    return

                title, desc, timer, negative_value = quiz_data
                time_disp = f"{timer} sec" if timer < 60 else f"{timer // 60} min"
                db_neg_val = negative_value if negative_value is not None else 0.0
                
                if not is_private and chat_id in GROUP_GAMES and GROUP_GAMES[chat_id].get("quiz_started"):
                    await update.message.reply_text(
                        "⚠️ A quiz is already running in this group. Please use /stop or wait for the current quiz results before starting a new quiz."
                    )
                    return

                init_text = (
                    f"<blockquote><ins><b>🎲 Get ready for the quiz!</b></ins></blockquote>\n\n"
                    f"<blockquote>📚 Title: {escape_markdown(title)}</blockquote>\n"
                    f"<blockquote>🔥 Description: {escape_markdown(desc) if desc else 'No description'}</blockquote>\n"
                    f"<blockquote>🖊️ Questions: {total_q}</blockquote>\n"
                    f"<blockquote>⏱ Time per question: {time_disp}</blockquote>\n"
                    f"<blockquote>📉 Negative Marking: `-{db_neg_val} Marks` per wrong answer</blockquote>\n\n"
                    "🏁 Click <b>'I am ready!'</b> to start the quiz.\n"
                    "The quiz will begin when at least 2 people are ready to play. Send /stop to stop it."
                )
                
                live_text = init_text + "\n👥 <b>Ready Players:</b> 0"
                
                raw_button = {
                    "text": "I am ready!",
                    "callback_data": f"ready_{quiz_id}",
                    "style": "primary"  
                }
                kb = [[raw_button]]
                
                quiz_panel_msg = await update.message.reply_text(
                    live_text, 
                    reply_markup=InlineKeyboardMarkup(kb), 
                    parse_mode="HTML"
                )
                
                if not is_private:
                    if chat_id not in GROUP_GAMES:
                        GROUP_GAMES[chat_id] = {}
                    
                    GROUP_GAMES[chat_id].update({
                        "quiz_id": quiz_id,
                        "joined_users": {},
                        "current_q": 0,
                        "scores": {},
                        "poll_map": {},                 
                        "start_time": None,
                        "user_answers": {},
                        "question_start_times": {},
                        "ready_users": set(),
                        "quiz_started": False,
                        "poll_message_ids": {},
                        "setup_message_id": quiz_panel_msg.message_id,
                        "setup_panel_text": init_text,  
                        "is_private": False,
                        "quiz_paused": False,
                        "consecutive_no_answers": 0,
                        "previous_panel_message_id": None,
                        "start_lock": asyncio.Lock()
                    })

                # ब्लिंकिंग टास्क के हैंडल को मेमोरी (GROUP_GAMES) में सेव किया ताकि बाद में बंद कर सकें
                task_handle = asyncio.create_task(repeat_ready_button_color_worker(chat_id, quiz_panel_msg.message_id, quiz_id, context))
                if not is_private and chat_id in GROUP_GAMES:
                    GROUP_GAMES[chat_id]["blink_task"] = task_handle
                return

        # Welcome message text layout se pehle active quiz check
        if is_private and check_active_quiz_creation(update.message.from_user.id, context):
            await update.message.reply_text(
                "⚠️ **You have an unfinished quiz.** Please finish creating your quiz or send /cancel.\n\n"
                "You cannot start a new quiz or use other commands until you complete this one."
            )
            return

        # Welcome message text layout
        welcome_text = (
            "<blockquote><ins>👋 Welcome to Premium Quiz Bot!</ins></blockquote>\n\n"
            "Aap is bot se quizzes bana kar apne dosto ke sath groups me realtime khel sakte hain.\n\n"
            "💡 Check Available Commands:\n"
            "➤ /help – Open help center\n\n"
            "👥 Add the bot to a group and start quizzes\n"
            "🤖 Welcome to AI Auto-Quiz Generator Bot!\n\n"
            "<blockquote>⚡ Commands Layout:</blockquote>\n"
            "👉 `/autoquiz` - Naya AI Quiz generate karne ki step-by-step process shuru karein.\n"
            f"<tg-spoiler>📢 Owner Details: ID `{OWNER_ID}`</tg-spoiler>"
        )
        
        if is_private:
            kb = [
                [{"text": "🚀 Create New Quiz", "callback_data": "btn_newquiz", "style": "success"}],
                [{"text": "📚 View My Quizzes", "callback_data": "btn_viewquizzes", "style": "primary"}]
            ]
        else:
            bot_username = context.bot.username
            add_url = f"https://t.me/{bot_username}?startgroup=true"
            kb = [
                [{"text": "✨ Add me in your group", "url": add_url, "style": "primary"}]
            ]
        
        welcome_msg = await update.message.reply_text(
            welcome_text, 
            reply_markup=InlineKeyboardMarkup(kb), 
            parse_mode="HTML"
        )
        
        if not is_private:
            if chat_id not in GROUP_GAMES:
                GROUP_GAMES[chat_id] = {}
            GROUP_GAMES[chat_id]["welcome_message_id"] = welcome_msg.message_id

    except Exception as e:
        logging.error(f"Error in start: {e}", exc_info=True)  
        await update.message.reply_text("❌ An error occurred. Please try again with /start")

# Help command Handel
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.chat.id
        chat_type = update.message.chat.type
        is_private = str(chat_type) == "private" or (hasattr(chat_type, "value") and chat_type.value == "private")

        # Check for active quiz creation
        if check_active_quiz_creation(update.message.from_user.id, context):
            await update.message.reply_text(
                "⚠️ **You have an unfinished quiz.** Please finish creating your quiz or send /cancel.\n\n"
                "You cannot use commands until you complete this quiz."
            )
            return
        
        # 🔥 SMART OLD HELP BUTTONS CLEANUP
        if not is_private:
            if chat_id in GROUP_GAMES:
                game = GROUP_GAMES[chat_id]
                if "help_message_id" in game:
                    try:
                        await context.bot.edit_message_reply_markup(
                            chat_id=chat_id,
                            message_id=game["help_message_id"],
                            reply_markup=None
                        )
                    except Exception:
                        pass

        help_text = (
            "<blockquote><ins>Help Menu</ins></blockquote>\n\n"
            "Aap is bot se quizzes bana kar apne dosto ke sath groups me realtime khel sakte hain.\n\n"
            "<blockquote><ins>💡 Available Commands:</ins></blockquote>\n"
            "➤ /newquiz – Create a new quiz\n"
            "➤ /quizzes – View your quizzes\n"
            "➤ /start – Start the bot | quiz\n"
            "➤ /stop – Stop running quiz (admin)\n"
            "➤ /cancel – cancel old all activities\n\n"
            "👥 Add the bot to a group and start quizzes\n"
            "📢 For support, contact owner."
        )
        
        # Owner ka dynamic chat link layout (t.me/user?id=...)
        owner_link = f"tg://user?id={OWNER_ID}"
        owner_button = InlineKeyboardButton("📢 Contact Owner", url=owner_link)
        
        # 🔥 CHAT TYPE BASE PAR BUTTONS KA LOGIC
        if is_private:
            # Private chat: Total 3 Buttons (2 purane + 1 contact owner)
            keyboard = [
                [InlineKeyboardButton("Create New Quiz 🚀", callback_data="btn_newquiz")],
                [InlineKeyboardButton("View My Quizzes 📚", callback_data="btn_viewquizzes")],
                [owner_button]
            ]
        else:
            # Group: Total 2 Buttons (Add me + Contact owner)
            bot_username = context.bot.username
            add_url = f"https://t.me/{bot_username}?startgroup=true"
            keyboard = [
                [InlineKeyboardButton("➕ Add me in your group", url=add_url)],
                [owner_button]
            ]
            
        # Help message send karke use variable me liya
        help_msg = await update.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        
        # Agar ye group hai, toh help message ki ID save karein taaki agli baar ye delete ho sake
        if not is_private:
            if chat_id not in GROUP_GAMES:
                GROUP_GAMES[chat_id] = {}
            GROUP_GAMES[chat_id]["help_message_id"] = help_msg.message_id
            
    except Exception as e:
        logging.error(f"Error in help_command: {e}")

# ========================================
# 🔴 NEW COMMAND: /quizzes
# ========================================
# 1. मुख्य कमांड फ़ंक्शन
async def quizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display user's quizzes with a limit of 10 per page"""
    try:
        if update.effective_chat.type in ['group', 'supergroup']:
            await update.message.reply_text(
                "⚠️ यह कमांड केवल प्राइवेट चैट में काम करती है। कृपया मुझे पर्सनल मैसेज (DM) में `/quizzes` भेजें।"
            )
            return

        if check_active_quiz_creation(update.message.from_user.id, context):
            await update.message.reply_text(
                "⚠️ *You have an unfinished quiz.* Please finish creating your quiz or send /cancel.\n\n"
                "You cannot use commands until you complete this quiz."
            )
            return
        
        # डिफ़ॉल्ट रूप से पहला पेज (Page 0) लोड करें
        await send_quizzes_page(update, update.message.from_user.id, page=0, is_callback=False)

    except Exception as e:
        logging.error(f"Error in quizzes_command: {e}")
        if update.message:
            await update.message.reply_text("❌ Error loading quizzes. Please try again.")

# 2. पेज रेंडर करने वाला हेल्पर फ़ंक्शन (इसे आप अपने कोड में कहीं भी रख सकते हैं)
async def send_quizzes_page(update: Update, user_id: int, page: int, is_callback: bool = False):
    limit = 10  # एक पेज पर अधिकतम 10 क्विज़
    offset = page * limit

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # कुल क्विज़ की संख्या पता करें
    cursor.execute("SELECT COUNT(quiz_id) FROM quizzes WHERE creator_id = ?", (user_id,))
    total_quizzes = cursor.fetchone()[0]

    # सिर्फ इस पेज के 10 क्विज़ निकालें
    cursor.execute("""
        SELECT q.quiz_id, q.title, q.timer, COUNT(qu.id) as question_count
        FROM quizzes q
        LEFT JOIN questions qu ON q.quiz_id = qu.quiz_id
        WHERE q.creator_id = ?
        GROUP BY q.quiz_id
        ORDER BY q.quiz_id DESC
        LIMIT ? OFFSET ?
    """, (user_id, limit, offset))
    rows = cursor.fetchall()
    conn.close()

    if not rows and page == 0:
        keyboard = [[InlineKeyboardButton("Create New Quiz 🚀", callback_data="btn_newquiz")]]
        msg_text = "❌ Aapne abhi tak koi quiz nahi banaya hai!\n\nNaya quiz banane ke liye 'Create New Quiz' button click karein."
        if is_callback:
            await update.callback_query.edit_message_text(text=msg_text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(text=msg_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text = f"<blockquote>Aapke Banaye Huye Quizzes Page {page + 1}:</blockquote>\n\n"
    keyboard = []
    
    for idx, (qid, title, timer, q_count) in enumerate(rows, 1):
        display_idx = offset + idx 
        time_display = f"{timer}s" if timer < 60 else f"{timer // 60}m"

        # HTML ke liye html.escape use karein aur tags ko proper order me band karein
        safe_title = html.escape(title)
        
        # FIX: Sahi tag nesting -> <b> Index </b> <blockquote> Title </blockquote>
        text += f"<blockquote><b>{display_idx}. {safe_title}</b></blockquote>\n"
        text += f"🎯 <b>{q_count} Questions | ⏰ Time/Q. {time_display}</b>\n\n"
        
        # ओपन बटन
        open_button = InlineKeyboardButton(text=f"Open Quiz {display_idx}", callback_data=f"viewq_{qid}")
        keyboard.append([open_button])

    # ⬅️ ➡️ Navigation Buttons (Next / Back) + ❌ Close List का लॉजिक
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"quizpage_{page - 1}"))
    
    # हमेशा बीच में या अंत में 'Close' बटन रखें
    nav_row.append(InlineKeyboardButton("❌ Close List", callback_data="close_quiz_list"))
    
    if offset + limit < total_quizzes:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"quizpage_{page + 1}"))
        
    keyboard.append(nav_row)

    if is_callback:
        query = update.callback_query
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# 3. पेज बदलने वाले बटन का Callback Handler
async def quiz_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data.startswith("quizpage_"):
        next_page = int(data.split("_")[1])
        user_id = query.from_user.id
        # अगला या पिछला पेज लोड करें
        await send_quizzes_page(update, user_id, page=next_page, is_callback=True)

async def close_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the message when close button is clicked"""
    query = update.callback_query
    await query.answer()
    
    # बटन क्लिक होने पर उस पर्टिकुलर मेसेज को डिलीट कर देगा
    try:
        await query.message.delete()
    except Exception as e:
        logging.error(f"Error deleting message: {e}")

async def new_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        # 1. Get the chat and message object
        chat_obj = update.effective_chat
        msg_obj = update.callback_query.message if update.callback_query else update.message
        user_id = update.callback_query.from_user.id if update.callback_query else update.message.from_user.id
        
        # 2. Check if the command is used in a group or supergroup
        if chat_obj.type in ['group', 'supergroup']:
            if update.callback_query:
                await update.callback_query.answer("Not allowed here", show_alert=True)
            
            await msg_obj.reply_text(
                "⚠️ यह कमांड केवल प्राइवेट चैट में काम करती है। कृपया मुझे पर्सनल मैसेज (DM) में `/newquiz` भेजें।"
            )
            return ConversationHandler.END  # Stop the conversation handler inside groups

        # 3. Rest of your original code for private chat
        if update.callback_query:
            await update.callback_query.answer()
            
        await msg_obj.reply_text(
            "Let's create a new quiz. First, send me the title of your quiz (e.g., 'Aptitude Test' or '10 questions about bears').\n\n⚠️ Note: Title must be 128 characters or less.",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["quiz_build"] = {"title": "", "description": "", "questions": []}
        context.user_data["quiz_build_creator_id"] = user_id
        return TITLE
    except Exception as e:
        logging.error(f"Error in new_quiz_start: {e}")
        # Safeguard if msg_obj is available during an error
        if 'msg_obj' in locals():
            await msg_obj.reply_text("❌ An error occurred. Please try again with /newquiz")
        return ConversationHandler.END

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        title = update.message.text.strip()
        
        if len(title) > 128:
            await update.message.reply_text(
                "⚠️ This title is too long. Please send a new one, 128 characters max."
            )
            return TITLE
            
        # 🟢 SAFETY FIX: Ensure 'quiz_build' structure is alive
        if "quiz_build" not in context.user_data or not isinstance(context.user_data["quiz_build"], dict):
            context.user_data["quiz_build"] = {"title": "", "description": "", "questions": []}
        
        context.user_data["quiz_build"]["title"] = title
        await update.message.reply_text(
            "Good. Now send me a description of your quiz. This is optional, you can /skip this step.",
            reply_markup=ReplyKeyboardRemove()
        )
        return DESCRIPTION
    except Exception as e:
        logging.error(f"Error in receive_title: {e}")
        return TITLE

async def receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        text = update.message.text
        context.user_data["quiz_build"]["description"] = "" if text.lower() == "/skip" else text.strip()
        
        # ========================================
        # 🔴 SHOW BOTTOM CONTAINER (QUESTIONS STATE)
        # ========================================
        poll_button = KeyboardButton(
            text="Create a Question",
            request_poll=KeyboardButtonPollType(type="quiz")
        )
        bottom_container = ReplyKeyboardMarkup(
            [[poll_button]], 
            resize_keyboard=True,
            one_time_keyboard=False
        )
        
        await update.message.reply_text(
            f"Good. Your quiz '{context.user_data['quiz_build']['title']}' now has 0 questions.\n\n"
            "💡 Now send me a poll with your first question.\n\n"
            "Enable Quiz Mode, add 2-7 options, pick the correct one, and tap Create.\n\n"
            "Warning: this bot can't create anonymous poll\n"
             "Users in groups will see votes from other members.",
            reply_markup=bottom_container
        )
        return QUESTIONS
    except Exception as e:
        logging.error(f"Error in receive_desc: {e}")
        return DESCRIPTION

async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        poll = update.message.poll
        if poll.type != "quiz":
            await update.message.reply_text("❌ Kripya Quiz mode wala poll hi send karein:")
            return QUESTIONS
        if len(poll.options) > 7:
            await update.message.reply_text("❌ Maximum 7 options allowed. Re-send poll:")
            return QUESTIONS

        opts = [o.text for o in poll.options]
        # 🟢 FIXED: Store correct_option_id (integer index) instead of text
        q_data = {
            "text": poll.question, 
            "options": opts, 
            "correct": poll.correct_option_id,  # 🟢 INDEX, not text
            "explanation": poll.explanation if poll.explanation else "", 
            "pre_message": ""
        }
        context.user_data["quiz_build"]["questions"].append(q_data)
        context.user_data["current_question_index"] = len(context.user_data["quiz_build"]["questions"]) - 1
        
        await update.message.reply_text(
            f"✅ Question added! Your quiz now has {len(context.user_data['quiz_build']['questions'])} question.\n\n"
            "⚡ Quick options:\n"
            "➤ 📎 Send media | details (text, image, video, etc.) that will be add context\n"
            "➤ 📄 Send text message for pre-message\n\n"
            "💬 Optional:\n"
            "➤ ➕ Now Send the next question directly (auto-skips pre-message)\n"
            "➤ ⚠️ Quiz Finish karne ke liye Pre-message set kare!"
        )
        return PRE_MESSAGE
    except Exception as e:
        logging.error(f"Error in receive_poll: {e}")
        await update.message.reply_text("❌ Error processing poll. Please try again.")
        return QUESTIONS

async def receive_pre_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        current_idx = context.user_data.get("current_question_index", -1)
        
        if current_idx < 0 or current_idx >= len(context.user_data.get("quiz_build", {}).get("questions", [])):
            await update.message.reply_text("❌ Error: Question not found!")
            return QUESTIONS
        
        # Agar user PRE_MESSAGE state mein /undo likhta hai
        if update.message.text and update.message.text.strip().lower() == "/undo":
            return await handle_undo(update, context)
        
        # Check if a new poll is being sent - auto-skip pre-message
        if update.message.poll:
            # Auto-skip pre-message and process the new poll
            context.user_data["quiz_build"]["questions"][current_idx]["pre_message"] = ""
            
            # Process the new poll
            poll = update.message.poll
            if poll.type != "quiz":
                await update.message.reply_text("❌ Kripya Quiz mode wala poll hi send karein:")
                return PRE_MESSAGE
            if len(poll.options) > 7:
                await update.message.reply_text("❌ Maximum 7 options allowed. Re-send poll:")
                return PRE_MESSAGE

            opts = [o.text for o in poll.options]
            q_data = {
                "text": poll.question, "options": opts, "correct": opts[poll.correct_option_id],
                "explanation": poll.explanation if poll.explanation else "", "pre_message": ""
            }
            context.user_data["quiz_build"]["questions"].append(q_data)
            context.user_data["current_question_index"] = len(context.user_data["quiz_build"]["questions"]) - 1
            
            # 🟢 FIXED: Naya poll aane par purana bottom keyboard hide karne ke liye ReplyKeyboardRemove joda hai
            await update.message.reply_text(
                f"✅ Question added! Your quiz now has {len(context.user_data['quiz_build']['questions'])} question.\n\n"
                "⚡ Quick options:\n"
                "➤ 📎 Send media | details (text, image, video, etc.) that will be add context\n"
                "➤ 📄 Send text message for pre-message\n\n"
                "💬 Optional:\n"
                "➤ ➕ Now Send the next question directly (auto-skips pre-message)\n"
                "➤ ⚠️ Quiz Finish karne ke liye Pre-message set kare!",
                reply_markup=ReplyKeyboardRemove() # 👈 Isse bottom button temporary hide ho jayega jab tak pre-message bhej rahe ho
            )
            return PRE_MESSAGE
        
        # Handle /skip command
        if update.message.text and update.message.text.lower() == "/skip":
            context.user_data["quiz_build"]["questions"][current_idx]["pre_message"] = ""
        else:
            # Store text or media caption
            if update.message.text:
                context.user_data["quiz_build"]["questions"][current_idx]["pre_message"] = update.message.text.strip()
            elif update.message.caption:
                context.user_data["quiz_build"]["questions"][current_idx]["pre_message"] = update.message.caption.strip()
            else:
                context.user_data["quiz_build"]["questions"][current_idx]["pre_message"] = ""
        
        # ========================================
        # 🔴 SHOW BOTTOM CONTAINER (QUESTIONS STATE)
        # ========================================
        poll_button = KeyboardButton(
            text="Create a Question",
            request_poll=KeyboardButtonPollType(type="quiz")
        )
        bottom_container = ReplyKeyboardMarkup(
            [[poll_button]], 
            resize_keyboard=True,
            one_time_keyboard=False
        )
        
        await update.message.reply_text(
            f"✅ Pre-message set! Your quiz now has {len(context.user_data['quiz_build']['questions'])} question(s).\n\n"
            "💬 Next step:\n"
            "➤ Send next question poll\n"
            "✨ Or\n"
            "➤ type /done to finish quiz\n"
            "↩️ Galti se galat pre-message set ho gaya? Type /undo to delete it.",
            reply_markup=bottom_container
        )
        return QUESTIONS
    except Exception as e:
        logging.error(f"Error in receive_pre_message: {e}")
        return QUESTIONS
            
async def handle_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        quiz = context.user_data.get("quiz_build")
        if quiz and quiz["questions"]:
            quiz["questions"].pop()
            
            # ========================================
            # 🔴 KEEP BOTTOM CONTAINER (STILL IN QUESTIONS STATE)
            # ========================================
            poll_button = KeyboardButton(
                text="Create a Question",
                request_poll=KeyboardButtonPollType(type="quiz")
            )
            bottom_container = ReplyKeyboardMarkup(
                [[poll_button]], 
                resize_keyboard=True,
                one_time_keyboard=False
            )
            
            await update.message.reply_text(
                f"↩️ Last question removed! Quiz now has {len(quiz['questions'])} question(s).\n\nSend next question or /done.",
                reply_markup=bottom_container
            )
        else:
            await update.message.reply_text("❌ No questions to remove!")
        return QUESTIONS
    except Exception as e:
        logging.error(f"Error in handle_undo: {e}")
        return QUESTIONS

async def finish_quiz_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        quiz = context.user_data.get("quiz_build", {})
        if not quiz or not quiz.get("questions"):
            await update.message.reply_text("❌ Error: Quiz must have at least 1 question!")
            return QUESTIONS
        
        # ====================================================================
        # 🟢 FIXED: /done bhejte hi sabse pehle bottom question container ko hide karein
        # ====================================================================
        await update.message.reply_text(
            "⏳ Saving questions and closing creator panel...", 
            reply_markup=ReplyKeyboardRemove() # 👈 Isse "Create a Question" container permanently screen se hat jayega
        )
        
        # Inline Buttons for timer setup
        timer_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏱️ 15s", callback_data="timer_15"),
                InlineKeyboardButton("⏱️ 30s", callback_data="timer_30")
            ],
            [
                InlineKeyboardButton("⏱️ 40s", callback_data="timer_40"),
                InlineKeyboardButton("⏱️ 60s", callback_data="timer_60")
            ]
        ])
        
        # Ab timer select karne ke liye main options bhejenge
        await update.message.reply_text(
            "⏱️ Please set a time limit for questions:\n\n"
            "Select an option from the buttons below or type any of these: 15, 30, 40, 60\n\n"
            "Example: Type '30' for 30 seconds per question",
            reply_markup=timer_keyboard
        )
        return TIMER
    except Exception as e:
        logging.error(f"Error in finish_quiz_creation: {e}")
        return QUESTIONS
        
async def handle_timer_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            text = query.data.replace("timer_", "").strip()
            msg_target = query.message
        else:
            text = update.message.text.strip()
            msg_target = update.message

        time_map = {"15": 15, "30": 30, "40": 40, "60": 60}
        
        if text not in time_map:
            await msg_target.reply_text("❌ Invalid time. Please enter: 15, 30, 40, or 60")
            return TIMER
        
        # Timer value temporary save karein
        context.user_data["quiz_build"]["timer"] = time_map[text]
        
        if update.callback_query:
            await msg_target.edit_reply_markup(reply_markup=None)

        # 🔥 Custom buttons aapki demand ke mutabik: 0.25, 0.5, 1.0, 1.5
        neg_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ No Negative (0.0)", callback_data="neg_0.0"),
                InlineKeyboardButton("📉 1/4th (-0.25)", callback_data="neg_0.25")
            ],
            [
                InlineKeyboardButton("📉 Half (-0.5)", callback_data="neg_0.5"),
                InlineKeyboardButton("📉 Single (-1.0)", callback_data="neg_1.0")
            ],
            [
                InlineKeyboardButton("📉 Heavy (-1.5)", callback_data="neg_1.5")
            ]
        ])
        
        await msg_target.reply_text(
            "<blockquote>💌 Select Negative Marking Schema:</blockquote>\n\n"
            "Aap is quiz ke liye kitni negative marking set karna chahte hain? Niche diye gaye buttons se choose karein:",
            reply_markup=neg_keyboard,
            parse_mode="HTML"
        )
        return NEGATIVE 
    except Exception as e:
        logging.error(f"Error in handle_timer_text: {e}")
        return TIMER
        
async def view_my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches and displays all quizzes created by the user with 10 items per page"""
    try:
        query = update.callback_query
        # Check for active quiz creation
        if check_active_quiz_creation(query.from_user.id, context):
            await query.answer(
                "⚠️ You have an unfinished quiz. Please finish creating your quiz or send /cancel.",
                show_alert=True
            )
            return
        
        await query.answer()
        user_id = query.from_user.id
        
        # डिफ़ॉल्ट रूप से पहला पेज (Page 0) रेंडर करें
        await render_quizzes_page_callback(update, user_id, page=0)

    except Exception as e:
        logging.error(f"Error in view_my_quizzes: {e}")
        try:
            await update.callback_query.answer("❌ Error loading quizzes", show_alert=True)
        except Exception:
            pass

# 🆕 हेल्पर फ़ंक्शन: जो पेजिनेशन के साथ लिस्ट को एडिट या शो करेगा
async def render_quizzes_page_callback(update: Update, user_id: int, page: int):
    limit = 10  # एक पेज पर अधिकतम 10 क्विज़
    offset = page * limit
    query = update.callback_query

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # कुल क्विज़ की संख्या पता करें (Pagination के लिए)
    cursor.execute("SELECT COUNT(quiz_id) FROM quizzes WHERE creator_id = ?", (user_id,))
    total_quizzes = cursor.fetchone()[0]

    # सिर्फ इस पेज के 10 क्विज़ निकालें
    cursor.execute("""
        SELECT q.quiz_id, q.title, q.timer, COUNT(qu.id) as question_count
        FROM quizzes q
        LEFT JOIN questions qu ON q.quiz_id = qu.quiz_id
        WHERE q.creator_id = ?
        GROUP BY q.quiz_id
        ORDER BY q.quiz_id DESC
        LIMIT ? OFFSET ?
    """, (user_id, limit, offset))
    rows = cursor.fetchall()
    conn.close()

    if not rows and page == 0:
        keyboard = [[InlineKeyboardButton("Create New Quiz 🚀", callback_data="btn_newquiz")]]
        keyboard.append([InlineKeyboardButton("Back to Main Menu 🔙", callback_data="back_main")])
        await query.edit_message_text(
            text="❌ Aapne abhi tak koi quiz nahi banaya hai!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # मेसेज का हेडर टेक्स्ट
    text = f"<blockquote>Aapke Banaye Huye Quizzes Page {page + 1}:</blockquote>\n\n"
    keyboard = []
    
    for idx, (qid, title, timer, q_count) in enumerate(rows, 1):
        display_idx = offset + idx 
        time_display = f"{timer}s" if timer < 60 else f"{timer // 60}m"
        
        # HTML ke liye html.escape use karein aur tags ko proper order me band karein
        safe_title = html.escape(title)
        
        # FIX: Sahi tag nesting -> <b> Index </b> <blockquote> Title </blockquote>
        text += f"<blockquote><b>{display_idx}. {safe_title}</b></blockquote>\n"
        text += f"🎯 <b>{q_count} Questions | ⏰ Time/Q. {time_display}</b>\n\n"
        
        # ओपन बटन (हर क्विज़ के लिए सिंगल रो में)
        open_button = InlineKeyboardButton(text=f"Open Quiz {display_idx}", callback_data=f"viewq_{qid}")
        keyboard.append([open_button])

    # ⬅️ ➡️ Navigation Buttons + ❌ Close List का लॉजिक
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Back", callback_data=f"quizpage_{page - 1}"))
    
    # बीच में क्लोज लिस्ट का बटन
    nav_row.append(InlineKeyboardButton("❌ Close List", callback_data="close_quiz_list"))
    
    if offset + limit < total_quizzes:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"quizpage_{page + 1}"))
        
    keyboard.append(nav_row)
    
    # बैक टू मेन मेन्यू बटन हमेशा सबसे नीचे रहेगा
    keyboard.append([InlineKeyboardButton("Back to Main Menu 🔙", callback_data="back_main")])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# 🛠️ संशोधित हैंडलर (ताकि ओपन होने पर पुरानी लिस्ट डिलीट न हो)
async def handle_view_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles opening summary panel from the quiz list without deleting the list"""
    query = update.callback_query
    try:
        # Callback query format validation (Bug #6 Fix)
        if not query.data or "_" not in query.data:
            await query.answer("❌ Invalid callback data format", show_alert=True)
            return
            
        parts = query.data.split("_")
        if len(parts) < 2:
            await query.answer("❌ Invalid callback data format", show_alert=True)
            return

        # Quiz ID parse aur check
        try:
            quiz_id = int(parts[1])
        except (ValueError, IndexError):
            await query.answer("❌ Invalid quiz ID format", show_alert=True)
            return

        # Sab sahi hone par process aage badhayenge
        await query.answer()
        
        # 🚫 REMOVED: await query.message.delete() को यहाँ से हटा दिया गया है 
        # ताकि समरी पैनल खुलते ही पुरानी लिस्ट गायब न हो।

        # सीधे समरी पैनल को लोड करें (यह एक नया मैसेज भेजेगा, पुराना डिलीट नहीं करेगा)
        await show_summary_panel(query, context, quiz_id)

    except Exception as e:
        logging.error(f"Error in handle_view_quiz_callback: {e}")
        try:
            await query.answer("❌ Error loading quiz", show_alert=True)
        except Exception:
            pass

            
async def show_summary_panel(query, context, quiz_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT title, description, timer FROM quizzes WHERE quiz_id = ?", (quiz_id,))
        quiz_data = cursor.fetchone()
        
        if not quiz_data:
            await query.message.reply_text("❌ Error: Quiz data could not be retrieved.")
            conn.close()
            return
        
        title, description, timer = quiz_data
        cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
        total_q = cursor.fetchone()
        conn.close()

        time_display = f"{timer} sec" if timer < 60 else f"{timer // 60} min"
        bot_username = context.bot.username if context.bot.username else "quiz_bot"
        escaped_title = escape_markdown(title)
        escaped_desc = escape_markdown(description) if description else "No description"
        
        summary_text = (
            "<blockquote>👍 Here's your quiz:</blockquote>\n\n"
            f"💌 <b>Title:</b> {escaped_title}\n"
            f"🫥 <b>Description:</b> {escaped_desc}\n"
            f"⚡ {total_q[0]} questions · ⏱ Time: {time_display}\n\n"
            f"<blockquote>🔗 External sharing link:</blockquote>\n"
            f"<tg-spoiler>https://t.me/{bot_username}?start=quiz_{quiz_id}</tg-spoiler>"
        )
        
        inline_keyboard = [
            [InlineKeyboardButton("Start quiz in Private Chat", callback_data=f"startprivate_{quiz_id}")],
            [InlineKeyboardButton("Start quiz in Group", url=f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}")],
            [InlineKeyboardButton("Share Quiz", switch_inline_query=f"quiz_{quiz_id}")],
            [InlineKeyboardButton("⚙️ Edit", callback_data=f"edit_{quiz_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        await query.message.reply_text(summary_text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error in show_summary_panel: {e}")
        await query.message.reply_text(f"❌ Error: {str(e)}")

async def show_summary_panel_text(update, context, quiz_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT title, description, timer FROM quizzes WHERE quiz_id = ?", (quiz_id,))
        quiz_data = cursor.fetchone()
        
        if not quiz_data:
            await update.message.reply_text("❌ Error: Quiz data could not be retrieved.")
            conn.close()
            return
        
        title, description, timer = quiz_data
        cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
        total_q = cursor.fetchone()
        conn.close()

        time_display = f"{timer} sec" if timer < 60 else f"{timer // 60} min"
        bot_username = context.bot.username if context.bot.username else "quiz_bot"
        escaped_title = escape_markdown(title)
        escaped_desc = escape_markdown(description) if description else "No description"
        
        summary_text = (
            "<blockquote>🏁 Here's your quiz:</blockquote>\n\n"
            f"📒 <b>Title:</b> {escaped_title}\n"
            f"🫥 <b>Description:</b> {escaped_desc}\n"
            f"⚡ {total_q[0]} questions · ⏱ Time: {time_display}\n\n"
            f"<blockquote>🔗 External sharing link:</blockquote>\n"
            f"<tg-spoiler>https://t.me/{bot_username}?start=quiz_{quiz_id}</tg-spoiler>"
        )
        
        inline_keyboard = [
            [InlineKeyboardButton("Start Private Chat", callback_data=f"startprivate_{quiz_id}")],
            [InlineKeyboardButton("Start Quiz in Group", url=f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}")],
            [InlineKeyboardButton("Share Quiz", switch_inline_query=f"quiz_{quiz_id}")],
            [InlineKeyboardButton("⚙️ Edit", callback_data=f"edit_{quiz_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(inline_keyboard)
        await update.message.reply_text(summary_text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error in show_summary_panel_text: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_start_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private chat quiz start - requires only 1 user"""
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        
        await query.edit_message_text(
            text="🎮 Private Mode\n\nAap akele is quiz ko start karne ke liye ready ho gaye?\n\nClick 'Confirm' to begin!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Start", callback_data=f"confirm_private_{quiz_id}")]
            ])
        )
    except Exception as e:
        logging.error(f"Error in handle_start_private: {e}")
        await query.answer("❌ Error", show_alert=True)

async def handle_negative_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        query = update.callback_query
        await query.answer()
        
        neg_val = float(query.data.replace("neg_", "").strip())
        quiz = context.user_data.get("quiz_build", {})
        user_id = context.user_data.get("quiz_build_creator_id")
        
        if not quiz or not quiz.get("title"):
            await query.message.reply_text("❌ Error: Quiz data missing. Start over with /newquiz")
            return ConversationHandler.END

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # negative_value column ke sath data insert kiya
        cursor.execute(
            "INSERT INTO quizzes (creator_id, title, description, timer, negative_value) VALUES (?, ?, ?, ?, ?)", 
            (user_id, quiz["title"], quiz["description"], quiz["timer"], neg_val)
        )
        qid = cursor.lastrowid
        
        for q in quiz["questions"]:
            cursor.execute(
                "INSERT INTO questions (quiz_id, question_text, options, correct_answer, explanation, pre_message) VALUES (?, ?, ?, ?, ?, ?)", 
                (qid, q["text"], json.dumps(q["options"]), q["correct"], q["explanation"], q["pre_message"])
            )
        conn.commit()
        conn.close()
        
        context.user_data.pop("quiz_build", None)
        context.user_data.pop("quiz_build_creator_id", None)
        
        await query.message.edit_reply_markup(reply_markup=None)
        
        neg_display = "Disabled" if neg_val == 0.0 else f"-{neg_val} per wrong answer"
        await query.message.reply_text(f"✅ Quiz Created Successfully!\n⏱ Timer: {quiz['timer']}s\n📉 Negative Marking: {neg_display}")
        
        await show_summary_panel_text(query, context, qid)
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in handle_negative_selection: {e}")
        return ConversationHandler.END
    
async def handle_confirm_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and start private quiz with 1 user"""
    try:
        query = update.callback_query
        chat_id = query.message.chat_id
        user_id = query.from_user.id
        quiz_id = int(query.data.split("_")[2])
        
        await query.answer("🚀 Quiz shuru ho rahi hai!")
        await query.edit_message_text("⏳ Quiz loading... Please wait!")
        
        if chat_id not in GROUP_GAMES:
            GROUP_GAMES[chat_id] = {
                "quiz_id": quiz_id,
                "joined_users": {user_id: query.from_user.first_name or "Player"},
                "current_q": 0,
                "scores": {user_id: {"score": 0, "total_time": 0.0}},
                "poll_map": {},
                "start_time": None,
                "user_answers": {user_id: {}},
                "question_start_times": {},
                "ready_users": {user_id},
                "quiz_started": True,
                "poll_message_ids": {},
                "setup_message_id": None,
                "is_private": True,
                "quiz_paused": False,
                "consecutive_no_answers": 0
            }
        
        await asyncio.sleep(1)
        asyncio.create_task(send_next_group_poll(chat_id, context))
    except Exception as e:
        logging.error(f"Error in handle_confirm_private: {e}")
        await query.answer("❌ Error starting quiz", show_alert=True)

async def handle_quiz_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show quiz status/statistics"""
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT title, description, timer FROM quizzes WHERE quiz_id = ?", (quiz_id,))
        quiz_data = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
        total_q = cursor.fetchone()
        conn.close()
        
        if not quiz_data:
            await query.edit_message_text(text="❌ Quiz not found!")
            return
        
        title, description, timer = quiz_data
        time_display = f"{timer} sec" if timer < 60 else f"{timer // 60} min"
        
        # 🌟 FIX: Markdown double asterisks (**) ko title string ke dono taraf sahi lagaya hai
        status_text = (
            f"📊 *Quiz Status*\n\n"
            f"*Title:* {escape_markdown(title)}\n"
            f"*Description:* {escape_markdown(description) if description else 'No description'}\n"
            f"*Total Questions:* {total_q[0]}\n"
            f"*Time per Q:* {time_display}\n"
            f"✅ *Status:* Active"
        )
        
        await query.edit_message_text(
            text=status_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"backto_{quiz_id}")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error in handle_quiz_status: {e}")
        await query.answer("❌ Error", show_alert=True)
        
async def edit_quiz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        
        keyboard = [
            [InlineKeyboardButton("Edit Question 📖", callback_data=f"edquestion_{quiz_id}")],
            [InlineKeyboardButton("Edit Title 📝", callback_data=f"edtitle_{quiz_id}")],
            [InlineKeyboardButton("Edit Description ℹ️", callback_data=f"eddesc_{quiz_id}")],
            [InlineKeyboardButton("Edit Timer ⏱", callback_data=f"edtime_{quiz_id}")],
            [InlineKeyboardButton("Edit Negative Marking 📉", callback_data=f"edneg_{quiz_id}")], # 👈 Yeh naya button joda hai
            [InlineKeyboardButton("Back 🔙", callback_data=f"backto_{quiz_id}")]
        ]
        await query.edit_message_text(
            text="⚙️ **Edit Quiz Menu**\n\nAap is quiz ka kya badalna chahte hain? Niche se chunyein:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error in edit_quiz_menu: {e}")
        await query.answer("❌ Error", show_alert=True)
        
async def back_to_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        await query.message.delete()
        await show_summary_panel(query, context, quiz_id)
    except Exception as e:
        logging.error(f"Error in back_to_summary: {e}")

# ==========================================
# ⚙️ FULLY OPERATIONAL QUIZ EDITOR HANDLERS
# ==========================================

async def edit_question_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of questions to edit - 2 buttons per row"""
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, question_text FROM questions WHERE quiz_id = ?", (quiz_id,))
        questions = cursor.fetchall()
        conn.close()
        
        if not questions:
            await query.edit_message_text(
                text="❌ No questions found in this quiz!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"edit_{quiz_id}")]])
            )
            return
        
        text = "📚 Select a question to edit:\n\n"
        keyboard = []
        
        for idx, (q_id, q_text) in enumerate(questions, 1):
            # Truncate long question text for display
            display_text = q_text[:30] + "..." if len(q_text) > 30 else q_text
            text += f"{idx}. {escape_markdown(display_text)}\n"
            # Add button - 2 per row
            if len(keyboard) == 0 or len(keyboard[-1]) == 2:
                keyboard.append([])
            keyboard[-1].append(InlineKeyboardButton(f"Q{idx}", callback_data=f"editq_{quiz_id}_{q_id}"))
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"edit_{quiz_id}")])
        
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error in edit_question_trigger: {e}")
        await query.answer("❌ Error", show_alert=True)

async def show_question_detail_panel(query, context, quiz_id, question_id):
    """Display complete question preview with all action buttons - 1 per row"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, question_text, options, correct_answer, explanation, pre_message FROM questions WHERE id = ? AND quiz_id = ?", (question_id, quiz_id))
        q_data = cursor.fetchone()
        
        # Get question number
        cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ? AND id < ?", (quiz_id, question_id))
        q_number = cursor.fetchone()[0] + 1
        
        conn.close()
        
        if not q_data:
            await query.answer("❌ Question not found!", show_alert=True)
            return
        
        q_id, q_text, options_json, correct_ans, explanation, pre_message = q_data
        options = json.loads(options_json)
        
        # Build detailed preview message
        detail_text = f"❓ **Question #{q_number}** (Current Status: Active)\n\n"
        detail_text += f"**Question Text:** {escape_markdown(q_text)}\n\n"
        
        detail_text += "👇 Options:\n"
        for idx, opt in enumerate(options, 1):
            status = "✅" if opt == correct_ans else "❌"
            detail_text += f"• {status} {escape_markdown(opt)}"
            if opt == correct_ans:
                detail_text += " (Correct Answer)"
            detail_text += "\n"
        
        detail_text += f"\n⏱️ Timer: 30 seconds\n"
        
        if pre_message:
            detail_text += f"\n💌 Pre-message: {escape_markdown(pre_message)}\n"
        else:
            detail_text += f"\n💌 Pre-message: None set\n"
        
        if explanation:
            detail_text += f"\n📖 Explanation: {escape_markdown(explanation)}\n"
        else:
            detail_text += f"\n📖 Explanation: None set\n"
        
        # Build action buttons - 1 per row (ek ke niche ek)
        keyboard = [
            [InlineKeyboardButton("Pre-message", callback_data=f"editpre_{quiz_id}_{q_id}")],
            [InlineKeyboardButton("Explanation", callback_data=f"editexpl_{quiz_id}_{q_id}")],
            [InlineKeyboardButton("Delete Question", callback_data=f"delq_{quiz_id}_{q_id}")],
            [InlineKeyboardButton("Replace Question", callback_data=f"replaceq_{quiz_id}_{q_id}")],
            [InlineKeyboardButton("Back to Questions List", callback_data=f"edquestion_{quiz_id}")]
        ]
        
        await query.edit_message_text(
            text=detail_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error in show_question_detail_panel: {e}")
        await query.answer("❌ Error loading question details", show_alert=True)

async def handle_question_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle click on specific question to show detail panel"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Parse: editq_quiz_id_question_id
        parts = query.data.split("_")
        quiz_id = int(parts[1])
        question_id = int(parts[2])
        
        await show_question_detail_panel(query, context, quiz_id, question_id)
    except Exception as e:
        logging.error(f"Error in handle_question_detail: {e}")
        await query.answer("❌ Error", show_alert=True)

async def edit_pre_message_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start conversation to edit pre-message"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Parse: editpre_quiz_id_question_id
        parts = query.data.split("_")
        quiz_id = int(parts[1])
        question_id = int(parts[2])
        
        context.user_data["editing_q_id"] = question_id
        context.user_data["editing_quiz_id"] = quiz_id
        
        await query.message.reply_text(
            "💬 *Send the pre-message content* (text, caption, etc.) that will appear before this question.\n\n"
            "Or type /remove to delete the existing pre-message, /skip to cancel."
        )
        return EDIT_QUESTION_PRE_MESSAGE
    except Exception as e:
        logging.error(f"Error in edit_pre_message_trigger: {e}")
        return ConversationHandler.END

async def save_pre_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save edited pre-message"""
    try:
        q_id = context.user_data.get("editing_q_id")
        quiz_id = context.user_data.get("editing_quiz_id")
        text = update.message.text.strip()
        
        if not q_id or not quiz_id:
            await update.message.reply_text("❌ Error: Session expired.")
            return ConversationHandler.END
        
        # Handle /remove or /skip commands
        if text.lower() == "/remove":
            new_pre_msg = ""
        elif text.lower() == "/skip":
            context.user_data.pop("editing_q_id", None)
            context.user_data.pop("editing_quiz_id", None)
            await update.message.reply_text("❌ Cancelled.")
            return ConversationHandler.END
        else:
            new_pre_msg = text
        
        # Update database
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE questions SET pre_message = ? WHERE id = ?", (new_pre_msg, q_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop("editing_q_id", None)
        context.user_data.pop("editing_quiz_id", None)
        
        await update.message.reply_text("✅ Pre-message updated successfully!")
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in save_pre_message: {e}")
        await update.message.reply_text("❌ Error saving pre-message.")
        return ConversationHandler.END

async def edit_explanation_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start conversation to edit explanation"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Parse: editexpl_quiz_id_question_id
        parts = query.data.split("_")
        quiz_id = int(parts[1])
        question_id = int(parts[2])
        
        context.user_data["editing_q_id"] = question_id
        context.user_data["editing_quiz_id"] = quiz_id
        
        await query.message.reply_text(
            "📖 *Send the explanation* for the correct answer.\n\n"
            "Or type /remove to delete the existing explanation, /skip to cancel."
        )
        return EDIT_QUESTION_EXPLANATION
    except Exception as e:
        logging.error(f"Error in edit_explanation_trigger: {e}")
        return ConversationHandler.END

async def save_explanation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save edited explanation"""
    try:
        q_id = context.user_data.get("editing_q_id")
        quiz_id = context.user_data.get("editing_quiz_id")
        text = update.message.text.strip()
        
        if not q_id or not quiz_id:
            await update.message.reply_text("❌ Error: Session expired.")
            return ConversationHandler.END
        
        # Handle /remove or /skip commands
        if text.lower() == "/remove":
            new_explanation = ""
        elif text.lower() == "/skip":
            context.user_data.pop("editing_q_id", None)
            context.user_data.pop("editing_quiz_id", None)
            await update.message.reply_text("❌ Cancelled.")
            return ConversationHandler.END
        else:
            new_explanation = text
        
        # Update database
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE questions SET explanation = ? WHERE id = ?", (new_explanation, q_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop("editing_q_id", None)
        context.user_data.pop("editing_quiz_id", None)
        
        await update.message.reply_text("✅ Explanation updated successfully!")
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in save_explanation: {e}")
        await update.message.reply_text("❌ Error saving explanation.")
        return ConversationHandler.END

async def handle_delete_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a question with confirmation"""
    try:
        query = update.callback_query
        
        # Parse: delq_quiz_id_question_id
        parts = query.data.split("_")
        quiz_id = int(parts[1])
        question_id = int(parts[2])
        
        # Show confirmation
        await query.edit_message_text(
            text="⚠️ Are you sure you want to delete this question?\n\n"
                 "This action cannot be undone!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirmdel_{quiz_id}_{question_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"editq_{quiz_id}_{question_id}")]
            ])
        )
        await query.answer()
    except Exception as e:
        logging.error(f"Error in handle_delete_question: {e}")
        await query.answer("❌ Error", show_alert=True)

async def confirm_delete_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and execute question deletion"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Parse: confirmdel_quiz_id_question_id
        parts = query.data.split("_")
        quiz_id = int(parts[1])
        question_id = int(parts[2])
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(
            text="✅ Question deleted successfully!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Questions", callback_data=f"edquestion_{quiz_id}")]])
        )
    except Exception as e:
        logging.error(f"Error in confirm_delete_question: {e}")
        await query.answer("❌ Error deleting question", show_alert=True)

async def edit_title_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        context.user_data["editing_quiz_id"] = quiz_id
        await query.message.reply_text("📝 Please send the **new title** for your quiz:\n\n⚠️ Note: Title must be 128 characters or less.")
        return EDIT_TITLE
    except Exception as e:
        logging.error(f"Error in edit_title_trigger: {e}")
        return ConversationHandler.END

async def save_edited_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_title = update.message.text.strip()
        quiz_id = context.user_data.get("editing_quiz_id")
        
        # 🔴 NEW: Check if title exceeds 128 characters
        if len(new_title) > 128:
            await update.message.reply_text(
                "⚠️ This title is too long. Please send a new one, 128 characters max."
            )
            return EDIT_TITLE
        
        if not quiz_id:
            await update.message.reply_text("❌ Error: Session expired. Restart using menu.")
            return ConversationHandler.END
            
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE quizzes SET title = ? WHERE quiz_id = ?", (new_title, quiz_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop("editing_quiz_id", None)
        await update.message.reply_text("✅ Quiz title successfully updated!")
        await show_summary_panel_text(update, context, quiz_id)
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in save_edited_title: {e}")
        await update.message.reply_text("❌ Error updating title. Please try again.")
        return ConversationHandler.END

async def edit_desc_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        context.user_data["editing_quiz_id"] = quiz_id
        await query.message.reply_text("ℹ️ Please send the **new description** for your quiz (or type /skip to remove it):")
        return EDIT_DESC
    except Exception as e:
        logging.error(f"Error in edit_desc_trigger: {e}")
        return ConversationHandler.END

async def save_edited_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        text = update.message.text.strip()
        new_desc = "" if text.lower() == "/skip" else text
        quiz_id = context.user_data.get("editing_quiz_id")
        
        if not quiz_id:
            await update.message.reply_text("❌ Error: Session expired.")
            return ConversationHandler.END
            
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE quizzes SET description = ? WHERE quiz_id = ?", (new_desc, quiz_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop("editing_quiz_id", None)
        await update.message.reply_text("✅ Quiz description successfully updated!")
        await show_summary_panel_text(update, context, quiz_id)
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in save_edited_desc: {e}")
        await update.message.reply_text("❌ Error updating description. Please try again.")
        return ConversationHandler.END

async def edit_timer_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        context.user_data["editing_quiz_id"] = quiz_id
        await query.message.reply_text("⏱ Please enter the new per-question timer limit: (15, 30, 40, or 60)")
        return EDIT_TIMER
    except Exception as e:
        logging.error(f"Error in edit_timer_trigger: {e}")
        return ConversationHandler.END

async def save_edited_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        text = update.message.text.strip()
        time_map = {"15": 15, "30": 30, "40": 40, "60": 60}
        
        if text not in time_map:
            await update.message.reply_text("❌ Invalid entry! Please type exactly 15, 30, 40, or 60:")
            return EDIT_TIMER
            
        quiz_id = context.user_data.get("editing_quiz_id")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE quizzes SET timer = ? WHERE quiz_id = ?", (time_map[text], quiz_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop("editing_quiz_id", None)
        await update.message.reply_text("✅ Quiz timer configuration updated!")
        await show_summary_panel_text(update, context, quiz_id)
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in save_edited_timer: {e}")
        await update.message.reply_text("❌ Error updating timer. Please try again.")
        return ConversationHandler.END

async def edit_negative_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show options to change negative marking from edit menu"""
    try:
        query = update.callback_query
        await query.answer()
        quiz_id = int(query.data.split("_")[1])
        context.user_data["editing_quiz_id"] = quiz_id
        
        neg_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ No Negative (0.0)", callback_data=f"updeneg_{quiz_id}_0.0"),
                InlineKeyboardButton("📉 1/4th (-0.25)", callback_data=f"updeneg_{quiz_id}_0.25")
            ],
            [
                InlineKeyboardButton("📉 Half (-0.5)", callback_data=f"updeneg_{quiz_id}_0.5"),
                InlineKeyboardButton("📉 Single (-1.0)", callback_data=f"updeneg_{quiz_id}_1.0")
            ],
            [
                InlineKeyboardButton("📉 Heavy (-1.5)", callback_data=f"updeneg_{quiz_id}_1.5")
            ],
            [InlineKeyboardButton("Back 🔙", callback_data=f"edit_{quiz_id}")]
        ])
        
        await query.message.reply_text(
            "⚙️ **Update Negative Marking:**\n\nAap is quiz ke liye kaunsa naya negative marking rule set karna chahte hain?",
            reply_markup=neg_keyboard,
            parse_mode="Markdown"
        )
        return EDIT_NEGATIVE
    except Exception as e:
        logging.error(f"Error in edit_negative_trigger: {e}")
        return ConversationHandler.END

async def save_edited_negative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the newly selected negative marking value to the database"""
    try:
        query = update.callback_query
        await query.answer()
        
        parts = query.data.split("_")
        quiz_id = int(parts[1])
        new_neg_val = float(parts[2])
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE quizzes SET negative_value = ? WHERE quiz_id = ?", (new_neg_val, quiz_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop("editing_quiz_id", None)
        await query.message.edit_reply_markup(reply_markup=None)
        
        neg_display = "Disabled" if new_neg_val == 0.0 else f"-{new_neg_val} per wrong answer"
        await query.message.reply_text(f"✅ Quiz configuration updated successfully!\n📉 New Negative Marking: {neg_display}")
        
        if hasattr(query, 'message') and query.message:
            await show_summary_panel(query, context, quiz_id)
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in save_edited_negative: {e}")
        return ConversationHandler.END


# ==========================================
# 🎯 SINGLE READY BUTTON DRIVEN ACTIVATION
# ==========================================
async def handle_ready_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-joins users and sets dynamic counter to verify activation benchmarks (race-safe)."""
    try:
        query = update.callback_query
        if not query or not query.message or not query.message.chat:
            logging.warning("handle_ready_click: invalid structure context mapping assets missing.")
            return

        chat_id = query.message.chat.id
        message_id = query.message.message_id
        user = query.from_user
        if not user:
            return

        user_id = user.id
        user_name = user.username or user.first_name or "Player"
        logging.info(f"handle_ready_click invoked: chat_id={chat_id} msg_id={message_id} user_id={user_id}")

        # Parse callback data safely
        data = query.data or ""
        parts = data.split("_")
        if len(parts) < 2:
            return

        try:
            quiz_id = int(parts[1])
        except Exception:
            return

        # Ensure a game state exists and normalize it
        game = GROUP_GAMES.get(chat_id)
        if game:
            try:
                old_qid = int(game.get("quiz_id", 0))
            except Exception:
                old_qid = None
            if (old_qid is not None and old_qid != quiz_id) and not game.get("quiz_started"):
                logging.info(f"Clearing stale GROUP_GAMES entry for chat {chat_id}")
                GROUP_GAMES.pop(chat_id, None)
                game = None

        if not game:
            # Create new in-memory game state
            GROUP_GAMES[chat_id] = {
                "quiz_id": quiz_id,
                "joined_users": {},
                "current_q": 0,
                "scores": {},
                "poll_map": {},
                "start_time": None,
                "user_answers": {},
                "question_start_times": {},
                "ready_users": set(),
                "quiz_started": False,
                "poll_message_ids": {},
                "setup_message_id": message_id,
                "setup_panel_text": query.message.text,
                "is_private": False,
                "quiz_paused": False,
                "consecutive_no_answers": 0,
                "previous_panel_message_id": None,
                "start_lock": asyncio.Lock()
            }
            game = GROUP_GAMES[chat_id]

        # Ensure keys + types mappings
        game.setdefault("joined_users", {})
        game.setdefault("scores", {})
        game.setdefault("user_answers", {})
        if not isinstance(game.get("ready_users"), set):
            game["ready_users"] = set(game.get("ready_users") or [])
        game.setdefault("poll_map", {})
        game.setdefault("poll_message_ids", {})
        game.setdefault("question_start_times", {})
        game.setdefault("quiz_started", False)
        game.setdefault("quiz_paused", False)
        
        if "start_lock" not in game or not isinstance(game["start_lock"], asyncio.Lock):
            game["start_lock"] = asyncio.Lock()

        if game.get("starting"):
            try: await query.answer("Quiz is starting, please wait...", show_alert=False)
            except Exception: pass
            return
        # If quiz already started, just add the user and ack
        if game.get("quiz_started"):
            if user_id not in game["joined_users"]:
                game["joined_users"][user_id] = f"@{user_name}" if user.username else user_name
                game["scores"][user_id] = {"score": 0, "total_time": 0.0, "wrong": 0, "points": 0.0}
                game["user_answers"][user_id] = {}
            game["ready_users"].add(user_id)
            try: await query.answer("Aapko chalte countdown me shaamil kar liya gaya hai! ⚡", show_alert=False)
            except Exception: pass
            return

        # Normal pre-start join
        if user_id not in game["joined_users"]:
            game["joined_users"][user_id] = f"@{user_name}" if user.username else user_name
            game["scores"][user_id] = {"score": 0, "total_time": 0.0, "wrong": 0, "points": 0.0}
            game["user_answers"][user_id] = {}

        game["ready_users"].add(user_id)
        ready_count = len(game["ready_users"])

        is_private_chat = str(query.message.chat.type) == "private"
        min_ready_required = 1 if is_private_chat else 2

        # ====================================================================
        # 🏁 QUIZ TARGET ACHIEVED: ATOMIC START & HIDE BUTTONS LOGIC
        # ====================================================================
        if ready_count >= min_ready_required and not game.get("quiz_started"):
            lock = game["start_lock"]
            game["starting"] = True
            try: await query.answer("🎯 Target achieved! Quiz start ho rahi hai...")
            except Exception: pass

            async with lock:
                if game.get("quiz_started"):
                    game.pop("starting", None)
                    return

                # 🔥 मुख्य सुधार: 'I am ready' बटन को स्क्रीन से तुरंत और परमानेंट हाइड (रिमूव) करें
                try: 
                    await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
                except Exception as e:
                    logging.warning(f"Primary button hide failed: {e}")

                if game.get("previous_panel_message_id"):
                    try: await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=game["previous_panel_message_id"], reply_markup=None)
                    except Exception: pass

                # काउंटडाउन एनिमेशन लूप
                try:
                    for count in ["🎲 The quiz is about to begin…", "3️⃣....", "2️⃣Ready...", "1️⃣ SET…", "Go..🚀"]:
                        cmsg = await context.bot.send_message(chat_id=chat_id, text=count)
                        await asyncio.sleep(1)
                        try: await context.bot.delete_message(chat_id=chat_id, message_id=cmsg.message_id)
                        except Exception: pass
                except Exception: pass

                # गेम फ्लैग को स्टार्ट मार्क करें
                game["current_q"] = 0
                game["quiz_started"] = True
                game.pop("starting", None)

                try: asyncio.create_task(send_next_group_poll(chat_id, context))
                except Exception: pass
            return

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 👥 LIVE COUNT: Update user count strictly inside message text
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if not game.get("quiz_started"):
            try:
                base_text = game.get("setup_panel_text")
                if not base_text:
                    base_text = query.message.text
                    if "\n👥 Ready Players:" in base_text:
                        base_text = base_text.split("\n👥 Ready Players:")
                    game["setup_panel_text"] = base_text

                updated_text = base_text + f"\n👥 <b>Ready Players:</b> {ready_count}"
                
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=updated_text,
                    reply_markup=query.message.reply_markup, # जब तक गेम शुरू नहीं होता, ब्लिंकिंग जारी रहेगी
                    parse_mode="HTML"
                )
            except Exception as e:
                if "message is not modified" in str(e).lower() or "bad request" in str(e).lower():
                    pass
                else:
                    logging.debug(f"Error updating panel text layout: {e}")

        try: await query.answer("Aapne confirmation register kar di! 👍")
        except Exception: pass

    except Exception as e:
        logging.exception(f"Unexpected error in handle_ready_click: {e}")

async def handle_pause_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz pause resume"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Parse: pausequiz_chat_id
        parts = query.data.split("_")
        chat_id = int(parts[1]) # Error fixed here
        
        if chat_id not in GROUP_GAMES:
            await query.answer("❌ Quiz not found", show_alert=True)
            return
        
        game = GROUP_GAMES[chat_id]
        game["quiz_paused"] = False
        game["consecutive_no_answers"] = 0
        
        # Safety line: ID tracking clear karein
        game.pop("pause_message_id", None)
        
        await query.edit_message_text(
            text="Quiz Resuming...\n\n🚀 Next question coming up!",
            reply_markup=InlineKeyboardMarkup([])
        )
        
        await asyncio.sleep(2)
        asyncio.create_task(send_next_group_poll(chat_id, context))
    except Exception as e:
        logging.error(f"Error in handle_pause_quiz: {e}")
        await query.answer("❌ Error", show_alert=True)
        
async def handle_stop_quiz_from_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz stop from pause menu"""
    query = update.callback_query
    try:
        await query.answer()
        
        # Parse: stopquiz_chat_id
        parts = query.data.split("_")
        chat_id = int(parts[1])
        
        if chat_id not in GROUP_GAMES:
            await query.answer("❌ Quiz not found", show_alert=True)
            return
            
        game = GROUP_GAMES[chat_id]
        
        # ⚡ फिक्स 1: बैकग्राउंड टाइमर/टास्क को तुरंत मारें (Cancel करें) ताकि अगला सवाल न आए
        if "current_task" in game and not game["current_task"].done():
            game["current_task"].cancel()
            logging.info(f"Quiz background task cancelled from pause menu for chat {chat_id}")
            
        # ⚡ फिक्स 2: अगर ग्रुप में कोई पोल खुला रह गया है, तो उसे तुरंत क्लोज करें
        current_q_idx = game.get("current_q", 0)
        poll_ids_dict = game.get("poll_message_ids", {})
        if current_q_idx in poll_ids_dict:
            try:
                await context.bot.stop_poll(chat_id=chat_id, message_id=poll_ids_dict[current_q_idx])
            except Exception:
                pass # अगर पोल पहले से बंद हो तो एरर न आए
        
        # Tracking clear karein
        game.pop("pause_message_id", None)
        
        await query.edit_message_text(
            text="❌ Quiz stopped!\n\n🏁 Final Result:",
            reply_markup=InlineKeyboardMarkup([])
        )
        
        await compile_group_leaderboard(chat_id, context)
        
        # ⚡ फिक्स 3: रिजल्ट दिखाने के बाद डेटा को मेमोरी से पूरी तरह डिलीट करें
        GROUP_GAMES.pop(chat_id, None)
        
    except Exception as e:
        logging.error(f"Error in handle_stop_quiz_from_pause: {e}", exc_info=True)
        await query.answer("❌ Error", show_alert=True)
        
async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the running quiz in group"""
    try:
        chat_id = update.effective_chat.id
        
        # Check if quiz is running in this chat
        if chat_id not in GROUP_GAMES:
            await update.message.reply_text("❌ Koi quiz is group me chal nahi rahi hai!")
            return
        
        game = GROUP_GAMES[chat_id]
        
        # Check if quiz has started
        if not game.get("quiz_started"):
            await update.message.reply_text("❌ Quiz abhi start hi nahi huya hai!")
            return
            
        # ⚡ फिक्स 1: बैकग्राउंड टाइमर/टास्क को तुरंत मारें (Cancel करें) ताकि अगला सवाल लोड न हो
        if "current_task" in game and not game["current_task"].done():
            game["current_task"].cancel()
            logging.info(f"Quiz background task cancelled via /stop for chat {chat_id}")
            
        # ⚡ फिक्स 2: ग्रुप में खुले हुए चालू पोल (Active Poll) को तुरंत बंद करें
        current_q_idx = game.get("current_q", 0)
        poll_ids_dict = game.get("poll_message_ids", {})
        if current_q_idx in poll_ids_dict:
            try:
                await context.bot.stop_poll(chat_id=chat_id, message_id=poll_ids_dict[current_q_idx])
            except Exception:
                pass # अगर पोल पहले से बंद हो तो क्रैश न हो
        
        # Stop the quiz and show leaderboard
        await update.message.reply_text("Quiz stop ho gaya! Final Result dikha raha hoon...")
        await compile_group_leaderboard(chat_id, context)
        
        # ⚡ फिक्स 3: लीडरबोर्ड दिखाने के बाद तुरंत डेटा हटा दें ताकि मेमोरी पूरी साफ हो जाए
        GROUP_GAMES.pop(chat_id, None)
        
    except Exception as e:
        logging.error(f"Error in stop_quiz: {e}", exc_info=True)
        await update.message.reply_text("❌ Error stopping quiz")
        
async def send_next_group_poll(chat_id, context):
    """Send the next quiz question as a poll to the group (Handles Question, Option & Explanation Limits)"""
    try:
        game = GROUP_GAMES.get(chat_id)
        if not game:
            logging.warning(f"Game not found for chat {chat_id}")
            return
        
        # Check if quiz is paused
        if game.get("quiz_paused"):
            logging.info(f"Quiz paused for chat {chat_id}")
            return
            
        quiz_id = game["quiz_id"]
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Get quiz details
        cursor.execute("SELECT title, timer, negative_value FROM quizzes WHERE quiz_id = ?", (quiz_id,))
        quiz_data = cursor.fetchone()
        if not quiz_data:
            logging.error(f"Quiz {quiz_id} not found")
            conn.close()
            return
        
        quiz_title, timer, negative_value = quiz_data
        
        # Get all questions
        cursor.execute("SELECT question_text, options, correct_answer, pre_message, explanation FROM questions WHERE quiz_id = ?", (quiz_id,))
        questions = cursor.fetchall()
        conn.close()
        
        # Check if all questions completed
        if game["current_q"] >= len(questions):
            # 🎯 सुरक्षा लॉक: फ़ाइनल रिज़ल्ट घोषित करने से पहले अगर कोई आखिरी लाइव रिज़ल्ट चैट में बचा है तो उसे डिलीट करें
            if "last_mid_leaderboard_id" in game and game["last_mid_leaderboard_id"]:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=game["last_mid_leaderboard_id"])
                except Exception:
                    pass
            
            await compile_group_leaderboard(chat_id, context)
            GROUP_GAMES.pop(chat_id, None)
            return

        q = questions[game["current_q"]]
        q_text, options_json, correct_ans, pre_msg, explanation = q
        options = json.loads(options_json)
        
        # 🟢 Convert correct_ans to INTEGER INDEX
        try:
            correct_idx = int(correct_ans)
            if correct_idx < 0 or correct_idx >= len(options):
                logging.warning(f"Q{game['current_q']}: Invalid index {correct_idx} for {len(options)} options")
                correct_idx = 0
            correct_option_text = options[correct_idx]
            logging.info(f"🎯 Q{game['current_q']}: correct_idx={correct_idx}, option='{correct_option_text}'")
        except (ValueError, TypeError):
            logging.warning(f"Q{game['current_q']}: correct_ans is string: {correct_ans}")
            try:
                correct_idx = options.index(str(correct_ans))
                logging.info(f"Converted '{correct_ans}' to index {correct_idx}")
            except ValueError:
                correct_idx = 0
                logging.warning(f"Could not find '{correct_ans}', using 0")
        
        # Send pre-message if exists
        if pre_msg:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"📢 Context: {pre_msg}")
                await asyncio.sleep(1)
            except Exception as e:
                logging.warning(f"Context message failed: {e}")

        # Check quiz is still active
        if chat_id not in GROUP_GAMES or GROUP_GAMES[chat_id].get("quiz_paused"):
            return

        game["question_start_times"][game["current_q"]] = datetime.now()
        game["start_time"] = datetime.now()
        
        # Clean explanation
        clean_explanation = explanation.strip() if explanation and str(explanation).strip() else None
        
        # 📊 EXPLANATION LIMIT CHECK (200 Chars Limit)
        if clean_explanation and len(clean_explanation) > 200:
            logging.warning(f"Q{game['current_q']} explanation exceeds 200 chars ({len(clean_explanation)}). Setting to None.")
            clean_explanation = None

        # 📊 CHARACTER LIMIT CHECK (For Question and Options)
        full_question_text = f"[{game['current_q'] + 1}/{len(questions)}] {q_text}"
        
        # Check if question text or any option is too long
        is_question_too_long = len(full_question_text) > 300
        is_any_option_too_long = any(len(str(opt)) > 100 for opt in options)
        
        # Fallback fields
        poll_question = full_question_text
        poll_options = options
        
        if is_question_too_long or is_any_option_too_long:
            logging.warning(f"Q{game['current_q']} exceeds Telegram limits. Using text fallback.")
            
            # 1. पूरा सवाल और ऑप्शंस चैट में नॉर्मल मैसेज की तरह भेजें
            fallback_text = f"<blockquote>📝 प्रश्न [{game['current_q'] + 1}/{len(questions)}]: {q_text}</blockquote>\n\n<blockquote>विकल्प (Options):</blockquote>\n"
            for i, opt in enumerate(options):
                fallback_text += f"<blockquote>{i+1}. {opt}</blockquote>\n"
                
            try:
                await context.bot.send_message(chat_id=chat_id, text=fallback_text, parse_mode="HTML")
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Failed to send fallback message text: {e}")
            
            # 2. पोल के लिए डेटा छोटा करें
            poll_question = f"[{game['current_q'] + 1}/{len(questions)}] ऊपर दिए गए प्रश्न का सही उत्तर चुनें:"
            poll_options = [f"Option {i+1}" for i in range(len(options))]

        # Send poll with retry
        poll_msg = None
        max_retries = 3
        raw_timer = timer if timer >= 10 else 10
        
        for attempt in range(max_retries):
            try:
                poll_msg = await context.bot.send_poll(
                    chat_id=chat_id, 
                    question=poll_question,
                    options=poll_options, 
                    type="quiz", 
                    correct_option_id=correct_idx,
                    explanation=clean_explanation, 
                    is_anonymous=False,
                    open_period=raw_timer
                )
                break
            except Exception as ne:
                logging.error(f"Attempt {attempt + 1} failed sending poll: {ne}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(4)
                else:
                    logging.error("All retries failed for poll")
                    game["quiz_paused"] = True
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Network problem or API limit! Quiz paused.",
                        parse_mode="Markdown"
                    )
                    return
        
        if not poll_msg:
            logging.error("Failed to create poll")
            return
        
        # Store poll info
        game["poll_message_ids"][game["current_q"]] = poll_msg.message_id
        game["poll_map"][poll_msg.poll.id] = {
            "correct_idx": correct_idx, 
            "chat_id": chat_id,
            "correct_answer": options[correct_idx],
            "question_index": game["current_q"]
        }
        
        logging.info(f"📤 Poll sent for Q{game['current_q']}: correct at index {correct_idx}")
        
        # Wait for timer
        try:
            await asyncio.sleep(raw_timer)
        except asyncio.CancelledError:
            logging.info(f"Quiz cancelled for chat {chat_id}")
            return
        
        # Check if quiz still active
        if chat_id not in GROUP_GAMES:
            return
            
        game = GROUP_GAMES[chat_id]
        if game.get("quiz_paused"):
            return

        # Stop poll
        try:
            if game["current_q"] in game["poll_message_ids"]:
                await context.bot.stop_poll(
                    chat_id=chat_id, 
                    message_id=game["poll_message_ids"][game["current_q"]]
                )
        except Exception:
            pass
        
        # Check if answers received
        answers_received = False
        if "user_answers" in game:
            for uid, user_answers in game["user_answers"].items():
                if game["current_q"] in user_answers:
                    answers_received = True
                    break
        
        if not answers_received:
            game["consecutive_no_answers"] += 1
            if game["consecutive_no_answers"] >= 100:
                game["quiz_paused"] = True
                pause_msg = f"🔐 Quiz paused - No one is attempting the questions.\n\nclick resume to continue or stop to end."
                keyboard = [
                    [InlineKeyboardButton("Resume", callback_data=f"pausequiz_{chat_id}")],
                    [InlineKeyboardButton("Stop", callback_data=f"stopquiz_{chat_id}")]
                ]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=pause_msg,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
        else:
            game["consecutive_no_answers"] = 0
        
        # Increment question index counter
        game["current_q"] += 1
        current_q_num = game["current_q"]
        total_q_count = len(questions)

        # 🎯 SEQUENCE CONDITION: हर 5वें सवाल के बाद बिना रुके बैकग्राउंड में लाइव रिजल्ट ट्रिगर करेगा
        if current_q_num < total_q_count and current_q_num % 5 == 0:
            await send_mid_game_leaderboard(chat_id, context, current_q_num, total_q_count)

        # Send next question immediately
        if chat_id in GROUP_GAMES and not game.get("quiz_paused"):
            asyncio.create_task(send_next_group_poll(chat_id, context))
            
    except Exception as e:
        logging.error(f"Error in send_next_group_poll: {e}", exc_info=True)

async def track_poll_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ans = update.poll_answer
        pid = ans.poll_id
        uid = ans.user.id
        user_name = ans.user.first_name or "Player"
        
        for cid, game in list(GROUP_GAMES.items()):
            if "poll_map" in game and pid in game["poll_map"]:
                poll_info = game["poll_map"][pid]
                correct_idx = poll_info["correct_idx"]
                question_idx = poll_info["question_index"]
                
                # FIX 2: Sabse pehle ensure karein ki 'scores' aur 'user_answers' keys game me exist karti hain
                if "scores" not in game:
                    game["scores"] = {}
                if "user_answers" not in game:
                    game["user_answers"] = {}
                if "joined_users" not in game:
                    game["joined_users"] = {}
                
                if uid not in game["joined_users"]:
                    game["joined_users"][uid] = user_name
                    logging.info(f"New participant added: {user_name} (ID: {uid})")
                
                # Agar user pehle se joined nahi tha ya uski entry scores me miss ho gayi thi
                if uid not in game["scores"]:
                    game["scores"][uid] = {"score": 0, "total_time": 0.0, "wrong": 0, "points": 0.0}
                
                if uid not in game["user_answers"]:
                    game["user_answers"][uid] = {}
                
                # FIX 5: ans.option_ids ek list hoti hai, isliye pehla element nikalenge
                # 🌟 OTHERS FIX: Agar user answer retract/unvote karta hai, toh option_ids empty ([]) ho jati hai, use safely -1 handle kiya
                selected_idx = ans.option_ids[0] if ans.option_ids else -1
                
                game["user_answers"][uid][question_idx] = {
                    "selected": selected_idx,  
                    "correct_idx": correct_idx,
                    "timestamp": datetime.now()
                }
                
                # 🌟 ANTI-RACE SYSTEM DEFLATOR: User response active benchmarks ko increment karta hai
                game["consecutive_no_answers"] = 0
                
    except Exception as e:
        logging.error(f"Error in track_poll_answers: {e}")

async def send_mid_game_leaderboard(chat_id, context, current_q_num, total_questions):
    """गेम के बीच में हर 5 सवाल के बाद Live Top 5 Result दिखाने के लिए फ़ंक्शन"""
    try:
        game = GROUP_GAMES.get(chat_id)
        if not game or "user_answers" not in game:
            return

        # 🎯 नया लाइव रिज़ल्ट भेजने से ठीक पहले पिछले वाले लाइव रिज़ल्ट मैसेज को डिलीट करें
        if "last_mid_leaderboard_id" in game and game["last_mid_leaderboard_id"]:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=game["last_mid_leaderboard_id"])
                game["last_mid_leaderboard_id"] = None
                logging.info(f"🗑️ Previous mid-game leaderboard deleted in chat {chat_id}")
            except Exception as delete_err:
                logging.warning(f"Could not delete previous mid-game leaderboard: {delete_err}")

        # क्विज़ की नेगेटिव मार्किंग की वैल्यू निकालें
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT negative_value FROM quizzes WHERE quiz_id = ?", (game["quiz_id"],))
            quiz_data = cursor.fetchone()
        
        db_neg_multiplier = quiz_data[0] if (quiz_data and len(quiz_data) > 0) else 0.0

        # सभी यूज़र्स के लाइव स्कोर और उनकी स्पीड की गणना करें
        final_scores = {}
        for uid in game["user_answers"].keys():
            final_scores[uid] = {"score": 0, "wrong": 0, "total_time": 0.0, "points": 0.0}

        for uid, user_answers in game["user_answers"].items():
            score = 0
            wrong = 0
            total_time = 0.0
            
            for question_idx, answer_data in user_answers.items():
                start_time = game["question_start_times"].get(question_idx, answer_data["timestamp"])
                elapsed_seconds = 0.0
                if isinstance(start_time, datetime):
                    elapsed_seconds = max(0, (answer_data["timestamp"] - start_time).total_seconds())

                if answer_data["selected"] == answer_data["correct_idx"]:
                    score += 1
                    total_time += elapsed_seconds
                else:
                    wrong += 1
            
            calculated_points = float(score) - (float(wrong) * float(db_neg_multiplier))
            final_scores[uid] = {"score": score, "wrong": wrong, "total_time": total_time, "points": calculated_points}

        # स्कोर के आधार पर टॉप 5 यूज़र्स को सॉर्ट करें (हाई स्कोर पहले, कम टाइम पहले)
        sorted_scores = sorted(final_scores.items(), key=lambda item: (-item[1]["points"], item[1]["total_time"]))[:5]

        if not sorted_scores:
            return  # अगर अभी तक किसी ने जवाब नहीं दिया, तो मैसेज न भेजें

        # जॉइन किए हुए कुल प्लेयर्स की संख्या
        total_joined_players = len(game.get("joined_users", {}))

        # लाइव रिज़ल्ट का मैसेज फ़ॉर्मेट
        text = f"📊 <b>LIVE RESULT: Top 5 Players</b>\n"
        text += f"👥 <b>Total Joined Players:</b> {total_joined_players}\n"
        text += f"📝 <b>Progress:</b> Completed {current_q_num} out of {total_questions} questions\n"
        text += f"━━━━━━━━━━━━━━━━━\n\n"

        for idx, (uid, meta) in enumerate(sorted_scores, 1):
            user_display_name = game["joined_users"].get(uid, "Unknown User")
            clean_username = user_display_name if str(user_display_name).startswith("@") else escape_markdown(user_display_name)
            
            # % की गणना: (सही जवाब / अभी तक के कुल सवाल) * 100 
            score_percentage = (meta["score"] / current_q_num) * 100 if current_q_num > 0 else 0.0
            
            rank_icon = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
            
            # नाम के आगे Right, Wrong और Percentage शो करने वाली मुख्य लाइन
            text += f"{rank_icon} <b>{clean_username}</b> ➻ <b>{meta['points']:.2f} Points</b>\n"
            text += f"   └── (✅ Right: {meta['score']} | ❌ Wrong: {meta['wrong']} | 📈 {score_percentage:.2f}%)\n\n"

        text += f"━━━━━━━━━━━━━━━━━\n"
        
        # 🎯 नया बदलाव: पहले नंबर (Rank 1) वाले खिलाड़ी का नाम निकालें और नीचे सेट करें
        rank_1_uid = sorted_scores[0][0]
        rank_1_meta = sorted_scores[0][1]
        rank_1_name = game["joined_users"].get(rank_1_uid, "Player")
        clean_rank_1_name = rank_1_name if str(rank_1_name).startswith("@") else escape_markdown(rank_1_name)
        
        # कुल सवालों को हल करने में लिया गया कुल समय (Total Time Taken)
        rank_1_total_time = rank_1_meta["total_time"]

        text += f"⚡ <b>सबसे तेज (Fastest Leader):</b> {clean_rank_1_name} ({rank_1_total_time:.2f}sec)\n"
        text += f"\n🎮 <i>Game is running continuously without any pause!</i>"

        # ग्रुप में लाइव रिज़ल्ट सेंड करें और नई Message ID को ट्रैक करें
        mid_result_msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        game["last_mid_leaderboard_id"] = mid_result_msg.message_id
        
    except Exception as e:
        logging.error(f"Error in send_mid_game_leaderboard: {e}", exc_info=True)

async def repeat_leaderboard_button_color_worker(chat_id: int, message_id: int, quiz_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Cycles the Leaderboard 'Start Again' button between success and primary every 3 seconds"""
    styles = ["primary", "success", "danger"]
    idx = 0
    bot_username = context.bot.username if context.bot.username else "quiz_bot"
    share_url = f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}"

    while True:
        try:
            await asyncio.sleep(3) # Wait for 3 seconds
            
            # Guard: If a new quiz panel is initiated or running in this chat, kill this old background worker
            if chat_id in GROUP_GAMES:
                # If a new quiz structure setup has overridden the previous run, terminate smoothly
                if GROUP_GAMES[chat_id].get("quiz_id") != quiz_id or GROUP_GAMES[chat_id].get("quiz_started"):
                    break
            
            raw_button = {
                "text": "Start Again ✨",
                "url": share_url,
                "style": styles[idx] # Toggles dynamically every 3 seconds
            }
            
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup([[raw_button]])
            )
            
            idx = (idx + 1) % len(styles)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.debug(f"Leaderboard flash button worker encountered an error: {e}")
            break

# 🎖️ result leaderboard 
async def compile_group_leaderboard(chat_id, context):
    try:
        game = GROUP_GAMES.get(chat_id)
        if not game:
            return
        
        bot_username = context.bot.username if context.bot.username else "quiz_bot"
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Title ke sath negative_value column fetch ki
        cursor.execute("SELECT title, negative_value FROM quizzes WHERE quiz_id = ?", (game["quiz_id"],))
        quiz_data = cursor.fetchone()
        quiz_title = quiz_data[0] if quiz_data else "Quiz"
        db_neg_multiplier = quiz_data[1] if (quiz_data and len(quiz_data) > 1) else 0.0
        
        cursor.execute("SELECT question_text, options, correct_answer FROM questions WHERE quiz_id = ?", (game["quiz_id"],))
        questions = cursor.fetchall()
        conn.close()
        
        total_questions_answered = len(questions)
        correct_answers = {}
        
        # 🟢 FIXED: Convert ALL correct_answer values to INTEGER index
        for idx, (q_text, options_json, correct_ans) in enumerate(questions):
            options = json.loads(options_json)
            
            # ✅ Convert correct_ans to INTEGER
            try:
                correct_idx = int(correct_ans)  # 🟢 Direct conversion
                # Validate range
                if correct_idx < 0 or correct_idx >= len(options):
                    logging.warning(f"Q{idx}: Invalid index {correct_idx}, using 0")
                    correct_idx = 0
            except (ValueError, TypeError):
                # Fallback: try string matching (backward compat)
                try:
                    correct_idx = options.index(str(correct_ans))
                    logging.info(f"Q{idx}: Converted string '{correct_ans}' to index {correct_idx}")
                except ValueError:
                    correct_idx = 0
                    logging.warning(f"Q{idx}: Could not find '{correct_ans}', using 0")
            
            correct_answers[idx] = correct_idx  # 🟢 Store INTEGER
            logging.info(f"✅ Leaderboard Q{idx}: correct_answer={correct_idx}, option='{options[correct_idx] if correct_idx < len(options) else 'N/A'}'")
        
        final_scores = {}
        for uid in game["user_answers"].keys():
            final_scores[uid] = {"score": 0, "wrong": 0, "total_time": 0.0, "points": 0.0}

        for uid, user_answers in game["user_answers"].items():
            score = 0
            wrong = 0
            total_time = 0.0
            
            for question_idx, answer_data in user_answers.items():
                selected_idx = answer_data["selected"]  # User ne jo select kiya
                correct_idx = correct_answers.get(question_idx, -1)  # 🟢 Correct answer index
                
                # 🟢 FIXED: Direct integer comparison (both are now INTEGER)
                logging.info(f"User {uid}, Q{question_idx}: selected={selected_idx} (type: {type(selected_idx).__name__}), correct={correct_idx} (type: {type(correct_idx).__name__}), match={selected_idx == correct_idx}")
                
                if selected_idx == correct_idx:
                    score += 1
                    start_time = game["question_start_times"].get(question_idx, answer_data["timestamp"])
                    if isinstance(start_time, datetime):
                        elapsed = (answer_data["timestamp"] - start_time).total_seconds()
                        total_time += max(0, elapsed)
                else:
                    wrong += 1
            
            # Core Formula: Right - (Wrong * Selected Button Value)
            calculated_points = float(score) - (float(wrong) * float(db_neg_multiplier))
            final_scores[uid] = {"score": score, "wrong": wrong, "total_time": total_time, "points": calculated_points}
        
        # Dynamic Sorting: Pehle high score (Descending), fir kam time (Ascending)
        sorted_scores = sorted(final_scores.items(), key=lambda item: (-item[1]["points"], item[1]["total_time"]))[:50]
        
        header = f"🏁 <b>The quiz '{escape_markdown(quiz_title)}' has finished!</b>\n"
        header += f"📉 <b>Negative Marking Applied: -{db_neg_multiplier} per wrong answer</b>\n\n"
        
        subheader = f"📋 <b>{total_questions_answered} questions answered</b>\n"
        subheader += f"👥 <b>Total Participants: {len(final_scores)}</b>\n"
        subheader += f"━━━━━━━━━━━━━━━━━\n\n"
        
        # 🎭 डायलॉग्स पूल (बिना किसी फिक्स नाम के - रैंडमली इस्तेमाल के लिए)
        roasts_topper = [
            "[टॉपर भाई] 🎉 भाई तुमने तो सीधे किताब ही रट मारी थी क्या? टॉपर बनने का इरादा प्रमाणित है!",
            "[किताबी कीड़ा] 🎊 इतनी पढ़ाई कहाँ से करते हो भाई? हमें भी थोड़ा ज्ञान दे दो, गुरुजी!",
            "[गूगल का दामाद] 💐 भाई गूगल से सीधा कनेक्शन है क्या तुम्हारा? या फिर अंतर्यामी हो?",
            "[वैज्ञानिक] 🥳 इतना दिमाग लाते कहाँ से हो भाई? नासा (NASA) वाले ढूंढ रहे हैं तुम्हें!",
            "[रट्टू तोता] 🎆 लगता है आज सुबह नाश्ते में पूरी किताब ही चबा कर खा गए थे। बाकी सब भूल गए!",
        ]
        
        roasts_middle = [
            "[उड़ता परिंदा] 🫠 नाम की तरह बस हवा में ही उड़ते रह गए, थोड़ा जमीन पर आते तो नहीं?",
            "[समीक्षा बाबू] 🤭 दूसरों की आलोचना करने में तो अव्वल हो, लेकिन नंबर देखकर लगता है सब भूल गए!",
            "[त्रिशंकु खिलाड़ी] 🤪 ना ऊपर पहुँच पाए, ना नीचे सुकून मिला। बीच में ऐसे लटके हो!",
            "[सेफ राइडर] 😜 भाई ने उतना ही रिस्क लिया जितना घरवाले शादी में दूर के रिश्ते दिखाते हैं!",
            "[मिस कॉल] 🙂 नंबर तो ठीक-ठाक आ गए, पर किस्मत ने आखिरी वक्त पर वैसे ही कट कर दिया!",
        ]
        
        roasts_low = [
            "[सिर्फ हाजिरी] 😀 आप सिर्फ परीक्षा हॉल की हवा खाने आए थे क्या? इतना कम स्कोर देखकर हैरानी हुई!",
            "[पूजा की थाली] 🙄 परीक्षा में केवल श्रद्धा और भावना से काम नहीं चलता, कुछ सहायक अध्ययन भी जरूरी है!",
            "[आंसू की बूंद] 😸 नंबर देखकर सच में आंखों में आंसू आ गए। यह नंबर है या शगुन का संकेत?",
            "[सिर्फ मुस्कान] 👻 चेहरे पर मुस्कान तो पूरी है, पर मार्कशीट देखकर रोना आ जाए तो क्या करें?",
            "[मिस्टर गुमनाम] 🤧नाम के आगे टैग लगाने से नंबर नहीं मिलते बाबूजी, इसके लिए पढ़ाई चाहिए!",
            "[दर्शक दीर्घा] 🫩 तुम क्विज़ खेलने नहीं, सिर्फ दूसरों के सही जवाबों पर तालियाँ बजाने आए थे!",
            "[अंगूठा छाप] 🫨 स्क्रीन पर उँगलियाँ तो ऐसे चल रही थीं जैसे हैकर हो, पर मार्क्स कहाँ से आएंगे?",
            "[धूप सेकने वाले] 🤯 परीक्षा हॉल में धूप सेकने आए थे क्या बाबूजी? जितना स्कोर मिला उतनी ही धूप है!",
            "[मार्कशीट का विलेन] 😳 घरवाले अगर यह मार्कशीट देख लें, तो इनाम में सिर्फ फ्लॉप कॉलर ही मिलेगा!",
        ]
        
        roasts_minus = [
            "[कर्जदार खिलाड़ी] 🤣 हंसना तो दूर की बात है, आप तो परीक्षक से भी उधार में नंबर माँग रहे हैं!",
            "[माइनस मास्टर] 😂 भाई साहब! माइनस मार्किंग आपके लिए ही बनी थी। अगली बार थोड़ा प्रयास करना!",
            "[दिवालिया] 😅 भाई साहब, आपका स्कोर देखकर बैंक वाले भी लोन देने से मना कर देंगे!",
            "[दानवीर कर्ण] 🤭 अपने सारे नंबर गलत जवाबों के रास्ते परीक्षक को दान कर आए। इसी को कहते हैं दान!",
            "[ब्लैक होल] 🤣 आपके अकाउंट में नंबर आते नहीं, सीधे गायब हो जाते हैं। माइनस मार्क की सुंदरता!",
        ]

        leaderboard = ""
        for idx, (uid, meta) in enumerate(sorted_scores, 1):
            user_display_name = game["joined_users"].get(uid, "Unknown User")
            
            # 🌟 FIX: Agar name @ se shuru hota hai (username hai), toh escape nahi karenge taaki link valid rahe
            if str(user_display_name).startswith("@"):
                clean_username = user_display_name  # Keep pure clickable username
            else:
                clean_username = escape_markdown(user_display_name) # Safe escape for normal names
                
            score = meta["score"]
            wrong_count = meta["wrong"]
            points = meta["points"]
            total_time = format_time(meta["total_time"])
            
            # रोस्ट लॉजिक के लिए स्कोर परसेंटेज निकालना
            percentage = (points / total_questions_answered * 100) if total_questions_answered > 0 else 0.0
            
            # 🔥 फिक्स रोस्ट सिलेक्शन: रैंक 1 को हमेशा टॉपर का सम्मान मिलेगा
            if idx == 1:
                roast_msg = random.choice(roasts_topper)
            elif points < 0:
                roast_msg = random.choice(roasts_minus)
            elif percentage < 25:
                roast_msg = random.choice(roasts_low)
            else:
                roast_msg = random.choice(roasts_middle)
                
            rank_icon = "🥇." if idx == 1 else "🥈." if idx == 2 else "🥉." if idx == 3 else f"{idx}."
            
            # Clean layout print without invalid characters or slashes
            leaderboard += f"{rank_icon} <b>{clean_username}</b>\n"
            leaderboard += f"   ➻ <b>Right:</b> {score} ✅\n"
            leaderboard += f"   ➻ <b>Wrong:</b> {wrong_count} ❎\n"
            leaderboard += f"   ➻ <b>Total Time Taken:</b> {total_time}\n"
            leaderboard += f"   <blockquote><b>Final Score: {points:.2f} Points</b></blockquote>\n"
            leaderboard += f"   <b>{roast_msg}</b>\n"
            leaderboard += f"   🔹 ┈┈┈┈┈┈|┈┈┈┈┈┈ 🔹\n"
        
        # Existing configuration keys mapping setup inside your code...
        footer = "\n🏆 <b>Congratulations to all participants!</b>"
        full_message = header + subheader + leaderboard + footer
        
        share_url = f"https://t.me/{bot_username}?startgroup=quiz_{game['quiz_id']}"
        
        raw_button = {
            "text": "Start Again ✨",
            "url": share_url,
            "style": "success"  # Initial start styling container configuration
        }
        
        kb = [[raw_button]]
        
        # 🟢 MODIFIED: Store the response inside a message variable
        leaderboard_msg = await context.bot.send_message(
            chat_id=chat_id, 
            text=full_message, 
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML"
        )
        
        # Extract the current quiz id value safely before popping state logs
        current_game_quiz_id = game["quiz_id"]
        
        GROUP_GAMES.pop(chat_id, None)
        
        # 🌟 ADD THIS LINE HERE to trigger the leaderboard blinking routine:
        asyncio.create_task(repeat_leaderboard_button_color_worker(chat_id, leaderboard_msg.message_id, current_game_quiz_id, context))
        
    except Exception as e:
        logging.error(f"Error in compile_group_leaderboard: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
        
        # 1. 🔄 COMPLETE FLUSH: Quiz creation ka saara temporary data complete clear karein
        keys_to_clear = [
            "quiz_build", "quiz_build_creator_id", "current_state", 
            "quiz_title", "quiz_desc", "quiz_timer", "questions_list",
            "awaiting_quiz_title", "awaiting_quiz_desc", "awaiting_quiz_timer",
            "awaiting_question_text", "awaiting_options", "awaiting_correct_answer"
        ]
        for key in keys_to_clear:
            if key in context.user_data:
                del context.user_data[key]
                
        # 🌟 FIX: context.bot_data se key ko safely pull kiya aur user_id check karke clean kiya
        if context.bot_data and "active_creations" in context.bot_data:
            if isinstance(context.bot_data["active_creations"], dict) and user_id in context.bot_data["active_creations"]:
                context.bot_data["active_creations"].pop(user_id, None)
            
        # Pure context.user_data dictionary ko verify karein agar state flag bacha ho
        context.user_data.pop("quiz_creation_active", None)

        # 2. Setup Cancel ka message bhej kar purana reply keyboard hatayein
        msg_obj = update.callback_query.message if update.callback_query else update.message
        
        # Safe message call handle
        if update.callback_query:
            try:
                await update.callback_query.answer("❌ Setup cancelled.")
            except Exception:
                pass

        await msg_obj.reply_text(
            "❌ *Quiz creation has been cancelled and all temporary data has been cleared.*", 
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="markdown"
        )
        
        # 3. 🔥 DIRECT START PANEL RETURN LOGIC
        # Kuch updates directly main menu display karna prefer karti hain, hum safely start panel return karenge
        await start(update, context)
            
        # 4. Conversation workflow end karein
        return ConversationHandler.END
        
    except Exception as e:
        logging.error(f"Error in cancel: {e}")
        return ConversationHandler.END
        
async def handle_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns to the original main greeting menu with native colored buttons"""
    try:
        query = update.callback_query
        
        # ✅ FIXED: edit_message_text se pehle answer lagaya hai timeout se bachne ke liye
        await query.answer()
        
        welcome_text = (
            "<blockquote>👋 Welcome to Premium Quiz Bot!</blockquote>\n\n"
            "Aap is bot se quizzes bana kar apne dosto ke sath groups me realtime khel sakte hain.\n\n"
            "<blockquote>💡 Check Available Commands:</blockquote>\n"
            "➤ /help – Open help center\n\n"
            "👥 Add the bot to a group and start quizzes\n"
            f"📢 Owner Details: ID `{OWNER_ID}`"
        )
        
        # 🌟 FIX: Raw dictionary payload use kiya buttons ko custom color dene ke liye (Bypassing validation)
        kb = [
            [{"text": "🚀 Create New Quiz", "callback_data": "btn_newquiz", "style": "success"}],     # Hara (Green) color
            [{"text": "📚 View My Quizzes", "callback_data": "btn_viewquizzes", "style": "primary"}]  # Neela (Blue) color
        ]
        
        # ✅ FIXED: parse_mode ko "Markdown" kiya aur timeouts ko api_kwargs me daala
        await query.edit_message_text(
            text=welcome_text, 
            reply_markup=InlineKeyboardMarkup(kb), 
            parse_mode="HTML",
            api_kwargs={
                "read_timeout": 20,
                "write_timeout": 20
            }
        )
        
    except Exception as e:
        logging.error(f"Error in handle_back_main: {e}", exc_info=True)
        try:
            await query.answer("❌ Error returning to main menu", show_alert=True)
        except Exception:
            pass
            

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline queries to show clean quiz list or share a specific quiz"""
    query = update.inline_query.query.strip()
    user_id = update.inline_query.from_user.id
    bot_username = context.bot.username if context.bot.username else "quiz_bot"
    results = []

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # CASE 1: Chat me sirf username type karne par short list dikhao (Empty query)
        if not query:
            cursor.execute(
                "SELECT quiz_id, title, description, timer FROM quizzes WHERE creator_id = ? ORDER BY quiz_id DESC LIMIT 50", 
                (user_id,)
            )
            user_quizzes = cursor.fetchall()
            
            if not user_quizzes:
                results.append(
                    InlineQueryResultArticle(
                        id="no_quiz",
                        title="❌ No Quizzes Found!",
                        description="Aapne koi quiz nahi banaya hai.",
                        input_message_content=InputTextMessageContent(
                            message_text="Aapne abhi tak koi quiz nahi banaya hai. Naya quiz banane ke liye bot me /newquiz likhein."
                        )
                    )
                )
            else:
                for quiz in user_quizzes:
                    quiz_id, title, description, timer = quiz
                    
                    # Total questions count fetch karein
                    cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
                    count_data = cursor.fetchone()
                    total_q = count_data[0] if count_data else 0
                    
                    time_display = f"{timer}s" if timer < 60 else f"{timer // 60}m"
                    escaped_title = escape_markdown(title)
                    escaped_desc = escape_markdown(description) if description else "No description"
                    
                    # List me se click karte hi ye text aur panel direct chat me share ho jayega
                    share_message_text = (
                        f"🎲 <b>Quiz {escaped_title}</b>\n\n"
                        f"💌 <b>Description:</b> {escaped_desc}\n"
                        f"🖋️ {total_q} questions · ⏱ {time_display}"
                    )
                    
                    start_private_url = f"https://t.me/{bot_username}?start=quiz_{quiz_id}"
                    start_group_url = f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}"
                    
                    inline_keyboard = [
                        [InlineKeyboardButton("Start quiz in Private Chat", url=start_private_url)],
                        [InlineKeyboardButton("Start quiz in Group", url=start_group_url)],
                        [InlineKeyboardButton("Share Quiz", switch_inline_query=f"quiz_{quiz_id}")]
                    ]
                    
                    # Short Display Popup for List
                    results.append(
                        InlineQueryResultArticle(
                            id=f"list_{quiz_id}",
                            title=f"🎲 Quiz {title}", 
                            description=f"⚡ {total_q} Qs   ·   ⏱ {time_display}", 
                            input_message_content=InputTextMessageContent(
                                message_text=share_message_text,
                                parse_mode="HTML"
                            ),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard)
                        )
                    )
            
            conn.close()
            await update.inline_query.answer(results, cache_time=0, is_personal=True)
            return

        # CASE 2: Single Quiz Share handler (`quiz_12` query trigger hone par)
        if query.startswith("quiz_"):
            # 🌟 FIX: String id ko extract karke safely int me convert kiya taaki SQL query NULL return na kare
            try:
                quiz_id = int(query.replace("quiz_", ""))
            except ValueError:
                conn.close()
                return  # Malformed input par safely exit karein
            
            cursor.execute("SELECT title, description, timer FROM quizzes WHERE quiz_id = ?", (quiz_id,))
            quiz_data = cursor.fetchone()
            
            if quiz_data:
                title, description, timer = quiz_data
                
                cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
                count_data = cursor.fetchone()
                total_q = count_data[0] if count_data else 0
                conn.close()

                time_display = f"{timer}s" if timer < 60 else f"{timer // 60}m"
                escaped_title = escape_markdown(title)
                escaped_desc = escape_markdown(description) if description else "No description"
                
                share_message_text = (
                        f"🎲 Quiz {escaped_title}\n\n"
                        f"💌 **Description:** {escaped_desc}\n"
                        f"🖋️ {total_q} questions · ⏱ {time_display}"
                )
                
                start_private_url = f"https://t.me/{bot_username}?start=quiz_{quiz_id}"
                start_group_url = f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}"
                
                inline_keyboard = [
                    [InlineKeyboardButton("Start quiz in Private Chat", url=start_private_url)],
                    [InlineKeyboardButton("Start quiz in Group", url=start_group_url)],
                    [InlineKeyboardButton("Share Quiz", switch_inline_query=f"quiz_{quiz_id}")]
                ]
                
                results = [
                    InlineQueryResultArticle(
                        id=str(quiz_id),
                        title=f"🎲 Quiz {title}",
                        description=f"⚡ {total_q} Qs   ·   ⏱ {time_display}",
                        input_message_content=InputTextMessageContent(
                            message_text=share_message_text,
                            parse_mode="Markdown"
                        ),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard)
                    )
                ]
                await update.inline_query.answer(results, cache_time=0)
            else:
                conn.close()
                
    except Exception as e:
        logging.error(f"Error in inline_query_handler: {e}")
        
async def owner_status_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct text command /status for the Owner to view all groups using broadcast_groups table"""
    try:
        user_id = update.message.from_user.id
        
        # 🟢 .env se li gayi OWNER_ID se matching check
        if OWNER_ID is None or user_id != OWNER_ID:
            await update.message.reply_text("❌ Unauthorized! This command is only accessible by the bot owner.")
            return

        processing_msg = await update.message.reply_text("🔍 Fetching active groups from broadcast logs...")
        
        # 🟢 Connecting to database and reading from broadcast_groups table
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT chat_id FROM broadcast_groups")
            chat_rows = cursor.fetchall()
        except Exception as db_err:
            logging.error(f"Database error while reading broadcast_groups: {db_err}")
            chat_rows = []
        conn.close()

        if not chat_rows:
            await processing_msg.delete()
            await update.message.reply_text(
                "⚠️ *No groups found in broadcast logs!.*\n\n"
                "💡 *Reason:* Aapki `broadcast_groups` table abhi khali hai. Jab bot kisi group me save hoga ya broadcast me add hoga, tabhi yahan data dikhega."
            )
            return

        status_report = "📊 *Bot Active Groups Status Report*\n\n"
        group_count = 0

        for row in chat_rows:
            target_chat_id = row[0] # Fetching the chat_id from tuple
            
            try:
                # Live Telegram API lookup call
                chat_details = await context.bot.get_chat(chat_id=target_chat_id)
                group_name = chat_details.title
                
                try:
                    invite_link = chat_details.invite_link
                    if not invite_link:
                        # Bot ke paas invite links export karne ki permission honi chahiye
                        invite_link = await context.bot.export_chat_invite_link(chat_id=target_chat_id)
                except Exception:
                    invite_link = "No Link Permission 🚫"

                group_count += 1
                status_report += f"*{group_count}. 👥 Name:* {escape_markdown(group_name)}\n"
                status_report += f"🆔 *Chat ID:* `{target_chat_id}`\n"
                status_report += f"🔗 *Link:* {invite_link}\n"
                status_report += "━" * 15 + "\n"
            except Exception:
                # Agar bot group se remove ho chuka hai toh use skip karein
                continue

        await processing_msg.delete()

        if group_count == 0:
            await update.message.reply_text("⚠️ No active groups accessible. Bot might have been removed from old groups.")
        else:
            status_report += f"\n📉 *Total Active Groups:* {group_count}"
            await update.message.reply_text(text=status_report, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error in owner_status_text_command: {e}")
        await update.message.reply_text("❌ Error generating groups status details.")
                    
# broadcast command handler
# Broadcast command handlers (converted to python-telegram-bot async style)
# =====================================================================

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the broadcast confirmation (owner only). Usage: reply to a message and run /broadcast"""
    message = update.message
    if not message:
        return

    is_owner = (OWNER_ID is not None and message.from_user.id == OWNER_ID)
    is_valid_chat = (str(message.chat.type) == 'private' or (SUPPORT_GROUP_ID and message.chat.id == SUPPORT_GROUP_ID))

    if not (is_owner and is_valid_chat):
        await message.reply_text("❌ This command is only valid for the bot owner and in authorized chats.")
        return

    if not message.reply_to_message:
        await message.reply_text(
            "⚠️ *How to use?*\n"
            "1. Send the text/photo/video/sticker you want to broadcast.\n"
            "2. Reply to that message and run `/broadcast`.",
            parse_mode="Markdown"
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(text="✅ YES (Pin)", callback_data=f"bcast_yes_{message.reply_to_message.message_id}"),
            InlineKeyboardButton(text="❌ NO (Don't Pin)", callback_data=f"bcast_no_{message.reply_to_message.message_id}")
        ]
    ])

    await message.reply_text(
        "🏵️ *Do you want to PIN this broadcast message in all groups?*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def execute_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executes broadcast when owner clicks YES/NO."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # Authorization check
    if OWNER_ID is not None and query.from_user.id != OWNER_ID:
        await query.answer("❌ You are not authorized to control this broadcast!", show_alert=True)
        return

    parts = query.data.split('_')
    if len(parts) < 3:
        await query.answer("❌ Invalid broadcast data.", show_alert=True)
        return

    should_pin = (parts[1] == 'yes')
    try:
        target_msg_id = int(parts[2])
    except ValueError:
        await query.answer("❌ Invalid message id.", show_alert=True)
        return

    # Update status message to show processing
    try:
        await query.edit_message_text("📢 **Initializing broadcast process, please wait....**", parse_mode="Markdown")
    except Exception:
        # ignore edit failures
        pass

    # Read the chat and user lists from the DB (use broadcast_* tables created by init_db)
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT chat_id FROM broadcast_groups")
            all_chats = cursor.fetchall()
        except Exception:
            all_chats = []
        try:
            cursor.execute("SELECT chat_id FROM broadcast_users")
            all_users = cursor.fetchall()
        except Exception:
            all_users = []

    g_success = g_fail = u_success = u_fail = 0

    # Broadcast to groups
    for (chat_id,) in all_chats:
        try:
            sent_msg = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=query.message.chat.id,
                message_id=target_msg_id
            )
            if should_pin and sent_msg and hasattr(sent_msg, "message_id"):
                try:
                    await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=False)
                except Exception:
                    pass
            g_success += 1
            await asyncio.sleep(0.15)
        except Exception:
            g_fail += 1

    # Broadcast to private users
    for (user_id,) in all_users:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=query.message.chat.id,
                message_id=target_msg_id
            )
            u_success += 1
            await asyncio.sleep(0.15)
        except Exception:
            u_fail += 1

    # Final report
    report_text = (
        f"📊 *Global Broadcast Report:*\n\n"
        f"📌 *Group Pin Status:* {'✅ Pinned' if should_pin else '❌ Not Pinned'}\n\n"
        f"👥 *Groups:*\n"
        f"✅ Done: {g_success} | ❌ Failed: {g_fail}\n\n"
        f"👤 *Private Users:*\n"
        f"✅ Done: {u_success} | ❌ Failed: {u_fail}\n\n"
        f"🎯 *Broadcast completed.*"
    )

    try:
        # Try editing original query message with summary
        await query.edit_message_text(report_text, parse_mode="Markdown")
    except Exception:
        # fallback: send a new message
        await context.bot.send_message(chat_id=query.message.chat.id, text=report_text, parse_mode="Markdown")
        
async def execute_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    logging.info(f"Broadcast callback triggered by {query.from_user.id}: {query.data}")

    if OWNER_ID is not None and query.from_user.id != OWNER_ID:
        try:
            await query.answer("❌ You are not authorized to control this broadcast!", show_alert=True)
        except Exception:
            pass
        return

    parts = query.data.split('_')
    if len(parts) < 3:
        try:
            await query.answer("❌ Invalid broadcast data.", show_alert=True)
        except Exception:
            pass
        return

    should_pin = (parts[1] == 'yes')
    try:
        target_msg_id = int(parts[2])
    except ValueError:
        try:
            await query.answer("❌ Invalid message id.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.edit_message_text("📢 Initializing broadcast process, please wait....")
    except Exception:
        pass

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT chat_id FROM broadcast_groups")
            all_chats = cursor.fetchall()
        except Exception:
            all_chats = []
        try:
            cursor.execute("SELECT chat_id FROM broadcast_users")
            all_users = cursor.fetchall()
        except Exception:
            all_users = []

    g_success = g_fail = u_success = u_fail = 0

    for (chat_id,) in all_chats:
        try:
            sent_msg = await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=query.message.chat.id,
                message_id=target_msg_id
            )
            if should_pin and sent_msg and hasattr(sent_msg, "message_id"):
                try:
                    await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=False)
                except Exception as e:
                    logging.warning(f"Pin failed in {chat_id}: {e}")
            g_success += 1
            await asyncio.sleep(0.15)
        except Exception as e:
            logging.warning(f"Broadcast to group {chat_id} failed: {e}")
            g_fail += 1

    for (user_id,) in all_users:
        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=query.message.chat.id,
                message_id=target_msg_id
            )
            u_success += 1
            await asyncio.sleep(0.15)
        except Exception as e:
            logging.warning(f"Broadcast to user {user_id} failed: {e}")
            u_fail += 1

    report_text = (
        f"📊 *Global Broadcast Report:*\n\n"
        f"📌 *Group Pin Status:* {'✅ Pinned' if should_pin else '❌ Not Pinned'}\n\n"
        f"👥 *Groups:*\n"
        f"✅ Done: {g_success} | ❌ Failed: {g_fail}\n\n"
        f"👤 *Private Users:*\n"
        f"✅ Done: {u_success} | ❌ Failed: {u_fail}\n\n"
        f"🎯 *Broadcast completed.*"
    )

    try:
        await query.edit_message_text(report_text, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat.id, text=report_text, parse_mode="Markdown")

# ------------------ Autorun helpers ------------------
async def autorun_worker(app, autorun_id, quiz_id, interval_minutes, wait_before_start=10, min_players_required=1, force_start=False):
    """
    🇮🇳 भाग 1: टाइमर शेड्यूलिंग और सुरक्षा लॉक प्रबंधन
    यह हिस्सा भारत के समय (IST) के अनुसार सही समय पर एक्टिव होता है और क्लैश रोकता है।
    """
    try:
        logging.info(f"🚀 Fully Automated AI Autorun worker {autorun_id} started.")
        
        while True:
            # 1. डेटाबेस से चेक करें कि ऑटोरन एक्टिव है या नहीं
            with sqlite3.connect(DB_FILE) as conn:
                cur = conn.cursor()
                cur.execute("SELECT active, schedule_time FROM autoruns WHERE id = ?", (autorun_id,))
                row = cur.fetchone()
                if not row or row[0] != 1:
                    logging.info(f"Autorun {autorun_id}: stopped via DB active flag constraint.")
                    break  
                schedule_time = row[1]

            now = datetime.now(tz=IST)

            # ⏰ HH:MM (जैसे 14:30) या मिनट इंटरवल के आधार पर वेट टाइम कैलकुलेट करें
            if schedule_time:
                next_ts = next_occurrence_from_hhmm(schedule_time, ref_dt=now)
                wait_seconds = (next_ts - now).total_seconds()
                with sqlite3.connect(DB_FILE) as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE autoruns SET next_run = ? WHERE id = ?", (next_ts.isoformat(), autorun_id))
                    conn.commit()
                logging.info(f"⏰ Autorun {autorun_id}: Waiting until scheduled time {next_ts.isoformat()} ({wait_seconds:.0f}s left)")
                try:
                    await asyncio.sleep(max(0, wait_seconds))
                except asyncio.CancelledError:
                    return
            else:
                next_ts = now + timedelta(minutes=interval_minutes)
                with sqlite3.connect(DB_FILE) as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE autoruns SET next_run = ? WHERE id = ?", (next_ts.isoformat(), autorun_id))
                    conn.commit()
                logging.info(f"⏰ Autorun {autorun_id}: Waiting for interval loop of {interval_minutes} minutes.")
                try:
                    await asyncio.sleep(interval_minutes * 60)
                except asyncio.CancelledError:
                    return

            # ---------- सीरियल सुरक्षा (क्रैश रोकने के लिए) ----------
            acquired = False
            try:
                logging.info(f"🔐 Autorun {autorun_id}: Attempting lock acquisition...")
                await AUTORUN_SERIAL_LOCK.acquire()
                acquired = True
                logging.info(f"✅ Autorun {autorun_id}: Acquired serial runtime lock.")

                # अगर ग्रुप में पहले से ही कोई दूसरी क्विज़ लाइव चल रही है, तो 10-10 सेकंड रुकें
                attempt_count = 0
                while SUPPORT_GROUP_ID in GROUP_GAMES and GROUP_GAMES[SUPPORT_GROUP_ID].get("quiz_started"):
                    attempt_count += 1
                    logging.info(f"⏳ Group busy. Attempt {attempt_count}/5. Waiting 10 seconds...")
                    await asyncio.sleep(10)
                    if attempt_count >= 5:
                        logging.warning("Group occupied beyond timeout thresholds. Postponing this run.")
                        break
                
                if attempt_count >= 5:
                    continue

                # ====================================================================
                # 🔥 भाग 2: /save द्वारा सेव्ड लिस्ट लोड करना अथवा 50+ रैंडम टॉपिक्स पूल
                # ====================================================================
                with sqlite3.connect(DB_FILE) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT topic_name FROM autorun_custom_topic WHERE key = 'active_topic'")
                    topic_row = cur.fetchone()
                
                saved_custom_payload = topic_row[0] if (topic_row and topic_row[0].strip()) else None

                if saved_custom_payload:
                    # यूज़र की सेव की गई लिस्ट (\n से अलग) को वापस ऐरे में बदलें और एक रैंडम चुनें
                    custom_pool = [line.strip() for line in saved_custom_payload.split('\n') if line.strip()]
                    selected_topic = random.choice(custom_pool)
                    logging.info(f"🎯 Loaded from custom /save pool ({len(custom_pool)} items). Selected: {selected_topic}")
                else:
                    # फॉलबैक पूल: यदि /save खाली है (यह हिस्सा हिस्सा 2 में जारी रहेगा)
                    # फॉलबैक पूल जारी: यदि /save खाली है तो कोड बेस का पुराना डिफ़ॉल्ट पूल चलेगा
                    auto_topics_pool = [
                        # 📰 Current Affairs & GK | 📜 Ancient & Medieval History
                        "सिंधु घाटी सभ्यता", "वैदिक काल का इतिहास", "बौद्ध धर्म और जैन धर्म",
                        "मौर्य साम्राज्य", "गुप्त साम्राज्य का इतिहास", "दिल्ली सल्तनत राजवंश", "मुग़ल साम्राज्य का इतिहास", 
                        "मराठा और विजयनगर साम्राज्य", "भक्ति और सूफी आंदोलन",
                        
                        # 🇮🇳 Modern & World History
                        "भारत में यूरोपीय शक्तियों का आगमन", "1857 का विद्रोह भारतीय इतिहास", "भारतीय राष्ट्रीय कांग्रेस का इतिहास", 
                        "महात्मा गांधी और स्वतंत्रता आंदोलन", "भारत के क्रांतिकारी स्वतंत्रता सेनानी", "भारत की स्वतंत्रता और विभाजन 1947",
                        "औद्योगिक क्रांति विश्व इतिहास", "फ्रांसीसी और रूसी क्रांतियाँ", "प्रथम विश्व युद्ध और द्वितीय विश्व युद्ध का इतिहास", "शीतयुद्ध काल की भू-राजनीति",
                        
                        # ✨ Polity (Constitutional Frameworks)
                        "भारतीय संविधान का निर्माण और उसके स्रोत", "भारतीय संविधान की प्रस्तावना और अनुसूचियाँ", 
                        "संघ और उसका राज्य क्षेत्र तथा नागरिकता", "भारत के मौलिक अधिकार", 
                        "राज्य के नीति निर्देशक तत्व और मौलिक कर्तव्य", "भारत के राष्ट्रपति, उपराष्ट्रपति और प्रधानमंत्री", 
                        "भारतीय संसद लोकसभा और राज्यसभा", "भारत का सर्वोच्च न्यायालय और न्यायिक समीक्षा", 
                        "राज्यपाल मुख्यमंत्री और राज्य विधानमंडल", "भारत में उच्च न्यायालय और अधीनस्थ न्यायालय", 
                        "पंचायती राज और स्थानीय स्वशासन", "भारत का निर्वाचन आयोग और चुनावी सुधार", 
                        "संवैधानिक और गैर-संवैधानिक निकाय", "भारत के महत्वपूर्ण संविधान संशोधन", 
                        "भारतीय संविधान में आपातकालीन प्रावधान",
                        
                        # 🌍 Geography (Indian & Global Ecosystems)
                        "भारत के भौतिक लक्षण", "भारत की नदियाँ और अपवाह तंत्र", "भारत की जलवायु और मिट्टी के प्रकार", 
                        "भारत की प्राकृतिक वनस्पति और राष्ट्रीय उद्यान", "भारत में कृषि और सिंचाई", 
                        "भारत के प्रमुख बांध और नदी घाटी परियोजनाएँ", "भारत के खनिज और ऊर्जा संसाधन", 
                        "भारत में उद्योग परिवहन और बंदरगाह", "भारत की जनगणना और जनजातियाँ", 
                        "विश्व के महाद्वीप पर्वत और मरुस्थल", "विश्व की नदियाँ झीलें और जलप्रपात", 
                        "विश्व के प्रमुख जलडमरू मध्य और नहरें", 
                        "पृथ्वी की आंतरिक संरचना ज्वालामुखी और भूकंप", "वायुमंडल की संरचना पवनें और चक्रवात", "महासागरीय धाराएँ और ज्वार-भाटा",
                        
                        # 🧬 General Science (Physics, Chemistry, Biology)
                        "कोशिका की संरचना और कार्य", "मानव शरीर की शारीरिक रचना और तंत्र", "मानव पोषण, vitamin और रोग", 
                        "पादप की शारीरिक रचना, कार्यिकी और पादप जगत", "आनुवंशिकी और जैविक विकास", 
                        "पदार्थ की अवस्थाएँ और परमाणु संरचना", "आवर्त सारणी और तत्वों के गुणधर्म", 
                        "अम्ल क्षार लवण और pH पैमाना", "धातु, अधातु और मिश्र धातु", "कार्बनिक रसायन, carbon और ईंधन",
                        "मात्रक मापन गति और बल", "कार्य, ऊर्जा और शक्ति", 
                        "प्रकाश प्रकाशिकी ध्वनि और तरंगें", "विद्युत और चुंबकत्व", "आधुनिक भौतिकी परमाणु ऊर्जा और अंतरिक्ष",
                        
                        # 🤖 Languages & Grammar (English & Hindi)
                        "वर्णमाला और संधि", "संज्ञा सर्वनाम क्रिया और अव्यय", "समास उपसर्ग और प्रत्यय", 
                        "विलोम और पर्यायवाची शब्द", "लिंग वचन कारक और काल", "हिंदी मुहावरे लोकोक्तियाँ",
                        "तद्भव-तत्सम और देशज-विदेशज शब्द", "अनेक शब्दों के लिए एक शब्द वाक्यांश",
                        "वाक्य शुद्धि और वर्तनी शुद्धि", "अनेकार्थक और भिन्नार्थक शब्द", "प्रमुख कवि लेखक और उनकी रचनाएँ",

                        # 🔢 Quantitative Aptitude (Mathematics)
                        "संख्या पद्धति और सरलीकरण", "समीकरण एलसीएम और एचसीएफ", "प्रतिशत और लाभ-हानि", 
                        "साधारण और चक्रवृद्धि ब्याज", "अनुपात समानुपात और साझेदारी", "औसत आयु और मिश्रण", 
                        "समय कार्य और मजदूरी", "नल और टंकी", "समय चाल और दूरी", "नाव और धारा रेलगाड़ी आधारित प्रश्न",

                        # 🧠 Logical Reasoning (Verbal & Non-Verbal)
                        "सादृश्यता और वर्गीकरण", "वर्णमाला और कोडिंग-डिकोडिंग", "रक्त संबंध", "दिशा और दूरी का ज्ञान", 
                        "क्रम और बैठने की व्यवस्था", "पहेली", "कथन-निष्कर्ष, तर्क और पूर्वधारणाएँ", "पासा और घन", 
                        "घड़ी और कैलेंडर", "लुप्त संख्या और पद ज्ञात करना", "गणितीय संक्रियाएँ", 
                    ]
                    selected_topic = random.choice(auto_topics_pool)
                    logging.info(f"🎲 Automated Engine selected random topic from fallback pool: {selected_topic}")
                
                logging.info(f"🎲 Automated Engine finalized topic: {selected_topic}")

                # 🆕 मेंबर्स के लिए जनरेशन शुरू होने का लाइव अलर्ट मैसेज
                try:
                    generation_alert_msg = await app.bot.send_message(
                        chat_id=SUPPORT_GROUP_ID,
                        text=f"🔍 <b>Gemini AI आपके लिए प्रश्न जनरेट कर रहा है...</b>\n\n🎯 <b>विषय (Topic):</b> <code>{selected_topic}</code>\n⏳ कृपया प्रतीक्षा करें, क्विज़ कुछ ही देर में लाइव होने वाली है! 🚀",
                        parse_mode="HTML"
                    )
                except Exception as alert_err:
                    logging.warning(f"Could not send generation alert to support group: {alert_err}")

                # 🧠 Gemini AI से 50 फ्रेश सवाल हिंदी भाषा में बनवाएं
                ai_questions = await asyncio.to_thread(
                    generate_bulk_questions_ai, selected_topic, 50, "Hindi", "Hard", 4
                )
                
                # (यह कोड नीचे दिए गए 'भाग B' में जारी रहेगा)
                if not ai_questions or len(ai_questions) == 0:
                    logging.error("⚠️ AI structural generation skipped or quota limited. Retrying on next cycle.")
                    try:
                        await app.bot.send_message(chat_id=SUPPORT_GROUP_ID, text="❌ <b>AI लोड या कोटा लिमिट के कारण क्विज़ जनरेट नहीं हो सकी।</b> अगली साइकिल में पुनः प्रयास किया जाएगा।", parse_mode="HTML")
                    except Exception: pass
                    continue

                # 💾 नए बने क्विज़ को डेटाबेस में ऑटो-सेव करें
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO quizzes (creator_id, title, description, timer, negative_value) VALUES (?, ?, ?, ?, ?)", 
                    (OWNER_ID, f"Auto AI Quiz: {selected_topic}", f"Daily automated session focused on {selected_topic}.", 15, 0.25)
                )
                new_quiz_id = cursor.lastrowid
                
                for q in ai_questions:
                    options = q.get("options", [])
                    correct_idx = q.get("correct", 0)
                    q_text = q.get("question", "")
                    explanation = q.get("explanation", f"The correct answer is option {correct_idx + 1}.")
                    
                    cursor.execute(
                        "INSERT INTO questions (quiz_id, question_text, options, correct_answer, explanation, pre_message) VALUES (?, ?, ?, ?, ?, ?)", 
                        (new_quiz_id, q_text, json.dumps(options), correct_idx, explanation, "")
                    )
                conn.commit()
                conn.close()

                # 📤 ग्रुप में घोषणा पैनल भेजें और उसे पिन करें
                init_text = (
                    f"<blockquote>🤖 <b><ins>DAILY AUTOMATIC LIVE QUIZ</ins></b> 🚀</blockquote>\n\n"
                    f"<blockquote>📚 <b>Topic:</b> {escape_markdown(selected_topic)}</blockquote>\n"
                    f"<blockquote>📝 <b>Questions Count:</b> 50 Fresh AI Generated Questions</blockquote>\n"
                    f"<blockquote>⏱ <b>Duration Limit:</b> 15s per question card</blockquote>\n"
                    f"<blockquote>📉 <b>Negative Schema:</b> -0.25 per wrong response</blockquote>\n\n"
                    f"🏁 <i>This quiz has been created natively by Gemini AI and will begin auto-running in {wait_before_start} seconds! Get ready!</i>"
                )

                old_panel_id = None
                if SUPPORT_GROUP_ID in GROUP_GAMES:
                    old_panel_id = GROUP_GAMES[SUPPORT_GROUP_ID].get("setup_message_id")

                sent_msg = await app.bot.send_message(chat_id=SUPPORT_GROUP_ID, text=init_text, parse_mode="HTML")
                try:
                    await app.bot.pin_chat_message(chat_id=SUPPORT_GROUP_ID, message_id=sent_msg.message_id)
                except Exception: pass

                # इन-मेमोरी गेम स्टेट को इनिशियलाइज़ करें
                if SUPPORT_GROUP_ID not in GROUP_GAMES:
                    GROUP_GAMES[SUPPORT_GROUP_ID] = {}
                
                GROUP_GAMES[SUPPORT_GROUP_ID].update({
                    "quiz_id": new_quiz_id, "setup_message_id": sent_msg.message_id, "setup_panel_text": init_text,
                    "previous_panel_message_id": old_panel_id, "joined_users": {}, "scores": {}, "poll_map": {}, 
                    "start_time": None, "user_answers": {}, "question_start_times": {}, "ready_users": set(), 
                    "quiz_started": False, "poll_message_ids": {}, "is_private": False, "quiz_paused": False, 
                    "consecutive_no_answers": 0, "autorun_id": autorun_id, "start_lock": asyncio.Lock()
                })

                # ज्वाइन करने का टाइमर होल्ड (Delay Window)
                await asyncio.sleep(wait_before_start)

                # बिना किसी बटन के सीधे लाइव पोल चालू (Force Start)
                GROUP_GAMES[SUPPORT_GROUP_ID].update({"quiz_started": True, "current_q": 0})
                
                if old_panel_id:
                    try: await app.bot.unpin_chat_message(chat_id=SUPPORT_GROUP_ID, message_id=old_panel_id)
                    except Exception: pass

                ctx = SimpleNamespace(bot=app.bot)
                asyncio.create_task(send_next_group_poll(SUPPORT_GROUP_ID, ctx))

                # क्विज़ खत्म होने तक इस टास्क को होल्ड रखें (ताकि लॉक सुरक्षित रहे)
                while SUPPORT_GROUP_ID in GROUP_GAMES and GROUP_GAMES[SUPPORT_GROUP_ID].get("quiz_started"):
                    await asyncio.sleep(5)

            finally:
                if acquired:
                    try:
                        AUTORUN_SERIAL_LOCK.release()
                        logging.info(f"🔓 Autorun {autorun_id}: Released serial lock constraint.")
                    except RuntimeError: pass

    except asyncio.CancelledError:
        logging.info(f"🛑 Autorun background worker loop {autorun_id} canceled safely.")
    except Exception as error:
        logging.error(f"❌ Critical exception encountered in autorun runtime: {error}", exc_info=True)

def schedule_autorun_task(app, autorun_id, quiz_id, interval_minutes, wait_before_start=10, min_players_required=1, force_start=False):
    if autorun_id in AUTORUN_TASKS and not AUTORUN_TASKS[autorun_id].done():
        return
    task = asyncio.create_task(autorun_worker(app, autorun_id, quiz_id, interval_minutes, wait_before_start, min_players_required, force_start))
    AUTORUN_TASKS[autorun_id] = task

def cancel_autorun_task(autorun_id):
    """Cancel and remove a running autorun task."""
    task = AUTORUN_TASKS.get(autorun_id)
    if task and not task.done():
        task.cancel()
    AUTORUN_TASKS.pop(autorun_id, None)


async def load_autoruns_on_startup(app):
    """Load active autoruns from DB and schedule them (call this in main after app ready)."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, quiz_id, interval_minutes FROM autoruns WHERE active = 1")
            rows = cur.fetchall()
        for autorun_id, quiz_id, interval in rows:
            schedule_autorun_task(app, autorun_id, quiz_id, interval)
        logging.info(f"Loaded {len(rows)} autorun(s) from DB on startup.")
    except Exception as e:
        logging.error(f"Error loading autoruns on startup: {e}")
# ---------------- end autorun helpers ------------------

async def autorun_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: /autorun <quiz_id> <interval_minutes|HH:MM> [wait_before_start_seconds]
    
    🇮🇳 All times are in IST (Indian Standard Time)
    Examples:
        /autorun 5 60       → Every 60 minutes
        /autorun 5 14:30    → Daily at 2:30 PM IST
        /autorun 5 09:00 15 → Daily at 9:00 AM IST, wait 15 seconds before auto-start
    """
    try:
        if OWNER_ID is None or update.message.from_user.id != OWNER_ID:
            await update.message.reply_text("❌ Unauthorized - Only bot owner can use this command")
            return
        if not SUPPORT_GROUP_ID:
            await update.message.reply_text("❌ SUPPORT_GROUP_ID not configured in .env")
            return
        args = context.args or []
        if len(args) < 1:
            # 🇮🇳 Help message with IST timezone info
            help_text = (
                "📖 *Autorun Quiz Command Help*\n\n"
                "🔧 *Usage:* `/autorun <quiz_id> <interval|HH:MM> [wait_seconds]`\n\n"
                "🇮🇳 *All times are in IST (Indian Standard Time)*\n\n"
                "📋 *Examples:*\n"
                "• `/autorun 5 60` → हर 60 मिनट में quiz चले\n"
                "• `/autorun 5 14:30` → रोज़ 2:30 PM IST पर quiz\n"
                "• `/autorun 5 09:00 15` → रोज़ 9:00 AM IST पर, 15 सेकंड का join time\n"
                "• `/autorun 5 18:45 20` → रोज़ 6:45 PM IST पर, 20 सेकंड का join time\n\n"
                "⏹️ *Stop करने के लिए:* `/stopautorun <autorun_id>`"
            )
            await update.message.reply_text(help_text, parse_mode="Markdown")
            return
        try:
            quiz_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid quiz_id. Provide a number.\nExample: `/autorun 5 60`", parse_mode="Markdown")
            return

        schedule_time = None
        interval = 60
        wait_before_start = 10

        if len(args) >= 2:
            second = args[1].strip()
            if TIME_RE.match(second):
                schedule_time = second  # 'HH:MM' 24-hour (IST)
            else:
                try:
                    interval = int(second)
                except ValueError:
                    await update.message.reply_text(
                        "❌ Invalid second parameter.\n\n"
                        "Provide either:\n"
                        "• Minutes (e.g., `60`)\n"
                        "• Time in HH:MM format (e.g., `14:30` for 2:30 PM IST)",
                        parse_mode="Markdown"
                    )
                    return
        if len(args) >= 3:
            try:
                wait_before_start = int(args[2])
            except ValueError:
                pass

        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM autoruns WHERE quiz_id = ? AND active = 1", (quiz_id,))
            row = cur.fetchone()
            if row:
                autorun_id = row[0]
                cur.execute("UPDATE autoruns SET interval_minutes = ?, schedule_time = ? WHERE id = ?", (interval, schedule_time, autorun_id))
            else:
                cur.execute("INSERT INTO autoruns (quiz_id, interval_minutes, schedule_time) VALUES (?, ?, ?)", (quiz_id, interval, schedule_time))
                autorun_id = cur.lastrowid
            conn.commit()

        schedule_autorun_task(context.application, autorun_id, quiz_id, interval, wait_before_start=wait_before_start)
        
        # 🇮🇳 Updated messages with IST timezone info
        if schedule_time:
            success_msg = (
                f"✅ *Autorun Created Successfully!*\n\n"
                f"🎯 *Quiz ID:* `{quiz_id}`\n"
                f"⏰ *Schedule:* Daily at `{schedule_time} IST`\n"
                f"🆔 *Autorun ID:* `{autorun_id}`\n"
                f"⏳ *Join Time:* {wait_before_start} seconds\n\n"
                f"📍 *Timezone:* India Standard Time (IST = UTC+5:30)\n\n"
                f"⏹️ *To stop:* `/stopautorun {autorun_id}`"
            )
            await update.message.reply_text(success_msg, parse_mode="Markdown")
        else:
            success_msg = (
                f"✅ *Autorun Created Successfully!*\n\n"
                f"🎯 *Quiz ID:* `{quiz_id}`\n"
                f"⏰ *Interval:* Every `{interval}` minutes\n"
                f"🆔 *Autorun ID:* `{autorun_id}`\n"
                f"⏳ *Join Time:* {wait_before_start} seconds\n\n"
                f"⏹️ *To stop:* `/stopautorun {autorun_id}`"
            )
            await update.message.reply_text(success_msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in autorun_command: {e}")
        await update.message.reply_text("❌ Error scheduling autorun. Check logs for details.")
        
async def stopautorun_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: /stopautorun <autorun_id|quiz_id|all>
    
    Stop running autoruns by ID or Quiz ID
    Examples:
        /stopautorun 1      → Stop autorun with ID 1
        /stopautorun 5      → Stop all autoruns for quiz ID 5
        /stopautorun all    → Stop ALL autoruns
    """
    try:
        if OWNER_ID is None or update.message.from_user.id != OWNER_ID:
            await update.message.reply_text("❌ Unauthorized - Only bot owner can use this command")
            return
        args = context.args or []
        if not args:
            # 🇮🇳 Help message
            help_text = (
                "📖 *Stop Autorun Command Help*\n\n"
                "🔧 *Usage:* `/stopautorun <autorun_id|quiz_id|all>`\n\n"
                "📋 *Examples:*\n"
                "• `/stopautorun 1` → Autorun ID 1 को stop करो\n"
                "• `/stopautorun 5` → Quiz ID 5 के सब autoruns stop करो\n"
                "• `/stopautorun all` → सब autoruns stop करो\n\n"
                "💡 *Tip:* Autorun ID अपने जब `/autorun` command भेजते हो तब मिलता है"
            )
            await update.message.reply_text(help_text, parse_mode="Markdown")
            return
        key = args[0].lower()
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            
            # ============ CASE 1: Stop ALL autoruns ============
            if key == "all":
                cur.execute("SELECT id FROM autoruns WHERE active = 1")
                active_autoruns = cur.fetchall()
                num_stopped = len(active_autoruns)
                
                cur.execute("UPDATE autoruns SET active = 0 WHERE active = 1")
                conn.commit()
                
                for aid in list(AUTORUN_TASKS.keys()):
                    cancel_autorun_task(aid)
                
                if num_stopped > 0:
                    stop_msg = (
                        f"✅ *All Autoruns Stopped!*\n\n"
                        f"🛑 Total stopped: `{num_stopped}` autorun(s)\n"
                        f"📊 All autoruns have been disabled and workers cancelled."
                    )
                    await update.message.reply_text(stop_msg, parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ No active autoruns found to stop.")
                return
            
            # ============ CASE 2: Stop by Autorun ID ============
            try:
                aid = int(key)
                cur.execute("SELECT quiz_id FROM autoruns WHERE id = ?", (aid,))
                autorun_row = cur.fetchone()
                
                if not autorun_row:
                    await update.message.reply_text(f"❌ Autorun with ID `{aid}` not found.", parse_mode="Markdown")
                    return
                
                quiz_id = autorun_row[0]
                
                cur.execute("UPDATE autoruns SET active = 0 WHERE id = ?", (aid,))
                conn.commit()
                cancel_autorun_task(aid)
                
                stop_msg = (
                    f"✅ *Autorun Stopped Successfully!*\n\n"
                    f"🆔 *Autorun ID:* `{aid}`\n"
                    f"🎯 *Quiz ID:* `{quiz_id}`\n"
                    f"🛑 *Status:* Disabled\n"
                    f"📊 *Worker:* Cancelled"
                )
                await update.message.reply_text(stop_msg, parse_mode="Markdown")
                return
            except ValueError:
                # ============ CASE 3: Stop by Quiz ID ============
                try:
                    qid = int(key)
                    cur.execute("SELECT id FROM autoruns WHERE quiz_id = ? AND active = 1", (qid,))
                    rows = cur.fetchall()
                    
                    if not rows:
                        await update.message.reply_text(f"⚠️ No active autorun found for quiz ID `{qid}`.", parse_mode="Markdown")
                        return
                    
                    num_stopped = len(rows)
                    for (aid,) in rows:
                        cur.execute("UPDATE autoruns SET active = 0 WHERE id = ?", (aid,))
                        cancel_autorun_task(aid)
                    conn.commit()
                    
                    stop_msg = (
                        f"✅ *Autoruns Stopped Successfully!*\n\n"
                        f"🎯 *Quiz ID:* `{qid}`\n"
                        f"🛑 *Total Stopped:* `{num_stopped}` autorun(s)\n"
                        f"📊 *Workers:* All cancelled"
                    )
                    await update.message.reply_text(stop_msg, parse_mode="Markdown")
                    return
                except ValueError:
                    await update.message.reply_text(
                        "❌ Invalid parameter.\n\n"
                        "Provide either:\n"
                        "• Autorun ID (e.g., `1`)\n"
                        "• Quiz ID (e.g., `5`)\n"
                        "• `all` (to stop everything)",
                        parse_mode="Markdown"
                    )
                    return
    except Exception as e:
        logging.error(f"Error in stopautorun_command: {e}")
        await update.message.reply_text("❌ Error stopping autorun. Check logs for details.")
        
# time check 
def next_occurrence_from_hhmm(hhmm_str, ref_dt=None):
    """
    hhmm_str: 'HH:MM' (24-hour IST).
    Returns IST datetime of next occurrence (today or tomorrow).
    Uses datetime.now(tz=IST) if ref_dt is None.
    """
    if not hhmm_str:
        return None
    
    # 🇮🇳 IST time use करो (UTC नहीं)
    if ref_dt is None:
        ref_dt = datetime.now(tz=IST)
    
    try:
        parts = hhmm_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return None
    
    # Build target for today (IST में)
    target = datetime(
        ref_dt.year, 
        ref_dt.month, 
        ref_dt.day, 
        hour, 
        minute,
        tzinfo=IST  # 👈 IST timezone add करो
    )
    
    if target <= ref_dt:
        target = target + timedelta(days=1)
    
    return target

async def build_autorun_list_text():
    """Build active autorun quiz list text."""

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                a.id,
                a.quiz_id,
                q.title,
                a.schedule_time,
                a.interval_minutes,
                a.next_run
            FROM autoruns a
            INNER JOIN quizzes q ON q.quiz_id = a.quiz_id
            WHERE a.active = 1
            ORDER BY
                CASE
                    WHEN a.schedule_time IS NULL THEN 1
                    ELSE 0
                END,
                a.schedule_time ASC,
                a.id ASC
        """)
        rows = cursor.fetchall()

    if not rows:
        return (
            "📋 <b>Active Autorun Quiz List</b>\n\n"
            "⚠️ अभी कोई active autorun quiz configured नहीं है।"
        )

    lines = [
        "📋 <b>Active Autorun Quiz List</b>",
        "🇮🇳 सभी समय IST के अनुसार हैं।",
        ""
    ]

    for index, (
        autorun_id,
        quiz_id,
        title,
        schedule_time,
        interval_minutes,
        next_run
    ) in enumerate(rows, 1):

        # HTML parse mode के लिए special characters escape करें
        safe_title = (
            str(title or "Untitled Quiz")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        if schedule_time:
            schedule_text = f"Daily at <b>{schedule_time} IST</b>"
        else:
            schedule_text = (
                f"Every <b>{interval_minutes} minutes</b>"
            )

        next_run_text = ""

        if next_run:
            try:
                next_run_dt = datetime.fromisoformat(next_run)

                if next_run_dt.tzinfo is None:
                    next_run_dt = next_run_dt.replace(tzinfo=IST)

                next_run_text = (
                    "\n   🔜 Next Run: <b>"
                    f"{next_run_dt.astimezone(IST).strftime('%d-%m-%Y %I:%M %p')} IST"
                    "</b>"
                )
            except (ValueError, TypeError):
                pass

        lines.append(
            f"{index}. 🎯 <b>{safe_title}</b>\n"
            f"   🆔 Quiz ID: <code>{quiz_id}</code>\n"
            f"   ⚙️ Autorun ID: <code>{autorun_id}</code>\n"
            f"   ⏰ Schedule: {schedule_text}"
            f"{next_run_text}\n"
        )

    lines.append(f"💖 <b>Onwer:=> Niraj</b>\n📊 <b>Total Active Autoruns:</b> {len(rows)}")

    return "\n".join(lines)


async def send_autorun_list_to_support(bot, old_message_id=None):
    """Delete old autorun list and send the latest list to support group safely."""

    if not SUPPORT_GROUP_ID:
        logging.warning("SUPPORT_GROUP_ID is not configured")
        return None

    # Delete previous autorun list message safely
    if old_message_id:
        try:
            # Delete call me bhi read_timeout add kiya hai taaki network delay me crash na ho
            await bot.delete_message(
                chat_id=SUPPORT_GROUP_ID,
                message_id=old_message_id,
                read_timeout=15
            )
        except Exception as error:
            logging.warning(
                f"Could not delete old autorun list message: {error}"
            )

    # Database se direct pure items ki raw rows manga lijiye
    # Kyunki lines ko character wise kaatne par HTML tags corrupt ho jate hain.
    list_text = await build_autorun_list_text()

    # Telegram message limit protection (Safe Timeout parameters ke saath)
    if len(list_text) <= 4056:
        try:
            sent_message = await bot.send_message(
                chat_id=SUPPORT_GROUP_ID,
                text=list_text,
                parse_mode="HTML",
                read_timeout=20,   # Telegram ko response dene ke liye 20s ka time diya
                write_timeout=20
            )
            return sent_message.message_id
        except (TimedOut, NetworkError) as e:
            logging.error(f"Timeout/Network error while sending single message: {e}")
            # Ek baar aur retry karte hain thoda ruk kar
            await asyncio.sleep(3)
            try:
                sent_message = await bot.send_message(
                    chat_id=SUPPORT_GROUP_ID, text=list_text, parse_mode="HTML", read_timeout=30
                )
                return sent_message.message_id
            except Exception:
                return None

    # AGAR LIST BAHUT BADI HAI TO SAFE CHUNKING (CHARACTER BY CHARACTER NAHI)
    # Behtar hoga ki build_autorun_list_text() ko lines me hi divide rakhein, 
    # par abhi ke liye character split ko crash-proof banaya gaya hai:
    first_message_id = None
    
    # 4000 ki jagah 3800 ka buffer rakhein taaki HTML tags break na hon
    for start in range(0, len(list_text), 3800):
        chunk = list_text[start:start + 3800]
        
        # HTML tag validation check (Agar tag aadha kata hai to use fix karein ya plain text bhejein)
        # Safe approach: text ko bina parse_mode ke bhej sakte hain agar parsing crash ho rahi ho
        current_parse_mode = "HTML"
        if chunk.count("<") != chunk.count(">"):
            current_parse_mode = None # Tag mismatch hone par plain text bhejega taaki error na aaye

        for retry in range(3): # 3 baar retry karega agar timeout aaye to
            try:
                sent_message = await bot.send_message(
                    chat_id=SUPPORT_GROUP_ID,
                    text=chunk,
                    parse_mode=current_parse_mode,
                    read_timeout=25,
                    write_timeout=25
                )
                
                if first_message_id is None:
                    first_message_id = sent_message.message_id
                
                # Ek chunk bhejte hi mandatory 1.5 second ka pause dein taaki Telegram flood limit ya timeout hit na kare
                await asyncio.sleep(1.5)
                break # Success hote hi retry loop se bahar niklein
                
            except TimedOut:
                logging.warning(
                    f"Timeout hit on chunk {start}, attempt {retry + 1}. Retrying after sleep..."
                )
                await asyncio.sleep(3 * (retry + 1)) # Har fail attempt par zyada wait karega
            except Exception as e:
                logging.error(f"Failed to send chunk due to general error: {e}")
                break

    return first_message_id


async def autorun_list_worker(bot):
    """
    Support group में autorun list हर 1 घंटे में update करता है।
    पुराना list message delete करके नया message भेजता है।
    """

    old_message_id = None

    try:
        while True:
            try:
                old_message_id = await send_autorun_list_to_support(
                    bot=bot,
                    old_message_id=old_message_id
                )

                logging.info(
                    "✅ Autorun list sent to support group. "
                    "Next update after 1 hours."
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                logging.error(
                    f"Error while updating autorun list: {error}",
                    exc_info=True
                )

            # 2 hours = 7200 seconds
            await asyncio.sleep(1 * 60 * 60)

    except asyncio.CancelledError:
        logging.info("Automatic autorun list worker stopped")

        # Worker stop होने पर आखिरी list भी delete कर दें
        if old_message_id:
            try:
                await bot.delete_message(
                    chat_id=SUPPORT_GROUP_ID,
                    message_id=old_message_id
                )
            except Exception:
                pass


async def autorun_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only command.

    /autorunlist:
    - तुरंत autorun list भेजता है
    - हर 1 घंटे में पुराने message को delete करके नया भेजता है
    """

    global AUTORUN_LIST_TASK

    try:
        message = update.message

        if not message:
            return

        if OWNER_ID is None or message.from_user.id != OWNER_ID:
            await message.reply_text(
                "❌ Unauthorized - Only bot owner can use this command."
            )
            return

        if not SUPPORT_GROUP_ID:
            await message.reply_text(
                "❌ SUPPORT_GROUP_ID is not configured in .env"
            )
            return

        # पहले से worker चल रहा है तो उसे बंद करें
        if AUTORUN_LIST_TASK and not AUTORUN_LIST_TASK.done():
            AUTORUN_LIST_TASK.cancel()

            try:
                await AUTORUN_LIST_TASK
            except asyncio.CancelledError:
                pass

        # नया worker शुरू करें
        AUTORUN_LIST_TASK = asyncio.create_task(
            autorun_list_worker(context.bot)
        )

        await message.reply_text(
            "✅ Autorun list automatic update शुरू हो गया है।\n\n"
            "📤 अभी support group में list भेजी गई है।\n"
            "🔄 हर 1 घंटे में पुराना message delete करके नया list भेजा जाएगा।\n\n"
            "⏳ बंद करने के लिए: /stopautorunlist"
        )

    except Exception as error:
        logging.error(
            f"Error starting autorun list worker: {error}",
            exc_info=True
        )

        if update.message:
            await update.message.reply_text(
                "❌ Autorun list automatic update शुरू नहीं हो सका।"
            )


async def stop_autorun_list_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """Stop automatic autorun list updates."""

    global AUTORUN_LIST_TASK

    try:
        if OWNER_ID is None or update.message.from_user.id != OWNER_ID:
            await update.message.reply_text(
                "❌ Unauthorized - Only bot owner can use this command."
            )
            return

        if AUTORUN_LIST_TASK and not AUTORUN_LIST_TASK.done():
            AUTORUN_LIST_TASK.cancel()

            try:
                await AUTORUN_LIST_TASK
            except asyncio.CancelledError:
                pass

            AUTORUN_LIST_TASK = None

            await update.message.reply_text(
                "✅ Automatic autorun list update बंद कर दिया गया है।"
            )
        else:
            await update.message.reply_text(
                "⚠️ Automatic autorun list पहले से बंद है।"
            )

    except Exception as error:
        logging.error(
            f"Error stopping autorun list worker: {error}",
            exc_info=True
)

async def ask_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers any user query in detail using Gemini AI with Real-Time Date/Time context ONLY in Hindi"""
    try:
        message = update.message
        if not message:
            return

        # यूज़र की सुरक्षा जाँच (Authorization Check)
        if not is_authorized(update):
            await message.reply_text("❌ <b>Sorry!</b> Yah command keval authorized users hi use kar sakte hain.", parse_mode="HTML")
            return

        # चेक करें कि सवाल भेजा गया है या नहीं
        if not context.args:
            await message.reply_text(
                "💡 <b>Ask AI Command Help:</b>\n\n"
                "Aapko jo bhi puchna hai, command ke sath likhein.\n"
                "👉 <b>Example:</b> <code>/ask Aaj ka current affairs kya hai?</code>",
                parse_mode="HTML"
            )
            return

        user_query = " ".join(context.args)

        if not ai_client:
            await message.reply_text("⚠️ <b>AI Client Error:</b> Gemini API key missing.", parse_mode="HTML")
            return

        processing_msg = await message.reply_text(
            "🤖 <b>AI is thinking...</b>\n🔎 Fetching accurate real-time details...",
            parse_mode="HTML"
        )

        # 🇮🇳 LIVE TIMESTAMP LOGIC (IST)
        now_ist = datetime.now(tz=IST)
        current_date_str = now_ist.strftime("%A, %d %B %Y")  # Example: Monday, 05 October 2026
        current_time_str = now_ist.strftime("%I:%M %p")      # Example: 02:42 PM

        # 🔥 SYSTEM PROMPT FOR STRICT HINDI & TELEGRAM HTML FORMAT
        prompt = f"""You are an expert AI assistant with real-time awareness.
        
CRITICAL REAL-TIME CONTEXT:
- Current Time: {current_time_str} (IST)
- Current Date/Day: {current_date_str}
- Current Year: {now_ist.year}

⚠️ LANGUAGE RULE:
You MUST write your entire response ONLY in Hindi language (हिंदी भाषा). Even if the user query is written in English or Hinglish, your explanation, points, and summary must be completely in Hindi.

⚠️ CRITICAL TELEGRAM HTML FORMATTING RULES:
1. DO NOT use markdown like **, ***, ###, or __ anywhere in your response. 
2. To make text BOLD, use HTML tags like <b>text here</b>. (e.g., use <b>1. मुख्य समाचार</b> instead of ### **1. मुख्य समाचार**)
3. To make text ITALIC, use <i>text here</i>.
4. For separating sections, use a simple line like "━━━━━━" and blank lines.
5. Bullet points should look like this: • Your point here.

Ensure your entire output strictly complies with Telegram HTML parsing mode. Do not leave any loose markdown symbols or stars (**).

User Query: {user_query}"""

        try:
            response = await asyncio.to_thread(
                ai_client.interactions.create,
                model='gemini-3.5-flash-lite',
                input=prompt,
            )

            if not response or not response.output_text:
                await processing_msg.edit_text("⚠️ AI ne koi response generate nahi kiya.")
                return

            ai_response = response.output_text.strip()

            # लोडिंग मैसेज डिलीट करें
            await processing_msg.delete()
            
            # बिना एस्केप किए डायरेक्ट HTML रिस्पांस सेंड करें
            await message.reply_text(
                f"🧠 <b>AI Detailed Response:</b>\n\n{ai_response}",
                parse_mode="HTML"
            )

        except Exception as ai_err:
            logging.error(f"Error calling Gemini in ask_ai_command: {ai_err}")
            if "429" in str(ai_err) or "too_many_requests" in str(ai_err):
                await processing_msg.edit_text("⚠️ <b>API Limit Exceeded:</b> Please try again later.", parse_mode="HTML")
            else:
                await processing_msg.edit_text("❌ AI response fetch karne me error aa gaya.")

    except Exception as e:
        logging.error(f"Critical error in ask_ai_command: {e}", exc_info=True)

# 🔒 SECURED COMMAND: /question <quiz_id> (OWNER & ALLOWED USERS ONLY)
async def export_questions_json_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches all questions of a quiz and sends them in a click-to-copy JSON format with strict chunking limits"""
    try:
        message = update.message
        if not message:
            return

        user_id = message.from_user.id
        allowed_ids = get_allowed_ids()  # .env se clean IDs load karne ka aapka helper function

        # 🔒 STRICT ACCESS LOCK CRITERIA
        if user_id != OWNER_ID and user_id not in allowed_ids:
            await message.reply_text(
                "❌ <b>Access Denied:</b> Yah command keval Bot Owner aur Authorized Users hi use kar sakte hain.", 
                parse_mode="HTML"
            )
            return

        # Check if argument (quiz_id) is provided
        args = context.args or []
        if len(args) < 1:
            await message.reply_text(
                "💡 <b>Usage Help:</b>\n"
                "Kripya command ke sath Quiz ID bhejein.\n"
                "👉 <b>Example:</b> <code>/question 5</code>",
                parse_mode="HTML"
            )
            return

        try:
            quiz_id = int(args[0])
        except ValueError:
            await message.reply_text("❌ <b>Invalid Quiz ID!</b> Kripya sirf ek number type karein. Example: <code>/question 5</code>", parse_mode="HTML")
            return

        # Fetch Quiz and Language context from Database
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT title, creator_id FROM quizzes WHERE quiz_id = ?", (quiz_id,))
        quiz_data = cursor.fetchone()
        
        if not quiz_data:
            await message.reply_text(f"❌ <b>Error:</b> Quiz ID <code>{quiz_id}</code> database me nahi mila.", parse_mode="HTML")
            conn.close()
            return
        
        quiz_title, creator_id = quiz_data

        # Fetch all questions mapped to this quiz_id
        cursor.execute("SELECT question_text, options, correct_answer, explanation FROM questions WHERE quiz_id = ?", (quiz_id,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            await message.reply_text(f"⚠️ Is quiz (<b>{html_escape(quiz_title)}</b>) ke andar koi bhi question save nahi hai.", parse_mode="HTML")
            return

        processing_msg = await message.reply_text("⚡ <b>Fetching and structuring JSON payload...</b>", parse_mode="HTML")

        # Map rows into your exact custom schema format
        custom_json_list = []
        for q_text, options_json, correct_ans, explanation in rows:
            options = json.loads(options_json)
            
            # Correct answer integer index conversion logic (Safeguard)
            try:
                correct_id = int(correct_ans)
            except (ValueError, TypeError):
                try:
                    correct_id = options.index(str(correct_ans))
                except ValueError:
                    correct_id = 0

            # Default language detect baseline fallback
            # Agar pure Hindi characters hain toh hindi, nahi toh default english strings
            detected_lang = "hindi" if any(ord(char) > 2300 and ord(char) < 2430 for char in q_text) else "english"

            custom_json_list.append({
                "question": q_text,
                "options": options,
                "correct_id": correct_id,
                "lang": detected_lang,
                "explanation": explanation if explanation else ""
            })

        # 📦 OBJECT-SAFE CHUNKING LOOP (Strict 3800 character message box limitation)
        current_chunk = []
        chunk_serial = 1

        await processing_msg.delete()

        for q_object in custom_json_list:
            test_list = current_chunk + [q_object]
            test_json_str = json.dumps(test_list, indent=2, ensure_ascii=False)
            wrapped_len = len(f"📋 <b>Quiz {quiz_id} JSON Box (Part 1):</b>\n<pre>{html_escape(test_json_str)}</pre>")
            
            if wrapped_len > 3800 and current_chunk:
                final_str = json.dumps(current_chunk, indent=2, ensure_ascii=False)
                await message.reply_text(
                    text=f"📋 <b>Quiz {quiz_id} JSON Box (Part {chunk_serial}):</b>\n<pre>{html_escape(final_str)}</pre>",
                    parse_mode="HTML"
                )
                await asyncio.sleep(0.5)  # API Flood lock security protection
                current_chunk = [q_object]
                chunk_serial += 1
            else:
                current_chunk.append(q_object)

        # Remaining data chunk delivery
        if current_chunk:
            final_str = json.dumps(current_chunk, indent=2, ensure_ascii=False)
            part_title = f" (Part {chunk_serial})" if chunk_serial > 1 else ""
            await message.reply_text(
                text=f"📋 <b>Quiz {quiz_id} JSON Box{part_title}:</b>\n<pre>{html_escape(final_str)}</pre>",
                parse_mode="HTML"
            )

    except Exception as e:
        logging.error(f"Error executing /question command setup: {e}", exc_info=True)
        if 'message' in locals():
            await message.reply_text("❌ Questions export loop executing process failed internal error.")

# ⚡ send message to support group (only use owner)
async def send_to_support_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: In private chat reply to a message and copy it (with buttons if any) to SUPPORT_GROUP_ID."""
    try:
        message = update.message
        if not message:
            return

        # Owner-only guard
        if OWNER_ID is None or message.from_user.id != OWNER_ID:
            await message.reply_text("❌ Unauthorized — this command is only for the bot owner.")
            return

        # Ensure used in private chat
        chat = message.chat
        is_private = str(chat.type) == "private" or (hasattr(chat.type, "value") and chat.type.value == "private")
        if not is_private:
            await message.reply_text("⚠️ This command works only in a private chat. Reply to the message here and send /send.")
            return

        # Ensure support group id configured
        if not SUPPORT_GROUP_ID:
            await message.reply_text("❌ SUPPORT_GROUP_ID is not configured. Set SUPPORT_GROUP_ID in the .env first.")
            return

        # Ensure the user replied to a message
        if not message.reply_to_message:
            await message.reply_text("❗ Please reply to the message (text/photo/video/sticker/etc.) you want to send to the support group, then send /send.")
            return

        target_msg = message.reply_to_message

        # If the original message has an inline keyboard or other reply_markup, pass it through.
        reply_markup = getattr(target_msg, "reply_markup", None)

        try:
            # copy_message preserves content; pass reply_markup so buttons are copied too (if present).
            await context.bot.copy_message(
                chat_id=SUPPORT_GROUP_ID,
                from_chat_id=target_msg.chat.id,
                message_id=target_msg.message_id,
                reply_markup=reply_markup
            )
            await message.reply_text("✅ Message successfully sent to the support group (buttons preserved if present).")
        except Exception as e:
            logging.error(f"Failed to send message to support group ({SUPPORT_GROUP_ID}): {e}", exc_info=True)
            # Friendly error to the user (likely bot missing from group or missing permissions)
            await message.reply_text(
                "❌ Could not send the message to the support group. "
                "Ensure the bot is a member of the configured support group and has permission to send messages."
            )

    except Exception as e:
        logging.error(f"Error in send_to_support_group: {e}", exc_info=True)
        try:
            await update.message.reply_text("❌ An unexpected error occurred. Please try again.")
        except Exception:
            pass

async def auto_quiz_summary_monitor(bot):
    """
    नया स्वतंत्र फ़ंक्शन: यह बैकग्राउंड में डेटाबेस को मॉनिटर करता है।
    जैसे ही autorun_worker कोई नई क्विज़ सेव करता है, यह बिना पुराना कोड बदले 
    उसकी समरी सीधे सपोर्ट चैनल (SUPPORT_GROUP_ID) में भेज देता है।
    """
    logging.info("🚀 Background Auto-Quiz Summary Monitor started successfully.")
    
    # सबसे आखरी क्विज़ आईडी का पता लगाएं ताकि सिर्फ नए वाले ही चैनल पर जाएं
    last_known_quiz_id = 0
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(quiz_id) FROM quizzes")
        row = cursor.fetchone()
        if row and row[0]:
            last_known_quiz_id = int(row[0])
        conn.close()
    except Exception as e:
        logging.error(f"Error initializing monitor baseline ID: {e}")

    while True:
        try:
            # हर 5 सेकंड में चेक करेगा कि कोई नई क्विज़ बनी है या नहीं
            await asyncio.sleep(5)
            
            if not SUPPORT_CHANNEL_ID:
                continue

            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # सिर्फ वही क्विज़ उठाएं जो ऑटोमैटिक जनरेट हुई हैं (Auto AI Quiz वाली)
            cursor.execute(
                "SELECT quiz_id, title, description, timer FROM quizzes WHERE quiz_id > ? AND title LIKE 'Auto AI Quiz:%' ORDER BY quiz_id ASC", 
                (last_known_quiz_id,)
            )
            new_quizzes = cursor.fetchall()
            
            for quiz_id, title, description, timer in new_quizzes:
                # कुल प्रश्नों की संख्या निकालें
                cursor.execute("SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (quiz_id,))
                total_q = cursor.fetchone()[0]
                
                time_display = f"{timer} sec" if timer < 60 else f"{timer // 60} min"
                bot_username = bot.username if bot.username else "quiz_bot"
                
                # HTML के लिए टाइटल और डिस्क्रिप्शन को सुरक्षित करें
                escaped_title = escape_markdown(title)
                escaped_desc = escape_markdown(description) if description else "No description"
                
                summary_text = (
                    "<b>🤖 NEW AUTOMATIC QUIZ GENERATED! 🚀</b>\n"
                    "यह क्विज़ ऑटोमैटिक जनरेट होकर डेटाबेस में सुरक्षित सेव हो चुकी है।\n\n"
                    f"📒 <b>Title:</b> {escaped_title}\n"
                    f"🫥 <b>Description:</b> {escaped_desc}\n"
                    f"⚡ {total_q} questions · ⏱ Time: {time_display}\n\n"
                    f"<blockquote>🔗 External sharing link:</blockquote>\n"
                    f"<tg-spoiler>https://t.me/{bot_username}?start=quiz_{quiz_id}</tg-spoiler>"
                )

                inline_keyboard = [
                    [{"text": "Start Quiz In Private Chat 🎮", "url": f"https://t.me/{bot_username}?start=quiz_{quiz_id}", "style": "primary"}],
                    [{"text": "Start Quiz in Group 👥", "url": f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}", "style": "success"}],
                    # 🔥 FIXED: switch_inline_query_current_chat को हटाकर URL शेयर लिंक लगाया है जो चैनल में 100% काम करेगा
                    [{"text": "Share Quiz 📢", "url": f"https://t.me/share/url?url=https://t.me/{bot_username}?start=quiz_{quiz_id}&text=Check%20out%20this%20awesome%20quiz!", "style": "danger"}]
                ]
                reply_markup = InlineKeyboardMarkup(inline_keyboard)

                # सपोर्ट चैनल/ग्रुप में समरी पैनल सेंड करें
                await bot.send_message(
                    chat_id=SUPPORT_CHANNEL_ID, 
                    text=summary_text, 
                    reply_markup=reply_markup, 
                    parse_mode="HTML"
                )
                logging.info(f"✨ [Monitor] Successfully sent Quiz ID {quiz_id} Summary Panel to Support Channel.")
                
                # बेसलाइन आईडी अपडेट करें ताकि दोबारा सेम मैसेज न जाए
                last_known_quiz_id = quiz_id
                
            conn.close()
            
        except Exception as monitor_err:
            logging.error(f"Error inside auto_quiz_summary_monitor loop: {monitor_err}", exc_info=True)

# =====================================================================

async def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN not found in environment variables!")
        return
    
    try:
        init_db()
        migrate_fix_correct_answer()  # 🟢 ADD THIS LINE
        
        request_config = HTTPXRequest(
            connect_timeout=35.0,
            read_timeout=45.0,
            write_timeout=35.0
        )
        
        # ... rest of the code ...
        
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .request(request_config)
            .build()
        )
        
        # 🔁 CONVERSATION HANDLERS
        new_quiz_handler = ConversationHandler(
            entry_points=[
                CommandHandler("newquiz", new_quiz_start),
                CallbackQueryHandler(new_quiz_start, pattern="^btn_newquiz$")
            ],
            states={
                TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
                DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc), CommandHandler("skip", receive_desc)],
                QUESTIONS: [CommandHandler("undo", handle_undo), CommandHandler("done", finish_quiz_creation), MessageHandler(filters.POLL, receive_poll)],
                PRE_MESSAGE: [
                    CommandHandler("undo", handle_undo),
                    CommandHandler("skip", receive_pre_message),
                    MessageHandler(filters.POLL, receive_pre_message),
                    MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION) & ~filters.COMMAND, receive_pre_message)
                ],
                TIMER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_timer_text),
                    CallbackQueryHandler(handle_timer_text, pattern="^timer_")
                ],
                NEGATIVE: [
                    CallbackQueryHandler(handle_negative_selection, pattern="^neg_")
                ]
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        quiz_edit_flow_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(edit_title_trigger, pattern="^edtitle_"),
                CallbackQueryHandler(edit_desc_trigger, pattern="^eddesc_"),
                CallbackQueryHandler(edit_timer_trigger, pattern="^edtime_"),
                CallbackQueryHandler(edit_negative_trigger, pattern="^edneg_"),
                CallbackQueryHandler(edit_pre_message_trigger, pattern="^editpre_"),
                CallbackQueryHandler(edit_explanation_trigger, pattern="^editexpl_")
            ],
            states={
                EDIT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_title)],
                EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_desc)],
                EDIT_TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_timer)],
                EDIT_NEGATIVE: [CallbackQueryHandler(save_edited_negative, pattern="^updeneg_")],
                EDIT_QUESTION_PRE_MESSAGE: [MessageHandler(filters.TEXT, save_pre_message)],
                EDIT_QUESTION_EXPLANATION: [MessageHandler(filters.TEXT, save_explanation)]
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        )

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("autoquiz", autoquiz_start)],
            states={
                # 📝 मोड 1: टेक्स्ट इनपुट मोड (यहाँ बटन का शुरुआती गेटवे और टेक्स्ट रिसीवर दोनों हैं)
                TOPIC: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic), 
                    CallbackQueryHandler(handle_topic_mode_routing, pattern="^mode_"), 
                 ], 
                # 📂 मोड 2: प्योर बटन मोड (यहाँ कोई MessageHandler नहीं है, इसलिए टेक्स्ट काम नहीं करेगा!)
                BUTTON_TOPIC: [
                    CallbackQueryHandler(handle_subject_navigation, pattern="^sub_"), 
                    CallbackQueryHandler(handle_final_topic_selection, pattern="^set_topic_"), 
                    CallbackQueryHandler(handle_topic_mode_routing, pattern="^back_to_subjects_nav$") 
                ],
                Q_COUNT: [
                    CallbackQueryHandler(handle_q_count, pattern="^qcnt_"),
                 ],
                TITLE: [
                    CallbackQueryHandler(handle_title, pattern="^title_"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title) # ✅ मैसेज इनपुट चालू है
                 ],
                DESCRIPTION: [
                    CallbackQueryHandler(handle_description, pattern="^desc_skip$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_description) # ✅ मैसेज इनपुट चालू है
                ],
                LANGUAGE: [
                    CallbackQueryHandler(handle_language, pattern="^lang_"),
                ],
                EXPLANATION: [
                    CallbackQueryHandler(handle_explanation, pattern="^expl_"),
                ],
                DIFFICULTY: [
                    CallbackQueryHandler(handle_difficulty, pattern="^diff_"),
                ],
                OPTIONS_COUNT: [
                    CallbackQueryHandler(handle_options_count, pattern="^opts_"),
                ],
                TIME_LIMIT: [
                    CallbackQueryHandler(handle_time_limit, pattern="^time_"),
                ],
                NEGATIVE: [CallbackQueryHandler(handle_negative_and_finish, pattern="^neg_")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        # ✅ FIXED: Use 'app' instead of 'application'
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("quizzes", quizzes_command))
        app.add_handler(CommandHandler("stop", stop_quiz))
        app.add_handler(CommandHandler("status", owner_status_text_command))
        app.add_handler(CallbackQueryHandler(quiz_page_callback, pattern="^quizpage_"))
        app.add_handler(CallbackQueryHandler(close_buttons_callback, pattern="^close_quiz_list$"))
        app.add_handler(CallbackQueryHandler(close_buttons_callback, pattern="^delete_this_msg$"))
        
        app.add_handler(new_quiz_handler)
        app.add_handler(quiz_edit_flow_handler)
        app.add_handler(conv_handler)

        # Core system triggers binding maps
        app.add_handler(CallbackQueryHandler(view_my_quizzes, pattern="^btn_viewquizzes$"))
        app.add_handler(CallbackQueryHandler(handle_back_main, pattern="^back_main$"))
        app.add_handler(CallbackQueryHandler(handle_view_quiz_callback, pattern="^viewq_"))
        
        app.add_handler(CallbackQueryHandler(handle_ready_click, pattern="^ready_"))
        app.add_handler(CallbackQueryHandler(handle_start_private, pattern="^startprivate_"))
        app.add_handler(CallbackQueryHandler(handle_confirm_private, pattern="^confirm_private_"))
        app.add_handler(CallbackQueryHandler(handle_quiz_status, pattern="^status_"))
        app.add_handler(CallbackQueryHandler(edit_quiz_menu, pattern="^edit_"))
        app.add_handler(CallbackQueryHandler(back_to_summary, pattern="^backto_"))
        app.add_handler(CallbackQueryHandler(edit_question_trigger, pattern="^edquestion_"))
        app.add_handler(CallbackQueryHandler(handle_question_detail, pattern="^editq_"))
        app.add_handler(CallbackQueryHandler(handle_delete_question, pattern="^delq_"))
        app.add_handler(CallbackQueryHandler(confirm_delete_question, pattern="^confirmdel_"))
        app.add_handler(CommandHandler("broadcast", broadcast_command))
        app.add_handler(CallbackQueryHandler(execute_broadcast_callback, pattern="^bcast_"))
        app.add_handler(CommandHandler("send", send_to_support_group))
        
        app.add_handler(CallbackQueryHandler(handle_pause_quiz, pattern="^pausequiz_"))
        app.add_handler(CallbackQueryHandler(handle_stop_quiz_from_pause, pattern="^stopquiz_"))
        app.add_handler(CommandHandler("autorun", autorun_command))
        app.add_handler(CommandHandler("stopautorun", stopautorun_command))
        app.add_handler(CommandHandler("autorunlist", autorun_list_command))
        app.add_handler(CommandHandler("stopautorunlist", stop_autorun_list_command))
        app.add_handler(CommandHandler("ask", ask_ai_command))
        app.add_handler(CommandHandler("question", export_questions_json_command))

        app.add_handler(CommandHandler("startgpoll", start_global_polling_command))
        app.add_handler(CommandHandler("stopgpoll", stop_global_polling_command))
        app.add_handler(CommandHandler("savetopic", savetopic_custom_topic_command))
        
        app.add_handler(PollAnswerHandler(track_poll_answers))
        app.add_handler(InlineQueryHandler(inline_query_handler))
        
        # 🚀 BOT RUN/POLLING INITIALIZATION
        logging.info("Starting Quiz Bot polling...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await load_autoruns_on_startup(app)
        asyncio.create_task(global_continuous_polling_worker(app))
        asyncio.create_task(auto_quiz_summary_monitor(app.bot)) 
        await asyncio.Event().wait()

    except Exception as e:
        logging.error(f"Critical error in main loop: {e}")
        
# 🛑 EXECUTION LOOPS CLOSURE:
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot execution stopped clean.")
        
