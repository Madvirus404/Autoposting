import asyncio
import logging
import uuid
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton, 
    ChatMemberUpdated
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Токен вашего бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# Хранилища в памяти
KNOWN_CHATS = {}            # {chat_id: {"title": "Название", "type": "тип"}}
SCHEDULED_POSTS_INFO = {}   # {job_id: {"chat_id": ..., "text": ..., "time_str": ..., "mode": ..., "hour": ..., "minute": ..., "target_date": ..., "action": ...}}

class PostState(StatesGroup):
    waiting_for_chat = State()
    waiting_for_text = State()
    waiting_for_action = State()          # Выбор: Сейчас / Один раз / Ежедневно
    waiting_for_date_choice = State()     # Выбор даты: Сегодня / Завтра / Своя дата
    waiting_for_custom_date = State()     # Ручной ввод даты ГГГГ-ММ-ДД
    waiting_for_hour = State()            # Выбор часа кнопками
    waiting_for_minute = State()          # Выбор минут кнопками
    editing_text = State()                # Редактирование текста существующего поста

# Клавиатура главного меню
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Создать новый пост", callback_data="menu_new_post")],
            [InlineKeyboardButton(text="📋 Запланированные посты", callback_data="menu_list_posts")]
        ]
    )

# Кнопка возврата в меню/отмены
def get_cancel_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить и в главное меню", callback_data="menu_main")]
        ]
    )

