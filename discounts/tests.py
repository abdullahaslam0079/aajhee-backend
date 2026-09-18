from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Address, Branch, Business, Category, Offer, OfferRedemption, OfferScan
from .offer_pricing import compute_offer_payment
from .offer_utils import can_user_redeem_offer, get_user_offer_usage_status

User = get_user_model()


class OfferUsageLimitTests(TestCase):
    def setUp(self):
        self.consumer = User.objects.create_user(
            email="consumer@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
        )
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Test Cafe",
            category=self.category,
        )
        self.branch_a = Branch.objects.create(
            business=self.business,
            name="Branch A",
            street="Main",
            house_number="1",
            postal_code="10001",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )
        self.branch_b = Branch.objects.create(
            business=self.business,
            name="Branch B",
            street="Side",
            house_number="2",
            postal_code="10002",
            city="Berlin",
            latitude=Decimal("52.530008"),
            longitude=Decimal("13.414954"),
        )
        self.offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="One Time Deal",
            description="10% off",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            usage_limit_count=1,
        )
        self.offer.branches.set([self.branch_a, self.branch_b])

    def test_one_time_limit_applies_across_branches(self):
        OfferRedemption.objects.create(
            offer=self.offer,
            branch=self.branch_a,
            user=self.consumer,
        )

        can_redeem, message = can_user_redeem_offer(
            self.consumer, self.offer, self.branch_b
        )
        self.assertFalse(can_redeem)
        self.assertIn("already used", message.lower())

    def test_once_per_month_blocks_second_redemption(self):
        self.offer.usage_limit_type = Offer.UsageLimitType.ONCE_PER_MONTH
        self.offer.save(update_fields=["usage_limit_type"])

        OfferRedemption.objects.create(
            offer=self.offer,
            branch=self.branch_a,
            user=self.consumer,
        )

        usage = get_user_offer_usage_status(self.consumer, self.offer)
        self.assertEqual(usage.redemption_count, 1)
        self.assertEqual(usage.remaining_uses, 0)
        self.assertFalse(usage.is_available_for_user)
        self.assertIsNotNone(usage.period_resets_at)

    def test_weekly_limit_resets_after_period(self):
        self.offer.usage_limit_type = Offer.UsageLimitType.ONCE_PER_WEEK
        self.offer.save(update_fields=["usage_limit_type"])

        old_redemption = OfferRedemption.objects.create(
            offer=self.offer,
            branch=self.branch_a,
            user=self.consumer,
        )
        OfferRedemption.objects.filter(pk=old_redemption.pk).update(
            redeemed_at=timezone.now() - timedelta(days=8)
        )

        usage = get_user_offer_usage_status(self.consumer, self.offer)
        self.assertEqual(usage.redemption_count, 0)
        self.assertTrue(usage.is_available_for_user)


class OfferRedeemAPITests(APITestCase):
    def setUp(self):
        self.consumer = User.objects.create_user(
            email="consumer@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
        )
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Test Cafe",
            category=self.category,
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name="Main Branch",
            street="Main",
            house_number="1",
            postal_code="10001",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )
        self.offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="One Time Deal",
            description="10% off",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            usage_limit_count=1,
        )
        self.offer.branches.add(self.branch)
        self.client.force_authenticate(user=self.consumer)

    def test_redeem_returns_usage_status(self):
        response = self.client.post(
            f"/api/offers/{self.offer.id}/redeem",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.offer.qr_code),
                "bill_amount": "80.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user_redemption_count"], 1)
        self.assertEqual(response.data["user_remaining_uses"], 0)
        self.assertFalse(response.data["is_available_for_user"])
        self.assertEqual(response.data["payment"]["amount_to_pay"], "72.00")
        self.assertEqual(response.data["payment"]["original_amount"], "80.00")
        self.assertEqual(response.data["payment"]["discount_amount"], "8.00")

    def test_second_redeem_is_rejected(self):
        self.client.post(
            f"/api/offers/{self.offer.id}/redeem",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.offer.qr_code),
                "bill_amount": "80.00",
            },
            format="json",
        )
        response = self.client.post(
            f"/api/offers/{self.offer.id}/redeem",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.offer.qr_code),
                "bill_amount": "80.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(OfferRedemption.objects.count(), 1)

    def test_usage_endpoint_reports_remaining_uses(self):
        OfferRedemption.objects.create(
            offer=self.offer,
            branch=self.branch,
            user=self.consumer,
        )

        response = self.client.get(f"/api/offers/{self.offer.id}/usage")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user_redemption_count"], 1)
        self.assertEqual(response.data["user_remaining_uses"], 0)
        self.assertFalse(response.data["is_available_for_user"])


