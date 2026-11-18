import asyncio
import base64
import io
import json
import os
import random
import sys
import threading
import time
import zipfile
from collections import deque
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

# Премиум-эмодзи (кастомные). Ключ — юникод-плейсхолдер, значение — custom_emoji_id.
PREMIUM_EMOJI = {
    "🔥": "5454182246250474905",  # огонь
    "💬": "5453952087543015968",  # сообщения
    "💵": "5456343997779831665",  # деньги
    "💲": "5456122837028857707",  # доллар
    "⚡": "5458746443571421160",  # молния
    "➕": "5456360443209609671",  # плюс (валид)
    "➖": "5456173826880593418",  # минус (ошибка)
    "🪐": "5454102570312166471",  # планета
    "❗": "5453960484204083655",  # восклицательный знак
    "🔗": "5454068128969417666",  # ссылка (цепь)
    "🖥️": "5454209184285356042",  # сайт
    "💰": "5454134554933619492",  # сундук с монетами
    "🔽": "5454172127307523969",  # стрелка вниз
    "🔒": "5453904641039298473",  # замок
    "👌": "5458604082585427959",  # ОК
    "🖤": "5456557633748100084",  # пылающее сердце
}


# Замена обычных эмодзи на премиум-плейсхолдеры в тексте сообщений.
EMOJI_MAP = {
    "🎫": "💵",  # токен → деньги
    "✅": "➕",  # встал/валид → плюс
    "⚠️": "➖",  # ошибка → минус
    "🎉": "🔥",  # праздник/огонь
    "🪙": "💲",  # монета → доллар
    "📦": "💰",  # архив/сундук с монетами
    "🔓": "➖",  # отвязка → минус
    "📭": "➖",  # нет файлов → минус
    "✨": "🔥",  # искра → огонь
    "📈": "💬",  # график → сообщения
    "👇": "🔽",  # палец вниз → стрелка вниз
}


def fmt(text: str) -> str:
    """Заменяет обычные эмодзи на премиум и оборачивает их в <tg-emoji>.

    Возвращает HTML-строку, готовую для отправки с parse_mode='HTML'.
    """
    for old, new in EMOJI_MAP.items():
        text = text.replace(old, new)
    # Оборачиваем каждый премиум-плейсхолдер в тег <tg-emoji emoji-id="...">.
    for char, custom_id in PREMIUM_EMOJI.items():
        tag = f'<tg-emoji emoji-id="{custom_id}">{char}</tg-emoji>'
        text = text.replace(char, tag)
    return text


def fmt_html(text: str) -> str:
    """Алиас fmt: возвращает HTML для сообщений."""
    return fmt(text)


# Укажите токен через переменную окружения BOT_TOKEN.
BOT_TOKEN = "9094860695:AAE-nPl9sA4289D8q-hYQtP3femDKkUyYyw"
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Установите токен бота в переменной окружения."
    )

# ID единственного администратора.
ADMIN_ID = 8708484648

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

TOKEN_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Получить токен",
                callback_data="get_token",
                style="primary",
                icon_custom_emoji_id="5456343997779831665",
            )
        ],
        [
            InlineKeyboardButton(
                text="Статистика",
                callback_data="menu_stats",
                style="primary",
                icon_custom_emoji_id="5453952087543015968",
            )
        ],
    ]
)


# chat_id -> message_id последнего отправленного меню (чтобы удалять старое перед новым).
_last_menu: dict[int, int] = {}

# id сообщения меню админ-панели (для редактирования вместо новых сообщений).
_admin_menu_id: dict[int, int] = {}


class AdminStates(StatesGroup):
    waiting_reset_chat = State()
    waiting_upload_pool = State()
    waiting_add_moderator = State()
    waiting_mod_chat_id = State()
    waiting_remove_moderator = State()
    waiting_work_hours = State()
    waiting_hold_minutes = State()


async def send_menu(chat_id: int, extra_text: str = "") -> None:
    """Удаляет предыдущее меню в чате (если есть) и отправляет новое.

    Вызывается из асинхронных обработчиков через await.
    """
    prev_id = _last_menu.pop(chat_id, None)
    if prev_id is not None:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception:
            pass

    html_menu = fmt(MENU_TEXT + extra_text)
    try:
        sent = await bot.send_message(
            chat_id,
            html_menu,
            parse_mode="HTML",
            reply_markup=TOKEN_MENU,
        )
        _last_menu[chat_id] = sent.message_id
    except Exception as exc:
        print(f"send_menu error: {exc}")


async def ap_show(chat_id: int, text: str, markup) -> None:
    """Показывает сообщение админ-панели: редактирует уже существующее меню,
    если оно есть, иначе отправляет новое. Сохраняет message_id.
    """
    html = fmt(text)
    prev = _admin_menu_id.get(chat_id)
    if prev is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=prev,
                text=html,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except Exception:
            pass
    try:
        sent = await bot.send_message(
            chat_id, html, parse_mode="HTML", reply_markup=markup
        )
        _admin_menu_id[chat_id] = sent.message_id
    except Exception as exc:
        print(f"ap_show error: {exc}")


APANEL_MAIN_TEXT = "🪐 АДМИН-ПАНЕЛЬ\nУправление пулами и статистикой"
MODPANEL_TEXT = "🔗 <b>ПАНЕЛЬ МОДЕРАТОРОВ</b>\nУправление модераторами чатов."
BACK_MARKUP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Вернуться в меню",
                callback_data="ap_back",
                style="primary",
                icon_custom_emoji_id="5454172127307523969",
            )
        ]
    ]
)


def apanel_main_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Статус чатов",
                    callback_data="apanel_status",
                    style="primary",
                    icon_custom_emoji_id="5454172127307523969",
                ),
                InlineKeyboardButton(
                    text="Сброс пулов",
                    callback_data="apanel_reset",
                    style="primary",
                    icon_custom_emoji_id="5456173826880593418",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Статистика",
                    callback_data="apanel_stats",
                    style="primary",
                    icon_custom_emoji_id="5453952087543015968",
                ),
                InlineKeyboardButton(
                    text="Сброс статистики чата",
                    callback_data="apanel_reset_stats",
                    style="primary",
                    icon_custom_emoji_id="5453960484204083655",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Пополнить пул",
                    callback_data="apanel_upload_pool",
                    style="primary",
                    icon_custom_emoji_id="5454134554933619492",
                ),
                InlineKeyboardButton(
                    text="Панель Модераторов",
                    callback_data="apanel_modpanel",
                    style="primary",
                    icon_custom_emoji_id="5454068128969417666",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Указать время работы",
                    callback_data="apanel_work_hours",
                    style="primary",
                    icon_custom_emoji_id="5453952087543015968",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Мин. время холда",
                    callback_data="apanel_hold_minutes",
                    style="primary",
                    icon_custom_emoji_id="5454172127307523969",
                ),
            ],
        ]
    )


async def approve_prompt(chat_id: int, text: str, markup=None) -> None:
    """Удаляет текущее меню и отправляет новое сообщение-запрос.

    Если markup не указан — ставится кнопка «Вернуться в меню».
    Сохраняет message_id запроса для дальнейшего удаления при отмене.
    """
    prev = _admin_menu_id.pop(chat_id, None)
    if prev is not None:
        try:
            await bot.delete_message(chat_id, prev)
        except Exception:
            pass
    if markup is None:
        markup = BACK_MARKUP
    sent = await bot.send_message(chat_id, fmt(text), parse_mode="HTML", reply_markup=markup)
    _admin_menu_id[chat_id] = sent.message_id


async def accept_prompt(chat_id: int, text: str) -> None:
    """Убирает сообщение-запрос, показывает подтверждение и возвращает главное меню."""
    prev = _admin_menu_id.pop(chat_id, None)
    if prev is not None:
        try:
            await bot.delete_message(chat_id, prev)
        except Exception:
            pass
    await bot.send_message(chat_id, fmt(text), parse_mode="HTML")
    await show_apanel_menu(chat_id)


async def show_apanel_menu(chat_id: int) -> None:
    """Отправляет главное меню админ-панели (удаляя старое сообщение, если осталось)."""
    prev = _admin_menu_id.pop(chat_id, None)
    if prev is not None:
        try:
            await bot.delete_message(chat_id, prev)
        except Exception:
            pass
    try:
        sent = await bot.send_message(
            chat_id, fmt(APANEL_MAIN_TEXT), parse_mode="HTML", reply_markup=apanel_main_kbd()
        )
        _admin_menu_id[chat_id] = sent.message_id
    except Exception as exc:
        print(f"show_apanel_menu error: {exc}")