# Клавиатура выбора часов (00 - 23)
def get_hours_keyboard(is_edit=False):
    buttons = []
    row = []
    prefix = "edithour_" if is_edit else "hour_"
    for h in range(24):
        row.append(InlineKeyboardButton(text=f"{h:02d}:00", callback_data=f"{prefix}{h}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Клавиатура выбора минут (округлено до десятков)
def get_minutes_keyboard(is_edit=False):
    minutes = [0, 10, 20, 30, 40, 50]
    buttons = []
    row = []
    prefix = "editmin_" if is_edit else "min_"
    for m in minutes:
        row.append(InlineKeyboardButton(text=f":{m:02d}", callback_data=f"{prefix}{m}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Отслеживание добавления бота в чаты/каналы
@dp.my_chat_member()
async def on_bot_chat_member(event: ChatMemberUpdated):
    chat = event.chat
    if event.new_chat_member.status in ["member", "administrator"]:
        KNOWN_CHATS[chat.id] = {
            "title": chat.title,
            "type": chat.type
        }
        logging.info(f"Бот добавлен в чат: {chat.title} (ID: {chat.id})")
    elif event.new_chat_member.status in ["left", "kicked"]:
        if chat.id in KNOWN_CHATS:
            del KNOWN_CHATS[chat.id]
            logging.info(f"Бот удален из чата: {chat.title} (ID: {chat.id})")

# Функция отправки сообщения
async def send_scheduled_post(chat_id: str | int, text: str):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения в {chat_id}: {e}")

# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот для автоматического и регулярного постинга.\n\n"
        "Выберите нужное действие в меню:",
        reply_markup=get_main_menu_keyboard()
    )

# Возврат в главное меню через callback
@dp.callback_query(F.data == "menu_main")
async def cb_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        "🏠 Главное меню:\n\nВыберите нужный раздел:",
        reply_markup=get_main_menu_keyboard()
    )

# --- РАЗДЕЛ: Создание поста ---

@dp.callback_query(F.data == "menu_new_post")
async def cb_new_post(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    admin_chats = {}

    for chat_id, chat_info in KNOWN_CHATS.items():
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ["creator", "administrator"]:
                admin_chats[chat_id] = chat_info["title"]
        except Exception as e:
            logging.warning(f"Не удалось проверить статус в чате {chat_id}: {e}")

    buttons = []
    for chat_id, title in admin_chats.items():
        buttons.append([InlineKeyboardButton(text=f"📢 {title}", callback_data=f"select_chat_{chat_id}")])
    
    buttons.append([InlineKeyboardButton(text="✍️ Ввести ID / @username вручную", callback_data="select_chat_manual")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu_main")])
    
    await callback.message.edit_text(
        "📢 Выберите канал или группу для публикации\n*(доступны только те, где вы администратор)*:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await state.set_state(PostState.waiting_for_chat)

@dp.callback_query(F.data.startswith("select_chat_"), PostState.waiting_for_chat)
async def cb_select_chat(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = callback.data.replace("select_chat_", "")
    
    if data == "manual":
        await callback.message.edit_text(
            "✍️ Введите `@username` канала или его числовой `ID` (например, `-100123456789`):",
            reply_markup=get_cancel_keyboard(),
            parse_mode="Markdown"
        )
        await state.update_data(chat_id=None)
        await state.set_state(PostState.waiting_for_text)
        return

    chat_id = int(data)
    await state.update_data(chat_id=chat_id)
    chat_title = KNOWN_CHATS.get(chat_id, {}).get("title", chat_id)
    
    await callback.message.edit_text(
        f"✅ Выбран чат: **{chat_title}**\n\n"
        "📝 Теперь отправьте текст сообщения, которое нужно опубликовать:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(PostState.waiting_for_text)

@dp.message(PostState.waiting_for_text)
async def process_text_or_chat(message: Message, state: FSMContext):
    data = await state.get_data()
    
    if data.get("chat_id") is None:
        chat_id_input = message.text.strip()
        try:
            if chat_id_input.startswith("-") or chat_id_input.isdigit():
                chat_id_input = int(chat_id_input)
        except ValueError:
            pass
            
        await state.update_data(chat_id=chat_id_input)
        await message.answer(
            "✅ Чат принят!\n\n📝 Теперь отправьте текст сообщения для публикации:",
            reply_markup=get_cancel_keyboard()
        )
        return

    await state.update_data(text=message.text)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡️ Опубликовать сейчас", callback_data="act_now")],
            [InlineKeyboardButton(text="1️⃣ Один раз (выбрать время)", callback_data="act_once")],
            [InlineKeyboardButton(text="🔄 Ежедневно", callback_data="act_daily")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")]
        ]
    )
    await message.answer(
        "⚙️ Когда вы хотите отправить этот пост?",
        reply_markup=keyboard
    )
    await state.set_state(PostState.waiting_for_action)

@dp.callback_query(F.data.startswith("act_"), PostState.waiting_for_action)
async def cb_action_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    action = callback.data.replace("act_", "")
    await state.update_data(action=action)
    
    if action == "now":
        data = await state.get_data()
        chat_id = data["chat_id"]
        text = data["text"]
        
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            await callback.message.edit_text(
                "✅ Пост успешно опубликован прямо сейчас!",
                reply_markup=get_main_menu_keyboard()
            )
        except Exception as e:
            await callback.message.edit_text(
                f"❌ Ошибка при отправке: {e}",
                reply_markup=get_main_menu_keyboard()
            )
        await state.clear()
        
    elif action == "daily":
        await state.update_data(target_date=None)
        await callback.message.edit_text(
            "⏰ Выберите **час** ежедневной отправки:",
            reply_markup=get_hours_keyboard(is_edit=False),
            parse_mode="Markdown"
        )
        await state.set_state(PostState.waiting_for_hour)
        
    elif action == "once":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📅 Сегодня", callback_data="date_today")],
                [InlineKeyboardButton(text="📅 Завтра", callback_data="date_tomorrow")],
                [InlineKeyboardButton(text="✍️ Ввести дату вручную", callback_data="date_custom")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")]
            ]
        )
        await callback.message.edit_text(
            "📅 Выберите дату публикации:",
            reply_markup=keyboard
        )
        await state.set_state(PostState.waiting_for_date_choice)

@dp.callback_query(F.data.startswith("date_"), PostState.waiting_for_date_choice)
async def cb_date_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    choice = callback.data.replace("date_", "")
    today = date.today()
    
    if choice == "today":
        await state.update_data(target_date=today)
        await callback.message.edit_text(
            "⏰ Выберите **час** публикации:",
            reply_markup=get_hours_keyboard(is_edit=False),
            parse_mode="Markdown"
        )
        await state.set_state(PostState.waiting_for_hour)
        
    elif choice == "tomorrow":
        tomorrow = today + timedelta(days=1)
        await state.update_data(target_date=tomorrow)
        await callback.message.edit_text(
            "⏰ Выберите **час** публикации:",
            reply_markup=get_hours_keyboard(is_edit=False),
            parse_mode="Markdown"
        )
        await state.set_state(PostState.waiting_for_hour)
        
    elif choice == "custom":
        await callback.message.edit_text(
            "✍️ Введите дату в формате `ГГГГ-ММ-ДД` (например, `2026-12-31`):",
            reply_markup=get_cancel_keyboard(),
            parse_mode="Markdown"
        )
        await state.set_state(PostState.waiting_for_custom_date)

@dp.message(PostState.waiting_for_custom_date)
async def process_custom_date(message: Message, state: FSMContext):
    try:
        parsed_date = datetime.strptime(message.text.strip(), "%Y-%m-%d").date()
        if parsed_date < date.today():
            await message.answer("⚠️ Указанная дата уже в прошлом. Введите актуальную дату в формате `ГГГГ-ММ-ДД`:")
            return
            
        await state.update_data(target_date=parsed_date)
        await message.answer(
            "⏰ Выберите **час** публикации:",
            reply_markup=get_hours_keyboard(is_edit=False),
            parse_mode="Markdown"
        )
        await state.set_state(PostState.waiting_for_hour)
    except ValueError:
        await message.answer("❌ Неверный формат даты. Используйте формат `ГГГГ-ММ-ДД` (например, `2026-06-01`):")

@dp.callback_query(F.data.startswith("hour_"), PostState.waiting_for_hour)
async def cb_select_hour(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    hour = int(callback.data.replace("hour_", ""))
    await state.update_data(selected_hour=hour)
    
    await callback.message.edit_text(
        f"⏰ Выбран час: `{hour:02d}:00`\n\nТеперь выберите **минуты** (округлено до десятков):",
        reply_markup=get_minutes_keyboard(is_edit=False),
        parse_mode="Markdown"
    )
    await state.set_state(PostState.waiting_for_minute)

@dp.callback_query(F.data.startswith("min_"), PostState.waiting_for_minute)
async def cb_select_minute(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    minute = int(callback.data.replace("min_", ""))
    data = await state.get_data()
    
    chat_id = data["chat_id"]
    text = data["text"]
    action = data["action"]
    hour = data["selected_hour"]
    target_date = data.get("target_date")
    
    job_id = str(uuid.uuid4())

    try:
        if action == "daily":
            scheduler.add_job(
                send_scheduled_post,
                trigger="cron",
                hour=hour,
                minute=minute,
                id=job_id,
                args=[chat_id, text]
            )
            time_display = f"Каждый день в {hour:02d}:{minute:02d}"
            mode_display = "Ежедневно"
            
        else:
            run_date = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
            if run_date <= datetime.now():
                await callback.message.edit_text(
                    "⚠️ Указанное время уже прошло в текущем дне! Пожалуйста, начните заново.",
                    reply_markup=get_main_menu_keyboard()
                )
                await state.clear()
                return

            scheduler.add_job(
                send_scheduled_post,
                trigger="date",
                run_date=run_date,
                id=job_id,
                args=[chat_id, text]
            )
            time_display = run_date.strftime('%d.%m.%Y %H:%M')
            mode_display = "Один раз"

        # Сохраняем расширенные данные для редактирования
        SCHEDULED_POSTS_INFO[job_id] = {
            "chat_id": chat_id,
            "text": text,
            "time_str": time_display,
            "mode": mode_display,
            "action": action,
            "hour": hour,
            "minute": minute,
            "target_date": target_date
        }

        await callback.message.edit_text(
            f"✅ Пост успешно запланирован!\n\n"
            f"📌 **Куда:** {chat_id}\n"
            f"🔁 **Режим:** {mode_display}\n"
            f"⏰ **Время:** {time_display}",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="Markdown"
        )
        await state.clear()

    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка при создании задачи: {e}",
            reply_markup=get_main_menu_keyboard()
        )
        await state.clear()

# --- РАЗДЕЛ: Список запланированных постов и Меню управления отдельным постом ---

@dp.callback_query(F.data == "menu_list_posts")
async def cb_list_posts(callback: CallbackQuery):
    await callback.answer()
    
    active_jobs = scheduler.get_jobs()
    job_ids = [job.id for job in active_jobs]
    
    for j_id in list(SCHEDULED_POSTS_INFO.keys()):
        if j_id not in job_ids:
            del SCHEDULED_POSTS_INFO[j_id]

    if not SCHEDULED_POSTS_INFO:
        await callback.message.edit_text(
            "📭 У вас нет активных запланированных постов.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu_main")]]
            )
        )
        return

    buttons = []
    for j_id, info in SCHEDULED_POSTS_INFO.items():
        btn_text = f"[{info['mode']}] {info['time_str']} ({info['chat_id']})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"view_job_{j_id}")])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu_main")])
    
    await callback.message.edit_text(
        "📋 **Список запланированных постов:**\nНажмите на пост, чтобы управлять им:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )

# Открытие конкретного поста
@dp.callback_query(F.data.startswith("view_job_"))
async def cb_view_job(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    job_id = callback.data.replace("view_job_", "")
    
    if job_id not in SCHEDULED_POSTS_INFO:
        await callback.message.edit_text(
            "❌ Этот пост не найден или уже был отправлен/удален.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📋 К списку постов", callback_data="menu_list_posts")]]
            )
        )
        return

    info = SCHEDULED_POSTS_INFO[job_id]
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"edit_text_{job_id}")],
            [InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"edit_time_{job_id}")],
            [InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"del_job_{job_id}")],
            [InlineKeyboardButton(text="📋 К списку постов", callback_data="menu_list_posts")]
        ]
    )
    
    await callback.message.edit_text(
        f"📄 **Управление постом:**\n\n"
        f"📌 **Чат:** `{info['chat_id']}`\n"
        f"🔁 **Режим:** {info['mode']}\n"
        f"⏰ **Время:** {info['time_str']}\n\n"
        f"💬 **Текст:**\n{info['text']}",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# --- РЕДАКТИРОВАНИЕ ТЕКСТА ПОСТА ---

@dp.callback_query(F.data.startswith("edit_text_"))
async def cb_edit_text_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    job_id = callback.data.replace("edit_text_", "")
    
    if job_id not in SCHEDULED_POSTS_INFO:
        await callback.message.edit_text("❌ Пост не найден.", reply_markup=get_main_menu_keyboard())
        return

    await state.update_data(editing_job_id=job_id)
    await callback.message.edit_text(
        "✍️ Отправьте новый текст для этого поста:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_job_{job_id}")]]
        )
    )
    await state.set_state(PostState.editing_text)

