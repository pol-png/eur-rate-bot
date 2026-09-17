"""
Проверка курса евро за рубли во Freedom Bank (мобильное приложение)
для запуска по расписанию в GitHub Actions.

Настройки берутся из GitHub:
  BOT_TOKEN — секрет, токен бота от BotFather
  OWNER_ID  — секрет, ваш chat_id
  TARGET    — переменная, порог в рублях за 1 евро (например 98.5)

Состояние (было ли уже уведомление) хранится в state.json.
"""
import datetime
import json
import os
import sys

import requests

RATES_URL = "https://bankffin.kz/api/exchange-rates/getRates"
STATE_FILE = "state.json"

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = os.environ["OWNER_ID"]
TARGET_RAW = os.environ.get("TARGET", "").strip()


def num(s):
    """Банк пишет крупные числа с пробелами: '11 595.28'."""
    return float(str(s).replace(" ", "").replace(",", "."))


def get_eur_price():
    """Сколько рублей банк берёт за 1 евро в мобильном приложении."""
    r = requests.get(RATES_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    pairs = r.json()["data"]["mobile"]

    for p in pairs:
        if p["buyCode"] == "EUR" and p["sellCode"] == "RUB":
            return num(p["sellRate"])

    # Запасной вариант: кросс через тенге (тоже курсы приложения)
    kzt = {p["buyCode"]: p for p in pairs if p["sellCode"] == "KZT"}
    return round(num(kzt["EUR"]["sellRate"]) / num(kzt["RUB"]["buyRate"]), 2)


def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": OWNER_ID, "text": text}, timeout=20)
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    if not TARGET_RAW:
        print("Порог TARGET не задан в настройках репозитория — проверка пропущена.")
        return
    target = num(TARGET_RAW)

    state = load_state()
    if state.get("target") != target:
        # Порог изменился — начинаем следить заново
        state = {"target": target, "alerted": False}
        send(f"Порог установлен: сообщу, когда 1 € будет стоить не больше {target:.2f} ₽.")

    price = get_eur_price()
    print(f"Курс в приложении: 1 € = {price:.2f} ₽, порог: {target:.2f} ₽")

    if price <= target and not state.get("alerted"):
        send(f"🔔 Евро подешевел: 1 € = {price:.2f} ₽ (ваш порог {target:.2f} ₽).\n"
             "Перед покупкой проверьте курс в приложении банка.")
        state["alerted"] = True
    elif price > target and state.get("alerted"):
        # Курс ушёл выше порога — при следующем снижении снова уведомим
        state["alerted"] = False

    # Раз в месяц файл меняется — это не даёт GitHub отключить расписание
    state["month"] = datetime.date.today().strftime("%Y-%m")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Текст ошибки может содержать адрес запроса с токеном —
        # вычищаем его, потому что журнал публичного репозитория виден всем
        print("Ошибка:", str(e).replace(TOKEN, "***"))
        sys.exit(1)
