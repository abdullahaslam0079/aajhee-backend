"""Realistic Lahore demo catalog + remote image download for seeding."""

from __future__ import annotations

import urllib.error
import urllib.request
from io import BytesIO

from django.core.files.base import ContentFile

# Shared password for admin, merchants, and consumers in seed data.
DEMO_PASSWORD = "Bscs@0079"
ADMIN_EMAIL = "abdullah@gmail.com"

ROOT_CATEGORIES = [
    ("Grocery & Food", "grocery-food", 10),
    ("Electronics", "electronics", 20),
    ("Fashion", "fashion", 30),
    ("Home & Living", "home-living", 40),
    ("Beauty", "beauty", 50),
    ("Gifts", "gifts", 60),
    ("Health", "health", 70),
    ("Other", "other", 80),
]

DEFAULT_BUSINESS_HOURS = {
    "mon": {"open": "10:00", "close": "22:00", "closed": False},
    "tue": {"open": "10:00", "close": "22:00", "closed": False},
    "wed": {"open": "10:00", "close": "22:00", "closed": False},
    "thu": {"open": "10:00", "close": "22:00", "closed": False},
    "fri": {"open": "10:00", "close": "23:00", "closed": False},
    "sat": {"open": "11:00", "close": "23:00", "closed": False},
    "sun": {"open": "12:00", "close": "21:00", "closed": False},
}


def _img(photo_id: str) -> str:
    return (
        f"https://images.unsplash.com/{photo_id}"
        f"?auto=format&fit=crop&w=900&h=900&q=80&fm=jpg"
    )


