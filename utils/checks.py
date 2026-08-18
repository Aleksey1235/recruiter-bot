from disnake.ext import commands
import config


def _role_ids(member) -> set[int]:
    return {role.id for role in getattr(member, "roles", [])}


def is_recruiter():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        roles = _role_ids(ctx.author)
        return bool(roles & {config.RECRUITER_ROLE_ID, config.SENIOR_ROLE_ID, config.ADMIN_ROLE_ID})
    return commands.check(predicate)


def is_senior():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        roles = _role_ids(ctx.author)
        return bool(roles & {config.SENIOR_ROLE_ID, config.ADMIN_ROLE_ID})
    return commands.check(predicate)


def is_admin():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        return config.ADMIN_ROLE_ID in _role_ids(ctx.author)
    return commands.check(predicate)


def is_recruiter_or_higher(member) -> bool:
    roles = _role_ids(member)
    return bool(roles & {config.RECRUITER_ROLE_ID, config.SENIOR_ROLE_ID, config.ADMIN_ROLE_ID})


def is_senior_or_admin(member) -> bool:
    roles = _role_ids(member)
    return bool(roles & {config.SENIOR_ROLE_ID, config.ADMIN_ROLE_ID})
