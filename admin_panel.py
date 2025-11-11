import os
import random
import sqlite3
import requests  # ← ВАЖНО: Добавлен импорт
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room
from dotenv import load_dotenv
import threading

# === Загрузка переменных окружения ===
load_dotenv()
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")
VK_TOKEN = os.getenv("VK_TOKEN", "")
ADMIN_URL = os.getenv("ADMIN_URL", "http://127.0.0.1:8080").rstrip('/')  # ← Убран пробел
DATABASE = "interactions.db"

# === Flask и Socket.IO ===
app = Flask(__name__)
app.secret_key = os.urandom(24)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*", logger=True, engineio_logger=True)

# === База данных ===
def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS interactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        username TEXT,
                        message TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        status TEXT DEFAULT 'Открыто',
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

def get_or_create_username(user_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM interactions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else f"id{user_id}"

def update_username_sync(user_id):
    """Безопасная синхронная загрузка имени VK"""
    def task():
        try:
            params = {
                "access_token": VK_TOKEN,
                "v": "5.199",
                "user_ids": user_id
            }
            response = requests.get("https://api.vk.com/method/users.get", params=params, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if "response" in data and data["response"]:
                    user = data["response"][0]
                    name = f"{user['first_name']} {user['last_name']}"
                    
                    conn = sqlite3.connect(DATABASE)
                    cursor = conn.cursor()
                    cursor.execute("UPDATE interactions SET username=? WHERE user_id=? AND username LIKE ?",
                                   (name, user_id, f"id{user_id}%"))
                    conn.commit()
                    conn.close()
                    socketio.emit("update_username", {"user_id": user_id, "username": name})
                    print(f"✅ Имя VK обновлено: {name}")
        except Exception as e:
            print(f"❌ Ошибка получения имени VK: {e}")

    threading.Thread(target=task, daemon=True).start()

def add_message(user_id, text, sender="user", status="Открыто"):
    username = get_or_create_username(user_id)
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO interactions (user_id, username, message, sender, status) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, text, sender, status)
    )
    conn.commit()
    conn.close()
    
    event_data = {
        "user_id": user_id,
        "username": username,
        "text": text,
        "sender": sender,
        "status": status,
        "timestamp": datetime.now().isoformat()
    }
    
    if sender == "user":
        socketio.emit("new_request", event_data)
    else:
        socketio.emit(f"message_{user_id}", event_data)
        socketio.emit("update_status", {"user_id": user_id, "status": status})
    
    if username.startswith("id"):
        update_username_sync(user_id)

