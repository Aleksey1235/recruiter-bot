import disnake

from utils.time_utils import parse_db


class EmbedGenerator:
    @staticmethod
    def create_shift_embed(shift, members=()):
        status = shift["status"]
        if status == "cancelled":
            title, color = "⚫ СМЕНА ОТМЕНЕНА", disnake.Color.dark_gray()
        elif status == "completed":
            title, color = "🔵 СМЕНА ЗАВЕРШЕНА", disnake.Color.blue()
        elif status == "missed":
            title, color = "🔴 СМЕНА ПРОПУЩЕНА", disnake.Color.red()
        elif status == "active":
            title, color = "🟢 СМЕНА ИДЁТ", disnake.Color.green()
        elif (shift["slots"] or 0) <= 0:
            title, color = "🔴 СМЕНА ЗАНЯТА", disnake.Color.red()
        elif members:
            title, color = "🟡 СМЕНА ЗАБРОНИРОВАНА", disnake.Color.yellow()
        else:
            title, color = "🟢 РАБОЧАЯ СМЕНА РЕКРУТЕРА", disnake.Color.green()

        embed = disnake.Embed(title=title, color=color)
        start = parse_db(shift["scheduled_start"])
        end = parse_db(shift["scheduled_end"])
        if start and end:
            info = (
                f"**Смена #{shift['id']}**\n"
                f"📅 {start.strftime('%d.%m.%Y')}\n"
                f"🕐 {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
            )
        else:
            info = f"**Смена #{shift['id']}**"
        embed.add_field(name="📋 Информация", value=info, inline=False)

        if shift["description"]:
            embed.add_field(name="📝 Описание", value=shift["description"], inline=False)

        live_members = [m for m in members if m["status"] in ("booked", "active")]
        if live_members:
            lines = []
            for index, member in enumerate(live_members, start=1):
                marker = "🟢" if member["status"] == "active" else "🟡"
                lines.append(f"{index}. {marker} <@{member['user_id']}> | {member['static_id'] or '—'}")
            embed.add_field(name=f"👤 Рекрутеры ({len(live_members)})", value="\n".join(lines), inline=False)

        embed.add_field(name="👥 Места", value=f"Свободно: {shift['slots']}", inline=False)
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