@dp.message(PostState.editing_text)
async def process_new_text(message: Message, state: FSMContext):
    data = await state.get_data()
    job_id = data.get("editing_job_id")
    new_text = message.text.strip()
    
    if job_id in SCHEDULED_POSTS_INFO:
        info = SCHEDULED_POSTS_INFO[job_id]
        info["text"] = new_text
        
        # Пересоздаем задачу в планировщике с новым текстом
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
            
        if info["mode"] == "Ежедневно":
            scheduler.add_job(
                send_scheduled_post,
                trigger="cron",
                hour=info["hour"],
                minute=info["minute"],
                id=job_id,
                args=[info["chat_id"], new_text]
            )
        else:
            run_date = datetime.combine(info["target_date"], datetime.min.time()).replace(hour=info["hour"], minute=info["minute"])
            scheduler.add_job(
                send_scheduled_post,
                trigger="date",
                run_date=run_date,
                id=job_id,
                args=[info["chat_id"], new_text]
            )

        await state.clear()
        
        # Показываем обновленное меню поста
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"edit_text_{job_id}")],
                [InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"edit_time_{job_id}")],
                [InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"del_job_{job_id}")],
                [InlineKeyboardButton(text="📋 К списку постов", callback_data="menu_list_posts")]
            ]
        )
        await message.answer(
            f"✅ Текст поста успешно обновлен!\n\n"
            f"📄 **Управление постом:**\n\n"
            f"📌 **Чат:** `{info['chat_id']}`\n"
            f"🔁 **Режим:** {info['mode']}\n"
            f"⏰ **Время:** {info['time_str']}\n\n"
            f"💬 **Текст:**\n{info['text']}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        await state.clear()
        await message.answer("❌ Ошибка: пост не найден.", reply_markup=get_main_menu_keyboard())

# --- ИЗМЕНЕНИЕ ВРЕМЕНИ ПОСТА ---

@dp.callback_query(F.data.startswith("edit_time_"))
async def cb_edit_time_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    job_id = callback.data.replace("edit_time_", "")
    
    if job_id not in SCHEDULED_POSTS_INFO:
        await callback.message.edit_text("❌ Пост не найден.", reply_markup=get_main_menu_keyboard())
        return

    await state.update_data(editing_job_id=job_id)
    
    # Спрашиваем новый режим для изменения времени
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Один раз (выбрать дату/время)", callback_data=f"edittype_once_{job_id}")],
            [InlineKeyboardButton(text="🔄 Ежедневно", callback_data=f"edittype_daily_{job_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_job_{job_id}")]
        ]
    )
    await callback.message.edit_text(
        "⚙️ Выберите новый режим отправки для изменения времени:",
        reply_markup=keyboard
    )

