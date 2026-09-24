"""Password and API-key primitives."""

import hashlib
import secrets

import bcrypt

MIN_PASSWORD_LENGTH = 8


def password_problem(password: str) -> str | None:
    """Почему такой пароль брать нельзя — или None, если можно.

    Длина не проверялась нигде: ни при регистрации, ни в scripts/manage_admin.py,
    где `--password` объявлен обязательным, но пустая строка проходит как
    значение. Один раз так уже завели администратора с ПУСТЫМ паролем — путь
    к временному файлу не существовал, переменная оболочки оказалась пустой,
    и скрипт молча это принял.
    """
    if not password or not password.strip():
        return "пароль не может быть пустым"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"пароль короче {MIN_PASSWORD_LENGTH} символов"
    return None


def hash_password(password: str) -> str:
    problem = password_problem(password)
    if problem is not None:
        # Последний рубеж: хешировать заведомо негодный пароль нельзя даже
        # если вызывающий забыл проверить. Дешевле упасть здесь, чем завести
        # учётку, в которую войдёт кто угодно.
        raise ValueError(problem)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    return "nh_" + secrets.token_urlsafe(32)


def generate_temp_password() -> str:
    return secrets.token_urlsafe(9)
