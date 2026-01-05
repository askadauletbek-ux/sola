# -*- coding: utf-8 -*-
import sqlite3
import os

# --- 1. Настройки ---
# Имя файла вашей базы данных
DB_NAME = '35healthclubs.db'
# Имя таблицы, в которую нужно добавить столбцы
TABLE_NAME = 'user'

# --- 2. Функция для безопасного добавления столбца ---
def add_column(cursor, column_name, column_definition):
    """
    Проверяет, существует ли столбец, и добавляет его, если нет.
    Это предотвращает ошибки при повторном запуске скрипта.
    """
    try:
        # Выполняем SQL-команду ALTER TABLE для добавления столбца
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {column_definition}")
        print(f"Столбец '{column_name}' успешно добавлен в таблицу '{TABLE_NAME}'.")
    except sqlite3.OperationalError as e:
        # SQLite выдаст ошибку, если столбец уже существует.
        # Мы "ловим" эту ошибку и просто сообщаем, что столбец уже на месте.
        if f"duplicate column name: {column_name}" in str(e):
            print(f"Столбец '{column_name}' уже существует в таблице '{TABLE_NAME}'. Никаких действий не требуется.")
        else:
            # Если произошла другая ошибка, выводим ее.
            raise e

# --- 3. Основная логика скрипта ---
def main():
    """
    Главная функция, которая подключается к БД и запускает процесс добавления столбцов.
    """
    # Составляем полный путь к файлу БД, чтобы скрипт работал из любого места
    basedir = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(basedir, DB_NAME)

    if not os.path.exists(db_path):
        print(f"ОШИБКА: Файл базы данных '{DB_NAME}' не найден.")
        print("Убедитесь, что этот скрипт находится в том же каталоге, что и ваша база данных.")
        return

    print(f"Подключение к базе данных: {db_path}...")
    conn = None
    try:
        # Устанавливаем соединение с файлом базы данных
        conn = sqlite3.connect(db_path)
        # Создаем "курсор" - объект для выполнения SQL-команд
        cursor = conn.cursor()

        # Добавляем столбец 'is_trainer'
        # В SQLite тип BOOLEAN хранится как INTEGER (0 для False, 1 для True).
        # NOT NULL DEFAULT 0 означает, что поле не может быть пустым и по умолчанию равно False.
        add_column(cursor, 'is_trainer', 'BOOLEAN NOT NULL DEFAULT 0')

        # Добавляем столбец 'avatar'
        # VARCHAR(200) - это текстовое поле. По умолчанию оно может быть пустым (NULL).
        add_column(cursor, 'avatar', 'VARCHAR(200)')

        # Сохраняем все сделанные изменения в базе данных
        conn.commit()
        print("\nИзменения успешно сохранены в базе данных.")

    except sqlite3.Error as e:
        print(f"Произошла ошибка SQLite: {e}")
    finally:
        # Вне зависимости от успеха или ошибки, закрываем соединение с базой данных
        if conn:
            conn.close()
            print("Соединение с базой данных закрыто.")

# --- 4. Запуск скрипта ---
# Этот блок кода выполнится только если вы запустите этот файл напрямую
if __name__ == '__main__':
    main()