@dp.callback_query(F.data.startswith("edittype_"))
async def cb_edit_type_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    action = parts[1] # once или daily
    job_id = parts[2]
    
    await state.update_data(action=action, editing_job_id=job_id)
    
    if action == "daily":
        await state.update_data(target_date=None)
        await callback.message.edit_text(
            "⏰ Выберите новый **час** ежедневной отправки:",
            reply_markup=get_hours_keyboard(is_edit=True),
            parse_mode="Markdown"
        )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📅 Сегодня", callback_data=f"editdate_today_{job_id}")],
                [InlineKeyboardButton(text="📅 Завтра", callback_data=f"editdate_tomorrow_{job_id}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_job_{job_id}")]
            ]
        )
        await callback.message.edit_text(
            "📅 Выберите новую дату публикации:",
            reply_markup=keyboard
        )

@dp.callback_query(F.data.startswith("editdate_"))
async def cb_edit_date_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    choice = parts[1] # today или tomorrow
    job_id = parts[2]
    today = date.today()
    
    if choice == "today":
        target_date = today
    else:
        target_date = today + timedelta(days=1)
        
    await state.update_data(target_date=target_date)
    await callback.message.edit_text(
        "⏰ Выберите новый **час** публикации:",
        reply_markup=get_hours_keyboard(is_edit=True),
        parse_mode="Markdown"
    )

