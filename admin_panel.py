import os
import random
import sqlite3
import requests
import json
import portalocker
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, emit  # <-- ВАЖНО: добавлен emit
from flask_wtf.csrf import CSRFProtect
from contextlib import contextmanager
from dotenv import load_dotenv
import threading

# === Загрузка переменных окружения ===
load_dotenv()
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")
VK_TOKEN = os.getenv("VK_TOKEN", "")
ADMIN_URL = os.getenv("ADMIN_URL", "http://127.0.0.1:8080").rstrip('/')
DATABASE = "interactions.db"
SETTINGS_FILE = "settings.json"

# === Flask и Socket.IO ===
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-change-me-in-production')
app.config['WTF_CSRF_SECRET_KEY'] = os.getenv('CSRF_SECRET_KEY', 'csrf-secret-key')
csrf = CSRFProtect(app)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*", logger=True, engineio_logger=True)

# === КОНТЕКСТНЫЙ МЕНЕДЖЕР ДЛЯ БД ===
@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# === БАЗА ДАННЫХ ===
def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            message TEXT NOT NULL,
            sender TEXT NOT NULL,
            status TEXT DEFAULT 'Открыто',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

init_db()

# === НАСТРОЙКИ ===
def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {
            "dark_mode": False,
            "sound_notify": True,
            "auto_refresh": 0,
            "status_colors": {
                "Открыто": "#ffe066",
                "Отвечено": "#b2f2bb",
                "Закрыто": "#ccc"
            }
        }
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return load_settings()

def save_settings_to_file(settings):
    temp_file = SETTINGS_FILE + '.tmp'
    with open(temp_file, 'w', encoding='utf-8') as f:
        portalocker.lock(f, portalocker.LOCK_EX)
        json.dump(settings, f, ensure_ascii=False, indent=2)
        portalocker.unlock(f)
    os.replace(temp_file, SETTINGS_FILE)

# === РАБОТА С VK ===
def get_or_create_username(user_id):
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT username FROM interactions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else f"id{user_id}"

def update_username_sync(user_id):
    def task():
        try:
            params = {"access_token": VK_TOKEN, "v": "5.199", "user_ids": user_id}
            response = requests.get("https://api.vk.com/method/users.get", params=params, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if "response" in data and data["response"]:
                    user = data["response"][0]
                    name = f"{user['first_name']} {user['last_name']}"
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE interactions SET username=? WHERE user_id=? AND username LIKE ?",
                            (name, user_id, f"id{user_id}%")
                        )
                    socketio.emit("update_username", {"user_id": user_id, "username": name})
        except Exception as e:
            print(f"❌ Ошибка получения имени VK: {e}")
    threading.Thread(target=task, daemon=True).start()

# === ДОБАВЛЕНИЕ СООБЩЕНИЙ ===
def add_message(user_id, text, sender="user", status="Открыто"):
    username = get_or_create_username(user_id)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO interactions (user_id, username, message, sender, status) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, text, sender, status)
        )
    event_data = {
        "user_id": user_id, "username": username, "text": text,
        "sender": sender, "status": status, "timestamp": datetime.now().isoformat()
    }
    if sender == "user":
        socketio.emit("new_request", event_data)
    else:
        socketio.emit(f"message_{user_id}", event_data)
        socketio.emit("update_status", {"user_id": user_id, "status": status})
    if username.startswith("id"):
        update_username_sync(user_id)