# Текст, сопровождающий меню токенов.
MENU_TEXT = (
    "✨ <b>Чтобы взять токен</b> — нажми кнопку ниже 👇\n"
    "📈 <b>Чтобы посмотреть актуальную статистику</b> — нажми «Статистика»."
)

MARK_UP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Встал", callback_data="stood", style="success",
                icon_custom_emoji_id="5456360443209609671",
            ),
            InlineKeyboardButton(
                text="Невалид", callback_data="error", style="danger",
                icon_custom_emoji_id="5456173826880593418",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Спам-блок", callback_data="spamblock", style="danger",
                icon_custom_emoji_id="5453960484204083655",
            ),
            InlineKeyboardButton(
                text="Посмотреть токен", callback_data="view_token",
                style="primary",
                icon_custom_emoji_id="5453952087543015968",
            ),
        ],
    ]
)

# Кнопки после отметки «Встал» — для корректировки статуса токена.
STOOD_MARKUP = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Спам-блок", callback_data="spamblock_followup", style="danger",
                icon_custom_emoji_id="5453960484204083655",
            ),
            InlineKeyboardButton(
                text="Пометить слет", callback_data="unhold", style="danger",
                icon_custom_emoji_id="5456173826880593418",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Посмотреть токен", callback_data="view_token",
                style="primary",
                icon_custom_emoji_id="5453952087543015968",
            ),
        ],
    ]
)

# Общий пул файлов: (имя файла, содержимое).
chat_pools: deque[tuple[str, bytes]] = deque()

# Флаги отправленных уведомлений о низком остатке пула (5, 1, 0).
# Сбрасываются при пополнении пула, чтобы пороги срабатывали заново.
_pool_notified: set[str] = set()

# Последний известный размер пула — чтобы ловить момент реального уменьшения
# и не слать несколько уведомлений, когда пул уже пуст (0 ≤ 5, 0 ≤ 1, 0 ≤ 0).
_last_pool_len: int | None = None

# Буфер медиа-групп загрузки: media_group_id -> [message, ...].
# Когда несколько .txt приходят одним сообщением (медиа-группа), Telegram шлёт их
# отдельными апдейтами с одинаковым media_group_id. Собираем их и отвечаем один раз.
_pending_groups: dict[str, list] = {}
_GROUP_WINDOW_SEC = 1.2

# Дедуп документов внутри медиа-группы: (chat_id, message_id).
_processed_docs: set[tuple[int, int]] = set()

# Чаты, привязанные командой /connect.
connected_chats: set[int] = set()

# Статистика: chat_id -> {date, tokens, stood, error, users}.
chat_stats: dict[int, dict] = {}

# Лимит выданных токенов на чат за день: chat_id -> int (0 или отсутствие = без лимита).
chat_limits: dict[int, int] = {}

# Рабочее время чата (МСК): chat_id -> (start_minutes, end_minutes).
# Вне этого промежутка токены в чате не выдаются.
chat_work_hours: dict[int, tuple[int, int]] = {}

# Минимальное время холда в минутах: chat_id -> int. По умолчанию (если нет) = 0.
# Используется для кнопки «Пометить слет»: если токен отстоял меньше этого срока,
# он снимается из «валидных» и уходит в «невалид».
chat_hold_minutes: dict[int, int] = {}

# Состояние отметки токена: (chat_id, message_id) -> {"field": str, "ts": float}.
# field: "stood"/"error"/"spamblock" — текущий статус; ts — время отметки (unix).
# Нужно для кнопок «Спам-блок» и «Пометить слет» после отметки «Встал».
mark_state: dict[tuple[int, int], dict] = {}

# Модераторы: (chat_id, user_id) -> True. Могут использовать бота в назначенном чате.
moderators: dict[tuple[int, int], bool] = {}

# (chat_id, message_id) -> user_id.
token_owner: dict[tuple[int, int], int] = {}

# (chat_id, message_id) -> (имя файла, содержимое) для кнопки «Посмотреть токен».
token_content: dict[tuple[int, int], tuple[str, bytes]] = {}

# Уже обработанные «chat_id:menu_message_id» (защита от дублей и повторной доставки колбэка).
_processed_callbacks: set[str] = set()

# Кулдаун выдачи: user_id -> время (unix) последнего нажатия «Получить токен».
# Защита от многократных нажатий, чтобы не выдавать несколько токенов подряд.
_token_cooldown: dict[int, float] = {}

# Интервал между нажатиями кнопки «Получить токен» (секунды).
# КД применяется только к тому пользователю, который уже нажимал кнопку, —
# остальные пользователи не блокируются.
TOKEN_CD_SEC = 10

# Таймаут автовалидации (секунды): если за это время после выдачи токена никто
# не нажал ни одной кнопки подтверждения, токен автоматически помечается
# валидным («встал») с юзернеймом «автоматически».
AUTO_VALIDATE_SEC = 60

STATE_FILE = Path(__file__).resolve().with_name("bot_state.json")


def get_stats(chat_id: int, today: date) -> dict:
    """Возвращает статистику за день, создавая её при необходимости."""
    st = chat_stats.get(chat_id)
    if st is None or st.get("date") != today:
        st = {
            "date": today,
            "tokens": 0,
            "stood": 0,
            "error": 0,
            "spamblock": 0,
            "users": {},
        }
        chat_stats[chat_id] = st
    return st


def _save_state_blocking() -> None:
    """Синхронно сохраняет пул, привязки, статистику и ожидающие отметки."""
    stats_data = {}
    for chat_id, st in chat_stats.items():
        stats_data[str(chat_id)] = {
            "date": st["date"].isoformat(),
            "tokens": int(st.get("tokens", 0)),
            "stood": int(st.get("stood", 0)),
            "error": int(st.get("error", 0)),
            "spamblock": int(st.get("spamblock", 0)),
            "users": {
                str(uid): {
                    "name": user.get("name", "unknown"),
                    "tokens": int(user.get("tokens", 0)),
                    "stood": int(user.get("stood", 0)),
                    "error": int(user.get("error", 0)),
                    "spamblock": int(user.get("spamblock", 0)),
                }
                for uid, user in st.get("users", {}).items()
            },
        }

    data = {
        "connected": sorted(connected_chats),
        "pool": [
            [name, base64.b64encode(blob).decode("ascii")]
            for name, blob in chat_pools
        ],
        "stats": stats_data,
        "limits": {str(k): int(v) for k, v in chat_limits.items()},
        "work_hours": {
            str(k): [v[0], v[1]] for k, v in chat_work_hours.items()
        },
        "hold_minutes": {str(k): int(v) for k, v in chat_hold_minutes.items()},
        "mark_state": [
            [chat_id, message_id, entry.get("field"), entry.get("ts")]
            for (chat_id, message_id), entry in mark_state.items()
        ],
        "moderators": [
            [chat_id, uid] for (chat_id, uid) in moderators.keys()
        ],
        "token_owner": [
            [chat_id, message_id, uid]
            for (chat_id, message_id), uid in token_owner.items()
        ],
    }

    temp_file = STATE_FILE.with_suffix(".tmp")
    try:
        temp_file.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_file.replace(STATE_FILE)
    except Exception as exc:
        print(f"save_state error: {exc}")
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass


# Диск пишется в фоновом потоке, чтобы синхронная запись bot_state.json
# не блокировала event loop (пул может быть большим). Вызовы save_state()
# не ждут записи — очереди теряются только при жёстком крахе процесса.
_SAVE_LOCK = threading.Lock()

def save_state() -> None:  # noqa: F811
    """Сохраняет состояние в фоновом потоке, не блокируя event loop."""
    def _worker() -> None:
        with _SAVE_LOCK:
            _save_state_blocking()

    threading.Thread(target=_worker, daemon=True).start()


