"""Server-owned identities for the local role-switching demonstration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    label: str


USERS = {
    role: Actor(role, role, label)
    for role, label in (
        ("staff", "영업지원 담당자"), ("lead", "영업지원 전결권자"),
        ("executive", "영업임원"), ("treasury", "회계·자금"),
    )
}


def require_role(actor, *allowed):
    if actor.role not in allowed:
        raise PermissionError("현재 사용자에게 이 작업을 수행할 권한이 없습니다.")
