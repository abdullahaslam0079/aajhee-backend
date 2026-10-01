"""Seed realistic Lahore demo catalog for commerce testing.

Creates admin, merchants, consumers, products (with real photos), carts,
orders, reviews, and likes.

On Render, only runs when SEED_TEST_DATA=true (or --force).
Use --refresh to replace placeholder images / old product names.
"""

from __future__ import annotations

import os
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.text import slugify

from discounts.geo_utils import get_or_create_city, get_or_create_default_country
from discounts.models import (
    Address,
    Branch,
    BranchContact,
    BranchFulfillmentSettings,
    Business,
    BusinessCategory,
    BusinessEngagementStats,
    BusinessLike,
    Cart,
    CartItem,
    Category,
    Order,
    OrderDeliverySnapshot,
    OrderItem,
    OrderStatusHistory,
    Product,
    ProductEngagementStats,
    ProductLike,
    ProductReview,
    UserPreferences,
)
from discounts.review_service import recompute_business_rating, recompute_product_rating
from discounts.seed_utils import (
    ADMIN_EMAIL,
    CONSUMERS,
    DEFAULT_BUSINESS_HOURS,
    DEMO_PASSWORD,
    LIVE_BUSINESSES,
    ROOT_CATEGORIES,
    download_image_or_none,
    ensure_l2_categories,
    resolve_product_l2_slug,
)


