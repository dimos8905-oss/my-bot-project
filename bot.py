import os
import json
from dotenv import load_dotenv
from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, Text, KeyboardButtonColor
import httpx

# УСТАНОВКА БИБЛИОТЕК:
# pip install fuzzywuzzy python-Levenshtein
try:
    from fuzzywuzzy import fuzz, process
    FUZZY_ENABLED = True
    print("✅ Fuzzy search активирован")
except ImportError:
    print("⚠️ Установите: pip install fuzzywuzzy python-Levenshteин")
    FUZZY_ENABLED = False

# -----------------------
# Загрузка токенов
# -----------------------
load_dotenv()
TOKEN = os.getenv("VK_TOKEN")
ADMIN_URL = os.getenv("ADMIN_URL", "http://127.0.0.1:8080").rstrip('/')

if not TOKEN:
    raise ValueError("❌ VK_TOKEN не найден в боте!")

bot = Bot(token=TOKEN)

# -----------------------
# КЛАВИАТУРЫ (правильно!)
# -----------------------
main_keyboard = (
    Keyboard(one_time=False, inline=False)
    .add(Text("Найти вакансию"), color=KeyboardButtonColor.PRIMARY)
    .add(Text("Записаться на приём"), color=KeyboardButtonColor.PRIMARY)
    .row()
    .add(Text("FAQ"), color=KeyboardButtonColor.SECONDARY)
    .add(Text("Контакты"), color=KeyboardButtonColor.SECONDARY)
    .row()
    .add(Text("Вызвать оператора"), color=KeyboardButtonColor.NEGATIVE)
)

back_keyboard = (
    Keyboard(one_time=True, inline=False)
    .add(Text("Назад"), color=KeyboardButtonColor.SECONDARY)
)

# -----------------------
# БАЗА ЗНАНИЙ FAQ (расширенная)
# -----------------------
FAQ_BASE = {
    "документы_учет": {
        "questions": ["какие документы нужны", "что взять в цзн", "документы для постановки", "оформление в центр занятости"],
        "answer": "🤖 Для постановки на учёт в ЦЗН нужны:\n• Паспорт\n• СНИЛС\n• Трудовая книжка (при наличии)\n• Документы об образовании"
    },
    
    "курсы": {
        "questions": ["можно ли пройти курсы", "хотел учиться", "обучение в центре", "бесплатные курсы"],
        "answer": "🤖 Да, в ЦЗН можно пройти бесплатные курсы повышения квалификации. Для записи обратитесь к специалисту."
    },
    
    "постановка_учет": {
        "questions": ["как встать на учёт", "как оформиться", "постановка на учет", "процедура постановки"],
        "answer": "🤖 Чтобы встать на учёт:\n1. Прийти в ЦЗН с паспортом и СНИЛС\n2. Написать заявление\n3. Пройти собеседование\n4. Получить статус"
    },
    
    "вакансии": {
        "questions": ["где вакансии", "искать работу", "подобрать работу"],
        "answer": "🤖 Вакансии доступны на сайте: https://czn-rzn.ru или по телефону +7 (XXX) XXX-XX-XX"
    },
    
    "пособие": {
        "questions": ["как оформить пособие", "размер пособия", "когда платят пособие"],
        "answer": "🤖 Пособие оформляется после постановки на учёт. Максимум — 12,792₽, минимум — 1,500₽ в месяц."
    }
}

# Преобразуем в старый формат для совместимости
FAQ_ANSWERS = {}
for key, data in FAQ_BASE.items():
    for q in data["questions"]:
        FAQ_ANSWERS[q] = data["answer"]

PREDEFINED_ANSWERS = {
    "привет": "🤖 Привет! Рад вас видеть 😊 Чем могу помочь?",
    "здравствуйте": "🤖 Здравствуйте! Я помогу с вопросами о трудоустройстве.",
    "спасибо": "🤖 Всегда рад помочь!",
    "до свидания": "🤖 До свидания! Удачи в поиске работы!",
}

# -----------------------
# Состояния и утилиты
# -----------------------
STATE_FILE = "user_states.json"