def load_state() -> None:
    """Загружает сохранённое состояние; поддерживает старый формат файла."""
    if not STATE_FILE.exists():
        return

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        connected_chats.clear()
        connected_chats.update(int(chat_id) for chat_id in data.get("connected", []))

        chat_pools.clear()
        for name, encoded_blob in data.get("pool", []):
            chat_pools.append((str(name), base64.b64decode(encoded_blob)))

        chat_stats.clear()
        for chat_id, raw in data.get("stats", {}).items():
            users = {}
            for uid, user in raw.get("users", {}).items():
                users[int(uid)] = {
                    "name": user.get("name", "unknown"),
                    "tokens": int(user.get("tokens", 0)),
                    "stood": int(user.get("stood", 0)),
                    "error": int(user.get("error", 0)),
                    "spamblock": int(user.get("spamblock", 0)),
                }

            chat_stats[int(chat_id)] = {
                "date": date.fromisoformat(raw["date"]),
                "tokens": int(raw.get("tokens", 0)),
                "stood": int(raw.get("stood", 0)),
                "error": int(raw.get("error", 0)),
                "spamblock": int(raw.get("spamblock", 0)),
                "users": users,
            }

        token_owner.clear()
        for chat_id, message_id, uid in data.get("token_owner", []):
            token_owner[(int(chat_id), int(message_id))] = int(uid)

        chat_limits.clear()
        for chat_id, limit in data.get("limits", {}).items():
            chat_limits[int(chat_id)] = int(limit)

        chat_work_hours.clear()
        for chat_id, wh in data.get("work_hours", {}).items():
            chat_work_hours[int(chat_id)] = (int(wh[0]), int(wh[1]))

        chat_hold_minutes.clear()
        for chat_id, minutes in data.get("hold_minutes", {}).items():
            chat_hold_minutes[int(chat_id)] = int(minutes)

        mark_state.clear()
        for row in data.get("mark_state", []):
            chat_id, message_id, field, ts = row
            mark_state[(int(chat_id), int(message_id))] = {"field": field, "ts": float(ts)}

        moderators.clear()
        for chat_id, uid in data.get("moderators", []):
            moderators[(int(chat_id), int(uid))] = True

    except Exception as exc:
        print(f"load_state error: {exc}")


load_state()


def extract_txt(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.lower().endswith(".txt") and not name.endswith("/"):
                files.append((name.rsplit("/", 1)[-1], archive.read(name)))
    return files


async def log_to_admin(text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, fmt(text), parse_mode="HTML")
    except Exception as exc:
        print(f"log_to_admin error: {exc}")


# Пороги уведомлений о низком остатке пула. Текст строится с реальным остатком.
POOL_THRESHOLDS = [
    (5, "🪙 <b>В пуле остался {n} {word}!</b>\nПоставь пополнение, чтобы пользователи не остались без файлов."),
    (1, "⚡ <b>В пуле остался {n} {word}!</b>\nОн закончится со следующей выдачей."),
    (0, "📭 <b>Токены в пуле закончились!</b>\nЖдём пополнение от @colissium (админ)."),
]


def _plural_tokens(n: int) -> str:
    """Склоняет слово «токен»/'токена'/'токенов' в зависимости от числа."""
    if 11 <= n % 100 <= 14:
        return "токенов"
    if n % 10 == 1:
        return "токен"
    if 2 <= n % 10 <= 4:
        return "токена"
    return "токенов"


def _notify_pool_low(chat_id: int) -> None:
    """Отправляет уведомление в чат и админу, когда пул реально иссяк.

    Уведомление шлётся ОДИН раз на границу, только при фактическом уменьшении:
    - когда остаток первый раз опустился до 5 (сейчас осталось 2, но сработало
      когда дошли до границы) — поэтому сравниваем с предыдущим значением;
    - до 1 и до 0.

    Раньше «0 ≤ 5, 0 ≤ 1, 0 ≤ 0» слали сразу три уведомления о 0 — убрали:
    теперь ловим момент пересечения вниз через _last_pool_len.
    """
    global _last_pool_len
    try:
        remaining = len(chat_pools)
        prev = _last_pool_len
        _last_pool_len = remaining

        # Ищем пересечение: предыдущий остаток был БОЛЬШЕ порога, а текущий ≤ него.
        for threshold, template in POOL_THRESHOLDS:
            key = f"{threshold}"
            crossed = prev is not None and prev > threshold and remaining <= threshold
            # На самом старте (prev is None) не спамим — подождём первого пересечения.
            if crossed and key not in _pool_notified:
                _pool_notified.add(key)
                if threshold == 0:
                    message = template
                else:
                    message = template.format(n=remaining, word=_plural_tokens(remaining))
                asyncio.get_running_loop().create_task(_send_pool_notice(chat_id, message))
            elif not crossed and key in _pool_notified:
                # Остаток снова стал выше границы (пополнение) — разрешаем повторно.
                _pool_notified.discard(key)
    except Exception as exc:
        print(f"_notify_pool_low error: {exc}")


async def _send_pool_notice(chat_id: int, message: str) -> None:
    """Отправляет найденное уведомление в чат и админу в ЛС."""
    html = fmt(message)
    try:
        await bot.send_message(chat_id, html, parse_mode="HTML")
    except Exception as exc:
        print(f"pool notice chat error: {exc}")
    await log_to_admin(message)


async def issue_token(chat_id: int, uid: int, username: str) -> str:
    """Выдаёт один токен из общего пула в чат и учитывает его в статистике.

    Возвращает статус: "ok", "empty" (пул пуст) или "limit" (достигнут лимит чата).
    """
    if not chat_pools:
        return "empty"

    # Проверка рабочего времени чата (МСК). Вне него токены не выдаются.
    if not _is_work_time(chat_id):
        return "offhours"

    # Проверка дневного лимита чата.
    limit = chat_limits.get(chat_id)
    if limit:
        st_check = get_stats(chat_id, date.today())
        if st_check["tokens"] >= limit:
            return "limit"

    name, data = chat_pools.popleft()
    caption_text = f"{name}\n➕ Готов к приёму · 💵 токен для @{username}"
    sent = await bot.send_document(
        chat_id,
        types.BufferedInputFile(data, filename=name),
        caption=fmt(caption_text),
        parse_mode="HTML",
        reply_markup=MARK_UP,
    )
    token_owner[(chat_id, sent.message_id)] = uid
    token_content[(chat_id, sent.message_id)] = (name, data)

    # Автовалидация: если за AUTO_VALIDATE_SEC никто не нажмёт кнопку статуса,
    # токен автоматически помечается валидным («встал») с юзернеймом «автоматически».
    asyncio.get_running_loop().create_task(
        _auto_validate(chat_id, sent.message_id, uid, username)
    )
    st = get_stats(chat_id, date.today())
    st["tokens"] += 1
    user = st["users"].setdefault(
        uid,
        {"name": username, "tokens": 0, "stood": 0, "error": 0, "spamblock": 0},
    )
    user["name"] = username
    user["tokens"] += 1
    save_state()

    # Проверяем, не пора ли предупредить о низком остатке пула.
    _notify_pool_low(chat_id)

    # Отправляем лог в фоне, чтобы он не задерживал выдачу следующего токена.
    async def _log():
        await log_to_admin(
            f"🪙 Токен взят:\nФайл: {name}\nПользователь: @{username} (id {uid})\n"
            f"Чат: {chat_id}\nОстаток в пуле: {len(chat_pools)}"
        )

    asyncio.get_running_loop().create_task(_log())
    return "ok"


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text = "🔥 Привет! 💰 Отправь ZIP-архив с .txt файлами, а затем нажми «💵 Получить токен» в меню бота."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("connect"))
async def cmd_connect(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    connected_chats.add(message.chat.id)
    save_state()
    await log_to_admin(f"🔒 Чат привязан: {message.chat.id}")

    text = "🔒 Бот привязан к этому чату.\n\n" + MENU_TEXT
    html = fmt(text)
    sent = await message.answer(html, parse_mode="HTML", reply_markup=TOKEN_MENU)
    _last_menu[message.chat.id] = sent.message_id


@dp.message(Command("unconnect"))
async def cmd_unconnect(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    connected_chats.discard(message.chat.id)
    save_state()
    await log_to_admin(f"🔓 Чат отвязан: {message.chat.id}")

    text = "🔓 Бот отвязан от этого чата. Команды бота больше недоступны."
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("limit"))
async def cmd_limit(message: types.Message):
    if message.from_user is None or not can_manage(message.from_user.id, message.chat.id):
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    parts = (message.text or "").split()
    chat_id = message.chat.id
    if len(parts) == 1:
        cur = chat_limits.get(chat_id, 0)
        if cur:
            text = f"💬 Текущий лимит токенов в этом чате: <b>{cur}</b> за день."
        else:
            text = "📭 Лимит в этом чате не установлен (без ограничений)."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    try:
        limit = int(parts[1])
    except ValueError:
        text = "➖ Лимит должен быть числом. Пример: /limit 100 или /limit 0 (убрать)."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if limit < 0:
        text = "➖ Лимит не может быть отрицательным."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if limit == 0:
        chat_limits.pop(chat_id, None)
        text = "➖ Лимит снят: токены в этом чате выдаются без ограничений."
    else:
        chat_limits[chat_id] = limit
        text = f"➕ Установлен лимит: <b>{limit}</b> токенов за день в этом чате."

    save_state()
    await log_to_admin(f"🔒 Лимит чата {chat_id} -> {limit if limit else 'без лимита'}")
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


async def is_connected(message: types.Message) -> bool:
    if message.chat.id not in connected_chats:
        text = "➖ Бот не привязан к этому чату. Используй /connect."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return False
    return True


def can_manage(user_id: int, chat_id: int) -> bool:
    """Имеет ли пользователь права управления в чате (админ или модератор)."""
    if user_id == ADMIN_ID:
        return True
    return moderators.get((chat_id, user_id), False)


def is_private(message: types.Message) -> bool:
    return message.chat.type == "private"


_MSK_TZ = timezone(timedelta(hours=3))


def _now_msk_minutes() -> int:
    """Текущее время по МСК (UTC+3) в минутах от полуночи."""
    now = datetime.now(_MSK_TZ)
    return now.hour * 60 + now.minute


def parse_work_range(raw: str) -> tuple[int, int] | None:
    """Парсит строку времени вида '9:00-18:00' (по МСК) в минуты от полуночи.

    Возвращает (start_minutes, end_minutes) или None при неверном формате.
    """
    try:
        start_raw, end_raw = raw.split("-", 1)
        start_h, start_m = (int(x) for x in start_raw.split(":", 1))
        end_h, end_m = (int(x) for x in end_raw.split(":", 1))
    except (ValueError, AttributeError):
        return None
    if not (0 <= start_h <= 23 and 0 <= start_m <= 59):
        return None
    if not (0 <= end_h <= 23 and 0 <= end_m <= 59):
        return None
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return None
    return start, end


def _is_work_time(chat_id: int) -> bool:
    """Разрешена ли выдача токенов в чате сейчас (с учётом рабочего времени, МСК)."""
    wh = chat_work_hours.get(chat_id)
    if not wh:
        return True
    start_min, end_min = wh
    cur = _now_msk_minutes()

    # Диапазон за один день (start < end), например 9:00-18:00.
    if start_min < end_min:
        return start_min <= cur < end_min
    # Диапазон через полночь (start > end), например 22:00-06:00.
    return cur >= start_min or cur < end_min


@dp.message(F.document)
async def on_document(message: types.Message, state: FSMContext):
    # Документы (архивы) может загружать администратор или модератор чата.
    if message.from_user is None or not can_manage(message.from_user.id, message.chat.id):
        text = "❗ Загружать файлы может только администратор или модератор."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    # Медиа-группа (несколько .txt одним сообщением): Telegram шлёт их отдельными
    # апдейтами с одинаковым media_group_id. Собираем и отвечаем ОДИН раз за группу.
    group_id = getattr(message, "media_group_id", None)
    if group_id:
        _pending_groups.setdefault(group_id, []).append(message)
        # Запускаем флаш-задачу один раз на группу.
        if len(_pending_groups[group_id]) == 1:
            asyncio.get_running_loop().create_task(_flush_group(group_id, state))
        return

    # Одиночный документ — обрабатываем сразу.
    await _process_single_document(message, state)


async def _process_single_document(message: types.Message, state: FSMContext) -> None:
    """Извлекает файлы из одного документа, добавляет в пул и отвечает."""
    doc = message.document
    if doc is None or not (doc.file_name or ""):
        await message.answer("➖ Пожалуйста, отправь ZIP-архив или .txt файл.")
        return

    fname = (doc.file_name or "").lower()
    is_zip = fname.endswith(".zip")
    is_txt = fname.endswith(".txt")
    if not is_zip and not is_txt:
        cur = await state.get_state()
        if cur == AdminStates.waiting_upload_pool.state:
            text = "➖ Отправь ZIP-архив или .txt файл для пополнения пула."
        else:
            text = "➖ Пожалуйста, отправь ZIP-архив или .txt файл."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    file_info = await bot.get_file(doc.file_id)
    destination = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=destination)
    data = destination.getvalue()

    if is_zip:
        try:
            files = extract_txt(data)
        except (zipfile.BadZipFile, OSError) as exc:
            text = f"➖ Не удалось прочитать ZIP-архив: {exc}"
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        if not files:
            text = "➖ В архиве нет .txt файлов."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
    else:
        name = doc.file_name.rsplit("/", 1)[-1]
        files = [(name, data)]

    await _apply_files(message, state, files, source_name=doc.file_name)


