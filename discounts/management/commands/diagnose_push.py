"""Diagnose / send a test FCM push for a user.

Usage (production Render shell or local with prod DB):

  python manage.py diagnose_push --email user@example.com
  python manage.py diagnose_push --user-id 12 --send-test
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from discounts.fcm import send_fcm_to_tokens
from discounts.firebase_app import get_firebase_app
from discounts.models import DeviceToken, Notification, UserPreferences

User = get_user_model()


class Command(BaseCommand):
    help = "Show push registration state and optionally send a test FCM."

    def add_arguments(self, parser):
        parser.add_argument("--email", type=str, default="")
        parser.add_argument("--phone", type=str, default="")
        parser.add_argument("--user-id", type=int, default=0)
        parser.add_argument(
            "--send-test",
            action="store_true",
            help="Send a test FCM to all of the user's registered device tokens.",
        )

    def handle(self, *args, **options):
        user = self._resolve_user(options)
        prefs = UserPreferences.objects.filter(user=user).first()
        tokens = list(DeviceToken.objects.filter(user=user))
        recent = list(
            Notification.objects.filter(user=user).order_by("-created_at")[:5]
        )

        app = get_firebase_app()
        self.stdout.write(f"user_id={user.id} email={user.email!r} phone={getattr(user, 'phone', None)!r}")
        self.stdout.write(
            f"Firebase Admin configured: {app is not None}"
        )
        self.stdout.write(
            f"notifications_enabled: "
            f"{True if prefs is None else prefs.notifications_enabled}"
        )
        self.stdout.write(f"device_tokens: {len(tokens)}")
        for device in tokens:
            self.stdout.write(
                f"  - {device.platform} …{device.token[-12:]} "
                f"(updated={device.updated_at if hasattr(device, 'updated_at') else 'n/a'})"
            )

        self.stdout.write(f"recent_inbox_notifications: {len(recent)}")
        for item in recent:
            self.stdout.write(
                f"  - #{item.id} {item.type} {item.title!r} at {item.created_at}"
            )

        if not options["send_test"]:
            self.stdout.write(
                self.style.NOTICE("Pass --send-test to deliver a test push now.")
            )
            return

        if app is None:
            raise CommandError(
                "Firebase Admin is not configured. Set FIREBASE_CREDENTIALS_JSON "
                "or FIREBASE_CREDENTIALS_PATH."
            )
        if not tokens:
            raise CommandError(
                "No DeviceToken rows for this user. Open the consumer app, log in, "
                "allow notifications, and confirm logs show "
                "'Device token registered for push'."
            )

        send_fcm_to_tokens(
            tokens=[d.token for d in tokens],
            title="Aajhee test push",
            body="If you see this banner, FCM + APNs are working.",
            data={"type": "test_push"},
        )
        self.stdout.write(self.style.SUCCESS("Test FCM send attempted. Check the device."))

    def _resolve_user(self, options):
        if options["user_id"]:
            try:
                return User.objects.get(pk=options["user_id"])
            except User.DoesNotExist as exc:
                raise CommandError(f"No user with id={options['user_id']}") from exc
        email = (options["email"] or "").strip()
        phone = (options["phone"] or "").strip()
        if email:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                return user
            raise CommandError(f"No user with email={email!r}")
        if phone:
            user = User.objects.filter(phone=phone).first()
            if user:
                return user
            raise CommandError(f"No user with phone={phone!r}")
        raise CommandError("Provide --user-id, --email, or --phone")