def load_states():
    try:
        with open(STATE_FILE, "r", encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_states(states):
    try:
        with open(STATE_FILE, "w", encoding='utf-8') as f:
            json.dump(states, f, ensure_ascii=False, indent=2)
        print(f"💾 Состояния сохранены в {STATE_FILE}")
    except Exception as e:
        print(f"❌ Ошибка сохранения состояний: {e}")

def is_operator_mode(user_id):
    states = load_states()
    return states.get(str(user_id)) == "operator"

def set_operator_mode(user_id, mode):
    states = load_states()
    states[str(user_id)] = mode
    save_states(states)

# -----------------------
# УМНЫЙ ПОИСК ПО НЕСКОЛЬКИМ ТЕМАМ
# -----------------------
def find_multiple_answers(question, threshold=60):
    """
    Ищет ВСЕ темы в одном вопросе и комбинирует ответы
    """
    if not FUZZY_ENABLED or not FAQ_BASE:
        return None
    
    found_topics = []
    question_lower = question.lower()
    
    # Проверяем каждую тему
    for topic_key, topic_data in FAQ_BASE.items():
        for q in topic_data["questions"]:
            score = fuzz.partial_ratio(question_lower, q)
            if score >= threshold:
                found_topics.append(topic_key)
                print(f"🔍 Найдена тема '{topic_key}' с точностью {score}%")
                break
    
    # Убираем дубликаты
    found_topics = list(set(found_topics))
    
    # Комбинируем ответы
    if found_topics:
        if len(found_topics) == 1:
            return FAQ_BASE[found_topics[0]]["answer"]
        
        combined_answer = "🤖 Нашёл ответы на ваши вопросы:\n\n"
        for i, topic_key in enumerate(found_topics, 1):
            answer = FAQ_BASE[topic_key]["answer"].replace("🤖 ", "")
            combined_answer += f"{i}. {answer}\n\n"
        
        combined_answer += "💡 Если нужна дополнительная информация — спрашивайте!"
        return combined_answer
    
    return None

# -----------------------
# Асинхронная отправка в админку
# -----------------------
async def send_to_admin(user_id: int, question: str):
    print(f"📤 Отправка в админку: user_id={user_id}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{ADMIN_URL}/new_message", json={
                "user_id": user_id,
                "question": question
            })
            if resp.status_code != 200:
                print(f"❌ Ошибка отправки в админку: {resp.status_code}")
    except Exception as e:
        print(f"❌ Ошибка при отправке в админку: {e}")

# -----------------------
# Обработчик сообщений
# -----------------------
last_messages = {}

@bot.on.message()
async def handle_message(message: Message):
    user_id = message.from_id
    text = message.text.strip()
    text_lower = text.lower().strip()

    # Игнорируем сообщения от оператора
    if text.startswith("👤"):
        print(f"⏭️ Пропускаем сообщение от оператора: {text[:50]}...")
        return

    # Проверка дубликатов
    global last_messages
    if last_messages.get(user_id) == text:
        print(f"⚠️ Дубликат сообщения от {user_id}: {text}")
        return
    
    last_messages[user_id] = text
    if len(last_messages) > 1000:
        last_messages.clear()
        print("🗑️ Очистка кэша дубликатов")

    # Режим оператора
    if is_operator_mode(user_id):
        await send_to_admin(user_id, text)
        await message.answer("✅ Сообщение отправлено оператору. Ожидайте ответа.", keyboard=main_keyboard)
        return

    # Команды
    if text_lower == "вызвать оператора":
        set_operator_mode(user_id, "operator")
        await message.answer("👤 Вы подключены к оператору.", keyboard=main_keyboard)
        return
    if text_lower == "назад":
        set_operator_mode(user_id, "normal")
        await message.answer("🤖 Главное меню:", keyboard=main_keyboard)
        return

    # === ОБРАБОТКА СЛОЖНЫХ ВОПРОСОВ ===
    
    # 1. Точное совпадение
    if text_lower in FAQ_ANSWERS:
        print(f"✅ Точное совпадение в FAQ")
        await message.answer(FAQ_ANSWERS[text_lower], keyboard=back_keyboard)
        return
    
    # 2. Ищем несколько тем
    if FUZZY_ENABLED:
        combined_answer = find_multiple_answers(text_lower, threshold=60)
        if combined_answer:
            await message.answer(combined_answer, keyboard=back_keyboard)
            return
    
    # 3. Если не нашли
    await message.answer(
        "🤖 Извините, я не нашёл ответа. Нажмите «Вызвать оператора».",
        keyboard=main_keyboard
    )

# -----------------------
# Запуск 
# -----------------------
if __name__ == "__main__":
    print(f"🤖 Бот запущен и подключен к {ADMIN_URL}")
    print(f"💾 Состояния сохраняются в {STATE_FILE}")
    print(f"🔍 Умный поиск по FAQ: {'✅ активен' if FUZZY_ENABLED else '❌ отключен'}")
    bot.run_forever()