async def _flush_group(group_id: str, state: FSMContext) -> None:
    """Ждёт окно, потом обрабатывает все документы медиа-группы разом."""
    await asyncio.sleep(_GROUP_WINDOW_SEC)
    messages = _pending_groups.pop(group_id, [])
    if not messages:
        return

    first = messages[0]
    files: list[tuple[str, bytes]] = []
    added_any = False
    error_parts: list[str] = []

    for msg in messages:
        doc = msg.document
        if doc is None or not (doc.file_name or ""):
            continue
        fname = (doc.file_name or "").lower()
        # В группе обычно идут .txt; zip внутри группы обработаем тоже.
        if not (fname.endswith(".zip") or fname.endswith(".txt")):
            error_parts.append(f"{doc.file_name}: не тот формат")
            continue

        key = (msg.chat.id, msg.message_id)
        if key in _processed_docs:
            continue
        _processed_docs.add(key)

        try:
            file_info = await bot.get_file(doc.file_id)
            destination = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=destination)
            data = destination.getvalue()
        except Exception as exc:
            error_parts.append(f"{doc.file_name}: ошибка скачивания ({exc})")
            continue

        if fname.endswith(".zip"):
            try:
                zipped = extract_txt(data)
            except (zipfile.BadZipFile, OSError) as exc:
                error_parts.append(f"{doc.file_name}: не ZIP ({exc})")
                continue
            if not zipped:
                error_parts.append(f"{doc.file_name}: нет .txt в архиве")
            else:
                files.extend(zipped)
                added_any = True
        else:
            files.append((doc.file_name.rsplit("/", 1)[-1], data))
            added_any = True

    if files:
        await _apply_files(first, state, files, source_name=f"медиа-группа ({len(messages)} файл.)")
    elif error_parts:
        text = "➖ " + "\n".join(error_parts)
        html = fmt(text)
        try:
            await first.answer(html, parse_mode="HTML")
        except Exception:
            pass


async def _apply_files(message: types.Message, state: FSMContext,
                       files: list[tuple[str, bytes]], source_name: str) -> None:
    """Добавляет файлы в пул, сохраняет, логирует и даёт ОДИН ответ."""
    chat_pools.extend(files)
    global _last_pool_len
    _last_pool_len = None
    _pool_notified.clear()
    save_state()

    # Если файл загружен в рамках пополнения пула — очищаем состояние.
    cur = await state.get_state()
    if cur == AdminStates.waiting_upload_pool.state:
        await state.clear()

    names = ", ".join(name for name, _ in files)
    await log_to_admin(
        f"💰 Загружен файл ({source_name}): +{len(files)} txt, "
        f"всего в пуле {len(chat_pools)}\nФайлы: {names}"
    )
    text = (
        f"➕ Файлы добавлены в пул. Всего в пуле: {len(chat_pools)}. "
        "Нажми «💵 Получить токен» в меню."
    )
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("token"))
async def cmd_token(message: types.Message):
    if is_private(message):
        text = "❗ Команда /token недоступна в личных сообщениях."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not await is_connected(message):
        return

    username = message.from_user.username or message.from_user.full_name
    status = await issue_token(message.chat.id, message.from_user.id, username)
    if status == "limit":
        lim = chat_limits.get(message.chat.id, 0)
        text = f"🔒 Дневной лимит токенов в этом чате достигнут ({lim}). Попробуй завтра."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        await send_menu(message.chat.id)
        return
    if status == "offhours":
        text = "⚡ Ворк ещё не начался или уже закончился. Обратитесь к Администратору."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        await send_menu(message.chat.id)
        return
    if status == "empty":
        text = "📭 Файлов в пуле нет. Отправь ZIP-архив, чтобы добавить файлы."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    # Показываем меню заново, чтобы кнопка не потерялась в чате.
    await send_menu(message.chat.id)


