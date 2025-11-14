import os
from threading import Thread
from admin_panel import app  # твой Flask-приложение
from bot import run_bot      # функция, которая стартует polling VK

# Берём порт от Render
port = int(os.environ.get("PORT", 5000))

if __name__ == "__main__":
    # Запуск бота в отдельном потоке
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True  # завершится вместе с основным процессом
    bot_thread.start()

    # Запуск Flask на 0.0.0.0:PORT
    app.run(host="0.0.0.0", port=port)
