"""Diagnose / send a test FCM push for a user or raw FCM token.

Usage (production Render shell or local with prod DB):

  python manage.py diagnose_push --email user@example.com
  python manage.py diagnose_push --user-id 12 --send-test

Send directly to a device FCM token (no DB row required — useful for iOS
state testing before backend registration works):

  python manage.py diagnose_push --token '<fcm_token>' --send-test
  python manage.py diagnose_push --token '<fcm_token>' --send-test --state foreground
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from discounts.fcm import send_fcm_to_tokens
from discounts.firebase_app import get_firebase_app
from discounts.models import DeviceToken, Notification, UserPreferences

User = get_user_model()

# Payloads for verifying OS banners + tap navigation on iOS.
_STATE_PAYLOADS = {
    "foreground": {
        "title": "Foreground test",
        "body": "App is open — banner should appear over the UI.",
        "data": {"type": "test_push", "route": "/notifications"},
    },
    "background": {
        "title": "Background test",
        "body": "App is backgrounded — system banner expected.",
        "data": {"type": "test_push", "route": "/notifications"},
    },
    "terminated": {
        "title": "Terminated test",
        "body": "App was killed — system banner expected; tap to cold-start.",
        "data": {"type": "test_push", "route": "/notifications"},
    },
    "order": {
        "title": "Test order update",
        "body": "Your order is being prepared",
        "data": {
            "type": "order_status_changed",
            "order_public_id": "00000000-0000-4000-8000-000000000001",
        },
    },
}


class Command(BaseCommand):
    help = "Show push registration state and optionally send a test FCM."

    def add_arguments(self, parser):
        parser.add_argument("--email", type=str, default="")
        parser.add_argument("--phone", type=str, default="")
        parser.add_argument("--user-id", type=int, default=0)
        parser.add_argument(
            "--token",
            type=str,
            default="",
            help="Raw FCM device token. Skips user/DB lookup when set.",
        )
        parser.add_argument(
            "--send-test",
            action="store_true",
            help="Send a test FCM to the resolved token(s).",
        )
        parser.add_argument(
            "--state",
            type=str,
            default="foreground",
            choices=sorted(_STATE_PAYLOADS.keys()),
            help="Which test payload to send (default: foreground).",
        )

    def handle(self, *args, **options):
        app = get_firebase_app()
        self.stdout.write(f"Firebase Admin configured: {app is not None}")

        raw_token = (options["token"] or "").strip()
        if raw_token:
            tokens = [raw_token]
            self.stdout.write(f"Using raw FCM token …{raw_token[-12:]}")
        else:
            user = self._resolve_user(options)
            prefs = UserPreferences.objects.filter(user=user).first()
            devices = list(DeviceToken.objects.filter(user=user))
            recent = list(
                Notification.objects.filter(user=user).order_by("-created_at")[:5]
            )

            self.stdout.write(
                f"user_id={user.id} email={user.email!r} "
                f"phone={getattr(user, 'phone', None)!r}"
            )
            self.stdout.write(
                f"notifications_enabled: "
                f"{True if prefs is None else prefs.notifications_enabled}"
            )
            self.stdout.write(
                f"marketing_notifications_enabled: "
                f"{True if prefs is None else prefs.marketing_notifications_enabled}"
            )
            self.stdout.write(f"device_tokens: {len(devices)}")
            for device in devices:
                self.stdout.write(
                    f"  - {device.platform} …{device.token[-12:]} "
                    f"(updated={device.updated_at if hasattr(device, 'updated_at') else 'n/a'})"
                )

            self.stdout.write(f"recent_inbox_notifications: {len(recent)}")
            for item in recent:
                self.stdout.write(
                    f"  - #{item.id} {item.type} {item.title!r} at {item.created_at}"
                )
            tokens = [d.token for d in devices]

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
                "No device token. Pass --token '<fcm_token>', or open the consumer "
                "app, log in, allow notifications, and confirm logs show "
                "'Device token registered for push'."
            )

        payload = _STATE_PAYLOADS[options["state"]]
        sent = send_fcm_to_tokens(
            tokens=tokens,
            title=payload["title"],
            body=payload["body"],
            data=payload["data"],
        )
        if sent < 1:
            raise CommandError(
                f"FCM send failed for all tokens (state={options['state']}). "
                "Check APNs key in Firebase and that the token matches this "
                "Firebase project / aps-environment."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Test FCM delivered to {sent}/{len(tokens)} device(s) "
                f"(state={options['state']}). Check the device."
            )
        )

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
        raise CommandError("Provide --user-id, --email, --phone, or --token")