@dp.callback_query(F.data == "menu_stats")
async def on_menu_stats(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Сразу подтверждаем колбэк.
    try:
        await callback.answer()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    if chat_id not in connected_chats:
        await callback.answer("Бот не привязан к этому чату.", show_alert=True)
        return

    html = fmt(build_stats_text(chat_id))
    try:
        await callback.message.edit_text(
            html,
            parse_mode="HTML",
            reply_markup=TOKEN_MENU,
        )
    except Exception:
        pass


@dp.callback_query(F.data == "get_token")
async def on_get_token(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    chat_id = callback.message.chat.id

    # Кулдаун проверяем СРАЗУ, до какого-либо подтверждения колбэка:
    # тогда всплывающее окно «Подождите немного» становится первым и единственным
    # ответом, и кнопка не виснет «грузится».
    now = time.time()
    last = _token_cooldown.get(callback.from_user.id)
    if last is not None and now - last < TOKEN_CD_SEC:
        await callback.answer("Подождите немного", show_alert=True)
        return

    # Ключ дедупликации: связка «чат + id меню-сообщения», которые уже обработаны.
    dedup_key = f"{chat_id}:{callback.message.message_id}"
    if dedup_key in _processed_callbacks:
        await callback.answer()
        return
    _processed_callbacks.add(dedup_key)

    # Сразу подтверждаем колбэк, не дожидаясь долгих операций.
    try:
        await callback.answer()
    except Exception:
        pass

    if chat_id not in connected_chats:
        await callback.answer("Бот не привязан к этому чату.", show_alert=True)
        return

    _token_cooldown[callback.from_user.id] = now

    original_message_id = callback.message.message_id
    username = callback.from_user.username or callback.from_user.full_name
    status = await issue_token(chat_id, callback.from_user.id, username)

    if status in ("limit", "offhours", "empty"):
        # Токен не выдан. Не плодим сообщения — просто редактируем текущее меню,
        # добавляя пояснение. Кнопки меню остаются.
        if status == "limit":
            lim = chat_limits.get(chat_id, 0)
            note = f"\n\n🔒 Дневной лимит токенов в этом чате достигнут ({lim})."
        elif status == "offhours":
            note = "\n\n⚡ Ворк ещё не начался или уже закончился. Обратитесь к Администратору."
        else:
            note = "\n\n📭 Файлы закончились."
        # Сообщение-меню остаётся тем же (редактируется), поэтому снимаем дедуп:
        # после пополнения пула нажатие на это же сообщение должно сработать снова.
        _processed_callbacks.discard(dedup_key)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=original_message_id,
                text=fmt(MENU_TEXT + note),
                parse_mode="HTML",
                reply_markup=TOKEN_MENU,
            )
        except Exception:
            # Не смогли отредактировать — отправим меню заново.
            await send_menu(chat_id, note)
        return

    # Токен успешно выдан: удаляем меню и показываем его заново.
    try:
        await bot.delete_message(chat_id, original_message_id)
    except Exception:
        pass

    # После выдачи снова отправляем меню.
    print(f"[get_token] issuing done, sending menu to {chat_id}, pool={len(chat_pools)}")
    await send_menu(chat_id)


@dp.callback_query(F.data == "view_token")
async def on_view_token(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Сразу подтверждаем колбэк.
    try:
        await callback.answer()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    key = (chat_id, callback.message.message_id)
    entry = token_content.get(key)
    if entry is None:
        await bot.send_message(
            chat_id,
            fmt("📭 Контент токена больше не доступен."),
            parse_mode="HTML",
        )
        return

    name, data = entry
    body = data.decode("utf-8", errors="replace")

    import html as html_mod
    safe = html_mod.escape(body)

    back_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Вернуться к файлу", callback_data="back_to_file",
                    style="primary",
                    icon_custom_emoji_id="5456343997779831665",
                )
            ]
        ]
    )
    viewer = callback.from_user.username or callback.from_user.full_name or str(callback.from_user.id)
    text = (
        f"✅ <b>Содержимое токена</b> ({name})\n"
        f"для: @{viewer}:\n\n"
        f"<pre>{safe}</pre>"
    )
    # Отправляем JSON отдельным сообщением, НЕ трогая исходный файл.
    try:
        sent = await bot.send_message(
            chat_id,
            fmt(text),
            parse_mode="HTML",
            reply_markup=back_markup,
        )
        # Автоудаление JSON-сообщения через 10 секунд, чтобы не засорять чат.
        import asyncio as _asyncio

        async def _autodelete():
            await _asyncio.sleep(10)
            try:
                await bot.delete_message(chat_id, sent.message_id)
            except Exception:
                pass

        _asyncio.get_running_loop().create_task(_autodelete())
    except Exception:
        pass

    # Меню должно ВСЕГДА оставаться последним сообщением в чате.
    await send_menu(chat_id)


@dp.callback_query(F.data == "back_to_file")
async def on_back_to_file(callback: types.CallbackQuery):
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Сразу подтверждаем колбэк.
    try:
        await callback.answer()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    # Удаляем JSON-сообщение; исходный файл остаётся в чате.
    try:
        await bot.delete_message(chat_id, callback.message.message_id)
    except Exception:
        pass

    # Меню должно ВСЕГДА оставаться последним сообщением в чате.
    await send_menu(chat_id)