# Merchants / product catalogs — Lahore market feel, real Unsplash photos.
LIVE_BUSINESSES = [
    {
        "slug": "greenbasket",
        "name": "Al-Noor Fresh Mart",
        "email": "merchant.greenbasket@aajhee.test",
        "category_slug": "grocery-food",
        "phone": "+923001111001",
        "instagram_url": "https://instagram.com/alnoorfresh",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1542838132-92c53300491e"),
        "branches": [
            {
                "name": "Johar Town",
                "street": "Abul Hassan Isphahani Road",
                "house_number": "Shop 14",
                "postal_code": "54782",
                "city": "Lahore",
                "lat": "31.469700",
                "lng": "74.272800",
                "whatsapp": "+923001111011",
                "same_day_areas": "Johar Town, DHA, Gulberg, Model Town",
            },
            {
                "name": "Gulberg III",
                "street": "Main Boulevard Gulberg",
                "house_number": "12-B",
                "postal_code": "54660",
                "city": "Lahore",
                "lat": "31.510500",
                "lng": "74.344200",
                "whatsapp": "+923001111012",
                "same_day_areas": "Gulberg, MM Alam, Liberty, Garden Town",
            },
        ],
        "products": [
            {
                "name": "Seasonal Fruit Tray (3kg)",
                "description": (
                    "Hand-picked seasonal mix — apples, bananas, oranges and "
                    "whatever is freshest at the sabzi mandi this morning."
                ),
                "base_price": "2200.00",
                "sale_price": "1890.00",
                "stock": 40,
                "image_url": _img("photo-1610832958506-aa56368176cf"),
            },
            {
                "name": "Olper's Full Cream Milk 1L",
                "description": "Chilled full cream milk, delivery same day if ordered before 6pm.",
                "base_price": "340.00",
                "sale_price": None,
                "stock": 120,
                "image_url": _img("photo-1550583724-b2692b85b150"),
            },
            {
                "name": "Super Kernel Basmati 5kg",
                "description": "Aged kernel basmati — long grain, fragrant, for daily daawat cooking.",
                "base_price": "2450.00",
                "sale_price": "2199.00",
                "stock": 55,
                "image_url": _img("photo-1536304993881-ff6e9eefa2a6"),
            },
            {
                "name": "Boneless Chicken Breast 1kg",
                "description": "Fresh boneless breast, cleaned and packed in-store. Keep refrigerated.",
                "base_price": "1050.00",
                "sale_price": "960.00",
                "stock": 30,
                "image_url": _img("photo-1604503468506-a8da13d82791"),
            },
        ],
    },
    {
        "slug": "techhive",
        "name": "Gadget Hub Gulberg",
        "email": "merchant.techhive@aajhee.test",
        "category_slug": "electronics",
        "phone": "+923001111002",
        "instagram_url": "https://instagram.com/gadgethub.lhr",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1518770660439-4636190af475"),
        "branches": [
            {
                "name": "Hafeez Center",
                "street": "Main Boulevard Gulberg",
                "house_number": "Plaza 45, Shop 208",
                "postal_code": "54660",
                "city": "Lahore",
                "lat": "31.510900",
                "lng": "74.342100",
                "whatsapp": "+923001111021",
                "same_day_areas": "Gulberg, DHA, Cantt, Model Town",
            }
        ],
        "products": [
            {
                "name": "Noise Cancelling Earbuds",
                "description": "Bluetooth 5.3 earbuds with charging case. 1-year local warranty.",
                "base_price": "8999.00",
                "sale_price": "6999.00",
                "stock": 25,
                "image_url": _img("photo-1590658268037-6bf12165a8df"),
            },
            {
                "name": "65W GaN Fast Charger",
                "description": "USB-C PD charger for MacBook, phones and tablets. Includes 1m cable.",
                "base_price": "4999.00",
                "sale_price": "3999.00",
                "stock": 60,
                "image_url": _img("photo-1583863788434-e58a36330cf0"),
            },
            {
                "name": "Fitness Smartwatch",
                "description": "Heart-rate, SpO2 and sleep tracking. Compatible with iOS & Android.",
                "base_price": "12999.00",
                "sale_price": None,
                "stock": 15,
                "image_url": _img("photo-1523275335684-37898b6baf30"),
            },
            {
                "name": "Aluminum Phone Stand",
                "description": "Adjustable desk stand — works with all phones and small tablets.",
                "base_price": "1499.00",
                "sale_price": "999.00",
                "stock": 80,
                "image_url": _img("photo-1601784551446-20c9e07cdbdb"),
            },
        ],
    },
    {
        "slug": "style-studio",
        "name": "Thread & Loom",
        "email": "merchant.stylestudio@aajhee.test",
        "category_slug": "fashion",
        "phone": "+923001111003",
        "instagram_url": "https://instagram.com/threadandloom.pk",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1441986300917-64674bd600d8"),
        "branches": [
            {
                "name": "Packages Mall",
                "street": "Walton Road",
                "house_number": "LG-22",
                "postal_code": "54760",
                "city": "Lahore",
                "lat": "31.474600",
                "lng": "74.355800",
                "whatsapp": "+923001111031",
                "same_day_areas": "Walton, DHA, Cantt, Garden Town",
            }
        ],
        "products": [
            {
                "name": "Cotton Pret Kurta (Men)",
                "description": "Breathable cotton pret kurta — sizes S–XXL. Washable at home.",
                "base_price": "4990.00",
                "sale_price": "3790.00",
                "stock": 35,
                "image_url": _img("photo-1594938298603-c8148c4dae35"),
            },
            {
                "name": "Classic Denim Jacket",
                "description": "Medium-wash denim jacket with brass buttons. Unisex fit.",
                "base_price": "7200.00",
                "sale_price": "5499.00",
                "stock": 20,
                "image_url": _img("photo-1544022613-e87ca75a784a"),
            },
            {
                "name": "Genuine Leather Belt",
                "description": "Full-grain leather belt with brushed metal buckle. Black / brown.",
                "base_price": "2490.00",
                "sale_price": None,
                "stock": 45,
                "image_url": _img("photo-1553062407-98eeb64c6a62"),
            },
        ],
    },
    {
        "slug": "cozynest",
        "name": "Ghar Aangan",
        "email": "merchant.cozynest@aajhee.test",
        "category_slug": "home-living",
        "phone": "+923001111004",
        "instagram_url": "https://instagram.com/gharaangan",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1555041469-a586c61ea9bc"),
        "branches": [
            {
                "name": "Bahria Town",
                "street": "Sector C Commercial",
                "house_number": "18",
                "postal_code": "53720",
                "city": "Lahore",
                "lat": "31.367500",
                "lng": "74.183200",
                "whatsapp": "+923001111041",
                "same_day_areas": "Bahria, Raiwind Road, Thokar",
            }
        ],
        "products": [
            {
                "name": "Velvet Cushion Covers (Set of 4)",
                "description": "Soft velvet covers with hidden zip — 16x16\". Inserts not included.",
                "base_price": "4800.00",
                "sale_price": "3499.00",
                "stock": 22,
                "image_url": _img("photo-1584100936595-c0654b55a2e2"),
            },
            {
                "name": "Ceramic Dinner Set (12 pcs)",
                "description": "Stoneware plates and bowls for 4 people. Microwave safe.",
                "base_price": "9200.00",
                "sale_price": "7499.00",
                "stock": 12,
                "image_url": _img("photo-1610701596007-11502861dcfa"),
            },
            {
                "name": "LED Study Lamp",
                "description": "Dimmable LED desk lamp with USB port. Warm & cool light modes.",
                "base_price": "3500.00",
                "sale_price": None,
                "stock": 40,
                "image_url": _img("photo-1507473885765-e6ed057f782c"),
            },
        ],
    },
    {
        "slug": "glamour-box",
        "name": "Noor Beauty Studio",
        "email": "merchant.glamourbox@aajhee.test",
        "category_slug": "beauty",
        "phone": "+923001111005",
        "instagram_url": "https://instagram.com/noorbeautystudio",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1596462502278-27bfdc403348"),
        "branches": [
            {
                "name": "MM Alam Road",
                "street": "MM Alam Road",
                "house_number": "55-A",
                "postal_code": "54000",
                "city": "Lahore",
                "lat": "31.520800",
                "lng": "74.351200",
                "whatsapp": "+923001111051",
                "same_day_areas": "Gulberg, MM Alam, Liberty, Faisal Town",
            }
        ],
        "products": [
            {
                "name": "Daily Skincare Trio",
                "description": "Gentle cleanser, hydrating toner and light moisturizer — for normal/combo skin.",
                "base_price": "7500.00",
                "sale_price": "5499.00",
                "stock": 28,
                "image_url": _img("photo-1556228578-0d85b1a4d571"),
            },
            {
                "name": "Matte Lipstick Trio",
                "description": "Three everyday shades — nude, berry and brick. Long-wear formula.",
                "base_price": "3900.00",
                "sale_price": "2999.00",
                "stock": 50,
                "image_url": _img("photo-1586495777744-4413f21062fa"),
            },
            {
                "name": "Argan Hair Serum 50ml",
                "description": "Lightweight serum for frizz control and shine. Safe for colour-treated hair.",
                "base_price": "2100.00",
                "sale_price": None,
                "stock": 70,
                "image_url": _img("photo-1571875257727-256c39da42af"),
            },
        ],
    },
    {
        "slug": "gift-haven",
        "name": "Surprise & Co.",
        "email": "merchant.gifthaven@aajhee.test",
        "category_slug": "gifts",
        "phone": "+923001111006",
        "instagram_url": "https://instagram.com/surpriseandco.pk",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1549465220-1a8b9238cd48"),
        "branches": [
            {
                "name": "Emporium Mall",
                "street": "Jail Road",
                "house_number": "2F-18",
                "postal_code": "54000",
                "city": "Lahore",
                "lat": "31.535600",
                "lng": "74.328900",
                "whatsapp": "+923001111061",
                "same_day_areas": "Jail Road, Gulberg, Shadman, Cantt",
            }
        ],
        "products": [
            {
                "name": "Custom Name Mug",
                "description": "Ceramic mug with custom name print. Ready in 24 hours for Lahore orders.",
                "base_price": "1400.00",
                "sale_price": "999.00",
                "stock": 100,
                "image_url": _img("photo-1514228742587-6b1558fcca3d"),
            },
            {
                "name": "Celebration Gift Hamper",
                "description": "Chocolates, scented candle, mini plant and a handwritten card.",
                "base_price": "5500.00",
                "sale_price": "4499.00",
                "stock": 18,
                "image_url": _img("photo-1513885535751-8b9238bd345a"),
            },
            {
                "name": "Wooden Photo Frame (A4)",
                "description": "Natural wood frame with glass front. Wall or desk stand included.",
                "base_price": "1800.00",
                "sale_price": None,
                "stock": 40,
                "image_url": _img("photo-1578301978693-85fa9c0320b9"),
            },
        ],
    },
    {
        "slug": "medcare-plus",
        "name": "CarePlus Pharmacy",
        "email": "merchant.medcare@aajhee.test",
        "category_slug": "health",
        "phone": "+923001111007",
        "instagram_url": "https://instagram.com/careplus.lhr",
        "presence_mode": "hybrid",
        "logo_url": _img("photo-1576602976047-174e57a47881"),
        "branches": [
            {
                "name": "DHA Phase 5",
                "street": "Y Block Market",
                "house_number": "3",
                "postal_code": "54792",
                "city": "Lahore",
                "lat": "31.472100",
                "lng": "74.409800",
                "whatsapp": "+923001111071",
                "same_day_areas": "DHA, Cantt, Bedian Road",
            }
        ],
        "products": [
            {
                "name": "Vitamin C 1000mg (30 tabs)",
                "description": "Daily vitamin C tablets. Imported, sealed pack. Price includes GST.",
                "base_price": "1150.00",
                "sale_price": "899.00",
                "stock": 90,
                "image_url": _img("photo-1550572017-edd951b55104"),
            },
            {
                "name": "Digital Thermometer",
                "description": "60-second reading, fever alert beep. Battery included.",
                "base_price": "1600.00",
                "sale_price": None,
                "stock": 35,
                "image_url": _img("photo-1584308666744-24d5c474f2ae"),
            },
            {
                "name": "Home First Aid Kit",
                "description": "Bandages, antiseptic, gauze, scissors and gloves in a hard case.",
                "base_price": "3200.00",
                "sale_price": "2699.00",
                "stock": 20,
                "image_url": _img("photo-1603398938378-e54eab446dde"),
            },
        ],
    },
]

