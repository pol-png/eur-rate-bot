"""
Курс евро за рубли во Freedom Bank (мобильное приложение).
Запускается по расписанию в GitHub Actions каждые 15 минут.

Сообщения приходят в двух случаях:
  1) регулярно — по будням в 09:00, 12:30 и 16:00, по выходным в 12:30
     (время московское, UTC+3);
  2) при снижении курса — первое при достижении порога TARGET,
     дальше при каждом падении ещё на STEP рублей.

Настройки берутся из GitHub:
  BOT_TOKEN — секрет, токен бота от BotFather
  OWNER_ID  — секрет, ваш chat_id
  TARGET    — переменная, порог в рублях за 1 евро (например 98)

Состояние хранится в state.json.
"""
import datetime
import json
import os
import sys
import time

import requests

RATES_URL = "https://bankffin.kz/api/exchange-rates/getRates"
STATE_FILE = "state.json"

STEP = 0.5                                  # шаг снижения для нового сообщения, ₽
TZ_OFFSET = 3                               # часовой пояс пользователя, UTC+3
TIMES_WEEKDAY = ["09:00", "12:30", "16:00"]  # регулярные сообщения, будни
TIMES_WEEKEND = ["12:30"]                    # регулярные сообщения, выходные

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


def local_now():
    """Текущее время в часовом поясе пользователя (сервер GitHub живёт по UTC)."""
    tz = datetime.timezone(datetime.timedelta(hours=TZ_OFFSET))
    return datetime.datetime.now(tz)


def due_slot(now):
    """Последнее наступившее сегодня время регулярного сообщения, иначе None."""
    times = TIMES_WEEKEND if now.weekday() >= 5 else TIMES_WEEKDAY
    passed = [t for t in times if t <= now.strftime("%H:%M")]
    return f"{now:%Y-%m-%d} {passed[-1]}" if passed else None


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


def check_drop(price, target, state):
    """Сообщения о снижении курса."""
    if price > target:
        # Курс выше порога — отсчёт начинается заново
        state["alerted"] = False
        state.pop("last", None)
    elif not state.get("alerted"):
        send(f"🔔 Евро подешевел: 1 € = {price:.2f} ₽ (ваш порог {target:.2f} ₽).\n"
             "Перед покупкой проверьте курс в приложении банка.")
        state["alerted"] = True
        state["last"] = price
    else:
        last = state.get("last", target)
        if price <= last - STEP:
            send(f"🔔 Евро ещё подешевел: 1 € = {price:.2f} ₽ "
                 f"(в прошлый раз было {last:.2f} ₽).\n"
                 "Перед покупкой проверьте курс в приложении банка.")
            state["last"] = price


def report(price, state, now):
    """Регулярное сообщение с текущим курсом по расписанию."""
    slot = due_slot(now)
    if slot is None or state.get("report") == slot:
        return

    before = state.get("report_price")
    if before is None:
        change = ""
    elif price < before:
        change = f"\nЭто на {before - price:.2f} ₽ дешевле прошлого раза ({before:.2f} ₽)."
    elif price > before:
        change = f"\nЭто на {price - before:.2f} ₽ дороже прошлого раза ({before:.2f} ₽)."
    else:
        change = f"\nБез изменений с прошлого раза."

    send(f"📊 Курс евро: 1 € = {price:.2f} ₽.{change}")
    state["report"] = slot
    state["report_price"] = price


def main():
    if not TARGET_RAW:
        print("Порог TARGET не задан в настройках репозитория — проверка пропущена.")
        return
    target = num(TARGET_RAW)
    now = local_now()

    # Сначала курс: если банк молчит, ничего больше делать не нужно
    price = get_eur_price()
    print(f"{now:%Y-%m-%d %H:%M} — 1 € = {price:.2f} ₽, порог: {target:.2f} ₽")

    state = load_state()
    if state.get("target") != target:
        # Порог изменился — начинаем следить заново
        state = {k: v for k, v in state.items() if k in ("report", "report_price")}
        state["target"] = target
        state["alerted"] = False
        send(f"Порог установлен: сообщу, когда 1 € будет стоить не больше {target:.2f} ₽, "
             f"и дальше при каждом снижении ещё на {STEP:g} ₽.")

    check_drop(price, target, state)
    report(price, state, now)

    # Раз в месяц файл меняется — это не даёт GitHub отключить расписание
    state["month"] = f"{now:%Y-%m}"
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