class Command(BaseCommand):
    help = (
        "Seed realistic demo admin, merchants, products (real photos), orders "
        f"(password for all demo accounts: {DEMO_PASSWORD})."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even on Render without SEED_TEST_DATA.",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help=(
                "Replace demo products/images and rebuild demo orders "
                "(use after upgrading from placeholder graphics)."
            ),
        )

    def handle(self, *args, **options):
        on_render = bool(os.environ.get("RENDER"))
        allow = (os.environ.get("SEED_TEST_DATA") or "").strip().lower() in (
            "true",
            "1",
            "yes",
        )
        if on_render and not allow and not options["force"]:
            self.stdout.write(
                self.style.WARNING(
                    "Skipped on Render (set SEED_TEST_DATA=true or pass --force)."
                )
            )
            return

        self.refresh = bool(options["refresh"])
        categories = self._seed_categories()
        country = get_or_create_default_country()
        lahore = get_or_create_city("Lahore", country=country)
        admin = self._seed_admin()
        merchants = self._seed_businesses(categories, lahore)
        consumers = self._seed_consumers(categories, lahore)
        self._seed_engagement(consumers, merchants)
        self._seed_carts(consumers, merchants)
        self._seed_orders(consumers, merchants, lahore, admin)

        self.stdout.write(self.style.SUCCESS("Seed data created/updated successfully."))
        self.stdout.write(f"Admin     -> {ADMIN_EMAIL} / {DEMO_PASSWORD}")
        self.stdout.write(
            f"Merchants -> merchant.*@aajhee.test / {DEMO_PASSWORD} "
            f"({len(LIVE_BUSINESSES)} stores)"
        )
        self.stdout.write(
            f"Consumers -> consumer@aajhee.test, fatima@aajhee.test, "
            f"hassan@aajhee.test / {DEMO_PASSWORD}"
        )

    def _seed_categories(self) -> dict[str, Category]:
        by_slug: dict[str, Category] = {}
        for name, slug, sort_order in ROOT_CATEGORIES:
            cat = Category.objects.filter(parent__isnull=True, slug=slug).first()
            if cat is None:
                cat = Category.objects.filter(
                    parent__isnull=True, name__iexact=name
                ).first()
            if cat is None:
                cat = Category.objects.create(
                    name=name,
                    slug=slug or slugify(name),
                    parent=None,
                    sort_order=sort_order,
                    is_active=True,
                )
            else:
                cat.name = name
                cat.slug = slug
                cat.sort_order = sort_order
                cat.is_active = True
                cat.save(update_fields=["name", "slug", "sort_order", "is_active"])
            by_slug[slug] = cat
        by_slug.update(ensure_l2_categories(Category))
        return by_slug

    def _seed_admin(self):
        User = get_user_model()
        email = User.objects.normalize_email(ADMIN_EMAIL)
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "first_name": "Abdullah",
                "last_name": "Aslam",
                "account_type": User.AccountType.CONSUMER,
                "is_staff": True,
                "is_superuser": True,
                "admin_role": User.AdminRole.OWNER,
            },
        )
        user.first_name = "Abdullah"
        user.last_name = "Aslam"
        user.is_staff = True
        user.is_superuser = True
        user.admin_role = User.AdminRole.OWNER
        user.set_password(DEMO_PASSWORD)
        user.save()
        label = "Created" if created else "Updated"
        self.stdout.write(f"{label} admin: {email}")
        return user

    def _ensure_password_user(self, email: str, *, account_type: str, **extra):
        User = get_user_model()
        email = User.objects.normalize_email(email)
        defaults = {"account_type": account_type, **extra}
        user, _ = User.objects.get_or_create(email=email, defaults=defaults)
        for key, value in extra.items():
            setattr(user, key, value)
        user.account_type = account_type
        user.set_password(DEMO_PASSWORD)
        user.save()
        return user

    def _seed_businesses(self, categories: dict[str, Category], lahore) -> dict:
        User = get_user_model()
        result = {}
        for payload in LIVE_BUSINESSES:
            owner = self._ensure_password_user(
                payload["email"],
                account_type=User.AccountType.BUSINESS,
                first_name=payload["name"].split()[0],
                last_name="Owner",
                phone=payload["phone"],
            )
            category = categories[payload["category_slug"]]
            business, _ = Business.objects.get_or_create(
                owner=owner,
                defaults={
                    "name": payload["name"],
                    "category": category,
                    "presence_mode": payload["presence_mode"],
                    "online_coverage": Business.OnlineCoverage.CITY,
                    "primary_country": lahore.country,
                    "primary_city": lahore,
                    "verification_status": Business.VerificationStatus.VERIFIED,
                    "phone": payload["phone"],
                    "instagram_url": payload.get("instagram_url", ""),
                    "notification_whatsapp": payload["phone"],
                    "business_hours": DEFAULT_BUSINESS_HOURS,
                },
            )
            business.name = payload["name"]
            business.category = category
            business.presence_mode = payload["presence_mode"]
            business.online_coverage = Business.OnlineCoverage.CITY
            business.primary_country = lahore.country
            business.primary_city = lahore
            business.verification_status = Business.VerificationStatus.VERIFIED
            business.phone = payload["phone"]
            business.instagram_url = payload.get("instagram_url", "")
            business.notification_whatsapp = payload["phone"]
            business.is_paused = False
            business.deleted_at = None
            business.business_hours = DEFAULT_BUSINESS_HOURS
            business.save()

            logo_url = payload.get("logo_url")
            if logo_url and (self.refresh or not business.logo):
                logo_file = download_image_or_none(
                    logo_url, f"{payload['slug']}-logo.jpg"
                )
                if logo_file:
                    if business.logo:
                        business.logo.delete(save=False)
                    business.logo.save(logo_file.name, logo_file, save=True)
                else:
                    self.stdout.write(
                        self.style.WARNING(f"Logo download failed: {payload['name']}")
                    )

            BusinessCategory.objects.get_or_create(business=business, category=category)
            BusinessEngagementStats.objects.get_or_create(business=business)

            branches = []
            for branch_data in payload["branches"]:
                branch, _ = Branch.objects.update_or_create(
                    business=business,
                    name=branch_data["name"],
                    defaults={
                        "street": branch_data["street"],
                        "house_number": branch_data["house_number"],
                        "postal_code": branch_data["postal_code"],
                        "city": branch_data["city"],
                        "city_ref": lahore,
                        "latitude": Decimal(branch_data["lat"]),
                        "longitude": Decimal(branch_data["lng"]),
                    },
                )
                BranchContact.objects.update_or_create(
                    branch=branch,
                    contact_type=BranchContact.ContactType.WHATSAPP,
                    value=branch_data["whatsapp"],
                    defaults={"is_primary": True},
                )
                BranchContact.objects.update_or_create(
                    branch=branch,
                    contact_type=BranchContact.ContactType.PHONE,
                    value=branch_data["whatsapp"],
                    defaults={"is_primary": False},
                )
                BranchFulfillmentSettings.objects.update_or_create(
                    branch=branch,
                    defaults={
                        "pickup_enabled": True,
                        "pickup_radius_km": Decimal("15.00"),
                        "same_day_enabled": True,
                        "same_day_fee": Decimal("150.00"),
                        "same_day_max_delivery_hours": 6,
                        "same_day_cutoff_time": time(18, 0),
                        "same_day_radius_km": Decimal("15.00"),
                        "same_day_areas": branch_data.get("same_day_areas", ""),
                        "nationwide_enabled": True,
                        "nationwide_delivery_fee": Decimal("300.00"),
                        "nationwide_max_delivery_hours": 72,
                        "customer_cancel_policy": (
                            BranchFulfillmentSettings.CustomerCancelPolicy.WINDOW_MINUTES
                        ),
                        "customer_cancel_window_minutes": 30,
                        "bank_transfer_enabled": True,
                        "bank_transfer_instructions": (
                            f"Transfer to HBL A/C 1234-5678901 ({payload['name']}). "
                            "Use order ID as reference."
                        ),
                        "jazzcash_enabled": True,
                        "jazzcash_instructions": (
                            f"Send to {payload['phone'].lstrip('+')} and upload proof."
                        ),
                        "easypaisa_enabled": True,
                        "easypaisa_instructions": (
                            f"Send to {payload['phone'].lstrip('+')} and upload proof."
                        ),
                        "cash_on_pickup_enabled": True,
                        "cash_on_delivery_enabled": True,
                    },
                )
                branches.append(branch)

            if self.refresh:
                deleted, _ = Product.objects.filter(business=business).delete()
                if deleted:
                    self.stdout.write(
                        f"  Cleared {deleted} old product rows for {payload['name']}"
                    )

            products = []
            for index, product_data in enumerate(payload["products"], start=1):
                base = Decimal(product_data["base_price"])
                sale = (
                    Decimal(product_data["sale_price"])
                    if product_data.get("sale_price")
                    else None
                )
                discount = (
                    Product.compute_discount_percent(base, sale) if sale else None
                )
                product_category = category
                l2_slug = product_data.get("category_slug") or resolve_product_l2_slug(
                    product_data["name"], payload["category_slug"]
                )
                if l2_slug and l2_slug in categories:
                    product_category = categories[l2_slug]
                product, created = Product.objects.update_or_create(
                    business=business,
                    name=product_data["name"],
                    defaults={
                        "category": product_category,
                        "description": product_data["description"],
                        "detailed_description": product_data["description"],
                        "base_price": base,
                        "sale_price": sale,
                        "discount_percent": discount,
                        "is_available": True,
                        "is_enabled": True,
                        "stock_quantity": product_data.get("stock"),
                        "sort_order": index,
                    },
                )
                product.branches.set(branches)

                image_url = product_data.get("image_url")
                if image_url and (created or self.refresh or not product.image):
                    image_file = download_image_or_none(
                        image_url, f"{payload['slug']}-product-{index}.jpg"
                    )
                    if image_file:
                        if product.image:
                            product.image.delete(save=False)
                        product.image.save(image_file.name, image_file, save=True)
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Image download failed: {product_data['name']}"
                            )
                        )

                stats, _ = ProductEngagementStats.objects.get_or_create(product=product)
                if stats.view_count == 0 or self.refresh:
                    stats.view_count = 40 + index * 11
                    stats.like_count = 3 + index
                    stats.order_count = index * 2
                    stats.save(
                        update_fields=["view_count", "like_count", "order_count"]
                    )
                products.append(product)

            result[payload["slug"]] = {
                "business": business,
                "branches": branches,
                "products": products,
                "owner": owner,
            }
            self.stdout.write(f"Merchant ready: {payload['name']} ({payload['email']})")
        return result

    def _seed_consumers(self, categories: dict[str, Category], lahore) -> list:
        User = get_user_model()
        consumers = []
        for payload in CONSUMERS:
            user = self._ensure_password_user(
                payload["email"],
                account_type=User.AccountType.CONSUMER,
                first_name=payload["first_name"],
                last_name=payload["last_name"],
                phone=payload["phone"],
            )
            addr = payload["address"]
            Address.objects.update_or_create(
                user=user,
                street=addr["street"],
                house_number=addr["house_number"],
                defaults={
                    "postal_code": addr["postal_code"],
                    "city": addr["city"],
                    "city_ref": lahore,
                    "county": addr.get("county", ""),
                    "latitude": Decimal(addr["latitude"]),
                    "longitude": Decimal(addr["longitude"]),
                    "is_default": True,
                    "delivery_instructions": addr.get("delivery_instructions", ""),
                    "landmark": addr.get("landmark", ""),
                },
            )
            prefs, _ = UserPreferences.objects.get_or_create(user=user)
            prefs.notifications_enabled = True
            prefs.save(update_fields=["notifications_enabled"])
            preferred = [
                categories[slug]
                for slug in payload.get("preferred_category_slugs", [])
                if slug in categories
            ]
            prefs.preferred_categories.set(preferred)
            consumers.append(user)
            self.stdout.write(f"Consumer ready: {payload['email']}")
        return consumers

    def _seed_engagement(self, consumers: list, merchants: dict) -> None:
        if not consumers:
            return
        ali, fatima, *rest = consumers
        hassan = rest[0] if rest else ali

        # Likes across a few stores / products
        like_pairs = [
            (ali, "greenbasket", 0),
            (ali, "techhive", 0),
            (fatima, "glamour-box", 0),
            (fatima, "style-studio", 1),
            (hassan, "techhive", 2),
            (hassan, "cozynest", 0),
        ]
        for user, slug, product_index in like_pairs:
            pack = merchants.get(slug)
            if not pack:
                continue
            BusinessLike.objects.get_or_create(user=user, business=pack["business"])
            if pack["products"]:
                idx = min(product_index, len(pack["products"]) - 1)
                ProductLike.objects.get_or_create(
                    user=user, product=pack["products"][idx]
                )

    def _seed_carts(self, consumers: list, merchants: dict) -> None:
        if not consumers:
            return
        ali = consumers[0]
        cart, _ = Cart.objects.get_or_create(user=ali)
        green = merchants.get("greenbasket")
        tech = merchants.get("techhive")
        if green and green["products"]:
            CartItem.objects.update_or_create(
                cart=cart,
                product=green["products"][0],
                branch=green["branches"][0],
                defaults={"quantity": 1},
            )
        if tech and tech["products"]:
            CartItem.objects.update_or_create(
                cart=cart,
                product=tech["products"][0],
                branch=tech["branches"][0],
                defaults={"quantity": 2},
            )

    def _unit_prices(self, product: Product) -> tuple[Decimal, Decimal | None, Decimal]:
        base = product.base_price
        sale = product.sale_price if product.has_discount else None
        unit = sale if sale is not None else base
        pct = product.effective_discount_percent
        return base, sale, pct

    def _create_order(
        self,
        *,
        user,
        pack: dict,
        branch: Branch,
        product: Product,
        quantity: int,
        status: str,
        payment_status: str,
        fulfillment_type: str,
        payment_method: str,
        delivery_fee: Decimal,
        lahore,
        actor=None,
        customer_notes: str = "",
        cancel_reason: str = "",
        cancelled_by: str = "",
        review_rating: int | None = None,
        review_comment: str = "",
        hours_ago: int = 2,
    ) -> Order:
        base, sale, pct = self._unit_prices(product)
        unit = sale if sale is not None else base
        line_total = (unit * quantity).quantize(Decimal("0.01"))
        subtotal = line_total
        total = (subtotal + delivery_fee).quantize(Decimal("0.01"))

        address = user.addresses.filter(is_default=True).first()
        delivery_text = address.formatted_address if address else ""
        house = address.house_number if address else ""
        landmark = address.landmark if address else ""

        placed_at = timezone.now() - timedelta(hours=hours_ago)
        order = Order.objects.create(
            user=user,
            business=pack["business"],
            branch=branch,
            status=status,
            payment_status=payment_status,
            fulfillment_type=fulfillment_type,
            payment_method=payment_method,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total=total,
            delivery_address_text=delivery_text,
            delivery_house_number=house,
            delivery_landmark=landmark,
            delivery_city=lahore,
            customer_phone=user.phone or "",
            customer_notes=customer_notes,
            customer_cancel_allowed=True,
            customer_cancel_until=placed_at + timedelta(minutes=30),
            cancelled_by=cancelled_by,
            cancel_reason=cancel_reason,
            cancelled_at=(
                placed_at + timedelta(minutes=10) if cancelled_by else None
            ),
        )
        # placed_at is auto_now_add — nudge for variety
        Order.objects.filter(pk=order.pk).update(placed_at=placed_at)
        order.refresh_from_db()

        item = OrderItem.objects.create(
            order=order,
            product=product,
            product_name=product.name,
            unit_base_price=base,
            unit_sale_price=sale,
            unit_discount_percent=pct,
            quantity=quantity,
            line_total=line_total,
        )

        max_hours = 6 if fulfillment_type == Order.FulfillmentType.LOCAL_SAME_DAY else (
            72 if fulfillment_type == Order.FulfillmentType.NATIONWIDE else None
        )
        OrderDeliverySnapshot.objects.create(
            order=order,
            fulfillment_type=fulfillment_type,
            delivery_fee=delivery_fee,
            max_delivery_hours=max_hours,
            promised_by=(
                placed_at + timedelta(hours=max_hours) if max_hours else None
            ),
            branch_city_id=lahore.id,
            branch_city_name=lahore.name,
            customer_city_id=lahore.id,
            customer_city_name=lahore.name,
            pickup_radius_km=Decimal("15.00"),
            settings_json={"seeded": True},
        )

        history = [
            ("", Order.Status.PENDING),
        ]
        if status != Order.Status.PENDING:
            if status == Order.Status.CANCELLED:
                history.append((Order.Status.PENDING, Order.Status.CANCELLED))
            elif status == Order.Status.AWAITING_PAYMENT:
                history.append((Order.Status.PENDING, Order.Status.AWAITING_PAYMENT))
            elif status == Order.Status.ACCEPTED:
                history.append((Order.Status.PENDING, Order.Status.ACCEPTED))
            elif status == Order.Status.PREPARING:
                history.extend(
                    [
                        (Order.Status.PENDING, Order.Status.ACCEPTED),
                        (Order.Status.ACCEPTED, Order.Status.PREPARING),
                    ]
                )
            elif status == Order.Status.OUT_FOR_DELIVERY:
                history.extend(
                    [
                        (Order.Status.PENDING, Order.Status.ACCEPTED),
                        (Order.Status.ACCEPTED, Order.Status.PREPARING),
                        (Order.Status.PREPARING, Order.Status.OUT_FOR_DELIVERY),
                    ]
                )
            elif status == Order.Status.COMPLETED:
                if fulfillment_type == Order.FulfillmentType.PICKUP:
                    history.extend(
                        [
                            (Order.Status.PENDING, Order.Status.ACCEPTED),
                            (Order.Status.ACCEPTED, Order.Status.PREPARING),
                            (Order.Status.PREPARING, Order.Status.READY_FOR_PICKUP),
                            (Order.Status.READY_FOR_PICKUP, Order.Status.COMPLETED),
                        ]
                    )
                else:
                    history.extend(
                        [
                            (Order.Status.PENDING, Order.Status.ACCEPTED),
                            (Order.Status.ACCEPTED, Order.Status.PREPARING),
                            (Order.Status.PREPARING, Order.Status.OUT_FOR_DELIVERY),
                            (Order.Status.OUT_FOR_DELIVERY, Order.Status.COMPLETED),
                        ]
                    )
            elif status == Order.Status.READY_FOR_PICKUP:
                history.extend(
                    [
                        (Order.Status.PENDING, Order.Status.ACCEPTED),
                        (Order.Status.ACCEPTED, Order.Status.PREPARING),
                        (Order.Status.PREPARING, Order.Status.READY_FOR_PICKUP),
                    ]
                )
            elif status == Order.Status.PAYMENT_SUBMITTED:
                history.extend(
                    [
                        (Order.Status.PENDING, Order.Status.AWAITING_PAYMENT),
                        (Order.Status.AWAITING_PAYMENT, Order.Status.PAYMENT_SUBMITTED),
                    ]
                )

        for from_status, to_status in history:
            OrderStatusHistory.objects.create(
                order=order,
                from_status=from_status,
                to_status=to_status,
                actor=actor or pack["owner"],
                note="Seeded status transition",
            )

        if review_rating and status == Order.Status.COMPLETED:
            review, created = ProductReview.objects.get_or_create(
                order_item=item,
                defaults={
                    "user": user,
                    "business": pack["business"],
                    "product": product,
                    "order": order,
                    "rating": review_rating,
                    "comment": review_comment,
                    "status": ProductReview.Status.PUBLISHED,
                },
            )
            if created:
                recompute_product_rating(product)
                recompute_business_rating(pack["business"])

        return order

    def _seed_orders(self, consumers: list, merchants: dict, lahore, admin) -> None:
        if len(consumers) < 2:
            return
        ali, fatima = consumers[0], consumers[1]
        hassan = consumers[2] if len(consumers) > 2 else ali

        demo_emails = {c["email"] for c in CONSUMERS}
        if self.refresh:
            deleted, _ = Order.objects.filter(user__email__in=demo_emails).delete()
            self.stdout.write(f"Cleared {deleted} demo orders for refresh.")
        elif Order.objects.filter(user=ali).exists():
            self.stdout.write(
                self.style.NOTICE(
                    "Orders already exist for consumer@aajhee.test — skipping order seed "
                    "(pass --refresh to rebuild)."
                )
            )
            return

        green = merchants["greenbasket"]
        tech = merchants["techhive"]
        style = merchants["style-studio"]
        glam = merchants["glamour-box"]
        gift = merchants["gift-haven"]
        cozy = merchants["cozynest"]
        med = merchants["medcare-plus"]

        specs = [
            # Ali — active + completed grocery
            dict(
                user=ali,
                pack=green,
                branch=green["branches"][0],
                product=green["products"][0],
                quantity=1,
                status=Order.Status.PENDING,
                payment_status=Order.PaymentStatus.UNPAID,
                fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
                payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
                delivery_fee=Decimal("150.00"),
                customer_notes="Please deliver after 5pm",
                hours_ago=1,
            ),
            dict(
                user=ali,
                pack=tech,
                branch=tech["branches"][0],
                product=tech["products"][0],
                quantity=1,
                status=Order.Status.ACCEPTED,
                payment_status=Order.PaymentStatus.UNPAID,
                fulfillment_type=Order.FulfillmentType.PICKUP,
                payment_method=Order.PaymentMethod.CASH_ON_PICKUP,
                delivery_fee=Decimal("0.00"),
                hours_ago=5,
            ),
            dict(
                user=ali,
                pack=green,
                branch=green["branches"][1],
                product=green["products"][2],
                quantity=2,
                status=Order.Status.COMPLETED,
                payment_status=Order.PaymentStatus.PAID,
                fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
                payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
                delivery_fee=Decimal("150.00"),
                review_rating=5,
                review_comment="Basmati was fragrant and arrived cold-packed. Will order again.",
                hours_ago=48,
            ),
            # Fatima — beauty / fashion
            dict(
                user=fatima,
                pack=glam,
                branch=glam["branches"][0],
                product=glam["products"][0],
                quantity=1,
                status=Order.Status.PREPARING,
                payment_status=Order.PaymentStatus.PAID,
                fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
                payment_method=Order.PaymentMethod.JAZZCASH,
                delivery_fee=Decimal("150.00"),
                hours_ago=3,
            ),
            dict(
                user=fatima,
                pack=style,
                branch=style["branches"][0],
                product=style["products"][0],
                quantity=1,
                status=Order.Status.OUT_FOR_DELIVERY,
                payment_status=Order.PaymentStatus.PAID,
                fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
                payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
                delivery_fee=Decimal("150.00"),
                hours_ago=6,
            ),
            dict(
                user=fatima,
                pack=gift,
                branch=gift["branches"][0],
                product=gift["products"][1],
                quantity=1,
                status=Order.Status.COMPLETED,
                payment_status=Order.PaymentStatus.PAID,
                fulfillment_type=Order.FulfillmentType.NATIONWIDE,
                payment_method=Order.PaymentMethod.BANK_TRANSFER,
                delivery_fee=Decimal("300.00"),
                review_rating=4,
                review_comment="Nice hamper, packaging was great.",
                hours_ago=72,
            ),
            dict(
                user=fatima,
                pack=glam,
                branch=glam["branches"][0],
                product=glam["products"][1],
                quantity=2,
                status=Order.Status.AWAITING_PAYMENT,
                payment_status=Order.PaymentStatus.AWAITING_CONFIRMATION,
                fulfillment_type=Order.FulfillmentType.PICKUP,
                payment_method=Order.PaymentMethod.BANK_TRANSFER,
                delivery_fee=Decimal("0.00"),
                hours_ago=2,
            ),
            # Hassan — electronics / home / cancel
            dict(
                user=hassan,
                pack=tech,
                branch=tech["branches"][0],
                product=tech["products"][1],
                quantity=1,
                status=Order.Status.READY_FOR_PICKUP,
                payment_status=Order.PaymentStatus.UNPAID,
                fulfillment_type=Order.FulfillmentType.PICKUP,
                payment_method=Order.PaymentMethod.CASH_ON_PICKUP,
                delivery_fee=Decimal("0.00"),
                hours_ago=4,
            ),
            dict(
                user=hassan,
                pack=cozy,
                branch=cozy["branches"][0],
                product=cozy["products"][0],
                quantity=1,
                status=Order.Status.COMPLETED,
                payment_status=Order.PaymentStatus.PAID,
                fulfillment_type=Order.FulfillmentType.LOCAL_SAME_DAY,
                payment_method=Order.PaymentMethod.CASH_ON_DELIVERY,
                delivery_fee=Decimal("150.00"),
                review_rating=5,
                review_comment="Cushions look premium.",
                hours_ago=96,
            ),
            dict(
                user=hassan,
                pack=med,
                branch=med["branches"][0],
                product=med["products"][0],
                quantity=2,
                status=Order.Status.CANCELLED,
                payment_status=Order.PaymentStatus.UNPAID,
                fulfillment_type=Order.FulfillmentType.PICKUP,
                payment_method=Order.PaymentMethod.CASH_ON_PICKUP,
                delivery_fee=Decimal("0.00"),
                cancelled_by=Order.CancelledBy.CUSTOMER,
                cancel_reason="Ordered by mistake",
                hours_ago=10,
            ),
            dict(
                user=hassan,
                pack=tech,
                branch=tech["branches"][0],
                product=tech["products"][3],
                quantity=3,
                status=Order.Status.PAYMENT_SUBMITTED,
                payment_status=Order.PaymentStatus.AWAITING_CONFIRMATION,
                fulfillment_type=Order.FulfillmentType.NATIONWIDE,
                payment_method=Order.PaymentMethod.BANK_TRANSFER,
                delivery_fee=Decimal("300.00"),
                hours_ago=8,
            ),
        ]

        created = 0
        for spec in specs:
            self._create_order(lahore=lahore, actor=admin, **spec)
            created += 1
        self.stdout.write(f"Created {created} demo orders across statuses.")
