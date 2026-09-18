import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone

from .models import PasswordResetToken

User = get_user_model()
logger = logging.getLogger(__name__)

FORGOT_PASSWORD_SUCCESS_MESSAGE = (
    "If an account exists for this email, password reset instructions have been sent."
)


def create_password_reset_token(user) -> PasswordResetToken:
    PasswordResetToken.objects.filter(user=user, used_at__isnull=True).update(
        used_at=timezone.now()
    )
    expires_at = timezone.now() + settings.PASSWORD_RESET_TOKEN_LIFETIME
    return PasswordResetToken.objects.create(user=user, expires_at=expires_at)


def send_password_reset_email(user, reset_token: PasswordResetToken) -> None:
    token = str(reset_token.token)
    frontend_url = settings.PASSWORD_RESET_FRONTEND_URL.strip()
    reset_link = f"{frontend_url}?token={token}" if frontend_url else ""
    minutes = int(settings.PASSWORD_RESET_TOKEN_LIFETIME.total_seconds() // 60)

    message_lines = [
        "Hello,",
        "",
        "We received a request to reset your Aajhee password.",
        "",
        f"Your reset token: {token}",
    ]
    if reset_link:
        message_lines.extend(["", f"Or open this link: {reset_link}"])
    message_lines.extend(
        [
            "",
            f"This token expires in {minutes} minutes.",
            "",
            "If you did not request a password reset, you can ignore this email.",
            "",
            "— Aajhee",
        ]
    )

    send_mail(
        subject="Reset your Aajhee password",
        message="\n".join(message_lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def request_password_reset(email: str) -> str:
    user = User.objects.filter(email__iexact=email.strip()).first()
    if not user or not user.is_active:
        return FORGOT_PASSWORD_SUCCESS_MESSAGE

    reset_token = create_password_reset_token(user)
    try:
        send_password_reset_email(user, reset_token)
    except Exception:
        logger.exception("Failed to send password reset email to %s", user.email)
        reset_token.mark_used()
        raise

    return FORGOT_PASSWORD_SUCCESS_MESSAGE