CONSUMERS = [
    {
        "email": "consumer@aajhee.test",
        "first_name": "Ali",
        "last_name": "Raza",
        "phone": "+923001222001",
        "preferred_category_slugs": ["grocery-food", "electronics", "fashion"],
        "address": {
            "street": "Street 12, Block B, DHA Phase 5",
            "house_number": "42",
            "postal_code": "54792",
            "city": "Lahore",
            "county": "Punjab",
            "latitude": "31.469200",
            "longitude": "74.410500",
            "landmark": "Near Y Block Market",
            "delivery_instructions": "Call on arrival — gate code 4521",
        },
    },
    {
        "email": "fatima@aajhee.test",
        "first_name": "Fatima",
        "last_name": "Khan",
        "phone": "+923001222002",
        "preferred_category_slugs": ["beauty", "fashion", "gifts"],
        "address": {
            "street": "MM Alam Road, Gulberg III",
            "house_number": "House 88",
            "postal_code": "54000",
            "city": "Lahore",
            "county": "Punjab",
            "latitude": "31.521100",
            "longitude": "74.350800",
            "landmark": "Opposite Café Flo",
            "delivery_instructions": "Leave with guard if unavailable",
        },
    },
    {
        "email": "hassan@aajhee.test",
        "first_name": "Hassan",
        "last_name": "Ahmed",
        "phone": "+923001222003",
        "preferred_category_slugs": ["electronics", "home-living"],
        "address": {
            "street": "Main Boulevard Gulberg",
            "house_number": "15-C",
            "postal_code": "54660",
            "city": "Lahore",
            "county": "Punjab",
            "latitude": "31.509800",
            "longitude": "74.343500",
            "landmark": "Near Liberty Market",
            "delivery_instructions": "",
        },
    },
]


def download_image(url: str, filename: str, *, timeout: int = 30) -> ContentFile:
    """Download a remote image and normalize it to JPEG for mobile clients."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AajheeSeed/1.0)",
            "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to download image {url}: {exc}") from exc

    if not data:
        raise RuntimeError(f"Empty image response for {url}")

    try:
        from PIL import Image

        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        data = buffer.getvalue()
    except Exception as exc:
        raise RuntimeError(f"Failed to convert image {url}: {exc}") from exc

    stem = filename.rsplit(".", 1)[0]
    return ContentFile(data, name=f"{stem}.jpg")


def download_image_or_none(url: str, filename: str) -> ContentFile | None:
    if not url:
        return None
    try:
        return download_image(url, filename)
    except RuntimeError:
        return None
