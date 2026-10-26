from django import template

from accounts.services import describe_user_agent

register = template.Library()


@register.filter
def browser(user_agent: str) -> str:
    """'Chrome on macOS' for a raw User-Agent string, as the Settings page shows it."""
    return describe_user_agent(user_agent or "")
