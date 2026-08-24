import disnake

from utils.time_utils import local_now, parse_db


class EmbedGenerator:
    @staticmethod
    def create_shift_embed(shift, members=()):
        status = shift["status"]
        start = parse_db(shift["scheduled_start"])
        end = parse_db(shift["scheduled_end"])
        now = local_now()
        has_booked = any(m["status"] == "booked" for m in members)
        if status == "cancelled":
            title, color = "⚫ СМЕНА ОТМЕНЕНА", disnake.Color.dark_gray()
        elif status == "completed":
            title, color = "🔵 СМЕНА ЗАВЕРШЕНА", disnake.Color.blue()
        elif status == "missed":
            title, color = "🔴 СМЕНА ПРОПУЩЕНА", disnake.Color.red()
        elif status == "active":
            if start and now < start and (shift["slots"] or 0) > 0:
                title = f"🟢 СМЕНА ИДЁТ • ЗАПИСЬ ДО {start.strftime('%H:%M')}"
            elif start and now < start:
                title = "🟢 СМЕНА ИДЁТ • МЕСТ НЕТ"
            else:
                title = "🟢 СМЕНА ИДЁТ"
            color = disnake.Color.green()
        elif start and end and start <= now < end:
            if has_booked:
                title = "🟠 СМЕНА НАЧАЛАСЬ • ОЖИДАЕТ СТАРТА"
            else:
                title = "🟠 СМЕНА НАЧАЛАСЬ • НЕТ УЧАСТНИКОВ"
            color = disnake.Color.orange()
        elif (shift["slots"] or 0) <= 0:
            title, color = "🔴 СМЕНА ЗАНЯТА", disnake.Color.red()
        elif has_booked:
            title, color = "🟡 СМЕНА ЗАБРОНИРОВАНА", disnake.Color.yellow()
        else:
            title, color = "🟢 РАБОЧАЯ СМЕНА РЕКРУТЕРА", disnake.Color.green()

        embed = disnake.Embed(title=title, color=color)
        if start and end:
            if start.date() == end.date():
                time_line = f"🕐 {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
                date_line = f"📅 {start.strftime('%d.%m.%Y')}"
            else:
                time_line = f"🕐 {start.strftime('%d.%m %H:%M')} → {end.strftime('%d.%m %H:%M')}"
                date_line = "📅 Смена переходит через полночь"
            info = f"**Смена #{shift['id']}**\n{date_line}\n{time_line}"
        else:
            info = f"**Смена #{shift['id']}**"
        embed.add_field(name="📋 Информация", value=info, inline=False)

        if shift["description"]:
            embed.add_field(name="📝 Описание", value=shift["description"], inline=False)

        # Детальная карточка не должна упираться в лимит Discord 1024 символа на поле.
        # Ушедшие/снятые участники могут накапливаться сверх исходной вместимости,
        # поэтому их выводим отдельно и компактно.
        current_members = [
            m for m in members
            if m["status"] in ("booked", "active", "completed", "missed", "cancelled")
        ]
        removed_members = [m for m in members if m["status"] == "removed"]
        if current_members:
            marker_by_status = {
                "booked": "🟡",
                "active": "🟢",
                "completed": "✅",
                "missed": "❌",
                "cancelled": "⚫",
            }
            label_by_status = {
                "booked": "записан",
                "active": "на смене",
                "completed": "завершил",
                "missed": "пропустил",
                "cancelled": "отменено",
            }
            lines = []
            for index, member in enumerate(current_members, start=1):
                status_name = member["status"]
                marker = marker_by_status.get(status_name, "•")
                label = label_by_status.get(status_name, status_name)
                lines.append(f"{index}. {marker} <@{member['user_id']}> | {member['static_id'] or '—'} — {label}")

            chunks, chunk = [], []
            size = 0
            for line in lines:
                extra = len(line) + (1 if chunk else 0)
                if chunk and size + extra > 950:
                    chunks.append(chunk)
                    chunk, size = [], 0
                chunk.append(line)
                size += len(line) + (1 if len(chunk) > 1 else 0)
            if chunk:
                chunks.append(chunk)
            for index, lines_chunk in enumerate(chunks[:4]):
                name = f"👤 Участники ({len(current_members)})" if index == 0 else "👤 Участники • продолжение"
                embed.add_field(name=name, value="\n".join(lines_chunk), inline=False)

        if removed_members:
            # Показываем недавнюю историю компактно; полная история остаётся в БД/логах.
            recent = removed_members[-10:]
            removed_lines = []
            for member in recent:
                reason = (member["cancel_reason"] or "").strip()
                action = "вышел" if reason.startswith("Самостоятельный выход:") else "снят"
                removed_lines.append(f"🚪 <@{member['user_id']}> | {member['static_id'] or '—'} — {action}")
            hidden = len(removed_members) - len(recent)
            if hidden > 0:
                removed_lines.append(f"… и ещё {hidden} в истории")
            embed.add_field(
                name=f"🚪 Вышли / сняты ({len(removed_members)})",
                value="\n".join(removed_lines)[:1024],
                inline=False,
            )

        terminal = status in ("completed", "cancelled", "missed")
        booking_closed = terminal or (start is not None and now >= start)
        places_value = "Запись закрыта" if booking_closed else f"Свободно: {shift['slots']}"
        embed.add_field(name="👥 Места", value=places_value, inline=False)
        embed.set_footer(text=f"Смена #{shift['id']}")
        return embed

    @staticmethod
    def create_report_embed(report, member, user_mention: str):
        embed = disnake.Embed(title="📋 НОВЫЙ ОТЧЁТ ПО СМЕНЕ", color=disnake.Color.orange())
        embed.add_field(name="📋 Смена", value=f"#{report['shift_id']}", inline=True)
        embed.add_field(name="👤 Рекрутер", value=user_mention, inline=True)
        embed.add_field(name="🆔 Статик", value=member["static_id"] or "—", inline=True)
        embed.add_field(name="👥 Принято всего", value=str(report["total_accepted"]), inline=True)
        embed.add_field(name="🏠 На особняке", value=str(report["came_to_base"]), inline=True)
        embed.add_field(name="👤 Самостоятельно", value=str(report["found_by_recruiter"]), inline=True)
        if report["comment"]:
            embed.add_field(name="📝 Комментарий", value=report["comment"], inline=False)

        start = parse_db(member["actual_start"])
        end = parse_db(member["actual_end"])
        if start and end:
            minutes = max(0, int((end - start).total_seconds() // 60))
            embed.add_field(
                name="⏱️ Время работы",
                value=f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} ({minutes // 60}ч {minutes % 60}м)",
                inline=False,
            )
        embed.set_footer(text=f"Отчёт #{report['id']}")
        return embed