def build_stats_text(chat_id: int) -> str:
    """Строит текст статистики чата за сегодня (без форматирования эмодзи)."""
    st = get_stats(chat_id, date.today())
    total = st["tokens"]
    if total == 0:
        return "📭 За сегодня токены ещё не выдавались."

    def pct(value: int) -> str:
        return f"{round(value / total * 100)}%"

    ranked = sorted(
        st["users"].values(),
        key=lambda user: user["tokens"],
        reverse=True,
    )
    top_lines = "\n".join(
        f"{index}. @{user['name']} - ({user['tokens']})"
        for index, user in enumerate(ranked, start=1)
    ) or "Пока нет участников."

    return (
        "➕ <b>Проверка успешно завершена!</b>\n"
        "💬 <b>Статистика за сегодня</b>\n\n"
        f"➕ <b>Валид:</b> {st['stood']} ({pct(st['stood'])})\n"
        f"➖ <b>Невалид:</b> {st['error']} ({pct(st['error'])})\n"
        f"❗ <b>Спам-блок:</b> {st.get('spamblock', 0)} ({pct(st.get('spamblock', 0))})\n"
        f"💰 <b>Итого:</b> {total} (100%)\n\n"
        "🔗 <b>ТОП ПОЛЬЗОВАТЕЛЕЙ</b>\n\n"
        f"{top_lines}"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if is_private(message):
        text = "❗ Команда /stats недоступна в личных сообщениях."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not await is_connected(message):
        return

    html = fmt(build_stats_text(message.chat.id))
    await message.answer(html, parse_mode="HTML")


@dp.message(Command("boost"), F.chat.type == "private")
async def cmd_boost_private(message: types.Message):
    """Админская корректировка счётчиков «валид» (+stood) из личного чата с ботом."""
    await _adjust_stats(
        message,
        kind="stood",
        plus=True,
        cmd_name="boost",
        field_user="stood",
        field_stat="stood",
        detail_label="Валид",
        what="валидных",
    )


@dp.message(Command("unboost"), F.chat.type == "private")
async def cmd_unboost_private(message: types.Message):
    """Админская корректировка счётчиков «ошибка» (+error) из личного чата с ботом."""
    await _adjust_stats(
        message,
        kind="error",
        plus=True,
        cmd_name="unboost",
        field_user="error",
        field_stat="error",
        detail_label="Ошибка",
        what="ошибочных",
    )


async def _adjust_stats(
    message: types.Message,
    *,
    kind: str,
    plus: bool,
    cmd_name: str,
    field_user: str,
    field_stat: str,
    detail_label: str,
    what: str,
) -> None:
    """Общая логика админской корректировки счётчиков /boost и /unboost."""
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет прав на эту команду."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    parts = (message.text or "").split()
    if len(parts) not in (2, 3):
        await message.answer(
            "Формат команды:\n"
            f"/{cmd_name} <количество> — если подключена одна группа\n"
            f"/{cmd_name} <chat_id> <количество> — если групп несколько"
        )
        return

    if len(parts) == 2:
        if not connected_chats:
            text = "📭 Нет подключённых групп. Сначала используй /connect в группе."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        if len(connected_chats) != 1:
            chat_ids = "\n".join(str(chat_id) for chat_id in sorted(connected_chats))
            text = (
                "➖ Подключено несколько групп. Укажи ID целевой группы:\n"
                f"/{cmd_name} <chat_id> <количество>\n\nПодключённые ID:\n{chat_ids}"
            )
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        chat_id = next(iter(connected_chats))
        amount_raw = parts[1]
    else:
        try:
            chat_id = int(parts[1])
        except ValueError:
            text = "➖ ID группы должен быть числом."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return
        amount_raw = parts[2]
        if chat_id not in connected_chats:
            text = "➖ Эта группа не подключена к боту."
            html = fmt(text)
            await message.answer(html, parse_mode="HTML")
            return

    try:
        amount = int(amount_raw)
    except ValueError:
        text = f"➖ Количество должно быть целым числом. Пример: /{cmd_name} 10"
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    if not 1 <= amount <= 1000:
        text = "➖ Укажи количество от 1 до 1000."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    st = get_stats(chat_id, date.today())
    eligible_ids = [
        uid for uid, user in st["users"].items()
        if int(user.get("tokens", 0)) > 0
    ]
    if not eligible_ids:
        text = "📭 В этой группе сегодня ещё нет участников, которые брали файлы через /token."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    # Случайно и равномерно распределяем корректировку между участниками.
    shuffled_ids = eligible_ids[:]
    random.shuffle(shuffled_ids)
    distribution = {uid: 0 for uid in eligible_ids}

    for index in range(amount):
        if index > 0 and index % len(shuffled_ids) == 0:
            random.shuffle(shuffled_ids)
        uid = shuffled_ids[index % len(shuffled_ids)]
        user = st["users"][uid]
        user["tokens"] = int(user.get("tokens", 0)) + 1
        user[field_user] = int(user.get(field_user, 0)) + 1
        distribution[uid] += 1

    st["tokens"] += amount
    st[field_stat] += amount
    save_state()

    details = [
        f"• @{st['users'][uid]['name']}: +{count}"
        for uid, count in distribution.items()
        if count
    ]
    text = (
        f"➖ Счётчики группы {chat_id} обновлены.\n"
        f"Добавлено {what}: {amount}\n"
        f"💰 Итого: {st['tokens']}\n"
        f"➖ {detail_label}: {st[field_stat]}\n\n"
        "Распределение по участникам:\n" + "\n".join(details)
    )
    html = fmt(text)
    await message.answer(html, parse_mode="HTML")
    await log_to_admin(
        f"Корректировка счётчиков ({cmd_name})\nЧат: {chat_id}\n"
        f"Количество: {amount} {what}\n"
        + "\n".join(details)
    )


@dp.message(Command("apanel"))
async def cmd_apanel(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        text = "❗ У вас нет доступа к админ-панели."
        html = fmt(text)
        await message.answer(html, parse_mode="HTML")
        return

    await show_apanel_menu(message.chat.id)


@dp.callback_query(F.data == "ap_back")
async def on_ap_back(callback: types.CallbackQuery):
    if callback.from_user is None or callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    chat_id = callback.message.chat.id
    # Удаляем сообщение с запросом.
    try:
        await bot.delete_message(chat_id, callback.message.message_id)
    except Exception:
        pass
    try:
        await callback.answer()
    except Exception:
        pass
    await show_apanel_menu(chat_id)


@dp.callback_query(F.data.startswith("apanel_"))
async def on_apanel_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    action = callback.data
    chat_id = callback.message.chat.id
    if action == "apanel_status":
        if connected_chats:
            lines = [f"🔗 {chat_id} — подключён" for chat_id in sorted(connected_chats)]
            lines.append(f"💰 Всего файлов в пуле: {len(chat_pools)}")
            text = "🔽 Статус чатов\n\n" + "\n".join(lines)
            await approve_prompt(chat_id, text, None)
        else:
            text = "📭 Нет подключённых чатов."
            await approve_prompt(chat_id, text, None)

    elif action == "apanel_reset":
        chat_pools.clear()
        global _last_pool_len
        _last_pool_len = None
        _pool_notified.clear()
        save_state()
        text = "➖ Все файлы из пула удалены."
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_stats":
        lines = []
        for cid in sorted(connected_chats):
            st = get_stats(cid, date.today())
            lines.append(
                f"💵 Токены: {st['tokens']}, ➕ встал: {st['stood']}, "
                f"➖ невалид: {st['error']}, ❗ спам-блок: {st.get('spamblock', 0)} "
                f"(чат {cid})"
            )
        body = "\n".join(lines) if lines else "📭 Нет данных."
        text = "💬 Статистика\n\n" + body
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_reset_stats":
        await state.set_state(AdminStates.waiting_reset_chat)
        text = "🔽 Отправь ID чата, для которого сбросить статистику ⚠️ (безвозвратно)."
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_upload_pool":
        await state.set_state(AdminStates.waiting_upload_pool)
        text = "💰 Отправь ZIP-архив с .txt файлами — он будет добавлен в общий пул."
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_work_hours":
        await state.set_state(AdminStates.waiting_work_hours)
        text = (
            "⚡ <b>Указать время работы чата (по МСК).</b>\n"
            "Отправь сообщение в формате:\n"
            "<code>ID_чата время_начала-время_конца</code>\n\n"
            "Пример: <code>-100564881 9:00-18:00</code>\n\n"
            "Время указывается по МСК. Вне рабочего промежутка токены "
            "в этом чате выдаваться не будут."
        )
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_hold_minutes":
        await state.set_state(AdminStates.waiting_hold_minutes)
        text = (
            "⚡ <b>Минимальное время холда чата.</b>\n"
            "Отправь сообщение в формате:\n"
            "<code>ID_чата минуты</code>\n\n"
            "Пример: <code>-100564881 5</code>\n\n"
            "Токен, помеченный «Встал» и отмеченный «слет» раньше этого срока, "
            "уйдёт в невалид. Отправь только ID — чтобы убрать настройку."
        )
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_modpanel":
        submenu = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Добавить модератора",
                        callback_data="apanel_add_mod",
                        style="primary",
                        icon_custom_emoji_id="5456360443209609671",
                    ),
                    InlineKeyboardButton(
                        text="Снять модератора",
                        callback_data="apanel_remove_mod",
                        style="primary",
                        icon_custom_emoji_id="5456173826880593418",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Вернуться в меню",
                        callback_data="ap_back",
                        style="primary",
                        icon_custom_emoji_id="5454172127307523969",
                    ),
                ],
            ]
        )
        text = "🔗 <b>ПАНЕЛЬ МОДЕРАТОРОВ</b>\nУправление модераторами чатов."
        await approve_prompt(chat_id, text, submenu)

    elif action == "apanel_back":
        # Возврат в главное меню админ-панели.
        await show_apanel_menu(chat_id)

    elif action == "apanel_add_mod":
        await state.set_state(AdminStates.waiting_mod_chat_id)
        text = (
            "➕ Добавление модератора.\n\n"
            "Отправь ID чата, в котором модератору дать права."
        )
        await approve_prompt(chat_id, text, None)

    elif action == "apanel_remove_mod":
        await state.set_state(AdminStates.waiting_remove_moderator)
        text = (
            "➖ Снятие модератора.\n\n"
            "Отправь ID модератора (аккаунта Telegram), которого снять.\n"
            ""
        )
        await approve_prompt(chat_id, text, None)

    await callback.answer()