def get_all_interactions():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT i.user_id, i.username, i.message, i.status, i.timestamp
        FROM interactions i
        INNER JOIN (
            SELECT user_id, MAX(id) AS max_id FROM interactions GROUP BY user_id
        ) grouped_i
        ON i.user_id = grouped_i.user_id AND i.id = grouped_i.max_id
        ORDER BY i.timestamp DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_chat_history(user_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT message, sender, timestamp FROM interactions WHERE user_id=? ORDER BY timestamp ASC",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    
    history = []
    for msg, sender, t in rows:
        history.append({
            'text': msg,
            'from': 'Оператор' if sender == 'operator' else 'Пользователь',
            'sender': sender,
            'timestamp': t
        })
    return history

def send_vk_message_sync(user_id, message):
    """Надежная синхронная отправка сообщения в VK через HTTP API"""
    print(f"\n{'='*55}")
    print(f"📤 ОТПРАВКА VK СООБЩЕНИЯ")
    print(f"{'='*55}")
    print(f"VK User ID: {user_id}")
    print(f"Сообщение: {message[:100]}{'...' if len(message) > 100 else ''}")  # ← Покажет 👤
    print(f"Токен: {VK_TOKEN[:15]}...{'*' * (len(VK_TOKEN) - 15) if VK_TOKEN else '❌ НЕ УСТАНОВЛЕН!'}")
    
    if not VK_TOKEN:
        print("❌ ОШИБКА: Токен VK не найден в переменных окружения!")
        return False
    
    try:
        params = {
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": user_id,
            "message": message,  # ← Отправляем как есть, с префиксом
            "random_id": random.randint(1, 1_000_000)
        }
        
        response = requests.post(
            "https://api.vk.com/method/messages.send",
            params=params,
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"❌ HTTP ошибка: {response.status_code}")
            print(f"Ответ: {response.text}")
            return False
            
        result = response.json()
        
        if "error" in result:
            error = result["error"]
            print(f"❌ Ошибка VK API:")
            print(f"   Код: {error.get('error_code')}")
            print(f"   Описание: {error.get('error_msg')}")
            return False
        else:
            msg_id = result.get("response")
            print(f"✅ Сообщение успешно отправлено (ID: {msg_id})")
            return True
            
    except requests.exceptions.Timeout:
        print("❌ Ошибка: Таймаут запроса к VK API (10 сек)")
        return False
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
        return False

# === Маршруты ===
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["username"] == ADMIN_LOGIN and request.form["password"] == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Неверный логин или пароль")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    interactions = get_all_interactions()
    return render_template("dashboard.html", interactions=interactions)

@app.route("/new_message", methods=["POST"])
def new_message():
    try:
        data = request.get_json(force=True)
        user_id = int(data.get("user_id"))
        text = str(data.get("question"))
        if not user_id or not text:
            return jsonify({"error": "Недостаточно данных"}), 400
        
        add_message(user_id, text, sender="user", status="Открыто")
        print(f"✅ Новое сообщение от {user_id}: {text}")
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        print(f"❌ Ошибка в /new_message: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/chat/<int:user_id>")
def chat(user_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    
    username = get_or_create_username(user_id)
    history = get_chat_history(user_id)
    return render_template("chat.html", user_id=user_id, username=username, history=history)

@app.route("/reply/<int:user_id>", methods=["POST"])
def reply(user_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    
    text = request.form.get("answer", "").strip()
    if not text:
        return jsonify({"error": "Пустой ответ"}), 400
    
    # Сохраняем в БД без префикса (чистый текст)
    add_message(user_id, text, sender="operator", status="Отвечено")
    
    # ФОРМИРУЕМ сообщение для VK С префиксом
    vk_message = f"👤 Оператор: {text}"
    print(f"📝 DEBUG: Отправка в VK: '{vk_message}'")  # ← ЛОГ для проверки
    
    # Отправляем в VK
    success = send_vk_message_sync(user_id, vk_message)
    
    return jsonify({"status": "ok", "vk_sent": success}), 200

@app.route("/end_chat/<int:user_id>", methods=["POST"])
def end_chat(user_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    
    final_message = request.form.get("message", 
        "👤 Спасибо за ваш вопрос! Оператор завершил чат. Если нужна помощь, напишите снова.")
    
    # Обновляем статус
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE interactions SET status='Закрыто' WHERE user_id=? AND status IN ('Открыто', 'Отвечено')",
        (user_id,)
    )
    conn.commit()
    conn.close()
    
    print(f"\n{'='*30} ЗАВЕРШЕНИЕ ЧАТА {'='*30}")
    print(f"📝 DEBUG: Финальное сообщение: '{final_message}'")  # ← ЛОГ
    success = send_vk_message_sync(user_id, final_message)
    
    # Сбрасываем состояние бота
    try:
        reset_resp = requests.post(f"{ADMIN_URL}/reset_bot_state/{user_id}", timeout=5)
        print(f"🔄 Состояние бота сброшено: {reset_resp.status_code}")
    except Exception as e:
        print(f"❌ Ошибка сброса состояния бота: {e}")
    
    if success:
        add_message(user_id, final_message, sender="operator", status="Закрыто")
        print(f"✅ Чат завершен успешно!")
    else:
        print(f"❌ Сообщение не отправилось в VK")
    
    return jsonify({"status": "ok", "vk_sent": success}), 200

@app.route("/reset_bot_state/<int:user_id>", methods=["POST"])
def reset_bot_state(user_id):
    try:
        import json
        STATE_FILE = "user_states.json"
        
        try:
            with open(STATE_FILE, "r") as f:
                states = json.load(f)
        except:
            states = {}
        
        states[str(user_id)] = "normal"
        
        with open(STATE_FILE, "w") as f:
            json.dump(states, f, ensure_ascii=False)
        
        print(f"🔄 Состояние бота сброшено для user_id: {user_id}")
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        print(f"❌ Ошибка сброса состояния: {e}")
        return jsonify({"error": str(e)}), 500

# === Socket.IO обработчики ===
@socketio.on('connect')
def handle_connect():
    print(f'🔗 Socket подключен: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    print(f'❌ Socket отключен: {request.sid}')

@socketio.on('join_chat')
def handle_join_chat(data):
    user_id = data.get('user_id')
    if user_id:
        room = f"chat_{user_id}"
        join_room(room)
        print(f'🏠 Админ подписался на комнату: {room}')

# === Запуск ===
if __name__ == "__main__":
    print(f"🚀 Админ-панель запущена на {ADMIN_URL}")
    print(f"🔑 VK Token: {'✅ Установлен' if VK_TOKEN else '❌ НЕ УСТАНОВЛЕН!'}")
    socketio.run(app, host="0.0.0.0", port=8080, debug=True)