def get_all_interactions():
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT i.user_id, i.username, i.message, i.status, i.timestamp
            FROM interactions i
            INNER JOIN (
                SELECT user_id, MAX(id) AS max_id FROM interactions GROUP BY user_id
            ) grouped_i
            ON i.user_id = grouped_i.user_id AND i.id = grouped_i.max_id
            ORDER BY i.timestamp DESC
        """)
        return cursor.fetchall()

def get_chat_history(user_id):
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT message, sender, timestamp FROM interactions WHERE user_id=? ORDER BY timestamp ASC",
            (user_id,)
        )
        rows = cursor.fetchall()
    return [{'text': msg, 'from': 'Оператор' if sender == 'operator' else 'Пользователь', 'sender': sender, 'timestamp': t} for msg, sender, t in rows]

def send_vk_message_sync(user_id, message):
    if not VK_TOKEN:
        print("❌ VK токен не установлен!")
        return False
    try:
        params = {
            "access_token": VK_TOKEN,
            "v": "5.199",
            "peer_id": user_id,
            "message": message,
            "random_id": random.randint(1, 1_000_000)
        }
        response = requests.post("https://api.vk.com/method/messages.send", params=params, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if "error" in result:
                print(f"❌ Ошибка VK: {result['error'].get('error_msg')}")
                return False
            return True
    except Exception as e:
        print(f"❌ Ошибка отправки VK: {e}")
    return False

# === МАРШРУТЫ ===
@csrf.exempt
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

@app.route("/statistics")
def statistics():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return render_template("statistics.html")

@app.route("/settings")
def settings():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return render_template("settings.html", settings=load_settings())

@app.route("/new_message", methods=["POST"])
def new_message():
    try:
        data = request.get_json(force=True)
        user_id = int(data.get("user_id"))
        text = str(data.get("question"))
        if not user_id or not text:
            return jsonify({"error": "Недостаточно данных"}), 400
        add_message(user_id, text, sender="user", status="Открыто")
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
    add_message(user_id, text, sender="operator", status="Отвечено")
    success = send_vk_message_sync(user_id, f"👤 Оператор: {text}")
    return jsonify({"status": "ok", "vk_sent": success}), 200

@app.route("/end_chat/<int:user_id>", methods=["POST"])
def end_chat(user_id):
    if not session.get("logged_in"):
        return jsonify({"error": "Unauthorized"}), 401
    final_message = request.form.get("message", "👤 Спасибо за ваш вопрос! Оператор завершил чат.")
    with get_db() as conn:
        conn.execute("UPDATE interactions SET status='Закрыто' WHERE user_id=? AND status IN ('Открыто', 'Отвечено')", (user_id,))
    success = send_vk_message_sync(user_id, final_message)
    add_message(user_id, final_message, sender="operator", status="Закрыто")
    return jsonify({"status": "ok", "vk_sent": success}), 200

# === SOCKET.IO ОБРАБОТЧИКИ ===
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

# === ОБРАБОТЧИК СТАТИСТИКИ (С ОТЛАДКОЙ) ===
@socketio.on('get_statistics')
def handle_get_statistics(filters):
    """Полная статистика с детальной отладкой"""
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # === ОТЛАДКА ===
        print(f"\n{'='*60}")
        print(f"📊 ЗАПРОС СТАТИСТИКИ")
        print(f"Фильтры: {filters}")
        
        # Всего в БД
        cursor.execute("SELECT COUNT(*) FROM interactions")
        total_db = cursor.fetchone()[0]
        print(f" Всего записей БД: {total_db}")
        
        if total_db == 0:
            emit('statistics_data', {
                'summary': {
                    'total_posts': 0, 'total_subscriptions': 0,
                    'total_unsubscriptions': 0, 'total_messages': 0, 'active_users': 0
                },
                'activity': [], 'ranking': [], 'details': []
            })
            conn.close()
            return
        
        # Пример записей
        cursor.execute("SELECT id, user_id, username, sender, status FROM interactions LIMIT 2")
        sample = cursor.fetchall()
        print(f" Пример записей: {sample}")
        
        # === СЧЁТЧИКИ ===
        cursor.execute("SELECT COUNT(*) FROM interactions WHERE sender='user'")
        total_posts = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM interactions WHERE status='Открыто'")
        total_subs = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM interactions WHERE status='Закрыто'")
        total_unsubs = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM interactions")
        total_messages = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT user_id) FROM interactions")
        active_users = cursor.fetchone()[0]
        
        print(f" Постов (sender='user'): {total_posts}")
        print(f" Подписок (status='Открыто'): {total_subs}")
        print(f" Отписок (status='Закрыто'): {total_unsubs}")
        print(f" Сообщений: {total_messages}")
        print(f" Пользователей: {active_users}")
        
        # === ГРАФИК ===
        cursor.execute("""
            SELECT DATE(timestamp) as date, COUNT(*) as actions 
            FROM interactions 
            GROUP BY DATE(timestamp) 
            ORDER BY date DESC LIMIT 30
        """)
        activity = [{"date": row[0], "actions": row[1]} for row in cursor.fetchall()]
        activity.reverse()
        
        # === ТОП ПОЛЬЗОВАТЕЛИ ===
        cursor.execute("""
            SELECT user_id, username, COUNT(*) as total
            FROM interactions 
            GROUP BY user_id, username
            ORDER BY total DESC
            LIMIT 10
        """)
        ranking = []
        for row in cursor.fetchall():
            ranking.append({
                "user_id": row[0],
                "username": row[1] or f"id{row[0]}",
                "total_actions": row[2]
            })
        
        # === ДЕТАЛИ ===
        cursor.execute("""
            SELECT timestamp, user_id, username, message, status 
            FROM interactions 
            ORDER BY timestamp DESC 
            LIMIT 50
        """)
        details = []
        for row in cursor.fetchall():
            details.append({
                "timestamp": row[0],
                "user_id": row[1],
                "username": row[2] or f"id{row[1]}",
                "action": row[4],
                "content": row[3]
            })
        
        conn.close()
        print(f"{'='*60}\n")
        
        # Отправка данных
        emit('statistics_data', {
            'summary': {
                'total_posts': total_posts,
                'total_subscriptions': total_subs,
                'total_unsubscriptions': total_unsubs,
                'total_messages': total_messages,
                'active_users': active_users
            },
            'activity': activity,
            'ranking': ranking,
            'details': details
        })
        
    except Exception as e:
        print(f"❌ Ошибка статистики: {e}")
        emit('statistics_error', {'message': str(e)})

# === ЭКСПОРТ ===
@socketio.on('export_requests')
def handle_export():
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, user_id, username, message, sender, status FROM interactions ORDER BY timestamp DESC")
        data = cursor.fetchall()
        conn.close()
        result = [{'timestamp': row[0], 'user_id': row[1], 'username': row[2], 'content': row[3], 'sender': row[4], 'status': row[5]} for row in data]
        emit('export_data', result)
    except Exception as e:
        emit('export_error', {'message': str(e)})

# === ЗАПУСК ===
if __name__ == "__main__":
    os.makedirs('templates', exist_ok=True)
    print(f"🚀 Админ-панель запущена на {ADMIN_URL}")
    print(f"🔑 VK Token: {'✅ Установлен' if VK_TOKEN else '❌ НЕ УСТАНОВЛЕН!'}")
    socketio.run(app, host="0.0.0.0", port=8080, debug=True)