@dp.message(AdminStates.waiting_reset_chat)
async def on_reset_stats_chat(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    text_in = (message.text or "").strip()
    try:
        chat_id = int(text_in)
    except ValueError:
        text = "➖ ID чата должен быть числом. Попробуй ещё раз."
        await approve_prompt(message.chat.id, text, None)
        return

    # Сброс статистики выбранного чата за сегодня.
    chat_stats.pop(chat_id, None)
    save_state()

    # Удаляем ввод пользователя, чтобы чат был компактным.
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    await accept_prompt(message.chat.id, f"➕ Статистика чата {chat_id} сброшена.")
    await state.clear()


@dp.message(AdminStates.waiting_work_hours)
async def on_work_hours(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    parts = (message.text or "").strip().split()
    # Удаляем ввод пользователя, чтобы чат был компактным.
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    # Формат: "ID_чата" — снять рабочее время; "ID_чата 9:00-18:00" — установить.
    if len(parts) < 1 or len(parts) > 2:
        await approve_prompt(
            message.chat.id,
            "➖ Неверный формат. Пример: <code>-100564881 9:00-18:00</code>"
            " (или только ID, чтобы снять ограничение).",
            None,
        )
        return

    try:
        chat_id = int(parts[0])
    except ValueError:
        await approve_prompt(
            message.chat.id,
            "➖ ID чата должен быть числом. Попробуй ещё раз.",
            None,
        )
        return

    # Если задано только время — парсим диапазон.
    if len(parts) == 2:
        wh = parse_work_range(parts[1])
        if wh is None:
            await approve_prompt(
                message.chat.id,
                "➖ Неверный формат времени. Ожидается <code>9:00-18:00</code>.",
                None,
            )
            return
        chat_work_hours[chat_id] = wh
        save_state()
        text = (
            f"➕ Для чата <b>{chat_id}</b> установлено рабочее время "
            f"(МСК): <b>{parts[1]}</b>."
        )
        await accept_prompt(message.chat.id, text)
        await state.clear()
        return

    # Только ID — снимаем ограничение по времени.
    chat_work_hours.pop(chat_id, None)
    save_state()
    await accept_prompt(
        message.chat.id,
        f"➖ Для чата <b>{chat_id}</b> рабочее время снято (токены выдаются круглосуточно).",
    )
    await state.clear()


@dp.message(AdminStates.waiting_hold_minutes)
async def on_hold_minutes(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    parts = (message.text or "").strip().split()
    # Удаляем ввод пользователя, чтобы чат был компактным.
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    # Формат: "ID_чата" — снять настройку; "ID_чата минуты" — установить.
    if len(parts) < 1 or len(parts) > 2:
        await approve_prompt(
            message.chat.id,
            "➖ Неверный формат. Пример: <code>-100564881 5</code>"
            " (или только ID, чтобы убрать настройку).",
            None,
        )
        return

    try:
        chat_id = int(parts[0])
    except ValueError:
        await approve_prompt(
            message.chat.id,
            "➖ ID чата должен быть числом. Попробуй ещё раз.",
            None,
        )
        return

    # ID + минуты — устанавливаем холд.
    if len(parts) == 2:
        try:
            minutes = int(parts[1])
        except ValueError:
            await approve_prompt(
                message.chat.id,
                "➖ Минуты должны быть целым числом. Пример: <code>5</code>.",
                None,
            )
            return
        if minutes <= 0:
            chat_hold_minutes.pop(chat_id, None)
        else:
            chat_hold_minutes[chat_id] = minutes
        save_state()
        note = f"{minutes} мин" if minutes > 0 else "без ограничений"
        await accept_prompt(
            message.chat.id,
            f"➕ Для чата <b>{chat_id}</b> минимальное время холда: <b>{note}</b>.",
        )
        await state.clear()
        return

    # Только ID — снимаем настройку холда.
    chat_hold_minutes.pop(chat_id, None)
    save_state()
    await accept_prompt(
        message.chat.id,
        f"➖ Для чата <b>{chat_id}</b> минимальное время холда снято.",
    )
    await state.clear()


async def _cancel_and_home(chat_id: int, state: FSMContext, note: str = "Отменено.") -> None:
    """Сбрасывает состояние и показывает главное меню админ-панели компактно.

    Удаляет сообщение-запрос и возвращает меню.
    """
    await state.clear()
    # Сообщение запроса — это последнее отправленное; удалим его.
    prev = _admin_menu_id.pop(chat_id, None)
    if prev is not None:
        try:
            await bot.delete_message(chat_id, prev)
        except Exception:
            pass
    await bot.send_message(chat_id, fmt(f"➖ {note}"), parse_mode="HTML")
    await show_apanel_menu(chat_id)


# Единый обработчик /cancel для всех FSM-состояний админ-панели.
@dp.message(Command("cancel"), AdminStates.waiting_reset_chat)
async def on_cancel_reset(message: types.Message, state: FSMContext):
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await _cancel_and_home(message.chat.id, state)


@dp.message(Command("cancel"), AdminStates.waiting_upload_pool)
async def on_cancel_upload(message: types.Message, state: FSMContext):
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await _cancel_and_home(message.chat.id, state)


@dp.message(Command("cancel"), AdminStates.waiting_mod_chat_id)
async def on_cancel_mod_chat(message: types.Message, state: FSMContext):
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await _cancel_and_home(message.chat.id, state)


@dp.message(Command("cancel"), AdminStates.waiting_add_moderator)
async def on_cancel_add_mod(message: types.Message, state: FSMContext):
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await _cancel_and_home(message.chat.id, state)


@dp.message(Command("cancel"), AdminStates.waiting_remove_moderator)
async def on_cancel_remove_mod(message: types.Message, state: FSMContext):
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await _cancel_and_home(message.chat.id, state)


@dp.message(AdminStates.waiting_mod_chat_id)
async def on_add_mod_chat(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        chat_id = int((message.text or "").strip())
    except ValueError:
        text = "➖ ID чата должен быть числом."
        await approve_prompt(message.chat.id, text, None)
        return
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    await state.update_data(mod_chat_id=chat_id)
    await state.set_state(AdminStates.waiting_add_moderator)
    text = (
        f"➕ Чат <b>{chat_id}</b> выбран.\n\n"
        "Теперь отправь <b>ID аккаунта Telegram</b> пользователя, "
        "которому дать права модератора.\n"
    )
    await approve_prompt(message.chat.id, text, None)


@dp.message(AdminStates.waiting_add_moderator)
async def on_add_mod_user(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        text = "➖ ID аккаунта должен быть числом."
        await approve_prompt(message.chat.id, text, None)
        return

    data = await state.get_data()
    chat_id = data.get("mod_chat_id")
    if chat_id is None:
        await state.clear()
        return

    moderators[(chat_id, user_id)] = True
    save_state()
    await log_to_admin(f"➕ Модератор {user_id} добавлен в чат {chat_id}")

    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    await accept_prompt(message.chat.id, f"➕ Модератор <b>{user_id}</b> получил права в чате <b>{chat_id}</b>.")
    await state.clear()


@dp.message(AdminStates.waiting_remove_moderator)
async def on_remove_mod(message: types.Message, state: FSMContext):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        text = "➖ ID аккаунта должен быть числом."
        await approve_prompt(message.chat.id, text, None)
        return

    removed = [(c, u) for (c, u) in moderators if u == user_id]
    for c, u in removed:
        moderators.pop((c, u), None)
    save_state()

    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    if removed:
        await log_to_admin(f"➖ Модератор {user_id} снят (чаты: {[c for c, _ in removed]})")
        text = f"➖ Модератор <b>{user_id}</b> снят со всех чатов."
        await accept_prompt(message.chat.id, text)
    else:
        text = f"❓ Модератор с ID <b>{user_id}</b> не найден."
        await accept_prompt(message.chat.id, text)
    await state.clear()


@dp.callback_query(F.data.in_({"stood", "error", "spamblock"}))
async def on_mark(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    key = (chat_id, callback.message.message_id)
    uid = token_owner.pop(key, None)
    if uid is None:
        await callback.answer("Эта отметка уже обработана или устарела.", show_alert=True)
        return

    meta = {
        "stood": ("встал", "➕"),
        "error": ("невалид", "➖"),
        "spamblock": ("спам-блок", "❗"),
    }
    label, marker_emoji = meta[callback.data]
    by_user = callback.from_user.username or callback.from_user.full_name or str(callback.from_user.id)

    # «Встал»: токен помечается валидным, а под сообщением появляются кнопки
    # «Спам-блок» и «Пометить слет» — для корректировки статуса позже.
    if callback.data == "stood":
        caption = (
            f"{marker_emoji} <b>Помечен как встал</b>\n"
            f"пользователем: @{by_user}"
        )
        html = fmt(caption)
        try:
            await callback.message.edit_caption(
                caption=html,
                parse_mode="HTML",
                reply_markup=STOOD_MARKUP,
            )
        except Exception:
            token_owner[key] = uid
            await callback.answer("Не удалось обновить сообщение. Попробуй ещё раз.", show_alert=True)
            return
        try:
            await callback.answer()
        except Exception:
            pass

        # Запоминаем статус и время отметки «Встал» для кнопки «Пометить слет».
        mark_state[key] = {"field": "stood", "ts": time.time()}

        # Автозакрытие: если за время холда никто не нажмёт кнопки корректировки,
        # убираем их и помечаем токен как вставший (редактируем то же сообщение).
        hold_min = chat_hold_minutes.get(chat_id, 0)
        if hold_min > 0:
            asyncio.get_running_loop().create_task(
                _auto_finalize_stood(chat_id, key[1], hold_min)
            )

        st = get_stats(chat_id, date.today())
        st["stood"] += 1
        user = st["users"].get(uid)
        if user is not None:
            user["stood"] += 1
        save_state()
        await log_to_admin(
            f"➖ Отметка: встал\n"
            f"Пользователь: id {uid}\nЧат: {chat_id}"
        )
        return

    # Невалид / спам-блок (первичная отметка, не «Встал»): финальная.
    caption = (
        f"{marker_emoji} <b>Помечен как {label}</b>\n"
        f"пользователем: @{by_user}"
    )
    html = fmt(caption)
    try:
        await callback.message.edit_caption(
            caption=html, parse_mode="HTML", reply_markup=None
        )
    except Exception:
        token_owner[key] = uid
        await callback.answer("Не удалось обновить сообщение. Попробуй ещё раз.", show_alert=True)
        return

    try:
        await callback.answer()
    except Exception:
        pass

    st = get_stats(chat_id, date.today())
    field = callback.data
    st[field] += 1
    user = st["users"].get(uid)
    if user is not None:
        user[field] += 1
    save_state()
    await log_to_admin(
        f"{marker_emoji} Отметка: {label}\n"
        f"Пользователь: id {uid}\nЧат: {chat_id}"
    )


async def _auto_validate(chat_id: int, message_id: int, uid: int, username: str) -> None:
    """Автопометка токена «встал», если за AUTO_VALIDATE_SEC никто не нажал кнопку.

    Если кнопка статуса была нажата (token_owner удалён) — ничего не делаем.
    Иначе: подпись меняется на «Помечен как встал пользователем: автоматически»,
    токен засчитывается в «валидные», а к сообщению крепятся кнопки корректировки
    («Спам-блок», «Пометить слет», «Посмотреть токен»).
    """
    await asyncio.sleep(AUTO_VALIDATE_SEC)
    key = (chat_id, message_id)
    # Если кнопка статуса уже нажата (кто-то подтвердил или отклонил) — не трогаем.
    if key not in token_owner:
        return
    if key in mark_state:
        return

    caption = "➕ <b>Помечен как встал</b>\nпользователем: автоматически"
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=fmt(caption),
            parse_mode="HTML",
            reply_markup=STOOD_MARKUP,
        )
    except Exception:
        return

    mark_state[key] = {"field": "stood", "ts": time.time()}
    st = get_stats(chat_id, date.today())
    st["stood"] += 1
    if st["users"].get(uid) is not None:
        st["users"][uid]["stood"] += 1
    save_state()
    await log_to_admin(
        f"➕ Автоматически помечен как встал\n"
        f"Пользователь: id {uid} (@{username})\nЧат: {chat_id}"
    )


async def _auto_finalize_stood(chat_id: int, message_id: int, hold_min: int) -> None:
    """После истечения холда убирает кнопки корректировки, если их не нажали.

    Токен остаётся «встал» в статистике; только редактируется подпись сообщения
    и снимаются кнопки «Спам-блок»/«Пометить слет». Если кнопки уже нажаты
    (mark_state удалён) — ничего не делаем.
    """
    await asyncio.sleep(hold_min * 60)
    key = (chat_id, message_id)
    # Если токен уже откорректирован (или отметка убрана) — не трогаем.
    if key not in mark_state:
        return

    by_user = "система (холд истёк)"
    html = fmt(
        f"➕ <b>Помечен как встал</b> (автоматически, холд истёк)\n"
        f"пользователем: {by_user}"
    )
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=html,
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        return
    mark_state.pop(key, None)
    token_owner.pop(key, None)


@dp.callback_query(F.data.in_({"spamblock_followup", "unhold"}))
async def on_followup(callback: types.CallbackQuery):
    """Корректировка статуса после отметки «Встал»:
    - spamblock_followup: перевести токен из «валид» в «спам-блок»;
    - unhold: «Пометить слет» — если токен отстоял меньше минимального холда,
      он снимается из «валид» и уходит в «невалид».
    """
    chat_id = callback.message.chat.id
    key = (chat_id, callback.message.message_id)
    uid = token_owner.get(key)
    if uid is None:
        await callback.answer("Эта отметка уже обработана или устарела.", show_alert=True)
        return

    entry = mark_state.get(key)
    if not entry or entry.get("field") != "stood":
        await callback.answer("Статус токена уже изменён или устарел.", show_alert=True)
        return

    by_user = callback.from_user.username or callback.from_user.full_name or str(callback.from_user.id)

    if callback.data == "spamblock_followup":
        # Из «валид» -> в «спам-блок».
        st = get_stats(chat_id, date.today())
        st["stood"] = max(0, st["stood"] - 1)
        st["spamblock"] += 1
        user = st["users"].get(uid)
        if user is not None:
            user["stood"] = max(0, user.get("stood", 0) - 1)
            user["spamblock"] += 1
        mark_state.pop(key, None)
        token_owner.pop(key, None)
        save_state()
        caption = f"❗ <b>Помечен как спам-блок</b>\nпользователем: @{by_user}"
        await _finish_followup(callback, caption)
        await log_to_admin(
            f"❗ Корректировка: встал -> спам-блок\n"
            f"Пользователь: id {uid}\nНажал: @{by_user}\nЧат: {chat_id}"
        )
        return

    # «Пометить слет».
    hold_min = chat_hold_minutes.get(chat_id, 0)
    held_for = (time.time() - entry.get("ts", 0)) / 60.0

    st = get_stats(chat_id, date.today())
    if hold_min > 0 and held_for < hold_min:
        # Токен не отстоял минимальный холд -> снимаем из «валид» в «невалид».
        st["stood"] = max(0, st["stood"] - 1)
        st["error"] += 1
        user = st["users"].get(uid)
        if user is not None:
            user["stood"] = max(0, user.get("stood", 0) - 1)
            user["error"] += 1
        caption = f"➖ <b>Слет (не отстоял холд)</b>\nпользователем: @{by_user}"
        mark_state.pop(key, None)
        token_owner.pop(key, None)
        save_state()
        await _finish_followup(callback, caption)
        await log_to_admin(
            f"➖ Слет (не отстоял холд, {held_for:.1f} мин)\n"
            f"Пользователь: id {uid}\nНажал: @{by_user}\nЧат: {chat_id}"
        )
    else:
        # Отстоял минимальный холд (или холд не задан) — только помечаем, в статистике не трогаем.
        caption = f"➕ <b>Слет зафиксирован</b>\nпользователем: @{by_user}"
        mark_state.pop(key, None)
        token_owner.pop(key, None)
        save_state()
        await _finish_followup(callback, caption)
        await log_to_admin(
            f"➕ Слет (отстоял холд)\n"
            f"Пользователь: id {uid}\nНажал: @{by_user}\nЧат: {chat_id}"
        )


async def _finish_followup(callback: types.CallbackQuery, caption: str) -> None:
    """Обновляет подпись у сообщения и подтверждает колбэк."""
    try:
        await callback.message.edit_caption(
            caption=fmt(caption), parse_mode="HTML", reply_markup=None
        )
    except Exception:
        pass
    try:
        await callback.answer()
    except Exception:
        pass


if __name__ == "__main__":
    import asyncio

    asyncio.run(dp.start_polling(bot))