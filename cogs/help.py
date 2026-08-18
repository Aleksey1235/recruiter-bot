import disnake
from disnake.ext import commands

import config


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="помощь", description="Помощь по боту")
    async def help(self, inter):
        await inter.response.defer(ephemeral=True)
        roles = {role.id for role in inter.author.roles}
        is_admin = config.ADMIN_ROLE_ID in roles
        is_senior = config.SENIOR_ROLE_ID in roles or is_admin
        role_name = "Администратор" if is_admin else "Старший состав" if is_senior else "Рекрутер"
        embed = disnake.Embed(
            title="🆘 ПОМОЩЬ — СИСТЕМА РЕКРУТИНГА",
            description="Выберите раздел. Команды ниже соответствуют текущей версии бота.",
            color=disnake.Color.blue(),
        )
        embed.add_field(name="Ваша роль", value=role_name, inline=False)
        await inter.edit_original_response(embed=embed, view=HelpMenuView(inter.author, is_admin, is_senior))


class HelpMenuView(disnake.ui.View):
    def __init__(self, user, is_admin: bool, is_senior: bool):
        super().__init__(timeout=300)
        self.user = user
        self.is_admin = is_admin
        self.is_senior = is_senior

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.user.id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True

    async def send_section(self, inter, title, description, fields):
        embed = disnake.Embed(title=title, description=description, color=disnake.Color.blue())
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="Выберите другой раздел кнопками ниже")
        await inter.response.edit_message(embed=embed, view=self)

    @disnake.ui.button(label="📋 Смены", style=disnake.ButtonStyle.primary, row=0)
    async def btn_shifts(self, button, inter):
        fields = [
            ("Как взять смену", "В канале смен нажмите **Взять смену** и введите свой статик."),
            ("/смена начать", "Начать ближайшую забронированную смену. Доступно за несколько минут до начала."),
            ("/смена завершить", "Заполнить отчёт: принято всего, на особняке, самостоятельно, комментарий."),
            ("/смена исправить", "Исправить отклонённый отчёт и отправить повторно."),
            ("/смена расписание", "Расписание на сегодня."),
        ]
        if self.is_senior:
            fields += [
                ("/смена создать", "Создать смену: дата, начало, конец, количество мест, описание."),
                ("/смена снять", "Снять рекрутера. Если у него несколько смен — укажите ID смены."),
                ("/смена отменить", "Отменить смену целиком и уведомить записанных рекрутеров."),
                ("/смена одобрить", "Резервное одобрение отчёта по ID, если сообщение с кнопками потеряно."),
                ("/смена отклонить", "Резервное отклонение отчёта по ID с причиной."),
            ]
        await self.send_section(inter, "📋 СМЕНЫ", "Управление рабочими сменами", fields)

    @disnake.ui.button(label="📊 Статистика", style=disnake.ButtonStyle.primary, row=0)
    async def btn_stats(self, button, inter):
        fields = [
            ("/статистика моя", "Личная статистика за выбранный период."),
            ("/статистика неделя", "Статистика текущей недели по дням."),
            ("/статистика топ", "Рейтинг рекрутеров."),
        ]
        if self.is_senior:
            fields.append(("/статистика рекрутера", "Статистика конкретного рекрутера."))
        await self.send_section(inter, "📊 СТАТИСТИКА", "Результаты работы", fields)

    @disnake.ui.button(label="💰 Финансы", style=disnake.ButtonStyle.primary, row=0)
    async def btn_finance(self, button, inter):
        fields = [("/финансы мои", "Начисления, выплаты и текущий остаток.")]
        if self.is_senior:
            fields += [
                ("/финансы общие", "Общие суммы по журналу финансов."),
                ("/финансы рекрутера", "Финансы конкретного рекрутера."),
            ]
        if self.is_admin:
            fields += [
                ("/финансы начислить", "Ручное начисление."),
                ("/финансы выплатить", "Отметить выплату. Больше доступного выплатить нельзя."),
                ("/финансы сверить", "Проверить журнал операций и кеш балансов."),
            ]
        await self.send_section(inter, "💰 ФИНАНСЫ", "Деньги считаются по журналу операций", fields)

    @disnake.ui.button(label="🎯 Цели", style=disnake.ButtonStyle.primary, row=0)
    async def btn_goals(self, button, inter):
        fields = [("/цель мои", "Мои активные цели."), ("/цель прогресс", "Текущий прогресс целей.")]
        if self.is_senior:
            fields += [
                ("/цель поставить", "Поставить цель по людям, сменам или часам."),
                ("/цель рекрутера", "Посмотреть цели рекрутера."),
                ("/цель удалить", "Удалить активные цели рекрутера."),
            ]
        await self.send_section(inter, "🎯 ЦЕЛИ", "Цели на день, неделю или месяц", fields)

    @disnake.ui.button(label="👤 Профиль", style=disnake.ButtonStyle.primary, row=0)
    async def btn_profile(self, button, inter):
        fields = [("/рекрутер профиль", "Профиль и агрегированная статистика рекрутера.")]
        if self.is_senior:
            fields.append(("/рекрутер заметка", "Добавить служебную заметку. Обычные рекрутеры её не видят."))
        await self.send_section(inter, "👤 ПРОФИЛЬ", "Профиль рекрутера", fields)

    @disnake.ui.button(label="📋 Инвайты", style=disnake.ButtonStyle.primary, row=1)
    async def btn_invites(self, button, inter):
        fields = [("/инвайт отчёт", "Создать отчёт о приглашённом."), ("/инвайт мои", "Мои инвайты.")]
        if self.is_senior:
            fields += [
                ("/инвайт проверить", "Список ожидающих проверки."),
                ("/инвайт база", "Только принятые инвайты."),
                ("/инвайт инфо", "Информация по статику."),
                ("/инвайт заметка", "Добавить заметку по статику."),
                ("/инвайт принять", "Резервное принятие инвайта по ID, если сообщение с кнопками потеряно."),
                ("/инвайт отклонить", "Резервное отклонение инвайта по ID."),
            ]
        await self.send_section(inter, "📋 ИНВАЙТЫ", "Учёт приглашённых", fields)

    @disnake.ui.button(label="👁️ Контроль", style=disnake.ButtonStyle.primary, row=1)
    async def btn_control(self, button, inter):
        fields = [
            ("Напоминания", "Бот напоминает о сменах, опозданиях и незавершённых отчётах."),
            ("Защита от дублей", "Уведомления и критические операции имеют защиту от повторного выполнения."),
            ("Короткие смены", "Подозрительно короткая смена отправляется в канал контроля."),
        ]
        await self.send_section(inter, "👁️ КОНТРОЛЬ", "Автоматические проверки", fields)

    @disnake.ui.button(label="ℹ️ Как работает", style=disnake.ButtonStyle.secondary, row=1)
    async def btn_how(self, button, inter):
        fields = [
            ("1️⃣ Запись", "Берёте смену кнопкой в канале."),
            ("2️⃣ Старт", "Используете `/смена начать`."),
            ("3️⃣ Работа", "Выполняете рекрутинг."),
            ("4️⃣ Отчёт", "Используете `/смена завершить`."),
            ("5️⃣ Проверка", "Старший одобряет или отклоняет отчёт."),
            ("6️⃣ Учёт", "Одобренный отчёт попадает в статистику и цели. Деньги начисляются отдельно."),
        ]
        await self.send_section(inter, "ℹ️ КАК РАБОТАЕТ", "Рабочий цикл", fields)

    @disnake.ui.button(label="🛡️ Проблемы", style=disnake.ButtonStyle.secondary, row=1)
    async def btn_issues(self, button, inter):
        fields = [
            ("Не начинается смена", "Проверьте время. Старт разрешён только около времени начала."),
            ("Нет ЛС", "Проверьте настройки приватности Discord. Ошибка сохраняется; повторный вызов уведомления выполнит ограниченное число попыток без дублей."),
            ("Несколько смен", "При снятии senior должен указать ID смены, если есть неоднозначность."),
        ]
        await self.send_section(inter, "🛡️ ПРОБЛЕМЫ", "Типовые ситуации", fields)

    @disnake.ui.button(label="⚙️ Админ", style=disnake.ButtonStyle.danger, row=1)
    async def btn_admin(self, button, inter):
        if not self.is_admin:
            return await inter.response.send_message("❌ Нет доступа.", ephemeral=True)
        fields = [
            ("/админ логи", "Последние события системы."),
            ("/админ бэкап", "Консистентный SQLite-бэкап."),
            ("/админ здоровье", "Проверка БД, каналов, ролей и фоновых задач."),
            ("/финансы сверить", "Проверка финансового журнала."),
            ("/база пользователь", "Интерактивная карточка пользователя: смены, отчёты, инвайты, финансы, логи."),
            ("/база найти", "Поиск по Discord ID, статику или имени."),
            ("/база статик / заметка", "Безопасное исправление статика и служебных заметок."),
            ("/база финоперация", "Просмотр конкретной финансовой операции по ID."),
        ]
        await self.send_section(inter, "⚙️ АДМИН", "Диагностика и обслуживание", fields)

    @disnake.ui.button(label="❌ Закрыть", style=disnake.ButtonStyle.gray, row=2)
    async def btn_close(self, button, inter):
        await inter.response.edit_message(content="✅ Меню закрыто.", embed=None, view=None)


def setup(bot):
    bot.add_cog(Help(bot))
