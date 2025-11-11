import os
from admin_panel import app  # импорт Flask приложения

port = int(os.environ.get("PORT", 5000))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port)
