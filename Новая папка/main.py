import threading
import subprocess

def run_flask():
    subprocess.run(["python", "app.py"])  # или "app.py", если он основной

def run_bot():
    subprocess.run(["python", "telegram_bot.py"])

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    bot_thread = threading.Thread(target=run_bot)

    flask_thread.start()
    bot_thread.start()

    flask_thread.join()
    bot_thread.join()