class LogoutAndAvailedOffersAPITests(APITestCase):
    def setUp(self):
        self.consumer = User.objects.create_user(
            email="consumer@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
        )
        self.business_user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.business_user,
            name="Test Cafe",
            category=self.category,
        )
        self.branch_a = Branch.objects.create(
            business=self.business,
            name="Branch A",
            street="Main",
            house_number="1",
            postal_code="10001",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )
        self.branch_b = Branch.objects.create(
            business=self.business,
            name="Branch B",
            street="Side",
            house_number="2",
            postal_code="10002",
            city="Berlin",
            latitude=Decimal("52.530008"),
            longitude=Decimal("13.414954"),
        )
        self.offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Lunch Deal",
            description="10% off",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.N_TIMES_TOTAL,
            usage_limit_count=5,
        )
        self.offer.branches.set([self.branch_a, self.branch_b])

    def test_consumer_logout_blacklists_tokens(self):
        login = self.client.post(
            "/api/auth/token",
            {"email": "consumer@example.com", "password": "testpass123"},
            format="json",
        )
        access = login.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        response = self.client.post("/api/auth/logout", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Logged out successfully", response.data["message"])

    def test_availed_offers_returns_newest_first_with_branch(self):
        older = OfferRedemption.objects.create(
            offer=self.offer,
            branch=self.branch_a,
            user=self.consumer,
        )
        newer = OfferRedemption.objects.create(
            offer=self.offer,
            branch=self.branch_b,
            user=self.consumer,
        )
        OfferRedemption.objects.filter(pk=older.pk).update(
            redeemed_at=timezone.now() - timedelta(days=2)
        )

        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/user/offers/availed")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], newer.id)
        self.assertEqual(results[0]["branch"]["id"], self.branch_b.id)
        self.assertEqual(results[0]["branch"]["name"], "Branch B")
        self.assertEqual(results[1]["id"], older.id)
        self.assertEqual(results[1]["branch"]["id"], self.branch_a.id)
        self.assertEqual(results[0]["offer"]["title"], "Lunch Deal")

    def test_availed_offers_requires_consumer_account(self):
        self.client.force_authenticate(user=self.business_user)
        response = self.client.get("/api/user/offers/availed")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class LocationFilteringAPITests(APITestCase):
    def setUp(self):
        self.consumer = User.objects.create_user(
            email="consumer@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
        )
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Test Cafe",
            category=self.category,
        )
        self.berlin_near = Branch.objects.create(
            business=self.business,
            name="Berlin Near",
            street="Near",
            house_number="1",
            postal_code="10115",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )
        self.berlin_far = Branch.objects.create(
            business=self.business,
            name="Berlin Far",
            street="Far",
            house_number="2",
            postal_code="10117",
            city="Berlin",
            latitude=Decimal("52.560008"),
            longitude=Decimal("13.454954"),
        )
        self.munich_branch = Branch.objects.create(
            business=self.business,
            name="Munich Branch",
            street="Marienplatz",
            house_number="1",
            postal_code="80331",
            city="Munich",
            latitude=Decimal("48.137154"),
            longitude=Decimal("11.576124"),
        )
        self.berlin_offer_near = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Berlin Near Deal",
            description="Near deal",
            discount_percent=Decimal("15.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        self.berlin_offer_near.branches.set([self.berlin_near])
        self.berlin_offer_far = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Berlin Far Deal",
            description="Far deal",
            discount_percent=Decimal("20.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        self.berlin_offer_far.branches.set([self.berlin_far])
        self.munich_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Munich Deal",
            description="Munich deal",
            discount_percent=Decimal("25.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        self.munich_offer.branches.set([self.munich_branch])
        Address.objects.create(
            user=self.consumer,
            street="Unter den Linden",
            house_number="1",
            postal_code="10117",
            city="Berlin",
            county="Berlin",
            latitude=Decimal("52.517036"),
            longitude=Decimal("13.388860"),
            is_default=True,
        )

    def test_offers_filtered_by_default_address_city(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/offers")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [offer["title"] for offer in response.data]
        self.assertIn("Berlin Near Deal", titles)
        self.assertIn("Berlin Far Deal", titles)
        self.assertNotIn("Munich Deal", titles)

    def test_offers_sorted_nearest_first(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/offers")
        berlin_offers = [
            offer for offer in response.data if offer["title"].startswith("Berlin")
        ]
        self.assertEqual(berlin_offers[0]["title"], "Berlin Near Deal")
        self.assertLess(
            berlin_offers[0]["nearest_distance_km"],
            berlin_offers[1]["nearest_distance_km"],
        )

    def test_map_branches_filtered_by_city(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/map/branches")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        branch_names = [branch["name"] for branch in response.data]
        self.assertIn("Berlin Near", branch_names)
        self.assertIn("Berlin Far", branch_names)
        self.assertNotIn("Munich Branch", branch_names)

    def test_map_branches_sorted_nearest_first(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/map/branches")
        berlin_branches = [
            branch for branch in response.data if branch["name"].startswith("Berlin")
        ]
        self.assertEqual(berlin_branches[0]["name"], "Berlin Near")
        self.assertLess(
            berlin_branches[0]["distance_km"],
            berlin_branches[1]["distance_km"],
        )

    def test_selected_address_changes_visible_city(self):
        munich_address = Address.objects.create(
            user=self.consumer,
            street="Sendlinger",
            house_number="1",
            postal_code="80331",
            city="Munich",
            county="Bavaria",
            latitude=Decimal("48.135125"),
            longitude=Decimal("11.581981"),
        )
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get(
            f"/api/offers?address_id=addr_{munich_address.id}"
        )
        titles = [offer["title"] for offer in response.data]
        self.assertIn("Munich Deal", titles)
        self.assertNotIn("Berlin Near Deal", titles)

    def test_small_town_falls_back_to_radius(self):
        nearby_branch = Branch.objects.create(
            business=self.business,
            name="Suburban Branch",
            street="Ring",
            house_number="5",
            postal_code="16515",
            city="Oranienburg",
            latitude=Decimal("52.525500"),
            longitude=Decimal("13.410500"),
        )
        nearby_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Suburban Deal",
            description="Just outside Berlin",
            discount_percent=Decimal("12.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        nearby_offer.branches.set([nearby_branch])

        self.client.force_authenticate(user=self.consumer)
        response = self.client.get(
            "/api/offers",
            {
                "latitude": "52.524000",
                "longitude": "13.405000",
                "city": "Kleinstadt",
            },
        )
        titles = [offer["title"] for offer in response.data]
        self.assertIn("Suburban Deal", titles)
        self.assertNotIn("Munich Deal", titles)

    def _nearby_names(self, response):
        return [branch["name"] for branch in response.data["results"]]

    def test_map_nearby_defaults_to_four_km_radius(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/map/nearby")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = self._nearby_names(response)
        self.assertIn("Berlin Near", names)
        self.assertNotIn("Berlin Far", names)
        self.assertNotIn("Munich Branch", names)
        self.assertEqual(response.data["radius_km"], 4.0)

    def test_map_nearby_search_area_uses_explicit_center(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get(
            "/api/map/nearby",
            {
                "latitude": "52.560008",
                "longitude": "13.454954",
                "radius_km": "4",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = self._nearby_names(response)
        self.assertIn("Berlin Far", names)
        self.assertNotIn("Berlin Near", names)
        self.assertNotIn("Munich Branch", names)

    def test_map_nearby_does_not_change_city_scoped_stores_feed(self):
        self.client.force_authenticate(user=self.consumer)
        nearby = self.client.get("/api/map/nearby")
        stores = self.client.get("/api/map/branches")
        self.assertNotIn("Berlin Far", self._nearby_names(nearby))
        payload = stores.data
        store_items = payload["results"] if isinstance(payload, dict) else payload
        store_names = [branch["name"] for branch in store_items]
        self.assertIn("Berlin Far", store_names)

    def test_map_nearby_requires_both_coordinates(self):
        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/map/nearby", {"latitude": "52.52"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_map_nearby_anonymous_without_center_is_empty(self):
        response = self.client.get("/api/map/nearby")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"], [])


class OfferPaymentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Test Cafe",
            category=self.category,
        )
        self.item_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.ITEM,
            title="Burger deal",
            item_name="Classic Burger",
            original_price=Decimal("10.00"),
            discounted_price=Decimal("7.00"),
            discount_percent=Decimal("30.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        self.percent_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="10% off bill",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )

    def test_item_offer_payment_is_fixed_discounted_price(self):
        payment = compute_offer_payment(self.item_offer)
        self.assertEqual(payment.amount_to_pay, Decimal("7.00"))
        self.assertEqual(payment.original_amount, Decimal("10.00"))
        self.assertEqual(payment.discount_amount, Decimal("3.00"))
        self.assertFalse(payment.requires_bill_amount)

    def test_percentage_offer_requires_bill_amount(self):
        payment = compute_offer_payment(self.percent_offer)
        self.assertIsNone(payment.amount_to_pay)
        self.assertTrue(payment.requires_bill_amount)

    def test_percentage_offer_calculates_payment_from_bill(self):
        payment = compute_offer_payment(self.percent_offer, bill_amount=Decimal("80.00"))
        self.assertEqual(payment.original_amount, Decimal("80.00"))
        self.assertEqual(payment.discount_amount, Decimal("8.00"))
        self.assertEqual(payment.amount_to_pay, Decimal("72.00"))

    def test_item_payment_summary(self):
        payment = compute_offer_payment(self.item_offer)
        self.assertIn("€7.00", payment.summary)
        self.assertIn("Classic Burger", payment.summary)

    def test_deal_offer_payment_is_fixed_deal_price(self):
        deal = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.DEAL,
            title="Zinger Box",
            included_items=["Zinger burger", "Regular fries", "Soft drink"],
            discounted_price=Decimal("8.99"),
            discount_percent=Decimal("0.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        payment = compute_offer_payment(deal)
        self.assertEqual(payment.amount_to_pay, Decimal("8.99"))
        self.assertIsNone(payment.original_amount)
        self.assertFalse(payment.requires_bill_amount)
        self.assertIn("Zinger Box", payment.summary)


class OfferPaymentAPITests(APITestCase):
    def setUp(self):
        self.consumer = User.objects.create_user(
            email="consumer@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
        )
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Test Cafe",
            category=self.category,
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name="Main Branch",
            street="Main",
            house_number="1",
            postal_code="10001",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )
        self.item_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.ITEM,
            title="Burger deal",
            item_name="Classic Burger",
            original_price=Decimal("10.00"),
            discounted_price=Decimal("7.00"),
            discount_percent=Decimal("30.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        self.item_offer.branches.add(self.branch)
        self.percent_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="10% off bill",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
        )
        self.percent_offer.branches.add(self.branch)
        self.client.force_authenticate(user=self.consumer)

    def test_item_scan_returns_amount_to_pay(self):
        response = self.client.post(
            f"/api/offers/{self.item_offer.id}/scan",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.item_offer.qr_code),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment"]["amount_to_pay"], "7.00")
        self.assertEqual(response.data["payment"]["original_amount"], "10.00")
        scan = OfferScan.objects.get()
        self.assertEqual(scan.amount_to_pay, Decimal("7.00"))

    def test_percentage_scan_requires_bill_amount(self):
        response = self.client.post(
            f"/api/offers/{self.percent_offer.id}/scan",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.percent_offer.qr_code),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("bill_amount", response.data["errors"])

    def test_percentage_scan_calculates_payment(self):
        response = self.client.post(
            f"/api/offers/{self.percent_offer.id}/scan",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.percent_offer.qr_code),
                "bill_amount": "80.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment"]["amount_to_pay"], "72.00")

    def test_payment_preview_for_item_offer(self):
        response = self.client.post(
            f"/api/offers/{self.item_offer.id}/payment-preview",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment"]["amount_to_pay"], "7.00")

    def test_payment_preview_for_percentage_offer(self):
        response = self.client.post(
            f"/api/offers/{self.percent_offer.id}/payment-preview",
            {"bill_amount": "50.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment"]["amount_to_pay"], "45.00")

    def test_by_qr_resolves_poster_offer(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(
            f"/api/offers/by-qr/{self.item_offer.qr_code}",
            {"branch_id": self.branch.id},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offer"]["id"], self.item_offer.id)
        self.assertEqual(response.data["payment"]["amount_to_pay"], "7.00")
        self.assertIn("counter", response.data["payment"]["summary"].lower())
        self.assertFalse(response.data["can_avail"])

    def test_by_qr_includes_usage_when_authenticated(self):
        response = self.client.get(
            f"/api/offers/by-qr/{self.item_offer.qr_code}",
            {"branch_id": self.branch.id},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["can_avail"])

    def test_by_qr_percentage_without_bill_amount(self):
        response = self.client.get(
            f"/api/offers/by-qr/{self.percent_offer.qr_code}",
            {"branch_id": self.branch.id},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["payment"]["requires_bill_amount"])
        self.assertIsNone(response.data["payment"]["amount_to_pay"])

    def test_avail_completes_poster_flow_in_one_step(self):
        self.assertEqual(OfferRedemption.objects.filter(offer=self.item_offer).count(), 0)
        response = self.client.post(
            f"/api/offers/{self.item_offer.id}/avail",
            {
                "branch_id": self.branch.id,
                "qr_code": str(self.item_offer.qr_code),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["payment"]["amount_to_pay"], "7.00")
        self.assertIn("counter", response.data["message"].lower())
        self.assertEqual(OfferScan.objects.count(), 1)
        self.assertEqual(OfferRedemption.objects.count(), 1)


class AIEnrichmentTests(TestCase):
    def test_skips_when_no_api_key(self):
        from django.test import override_settings

        from .ai_enrichment import enrich_product_draft

        draft = {
            "source_url": "https://example.com/p",
            "title": "",
            "description": "",
            "detailed_description": "",
            "original_price": None,
            "currency": None,
            "image_urls": ["https://example.com/a.jpg"],
            "suggested_offer_type": "percentage_bill",
            "confidence": {},
            "warnings": ["Title not found", "Description not found", "Price not found"],
        }
        with override_settings(GEMINI_API_KEY=""):
            result = enrich_product_draft(
                draft,
                categories=["Food", "Retail"],
                page_text="Espresso machine on sale",
            )
        self.assertFalse(result["ai_enriched"])
        self.assertEqual(result["title"], "")
        self.assertIsNone(result["suggested_category"])
        self.assertEqual(result["suggested_discount_copy"], "")

    def test_merge_fills_blanks_without_overwriting_scraped(self):
        from .ai_enrichment import _merge_ai_into_draft

        draft = {
            "source_url": "https://example.com/p",
            "title": "Scraped Title",
            "description": "",
            "detailed_description": "",
            "original_price": "19.99",
            "currency": "EUR",
            "image_urls": ["https://example.com/a.jpg"],
            "suggested_offer_type": "item",
            "confidence": {
                "title": "json_ld",
                "original_price": "json_ld",
                "currency": "json_ld",
            },
            "warnings": ["Description not found"],
        }
        ai = {
            "title": "AI Should Not Win",
            "description": "Great espresso for home baristas.",
            "detailed_description": "Detailed AI text about the espresso machine.",
            "original_price": "1.00",
            "currency": "USD",
            "suggested_category": "Food",
            "suggested_discount_percent": 15,
            "suggested_discount_copy": "15% off your next espresso machine.",
            "suggested_offer_type": "item",
        }
        result = _merge_ai_into_draft(draft, ai, category_names=["Food", "Retail"])
        self.assertTrue(result["ai_enriched"])
        self.assertEqual(result["title"], "Scraped Title")
        self.assertEqual(result["confidence"]["title"], "json_ld")
        self.assertEqual(result["original_price"], "19.99")
        self.assertEqual(result["currency"], "EUR")
        self.assertEqual(result["description"], "Great espresso for home baristas.")
        self.assertEqual(result["confidence"]["description"], "ai")
        self.assertEqual(result["suggested_category"], "Food")
        self.assertEqual(result["suggested_discount_percent"], "15.00")
        self.assertIn("espresso", result["suggested_discount_copy"].lower())
        self.assertNotIn("Description not found", result["warnings"])

    def test_ai_failure_returns_draft_with_warning(self):
        from django.test import override_settings
        from unittest.mock import patch

        from .ai_enrichment import enrich_product_draft

        draft = {
            "source_url": "https://example.com/p",
            "title": "Only Title",
            "description": "",
            "detailed_description": "",
            "original_price": None,
            "currency": None,
            "image_urls": ["https://example.com/a.jpg"],
            "suggested_offer_type": "percentage_bill",
            "confidence": {"title": "html"},
            "warnings": ["Description not found", "Price not found"],
        }
        with override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-2.0-flash"):
            with patch(
                "discounts.ai_enrichment._call_gemini",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertLogs("discounts.ai_enrichment", level="ERROR"):
                    result = enrich_product_draft(
                        draft,
                        categories=["Food"],
                        page_text="sparse page",
                    )
        self.assertFalse(result["ai_enriched"])
        self.assertEqual(result["title"], "Only Title")
        self.assertIn("ai_enrichment_failed", result["warnings"])

    def test_import_sparse_page_without_api_key(self):
        from django.test import override_settings
        from unittest.mock import patch

        from .product_import import import_product_from_url

        html = """
        <html><head>
          <title>Sparse Coffee Deal</title>
          <meta property="og:image" content="https://cdn.example.com/coffee.jpg" />
        </head><body><p>Limited espresso offer this week.</p></body></html>
        """
        with override_settings(GEMINI_API_KEY=""):
            with patch("discounts.product_import._validate_public_http_url", return_value="https://example.com/sparse"):
                with patch("discounts.product_import._fetch_html", return_value=html):
                    draft = import_product_from_url(
                        "https://example.com/sparse",
                        categories=["Food", "Retail"],
                    )
        self.assertEqual(draft["title"], "Sparse Coffee Deal")
        self.assertFalse(draft["ai_enriched"])
        self.assertEqual(draft["suggested_discount_copy"], "")
        self.assertIn("https://cdn.example.com/coffee.jpg", draft["image_urls"])
        self.assertIn("Description not found", draft["warnings"])

    def test_import_rich_page_keeps_scraped_fields(self):
        from django.test import override_settings
        from unittest.mock import patch

        from .product_import import import_product_from_url

        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@type": "Product",
            "name": "Classic Burger",
            "description": "Beef burger with fries",
            "image": "https://cdn.example.com/burger.jpg",
            "offers": {"@type": "Offer", "price": "12.50", "priceCurrency": "EUR"}
          }
          </script>
        </head><body></body></html>
        """
        ai_payload = {
            "title": "Should Not Replace",
            "description": "Should Not Replace Desc",
            "detailed_description": "Should Not Replace Detail",
            "original_price": "1.00",
            "currency": "USD",
            "suggested_category": "Food",
            "suggested_discount_percent": 10,
            "suggested_discount_copy": "10% off burgers today.",
            "suggested_offer_type": "item",
        }
        with override_settings(GEMINI_API_KEY="test-key"):
            with patch("discounts.product_import._validate_public_http_url", return_value="https://example.com/rich"):
                with patch("discounts.product_import._fetch_html", return_value=html):
                    with patch(
                        "discounts.ai_enrichment._call_gemini",
                        return_value=ai_payload,
                    ):
                        draft = import_product_from_url(
                            "https://example.com/rich",
                            categories=["Food"],
                        )
        self.assertEqual(draft["title"], "Classic Burger")
        self.assertEqual(draft["original_price"], "12.50")
        self.assertEqual(draft["currency"], "EUR")
        self.assertEqual(draft["confidence"]["title"], "json_ld")
        self.assertEqual(draft["suggested_category"], "Food")
        self.assertEqual(draft["suggested_discount_percent"], "10.00")
        self.assertTrue(draft["ai_enriched"])


class OnlineOfferAPITests(APITestCase):
    def setUp(self):
        self.consumer = User.objects.create_user(
            email="consumer@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
        )
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
            is_staff=True,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Online Shop",
            category=self.category,
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name="Berlin Store",
            street="Main",
            house_number="1",
            postal_code="10115",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )
        Address.objects.create(
            user=self.consumer,
            street="Unter den Linden",
            house_number="1",
            postal_code="10117",
            city="Berlin",
            county="Berlin",
            latitude=Decimal("52.517036"),
            longitude=Decimal("13.388860"),
            is_default=True,
        )

    def test_business_create_online_only_offer_without_branches(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            "/api/business/offers",
            {
                "offer_type": "percentage_bill",
                "title": "Online 15% off",
                "description": "Web only",
                "discount_percent": "15.00",
                "usage_limit_type": "one_time",
                "is_online": True,
                "is_enabled": True,
                "external_url": "https://shop.example.com/deal",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_online"])
        self.assertEqual(response.data["branches"], [])
        offer = Offer.objects.get(pk=response.data["id"])
        self.assertTrue(offer.is_online)
        self.assertEqual(offer.branches.count(), 0)

    def test_business_create_requires_branch_or_online(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.post(
            "/api/business/offers",
            {
                "offer_type": "percentage_bill",
                "title": "Missing location",
                "discount_percent": "10.00",
                "usage_limit_type": "one_time",
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data.get("errors", response.data)
        self.assertIn("branch_ids", errors)
        self.assertIn("is_online", errors)

    def test_admin_create_online_only_offer(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/admin/offers",
            {
                "business_id": self.business.id,
                "offer_type": "item",
                "title": "Admin online deal",
                "item_name": "Gift Card",
                "original_price": "50.00",
                "discounted_price": "40.00",
                "usage_limit_type": "one_time",
                "is_online": True,
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_online"])
        self.assertEqual(response.data["branches"], [])

    def test_consumer_offers_include_online_and_expose_flag(self):
        online_offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Online Only Deal",
            description="No store visit",
            discount_percent=Decimal("12.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            is_online=True,
        )
        in_store = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="In Store Deal",
            description="Berlin only",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            is_online=False,
        )
        in_store.branches.set([self.branch])

        self.client.force_authenticate(user=self.consumer)
        response = self.client.get("/api/offers")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_title = {offer["title"]: offer for offer in response.data}
        self.assertIn("Online Only Deal", by_title)
        self.assertIn("In Store Deal", by_title)
        self.assertTrue(by_title["Online Only Deal"]["is_online"])
        self.assertFalse(by_title["In Store Deal"]["is_online"])
        self.assertIsNone(by_title["Online Only Deal"]["nearest_distance_km"])
        self.assertEqual(online_offer.branches.count(), 0)


class OfferDealAPITests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="testpass123",
            account_type=User.AccountType.CONSUMER,
            is_staff=True,
        )
        self.category = Category.objects.create(name="Food")
        self.business = Business.objects.create(
            owner=self.owner,
            name="KFC Test",
            category=self.category,
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name="Main Branch",
            street="Main",
            house_number="1",
            postal_code="10001",
            city="Berlin",
            latitude=Decimal("52.520008"),
            longitude=Decimal("13.404954"),
        )

    def test_admin_create_deal_offer(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/admin/offers",
            {
                "business_id": self.business.id,
                "offer_type": "deal",
                "title": "Zinger Box",
                "included_items": ["Zinger burger", "Regular fries", "Soft drink"],
                "discounted_price": "8.99",
                "usage_limit_type": "one_time",
                "branch_ids": [self.branch.id],
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["offer_type"], "deal")
        self.assertEqual(
            response.data["included_items"],
            ["Zinger burger", "Regular fries", "Soft drink"],
        )
        self.assertEqual(response.data["external_url_label"], "View Deal")
        self.assertEqual(response.data["discounted_price"], "8.99")
        self.assertIsNone(response.data["original_price"])
        offer = Offer.objects.get(pk=response.data["id"])
        self.assertEqual(offer.offer_type, Offer.OfferType.DEAL)
        self.assertEqual(offer.included_items, ["Zinger burger", "Regular fries", "Soft drink"])
        self.assertIsNone(offer.original_price)

    def test_deal_requires_at_least_two_items(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/admin/offers",
            {
                "business_id": self.business.id,
                "offer_type": "deal",
                "title": "Incomplete Box",
                "included_items": ["Zinger burger"],
                "discounted_price": "7.00",
                "usage_limit_type": "one_time",
                "branch_ids": [self.branch.id],
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        errors = response.data.get("errors", response.data)
        self.assertIn("included_items", errors)

    def test_item_offer_defaults_view_offer_label(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/admin/offers",
            {
                "business_id": self.business.id,
                "offer_type": "item",
                "title": "Classic Burger",
                "item_name": "Classic Burger",
                "original_price": "12.00",
                "discounted_price": "8.00",
                "usage_limit_type": "one_time",
                "branch_ids": [self.branch.id],
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["external_url_label"], "View Offer")

    def test_custom_external_url_label_is_kept(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/admin/offers",
            {
                "business_id": self.business.id,
                "offer_type": "deal",
                "title": "Family Bucket",
                "included_items": ["8 pieces", "Large fries", "2 drinks"],
                "discounted_price": "22.00",
                "usage_limit_type": "one_time",
                "branch_ids": [self.branch.id],
                "external_url_label": "Order now",
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["external_url_label"], "Order now")

    def test_search_finds_deal_by_included_item(self):
        deal = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.DEAL,
            title="Zinger Box",
            included_items=["Zinger burger", "Regular fries", "Soft drink"],
            discounted_price=Decimal("8.99"),
            discount_percent=Decimal("0.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            is_enabled=True,
        )
        deal.branches.set([self.branch])
        response = self.client.get("/api/offers/search", {"q": "Zinger burger"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [offer["title"] for offer in response.data["results"]]
        self.assertIn("Zinger Box", titles)
        found = next(offer for offer in response.data["results"] if offer["title"] == "Zinger Box")
        self.assertEqual(found["offer_type"], "deal")
        self.assertEqual(found["included_items"][0], "Zinger burger")


class PhoneAuthAPITests(APITestCase):
    def setUp(self):
        self.business_user = User.objects.create_user(
            email="biz-phone@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
            phone="+491111111111",
            firebase_uid="biz-uid",
        )

    def test_phone_auth_creates_consumer(self):
        from unittest.mock import patch

        claims = {"uid": "firebase-uid-1", "phone_number": "+491701234567"}
        with patch(
            "discounts.views_auth.verify_firebase_id_token",
            return_value=claims,
        ):
            response = self.client.post(
                "/api/auth/phone",
                {"id_token": "fake-token"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["phone"], "+491701234567")
        user = User.objects.get(firebase_uid="firebase-uid-1")
        self.assertEqual(user.account_type, User.AccountType.CONSUMER)
        self.assertEqual(user.phone, "+491701234567")
        self.assertTrue(user.email.endswith("@phone.aajhee.local"))
        self.assertFalse(user.has_usable_password())

    def test_phone_auth_logs_in_existing_consumer(self):
        existing = User.objects.create_user(
            email="491709999999@phone.aajhee.local",
            password="unused",
            account_type=User.AccountType.CONSUMER,
            phone="+491709999999",
            firebase_uid="existing-uid",
        )
        existing.set_unusable_password()
        existing.save()

        from unittest.mock import patch

        with patch(
            "discounts.views_auth.verify_firebase_id_token",
            return_value={"uid": "existing-uid", "phone_number": "+491709999999"},
        ):
            response = self.client.post(
                "/api/auth/phone",
                {"id_token": "fake-token"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["id"], str(existing.pk))
        self.assertEqual(User.objects.filter(phone="+491709999999").count(), 1)

    def test_phone_auth_rejects_business_account(self):
        from unittest.mock import patch

        with patch(
            "discounts.views_auth.verify_firebase_id_token",
            return_value={"uid": "biz-uid", "phone_number": "+491111111111"},
        ):
            response = self.client.post(
                "/api/auth/phone",
                {"id_token": "fake-token"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_phone_auth_rejects_invalid_token(self):
        from rest_framework.exceptions import AuthenticationFailed
        from unittest.mock import patch

        with patch(
            "discounts.views_auth.verify_firebase_id_token",
            side_effect=AuthenticationFailed("Invalid or expired Firebase ID token."),
        ):
            response = self.client.post(
                "/api/auth/phone",
                {"id_token": "bad-token"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_phone_auth_requires_id_token(self):
        response = self.client.post("/api/auth/phone", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_google_auth_creates_consumer_with_email(self):
        from unittest.mock import patch

        claims = {
            "uid": "google-uid-1",
            "email": "alex@gmail.com",
            "name": "Alex Morgan",
            "firebase": {"sign_in_provider": "google.com"},
        }
        with patch(
            "discounts.views_auth.verify_firebase_id_token",
            return_value=claims,
        ):
            response = self.client.post(
                "/api/auth/firebase",
                {"id_token": "fake-google-token"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["email"], "alex@gmail.com")
        self.assertEqual(response.data["user"]["name"], "Alex Morgan")
        user = User.objects.get(firebase_uid="google-uid-1")
        self.assertEqual(user.email, "alex@gmail.com")
        self.assertIsNone(user.phone)

    def test_apple_auth_creates_consumer_without_email(self):
        from unittest.mock import patch

        claims = {
            "uid": "apple-uid-1",
            "firebase": {"sign_in_provider": "apple.com"},
        }
        with patch(
            "discounts.views_auth.verify_firebase_id_token",
            return_value=claims,
        ):
            response = self.client.post(
                "/api/auth/firebase",
                {"id_token": "fake-apple-token"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(firebase_uid="apple-uid-1")
        self.assertTrue(user.email.endswith("@firebase.aajhee.local"))
        self.assertFalse(user.has_usable_password())


class ListingDiscoverTests(TestCase):
    def test_item_list_json_ld(self):
        from unittest.mock import patch

        from .listing_discover import discover_product_urls_from_listing

        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@type": "ItemList",
            "itemListElement": [
              {"@type": "ListItem", "url": "https://shop.example.com/p/one"},
              {"@type": "ListItem", "item": {"@type": "Product", "url": "https://shop.example.com/p/two"}}
            ]
          }
          </script>
        </head><body></body></html>
        """
        with patch(
            "discounts.listing_discover.validate_public_http_url",
            return_value="https://shop.example.com/sale",
        ):
            with patch(
                "discounts.listing_discover.fetch_public_text",
                return_value=(html, 200),
            ):
                urls = discover_product_urls_from_listing(
                    "https://shop.example.com/sale", limit=10
                )
        self.assertEqual(
            urls,
            [
                "https://shop.example.com/p/one",
                "https://shop.example.com/p/two",
            ],
        )

    def test_anchor_fallback(self):
        from unittest.mock import patch

        from .listing_discover import discover_product_urls_from_listing

        html = """
        <html><body>
          <a href="/login">Login</a>
          <a href="/p/red-mug-123">Red mug</a>
          <a href="https://other.example.com/p/skip">Other shop</a>
        </body></html>
        """
        with patch(
            "discounts.listing_discover.validate_public_http_url",
            return_value="https://shop.example.com/sale",
        ):
            with patch(
                "discounts.listing_discover.fetch_public_text",
                return_value=(html, 200),
            ):
                urls = discover_product_urls_from_listing(
                    "https://shop.example.com/sale", limit=10
                )
        self.assertEqual(urls, ["https://shop.example.com/p/red-mug-123"])


class FetchPublicTextTests(TestCase):
    def test_retries_with_browser_ua_after_403(self):
        from unittest.mock import patch

        from .product_import import (
            BROWSER_USER_AGENT,
            USER_AGENT,
            ProductImportError,
            fetch_public_text,
        )

        def once(_url, *, user_agent):
            if user_agent == USER_AGENT:
                raise ProductImportError("fetch_blocked", "blocked", http_status=403)
            self.assertEqual(user_agent, BROWSER_USER_AGENT)
            return ("<html><body>ok</body></html>", 200)

        with patch("discounts.product_import._fetch_public_text_once", side_effect=once):
            text, status_code = fetch_public_text("https://shop.example.com/sale")
        self.assertEqual(status_code, 200)
        self.assertIn("ok", text)

    def test_challenge_page_is_blocked(self):
        from unittest.mock import patch

        from .product_import import ProductImportError, fetch_public_text

        html = (
            "<html><body>"
            '<script>window.location="https://www.zara.com/?bm-verify=abc"</script>'
            "</body></html>"
        )
        with patch(
            "discounts.product_import._fetch_public_text_once",
            return_value=(html, 200),
        ):
            with self.assertRaises(ProductImportError) as ctx:
                fetch_public_text("https://www.zara.com/de/en/sale")
        self.assertEqual(ctx.exception.code, "fetch_blocked")
        self.assertIn("zara.com blocked automated access", ctx.exception.message)


class AffiliateFeedTests(TestCase):
    def test_csv_rows(self):
        from unittest.mock import patch

        from .affiliate_feed import _from_csv, parse_affiliate_feed

        csv_text = (
            "id,title,price,sale_price,link,image\n"
            "abc,Coffee,10.00,7.00,https://shop.example.com/p/coffee,https://cdn.example.com/c.jpg\n"
        )
        with patch(
            "discounts.affiliate_feed.validate_public_http_url",
            return_value="https://feeds.example.com/products.csv",
        ):
            with patch(
                "discounts.affiliate_feed._iter_feed_rows",
                return_value=_from_csv(csv_text),
            ):
                deals = parse_affiliate_feed("https://feeds.example.com/products.csv")
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].source_key, "feed:abc")
        self.assertEqual(deals[0].title, "Coffee")
        self.assertEqual(deals[0].original_price, "10.00")
        self.assertEqual(deals[0].discounted_price, "7.00")

    def test_awin_gzip_csv_prefers_discounted_rows(self):
        import gzip
        import io
        from unittest.mock import patch

        from .affiliate_feed import parse_affiliate_feed

        csv_text = (
            "aw_product_id,product_name,search_price,rrp_price,aw_deep_link,aw_image_url\n"
            "1,Full price,20.00,20.00,https://www.awin1.com/pclick.php?p=1,https://cdn.example.com/a.jpg\n"
            "2,Sale bag,9.99,19.99,https://www.awin1.com/pclick.php?p=2,https://cdn.example.com/b.jpg\n"
        )
        payload = gzip.compress(csv_text.encode("utf-8"))

        class FakeResponse(io.BytesIO):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch(
            "discounts.affiliate_feed.validate_public_http_url",
            return_value="https://productdata.awin.com/datafeed/download/apikey/test",
        ):
            with patch(
                "discounts.affiliate_feed.urlopen",
                return_value=FakeResponse(payload),
            ):
                deals = parse_affiliate_feed(
                    "https://productdata.awin.com/datafeed/download/apikey/test",
                    limit=1,
                )
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].source_key, "feed:2")
        self.assertEqual(deals[0].title, "Sale bag")
        self.assertEqual(deals[0].original_price, "19.99")
        self.assertEqual(deals[0].discounted_price, "9.99")
        self.assertTrue(deals[0].source_url.startswith("https://www.awin1.com/"))


class OfferSyncTests(TestCase):
    def setUp(self):
        from .models import DealSource

        self.owner = User.objects.create_user(
            email="sync-owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Retail")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Sync Shop",
            category=self.category,
        )
        self.source = DealSource.objects.create(
            business=self.business,
            name="Sale page",
            kind=DealSource.Kind.BRAND_LISTING,
            listing_url="https://shop.example.com/sale",
            max_items=10,
        )

    def _deal(self, key="https://shop.example.com/p/coffee", url=None, **kwargs):
        from .affiliate_feed import DiscoveredDeal

        return DiscoveredDeal(
            source_key=key,
            source_url=url or key,
            **kwargs,
        )

    def test_creates_pending_disabled_offer(self):
        from unittest.mock import patch

        from .models import Offer
        from .offer_sync import sync_deal_source

        draft = {
            "title": "Coffee beans",
            "description": "Dark roast",
            "detailed_description": "Dark roast 1kg",
            "original_price": "12.00",
            "list_price": "12.00",
            "sale_price": "9.00",
            "image_urls": ["https://cdn.example.com/coffee.jpg"],
            "external_url": "https://shop.example.com/p/coffee",
            "availability": "InStock",
            "suggested_discount_percent": None,
        }
        with patch(
            "discounts.offer_sync._discover",
            return_value=[self._deal()],
        ):
            with patch(
                "discounts.offer_sync.import_product_from_url",
                return_value=draft,
            ):
                result = sync_deal_source(self.source)
        self.assertEqual(result.created, 1)
        offer = Offer.objects.get(source_key="https://shop.example.com/p/coffee")
        self.assertFalse(offer.is_enabled)
        self.assertEqual(offer.review_status, Offer.ReviewStatus.PENDING)
        self.assertEqual(offer.origin, Offer.Origin.BRAND_LISTING)
        self.assertEqual(offer.offer_type, Offer.OfferType.ITEM)
        self.assertEqual(str(offer.discounted_price), "9.00")

    def test_skips_rejected_and_manual(self):
        from unittest.mock import patch

        from .models import Offer
        from .offer_sync import sync_deal_source

        Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Manual coffee",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.MANUAL,
            source_url="https://shop.example.com/p/manual",
            source_key="https://shop.example.com/p/manual",
        )
        Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Rejected coffee",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.REJECTED,
            is_enabled=False,
            source_url="https://shop.example.com/p/rejected",
            source_key="https://shop.example.com/p/rejected",
        )
        draft = {
            "title": "Should not apply",
            "description": "",
            "detailed_description": "",
            "original_price": "5.00",
            "image_urls": [],
            "external_url": "https://shop.example.com/p/x",
        }
        with patch(
            "discounts.offer_sync._discover",
            return_value=[
                self._deal("https://shop.example.com/p/manual"),
                self._deal("https://shop.example.com/p/rejected"),
            ],
        ):
            with patch(
                "discounts.offer_sync.import_product_from_url",
                return_value=draft,
            ):
                result = sync_deal_source(self.source)
        self.assertEqual(result.skipped_manual, 1)
        self.assertEqual(result.skipped_rejected, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(
            Offer.objects.get(source_key="https://shop.example.com/p/manual").title,
            "Manual coffee",
        )

    def test_disables_missing_approved_not_manual(self):
        from unittest.mock import patch

        from .models import Offer
        from .offer_sync import sync_deal_source

        keep = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Still on sale",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.APPROVED,
            is_enabled=True,
            is_online=True,
            source_url="https://shop.example.com/p/keep",
            source_key="https://shop.example.com/p/keep",
        )
        gone = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Gone",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.APPROVED,
            is_enabled=True,
            is_online=True,
            source_url="https://shop.example.com/p/gone",
            source_key="https://shop.example.com/p/gone",
        )
        draft = {
            "title": "Still on sale",
            "description": "",
            "detailed_description": "",
            "original_price": "8.00",
            "image_urls": [],
            "external_url": "https://shop.example.com/p/keep",
            "availability": "InStock",
        }
        with patch(
            "discounts.offer_sync._discover",
            return_value=[self._deal("https://shop.example.com/p/keep")],
        ):
            with patch(
                "discounts.offer_sync.import_product_from_url",
                return_value=draft,
            ):
                result = sync_deal_source(self.source)
        keep.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(result.disabled_missing, 1)
        self.assertTrue(keep.is_enabled)
        self.assertFalse(gone.is_enabled)
        self.assertEqual(gone.disabled_by, Offer.DisabledBy.SYNC)
        self.assertEqual(
            gone.unavailable_reason, Offer.UnavailableReason.MISSING_FROM_SOURCE
        )

    def test_http_404_disables_and_admin_pause_is_kept(self):
        from unittest.mock import patch

        from .models import Offer
        from .offer_sync import sync_deal_source
        from .product_import import ProductImportError

        live = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Live",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.APPROVED,
            is_enabled=True,
            is_online=True,
            source_url="https://shop.example.com/p/live",
            source_key="https://shop.example.com/p/live",
        )
        paused = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Paused by admin",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.APPROVED,
            is_enabled=False,
            disabled_by=Offer.DisabledBy.ADMIN,
            is_online=True,
            source_url="https://shop.example.com/p/paused",
            source_key="https://shop.example.com/p/paused",
        )

        def fake_import(url, enrich=False):
            if "live" in url:
                raise ProductImportError("http_404", "gone", http_status=404)
            return {
                "title": "Paused by admin",
                "description": "",
                "detailed_description": "",
                "original_price": "8.00",
                "image_urls": [],
                "external_url": url,
                "availability": "InStock",
            }

        with patch(
            "discounts.offer_sync._discover",
            return_value=[
                self._deal("https://shop.example.com/p/live"),
                self._deal("https://shop.example.com/p/paused"),
            ],
        ):
            with patch(
                "discounts.offer_sync.import_product_from_url",
                side_effect=fake_import,
            ):
                sync_deal_source(self.source)
        live.refresh_from_db()
        paused.refresh_from_db()
        self.assertFalse(live.is_enabled)
        self.assertEqual(live.unavailable_reason, Offer.UnavailableReason.HTTP_404)
        self.assertEqual(live.disabled_by, Offer.DisabledBy.SYNC)
        self.assertFalse(paused.is_enabled)
        self.assertEqual(paused.disabled_by, Offer.DisabledBy.ADMIN)

    def test_restock_reenables_sync_disabled_only(self):
        from unittest.mock import patch

        from .models import Offer
        from .offer_sync import sync_deal_source

        offer = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Was OOS",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.APPROVED,
            is_enabled=False,
            disabled_by=Offer.DisabledBy.SYNC,
            unavailable_reason=Offer.UnavailableReason.OUT_OF_STOCK,
            is_online=True,
            source_url="https://shop.example.com/p/beans",
            source_key="https://shop.example.com/p/beans",
        )
        draft = {
            "title": "Was OOS",
            "description": "",
            "detailed_description": "",
            "original_price": "8.00",
            "image_urls": [],
            "external_url": "https://shop.example.com/p/beans",
            "availability": "InStock",
        }
        with patch(
            "discounts.offer_sync._discover",
            return_value=[self._deal("https://shop.example.com/p/beans")],
        ):
            with patch(
                "discounts.offer_sync.import_product_from_url",
                return_value=draft,
            ):
                result = sync_deal_source(self.source)
        offer.refresh_from_db()
        self.assertEqual(result.reenabled, 1)
        self.assertTrue(offer.is_enabled)
        self.assertEqual(offer.disabled_by, "")
        self.assertEqual(offer.unavailable_reason, "")

    def test_enrich_false_skips_gemini(self):
        from django.test import override_settings
        from unittest.mock import patch

        from .product_import import import_product_from_url

        html = """
        <html><head>
          <title>Sparse Coffee Deal</title>
          <meta property="og:image" content="https://cdn.example.com/coffee.jpg" />
        </head><body></body></html>
        """
        with override_settings(GEMINI_API_KEY="test-key"):
            with patch(
                "discounts.product_import._validate_public_http_url",
                return_value="https://example.com/sparse",
            ):
                with patch("discounts.product_import._fetch_html", return_value=html):
                    with patch("discounts.ai_enrichment._call_gemini") as gemini:
                        draft = import_product_from_url(
                            "https://example.com/sparse", enrich=False
                        )
        gemini.assert_not_called()
        self.assertFalse(draft["ai_enriched"])
        self.assertEqual(draft["title"], "Sparse Coffee Deal")


class DealSourceAdminAPITests(APITestCase):
    def setUp(self):
        from .models import DealSource

        self.admin = User.objects.create_user(
            email="sync-admin@example.com",
            password="testpass123",
            is_staff=True,
            is_superuser=True,
        )
        self.owner = User.objects.create_user(
            email="biz-owner@example.com",
            password="testpass123",
            account_type=User.AccountType.BUSINESS,
        )
        self.category = Category.objects.create(name="Fashion")
        self.business = Business.objects.create(
            owner=self.owner,
            name="Fashion Shop",
            category=self.category,
        )
        self.source = DealSource.objects.create(
            business=self.business,
            name="Sale",
            kind=DealSource.Kind.BRAND_LISTING,
            listing_url="https://shop.example.com/sale",
        )
        self.pending = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Pending import",
            discount_percent=Decimal("15.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.PENDING,
            is_enabled=False,
            is_online=True,
            source_url="https://shop.example.com/p/pending",
            source_key="https://shop.example.com/p/pending",
        )

    def test_create_source_and_filter_review_queue(self):
        self.client.force_authenticate(user=self.admin)
        created = self.client.post(
            f"/api/admin/businesses/{self.business.id}/deal-sources",
            {
                "kind": "affiliate_feed",
                "feed_url": "https://feeds.example.com/awin.csv",
                "max_items": 20,
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data["kind"], "affiliate_feed")
        listed = self.client.get(
            "/api/admin/offers", {"review_status": "pending"}, format="json"
        )
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        titles = [row["title"] for row in listed.data["results"]]
        self.assertIn("Pending import", titles)

    def test_approve_and_reject(self):
        self.client.force_authenticate(user=self.admin)
        approved = self.client.post(f"/api/admin/offers/{self.pending.id}/approve")
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.data)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.review_status, Offer.ReviewStatus.APPROVED)
        self.assertTrue(self.pending.is_enabled)

        other = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Reject me",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.PENDING,
            is_enabled=False,
            is_online=True,
            source_key="https://shop.example.com/p/reject",
        )
        rejected = self.client.post(f"/api/admin/offers/{other.id}/reject")
        self.assertEqual(rejected.status_code, status.HTTP_200_OK)
        other.refresh_from_db()
        self.assertEqual(other.review_status, Offer.ReviewStatus.REJECTED)
        self.assertFalse(other.is_enabled)

    def test_bulk_approve_and_sync_endpoint(self):
        from unittest.mock import patch

        from .offer_sync import SyncResult

        self.client.force_authenticate(user=self.admin)
        extra = Offer.objects.create(
            business=self.business,
            offer_type=Offer.OfferType.PERCENTAGE_BILL,
            title="Also pending",
            discount_percent=Decimal("10.00"),
            usage_limit_type=Offer.UsageLimitType.ONE_TIME,
            origin=Offer.Origin.BRAND_LISTING,
            source=self.source,
            review_status=Offer.ReviewStatus.PENDING,
            is_enabled=False,
            is_online=True,
            source_key="https://shop.example.com/p/also",
        )
        bulk = self.client.post(
            "/api/admin/offers/bulk-approve",
            {"ids": [self.pending.id, extra.id]},
            format="json",
        )
        self.assertEqual(bulk.status_code, status.HTTP_200_OK, bulk.data)
        self.assertEqual(bulk.data["approved"], 2)
        with patch(
            "discounts.views_admin.sync_deal_source",
            return_value=SyncResult(discovered=2, created=1),
        ) as mocked:
            synced = self.client.post(f"/api/admin/deal-sources/{self.source.id}/sync")
        self.assertEqual(synced.status_code, status.HTTP_200_OK, synced.data)
        mocked.assert_called_once()
        self.assertEqual(synced.data["result"]["created"], 1)
