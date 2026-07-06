import os
import time
import sqlite3
import threading
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# 📚 दूसरी फाइल से प्रश्न इम्पोर्ट करें
from questions import QUIZ_LIST

# .env से सभी क्रेडेंशियल्स लोड करें
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if not API_TOKEN:
    raise ValueError("Error: BOT_TOKEN एनवायरनमेंट वेरिएबल्स में नहीं मिला!")

bot = telebot.TeleBot(API_TOKEN)
DB_FILE = "bot_data.db"

if OWNER_ID:
    try:
        OWNER_ID = int(OWNER_ID)
    except ValueError:
        OWNER_ID = None

# 💾 डेटाबेस इनिशियलाइजेशन (interval डिफ़ॉल्ट रूप से 1800 सेकंड = 30 मिनट फिक्स है)
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                current_index INTEGER DEFAULT 0,
                last_poll_id INTEGER DEFAULT NULL,
                last_sent_time REAL DEFAULT 0,
                language TEXT DEFAULT 'hindi',
                interval INTEGER DEFAULT 1800,
                auto_delete INTEGER DEFAULT 1
            )
        ''')
        conn.commit()

init_db()

 # 🛡️ एडमिन चेक करने का हेल्पर फंक्शन
def is_user_admin(chat_id, user_id):
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False


  # 🔄 हर ग्रुप के कस्टमाइज्ड टाइम और लैंग्वेज के अनुसार पोल मैनेज करने वाला फंक्शन
def global_poll_manager():
    while True:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT chat_id, current_index, last_poll_id, last_sent_time, language, interval, auto_delete FROM groups")
                all_groups = cursor.fetchall()

                current_now = time.time()

                for chat_id, current_index, last_poll_id, last_sent_time, language, interval, auto_delete in all_groups:
                    if current_now - last_sent_time >= interval:
                        
                        # 🗑️ पुराना पोल डिलीट लॉजिक
                        if last_poll_id is not None and auto_delete == 1:
                            try:
                                bot.delete_message(chat_id=chat_id, message_id=last_poll_id)
                                print(f"Group {chat_id} से पुराना पोल डिलीट कर दिया गया।")
                            except Exception as e:
                                print(f"Group {chat_id} में पुराना पोल डिलीट नहीं हो सका: {e}")

                        # 🌐 ग्रुप की भाषा के आधार पर प्रश्नों को फ़िल्टर करें
                        filtered_quiz = [q for q in QUIZ_LIST if q.get("lang", "hindi") == language]

                        # ⚠️ सुरक्षा जांच: अगर चुनी गई भाषा का कोई प्रश्न न मिले तो डिफ़ॉल्ट रूप से सब दिखाएं
                        if not filtered_quiz:
                            filtered_quiz = QUIZ_LIST

                        # इंडेक्स आउट ऑफ़ बाउंड (Index Out of Bound) से बचने के लिए चेक
                        if current_index >= len(filtered_quiz):
                            current_index = 0

                        quiz = filtered_quiz[current_index]
                        explanation_text = quiz.get("explanation", None)
                        
                        try:
                            sent_message = bot.send_poll(
                                chat_id=chat_id,
                                question=quiz["question"],
                                options=quiz["options"],
                                type="quiz",
                                correct_option_id=quiz["correct_id"],
                                is_anonymous=True,
                                explanation=explanation_text
                            )
                            new_poll_id = sent_message.message_id
                            
                            # अगले पोल के लिए इंडेक्स को अपडेट करें
                            new_index = (current_index + 1) % len(filtered_quiz)

                            cursor.execute('''
                                UPDATE groups 
                                SET current_index = ?, last_poll_id = ?, last_sent_time = ? 
                                WHERE chat_id = ?
                            ''', (new_index, new_poll_id, current_now, chat_id))
                            conn.commit()
                            print(f"Group {chat_id} में नया पोल भेजा गया (Language: {language}, Index: {current_index})।")

                        except Exception as e:
                            print(f"Group {chat_id} में पोल भेजने में एरर: {e}")
                            if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                                cursor.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
                                conn.commit()
        except Exception as db_err:
            print(f"डेटाबेस लूप एरर: {db_err}")
        
        time.sleep(5)

 # ⚙️ मुख्य सेटिंग्स मेनू यूआई जेनरेटर
def get_settings_markup(chat_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT language, interval, auto_delete FROM groups WHERE chat_id = ?", (chat_id,))
        res = cursor.fetchone()
    
    if not res:
        return None, None
        
    lang, interval, auto_delete = res
    interval_mins = interval // 60
    del_status = "ON ✅" if auto_delete == 1 else "OFF 📴"
    
    text = (
        "⚙️ **ग्रुप क्विज़ सेटिंग्स (Quiz Settings)**\n\n"
        f"🌐 **वर्तमान भाषा (Language):** {lang.upper()}\n"
        f"⏱️ **क्विज़ अंतराल (Interval):** {interval_mins} मिनट\n"
        f"🗑️ **ऑटो-डिलीट स्टेटस:** {del_status}\n\n"
        "अपनी सेटिंग्स बदलने के लिए नीचे दिए गए बटनों का उपयोग करें:"
    )
    
    markup = InlineKeyboardMarkup()
    
    lang_text = "🌐 भाषा: HINDI 🇮🇳" if lang == 'hindi' else "🌐 Lang: ENGLISH 🇬🇧"
    btn_lang = InlineKeyboardButton(text=lang_text, callback_data=f"set_lang_{chat_id}")
    btn_autodel = InlineKeyboardButton(text="🗑️ Auto-Delete Settings", callback_data=f"menu_autodel_{chat_id}")
    
    # ⏱️ नए कस्टमाइज्ड टाइम बटन्स (5, 10, 20, 30 मिनट)
    btn_5m = InlineKeyboardButton(text="⏱️ 5 Min", callback_data=f"set_time_300_{chat_id}")
    btn_10m = InlineKeyboardButton(text="⏱️ 10 Min", callback_data=f"set_time_600_{chat_id}")
    btn_20m = InlineKeyboardButton(text="⏱️ 20 Min", callback_data=f"set_time_1200_{chat_id}")
    btn_30m = InlineKeyboardButton(text="⏱️ 30 Min", callback_data=f"set_time_1800_{chat_id}")
    
    markup.row(btn_lang)
    markup.row(btn_autodel)
    markup.row(btn_5m, btn_10m)
    markup.row(btn_20m, btn_30m)
    
    return text, markup

# 🗑️ ऑटो-डिलीट सेटिंग्स का सब-मेनू जेनरेटर
def get_autodelete_markup(chat_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_delete FROM groups WHERE chat_id = ?", (chat_id,))
        res = cursor.fetchone()
        
    auto_delete = res if res else 1
    status_text = "ON" if auto_delete == 1 else "OFF"
    
    text = (
        "🔐 **Auto-Delete Settings** 🗑\n\n"
        f"📊 **Current Status:** \" {status_text} \"\n\n"
        "🤷 **What does this do?**\n"
        "• When ON: Previous quiz will be automatically deleted\n"
        "• When OFF: Previous quiz will remain in chat\n\n"
        "⚡ Change auto-delete setting:"
    )
    
    markup = InlineKeyboardMarkup()
    btn_on = InlineKeyboardButton(text="Turn On ✅", callback_data=f"autodel_on_{chat_id}")
    btn_off = InlineKeyboardButton(text="Turn Off 📴", callback_data=f"autodel_off_{chat_id}")
    btn_back = InlineKeyboardButton(text="Back 🔙", callback_data=f"autodel_back_{chat_id}")
    
    markup.row(btn_on, btn_off)
    markup.row(btn_back)
    
    return text, markup

# 💬 /settings कमांड हैंडलर
@bot.message_handler(commands=['settings'], chat_types=['group', 'supergroup'])
def group_settings(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not is_user_admin(chat_id, user_id):
        bot.reply_to(message, "❌ केवल ग्रुप के एडमिन ही सेटिंग्स बदल सकते हैं।")
        return
        
    text, markup = get_settings_markup(chat_id)
    if text:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ ग्रुप डेटा अभी उपलब्ध नहीं है। कृपया पहले पोल का इंतजार करें।")

# 🔄 सेटिंग्स बटन के क्लिक को हैंडल करने वाला कॉलबैक प्रोसेसर
@bot.callback_query_handler(func=lambda call: call.data.startswith(('set_lang_', 'set_time_', 'menu_autodel_', 'autodel_')))
def handle_settings_callbacks(call):
    user_id = call.from_user.id
    data_parts = call.data.split('_')
    
    action = data_parts[0]       # 'set', 'menu' या 'autodel'
    sub_action = data_parts[1]   # 'lang', 'time', 'autodel', 'on', 'off' या 'back'
    chat_id = int(data_parts[-1]) # चैट आईडी हमेशा आखिरी एलिमेंट होता है
    
    if not is_user_admin(chat_id, user_id):
        bot.answer_callback_query(call.id, "❌ आपके पास एडमिन परमिशन नहीं है!", show_alert=True)
        return

    show_main_menu = True
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # 🌐 भाषा बदलने का लॉजिक
        if action == "set" and sub_action == "lang":
            cursor.execute("SELECT language FROM groups WHERE chat_id = ?", (chat_id,))
            current_lang = cursor.fetchone()
            current_lang = current_lang[0] if current_lang else 'hindi'
            new_lang = 'english' if current_lang == 'hindi' else 'hindi'
            cursor.execute("UPDATE groups SET language = ? WHERE chat_id = ?", (new_lang, chat_id))
            bot.answer_callback_query(call.id, f"भाषा बदलकर {new_lang.upper()} कर दी गई है।")
            
        # ⏱️ समय अंतराल बदलने का लॉजिक
        elif action == "set" and sub_action == "time":
            new_interval = int(data_parts[2]) # यहाँ तीसरे इंडेक्स (data_parts[2]) से टाइम निकाला गया है
            cursor.execute("UPDATE groups SET interval = ? WHERE chat_id = ?", (new_interval, chat_id))
            bot.answer_callback_query(call.id, f"समय अंतराल बदलकर {new_interval // 60} मिनट कर दिया गया है।")
            
        # 🗑️ ऑटो-डिलीट सब-मेनू ओपन करने का लॉजिक
        elif action == "menu" and sub_action == "autodel":
            show_main_menu = False
            bot.answer_callback_query(call.id) # टेलीग्राम का लोडिंग सर्कल हटाने के लिए empty रिस्पॉन्स
            
        # ⚙️ ऑटो-डिलीट सब-मेनू की क्रियाएं
        elif action == "autodel":
            if sub_action == "on":
                cursor.execute("UPDATE groups SET auto_delete = 1 WHERE chat_id = ?", (chat_id,))
                bot.answer_callback_query(call.id, "Auto-Delete चालू (ON) कर दिया गया है।")
                show_main_menu = False
            elif sub_action == "off":
                cursor.execute("UPDATE groups SET auto_delete = 0 WHERE chat_id = ?", (chat_id,))
                bot.answer_callback_query(call.id, "Auto-Delete बंद (OFF) कर दिया गया है।")
                show_main_menu = False
            elif sub_action == "back":
                bot.answer_callback_query(call.id, "मुख्य मेनू पर वापस जा रहे हैं...")
                show_main_menu = True
                
        conn.commit()
        
    # सही यूआई रेंडर (मैसेज एडिट) करना
    if show_main_menu:
        text, markup = get_settings_markup(chat_id)
    else:
        text, markup = get_autodelete_markup(chat_id)
        
    try:
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        print(f"मैसेज एडिट एरर (शायद टेक्स्ट में कोई बदलाव नहीं हुआ): {e}")
        
  # 💬 चैट (प्राइवेट और ग्रुप दोनों) में /start कमांड हैंडलर
@bot.message_handler(commands=['start'], chat_types=['private', 'group', 'supergroup'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_type = message.chat.type

    if chat_type in ['group', 'supergroup']:
        group_name = message.chat.title
        group_text = (
            f"👋 नमस्ते {message.from_user.first_name}!\n\n"
            f"🤖 मैं इस ग्रुप (**{group_name}**) में पूरी तरह एक्टिव हूँ।\n"
            f"🎯 मेरा काम आपके द्वारा चुने गए समय पर क्विज़ पोल भेजना है।\n\n"
            f"⚙️ **एडमिन ध्यान दें:** भाषा, समय और ऑटो-डिलीट बदलने के लिए ग्रुप में `/settings` टाइप करें।"
        )
        try:
            bot.reply_to(message, text=group_text, parse_mode="Markdown")
        except Exception as e:
            print(f"ग्रुप में /start रिप्लाई भेजने में एरर: {e}")
        return

    if OWNER_ID and user_id == OWNER_ID:
        welcome_text = f"👑 **प्रणाम मालिक ({message.from_user.first_name})!**\n\nआपका क्विज़ पोल बॉट नए समय विकल्पों (5, 10, 20, 30 मिनट) के साथ पूरी तरह से एक्टिव है।"
    else:
        welcome_text = f"👋 नमस्ते {message.from_user.first_name}!\n\n🤖 मैं एक ऑटोमैटिक क्विज़ पोल बॉट हूँ। मुझे अपने ग्रुप में जोड़ें और कॉन्फ़िगर करने के लिए `/settings` का इस्तेमाल करें।"
    
    markup = InlineKeyboardMarkup()
    try:
        bot_info = bot.get_me()
        add_to_group_url = f"https://t.me/{bot_info.username}?startgroup=true"
    except Exception:
        add_to_group_url = "https://t.meBotFather" 

    button = InlineKeyboardButton(text="➕ Add Me To Your Group ➕", url=add_to_group_url)
    markup.add(button)
    
    try:
        bot.send_message(chat_id=message.chat.id, text=welcome_text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        print(f"Start मैसेज भेजने में समस्या: {e}")

# ℹ️ हेल्प कमांड हैंडलर
@bot.message_handler(commands=['help'], chat_types=['private', 'group', 'supergroup'])
def send_help(message):
    help_text = (
        "❓ **बॉट उपयोग गाइड:**\n\n"
        "1️⃣ बॉट को टेलीग्राम ग्रुप में जोड़ें।\n"
        "2️⃣ बॉट को ग्रुप का **Admin** बनाएं और मैसेज डिलीट करने की अनुमति दें।\n"
        "3️⃣ ग्रुप के अंदर `/settings` कमांड चलाकर भाषा, अंतराल और ऑटो-डिलीट फीचर्स बदलें।"
    )
    markup = InlineKeyboardMarkup()
    owner_url = "https://t.me/comeback_009" # 👈 यहाँ आप अपना टेलीग्राम यूजरनेम लिंक डाल सकते हैं
        
    btn_owner = InlineKeyboardButton(text="Contact Support", url=owner_url)
    markup.add(btn_owner)
    
    try:
        bot.send_message(chat_id=message.chat.id, text=help_text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        print(f"Help मैसेज भेजने में समस्या: {e}")

# 📊 ओनर के लिए स्पेशल कमांड
@bot.message_handler(commands=['stats'], chat_types=['private'])
def send_stats(message):
    if OWNER_ID and message.from_user.id == OWNER_ID:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM groups")
            count = cursor.fetchone()
        bot.send_message(message.chat.id, f"📊 **बॉट स्टेट्स:**\n\n🎯 वर्तमान में बॉट कुल **{count} ग्रुप्स** में एक्टिव है और क्विज़ भेज रहा है।")

# 🤖 ग्रुप जॉइन/लीव ट्रैकर इवेंट हैंडलर
@bot.my_chat_member_handler()
def handle_left_or_joined(message):
    new_status = message.new_chat_member.status
    chat_id = message.chat.id
    group_name = message.chat.title

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        if new_status in ["administrator", "member"]:
            # नए ग्रुप्स के लिए डिफ़ॉल्ट रूप से interval = 1800 (30 मिनट) इंसर्ट होगा
            cursor.execute("INSERT OR IGNORE INTO groups (chat_id, interval) VALUES (?, 1800)", (chat_id,))
            cursor.execute("UPDATE groups SET last_sent_time = 0 WHERE chat_id = ?", (chat_id,))
            conn.commit()
            
            group_active_text = (
                f"🎉 **बॉट सफलतापूर्वक एक्टिव हो चुका है!**\n\n"
                f"📢 इस ग्रुप ('{group_name}') में ऑटोमैटिक क्विज़ पोल शेड्यूलर एक्टिव है।\n"
                f"⚙️ डिफ़ॉल्ट रूप से समय अंतराल **30 मिनट** सेट है। इसे बदलने के लिए ग्रुप एडमिन सीधे `/settings` टाइप कर सकते हैं।"
            )
            try:
                bot.send_message(chat_id=chat_id, text=group_active_text, parse_mode="Markdown")
            except Exception as e:
                print(f"ग्रुप में वेलकम अलर्ट भेजने में एरर: {e}")
        elif new_status in ["left", "kicked"]:
            cursor.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
            conn.commit()

# 🧵 बैकग्राउंड थ्रेड टास्क रनर शुरू करें
threading.Thread(target=global_poll_manager, daemon=True).start()

print("successfully deploy Bot a live now")

# ⏱️ पोलिंग को क्रैश और नेटवर्क टाइमआउट से बचाने के लिए सेटिंग्स
bot.infinity_polling(
    allowed_updates=["my_chat_member", "message", "callback_query"],
    timeout=60,
    long_polling_timeout=60,
    skip_pending=True
          )
