"""
Проверка курса евро за рубли во Freedom Bank (мобильное приложение)
для запуска по расписанию в GitHub Actions.

Настройки берутся из GitHub:
  BOT_TOKEN — секрет, токен бота от BotFather
  OWNER_ID  — секрет, ваш chat_id
  TARGET    — переменная, порог в рублях за 1 евро (например 98)

Уведомления:
  — первое, когда курс впервые опустился до порога;
  — затем каждый раз, когда курс опускается ещё на STEP рублей
    ниже цены последнего уведомления.
Если курс поднимается выше порога, отсчёт начинается заново.

Состояние хранится в state.json.
Если банк не отвечает, бот делает несколько попыток, а затем
спокойно завершает работу до следующей проверки.
"""
import datetime
import json
import os
import sys
import time

import requests

RATES_URL = "https://bankffin.kz/api/exchange-rates/getRates"
STATE_FILE = "state.json"
STEP = 0.5            # на сколько рублей курс должен упасть для нового сообщения
ATTEMPTS = 3          # сколько раз пробовать достучаться до банка
PAUSE = 20            # пауза между попытками, секунд

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = os.environ["OWNER_ID"]
TARGET_RAW = os.environ.get("TARGET", "").strip()


class BankUnavailable(Exception):
    """Банк не ответил — это не поломка бота, просто ждём следующей проверки."""


def num(s):
    """Банк пишет крупные числа с пробелами: '11 595.28'."""
    return float(str(s).replace(" ", "").replace(",", "."))


def fetch_rates():
    """Курсы мобильного приложения. Несколько попыток подряд."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = requests.get(RATES_URL, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()["data"]["mobile"]
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"Попытка {attempt} из {ATTEMPTS} не удалась: {e}")
            if attempt < ATTEMPTS:
                time.sleep(PAUSE)
    raise BankUnavailable


def get_eur_price():
    """Сколько рублей банк берёт за 1 евро в мобильном приложении."""
    pairs = fetch_rates()

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

    # Сначала курс: если банк молчит, ничего больше делать не нужно
    price = get_eur_price()
    print(f"Курс в приложении: 1 € = {price:.2f} ₽, порог: {target:.2f} ₽")

    state = load_state()
    if state.get("target") != target:
        # Порог изменился — начинаем следить заново
        state = {"target": target, "alerted": False}
        send(f"Порог установлен: сообщу, когда 1 € будет стоить не больше {target:.2f} ₽, "
             f"и дальше при каждом снижении ещё на {STEP:g} ₽.")

    if price > target:
        # Курс выше порога — отсчёт начинается заново
        state["alerted"] = False
        state.pop("last", None)
    elif not state.get("alerted"):
        # Первое пересечение порога
        send(f"🔔 Евро подешевел: 1 € = {price:.2f} ₽ (ваш порог {target:.2f} ₽).\n"
             "Перед покупкой проверьте курс в приложении банка.")
        state["alerted"] = True
        state["last"] = price
    else:
        # Уже сообщали — ждём снижения ещё на STEP рублей
        last = state.get("last", target)
        if price <= last - STEP:
            send(f"🔔 Евро ещё подешевел: 1 € = {price:.2f} ₽ "
                 f"(в прошлый раз было {last:.2f} ₽).\n"
                 "Перед покупкой проверьте курс в приложении банка.")
            state["last"] = price

    # Раз в месяц файл меняется — это не даёт GitHub отключить расписание
    state["month"] = datetime.date.today().strftime("%Y-%m")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except BankUnavailable:
        print("Банк не ответил ни на одну попытку. Проверим курс в следующий раз.")
    except Exception as e:
        # Текст ошибки может содержать адрес запроса с токеном —
        # вычищаем его, потому что журнал публичного репозитория виден всем
        print("Ошибка:", str(e).replace(TOKEN, "***"))
        sys.exit(1)