# Выбор часа при редактировании
@dp.callback_query(F.data.startswith("edithour_"))
async def cb_edit_select_hour(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    hour = int(callback.data.replace("edithour_", ""))
    await state.update_data(selected_hour=hour)
    
    await callback.message.edit_text(
        f"⏰ Выбран час: `{hour:02d}:00`\n\nТеперь выберите новые **минуты**:",
        reply_markup=get_minutes_keyboard(is_edit=True),
        parse_mode="Markdown"
    )

# Выбор минут и сохранение измененного времени
@dp.callback_query(F.data.startswith("editmin_"))
async def cb_edit_select_minute(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    minute = int(callback.data.replace("editmin_", ""))
    data = await state.get_data()
    
    job_id = data.get("editing_job_id")
    action = data.get("action")
    hour = data.get("selected_hour")
    target_date = data.get("target_date")
    
    if job_id not in SCHEDULED_POSTS_INFO:
        await callback.message.edit_text("❌ Ошибка: пост не найден.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    info = SCHEDULED_POSTS_INFO[job_id]
    chat_id = info["chat_id"]
    text = info["text"]

    try:
        # Удаляем старую задачу из шедулера
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

        if action == "daily":
            scheduler.add_job(
                send_scheduled_post,
                trigger="cron",
                hour=hour,
                minute=minute,
                id=job_id,
                args=[chat_id, text]
            )
            time_display = f"Каждый день в {hour:02d}:{minute:02d}"
            mode_display = "Ежедневно"
            target_date = None
        else:
            run_date = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
            if run_date <= datetime.now():
                await callback.message.edit_text(
                    "⚠️ Указанное время уже прошло! Время не было изменено.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к посту", callback_data=f"view_job_{job_id}")]]
                    )
                )
                await state.clear()
                return

            scheduler.add_job(
                send_scheduled_post,
                trigger="date",
                run_date=run_date,
                id=job_id,
                args=[chat_id, text]
            )
            time_display = run_date.strftime('%d.%m.%Y %H:%M')
            mode_display = "Один раз"

        # Обновляем информацию в словаре
        info["time_str"] = time_display
        info["mode"] = mode_display
        info["action"] = action
        info["hour"] = hour
        info["minute"] = minute
        info["target_date"] = target_date

        await state.clear()
        
        # Перенаправляем в обновленное меню просмотра поста
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"edit_text_{job_id}")],
                [InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"edit_time_{job_id}")],
                [InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"del_job_{job_id}")],
                [InlineKeyboardButton(text="📋 К списку постов", callback_data="menu_list_posts")]
            ]
        )
        
        await callback.message.edit_text(
            f"✅ Время успешно обновлено!\n\n"
            f"📄 **Управление постом:**\n\n"
            f"📌 **Чат:** `{info['chat_id']}`\n"
            f"🔁 **Режим:** {info['mode']}\n"
            f"⏰ **Время:** {info['time_str']}\n\n"
            f"💬 **Текст:**\n{info['text']}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка при изменении времени: {e}",
            reply_markup=get_main_menu_keyboard()
        )
        await state.clear()

# --- УДАЛЕНИЕ ПОСТА ---

@dp.callback_query(F.data.startswith("del_job_"))
async def cb_delete_job(callback: CallbackQuery):
    await callback.answer()
    job_id = callback.data.replace("del_job_", "")
    
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    
    if job_id in SCHEDULED_POSTS_INFO:
        del SCHEDULED_POSTS_INFO[job_id]
        
    # Перенаправляем обратно к списку постов
    await cb_list_posts(callback)

async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())