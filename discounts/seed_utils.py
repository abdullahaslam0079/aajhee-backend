"""Demo seed payloads and placeholder image helpers for local testing."""

from __future__ import annotations

import hashlib
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageDraw, ImageFont

# Shared password for admin, merchants, and consumers in seed data.
DEMO_PASSWORD = "Bscs@0079"
ADMIN_EMAIL = "abdullah@gmail.com"

# Canonical root categories (slug → name); matches migration 0032.
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

# Merchants / product catalogs. Coordinates are Lahore-area.
LIVE_BUSINESSES = [
    {
        "slug": "greenbasket",
        "name": "GreenBasket",
        "email": "merchant.greenbasket@aajhee.test",
        "category_slug": "grocery-food",
        "phone": "+923001111001",
        "instagram_url": "https://instagram.com/greenbasket.demo",
        "presence_mode": "hybrid",
        "branches": [
            {
                "name": "Johar Town",
                "street": "Rashid Minhas Road",
                "house_number": "66",
                "postal_code": "54782",
                "city": "Lahore",
                "lat": "31.469700",
                "lng": "74.272800",
                "whatsapp": "+923001111011",
                "same_day_areas": "Johar Town, DHA, Gulberg, Model Town",
            },
            {
                "name": "Gulberg",
                "street": "Main Boulevard",
                "house_number": "12",
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
                "name": "Organic Fruit Box",
                "description": "Seasonal organic fruits, weekly box.",
                "base_price": "2500.00",
                "sale_price": "1750.00",
                "stock": 40,
            },
            {
                "name": "Fresh Milk 1L",
                "description": "Farm-fresh full cream milk.",
                "base_price": "320.00",
                "sale_price": None,
                "stock": 120,
            },
            {
                "name": "Basmati Rice 5kg",
                "description": "Premium aged basmati.",
                "base_price": "2100.00",
                "sale_price": "1890.00",
                "stock": 55,
            },
            {
                "name": "Chicken Breast 1kg",
                "description": "Boneless chicken breast.",
                "base_price": "980.00",
                "sale_price": "880.00",
                "stock": 30,
            },
        ],
    },
    {
        "slug": "techhive",
        "name": "TechHive",
        "email": "merchant.techhive@aajhee.test",
        "category_slug": "electronics",
        "phone": "+923001111002",
        "instagram_url": "https://instagram.com/techhive.demo",
        "presence_mode": "hybrid",
        "branches": [
            {
                "name": "Hafeez Center",
                "street": "Main Boulevard Gulberg",
                "house_number": "45",
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
                "name": "Wireless Earbuds Pro",
                "description": "Noise-cancelling earbuds with charging case.",
                "base_price": "8999.00",
                "sale_price": "6499.00",
                "stock": 25,
            },
            {
                "name": "USB-C Fast Charger 65W",
                "description": "GaN charger for laptop and phone.",
                "base_price": "4500.00",
                "sale_price": "3799.00",
                "stock": 60,
            },
            {
                "name": "Smart Watch Lite",
                "description": "Fitness tracking smartwatch.",
                "base_price": "12500.00",
                "sale_price": None,
                "stock": 15,
            },
            {
                "name": "Phone Stand Aluminum",
                "description": "Adjustable desk phone stand.",
                "base_price": "1499.00",
                "sale_price": "999.00",
                "stock": 80,
            },
        ],
    },
    {
        "slug": "style-studio",
        "name": "Style Studio",
        "email": "merchant.stylestudio@aajhee.test",
        "category_slug": "fashion",
        "phone": "+923001111003",
        "instagram_url": "https://instagram.com/stylestudio.demo",
        "presence_mode": "hybrid",
        "branches": [
            {
                "name": "Packages Mall",
                "street": "Walton Road",
                "house_number": "1",
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
                "name": "Cotton Kurta Set",
                "description": "Unstitched cotton kurta with trousers.",
                "base_price": "4500.00",
                "sale_price": "3375.00",
                "stock": 35,
            },
            {
                "name": "Denim Jacket",
                "description": "Classic blue denim jacket.",
                "base_price": "6200.00",
                "sale_price": "4999.00",
                "stock": 20,
            },
            {
                "name": "Leather Belt",
                "description": "Genuine leather belt.",
                "base_price": "2200.00",
                "sale_price": None,
                "stock": 45,
            },
        ],
    },
    {
        "slug": "cozynest",
        "name": "CozyNest",
        "email": "merchant.cozynest@aajhee.test",
        "category_slug": "home-living",
        "phone": "+923001111004",
        "instagram_url": "https://instagram.com/cozynest.demo",
        "presence_mode": "hybrid",
        "branches": [
            {
                "name": "Bahria Town",
                "street": "Commercial Avenue",
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
                "name": "Luxury Cushion Set",
                "description": "Set of 4 decorative cushions.",
                "base_price": "5000.00",
                "sale_price": "3000.00",
                "stock": 22,
            },
            {
                "name": "Ceramic Dinner Set",
                "description": "12-piece ceramic dinner set.",
                "base_price": "8900.00",
                "sale_price": "7120.00",
                "stock": 12,
            },
            {
                "name": "LED Desk Lamp",
                "description": "Dimmable LED study lamp.",
                "base_price": "3200.00",
                "sale_price": None,
                "stock": 40,
            },
        ],
    },
    {
        "slug": "glamour-box",
        "name": "Glamour Box",
        "email": "merchant.glamourbox@aajhee.test",
        "category_slug": "beauty",
        "phone": "+923001111005",
        "instagram_url": "https://instagram.com/glamourbox.demo",
        "presence_mode": "hybrid",
        "branches": [
            {
                "name": "MM Alam Road",
                "street": "MM Alam Road",
                "house_number": "55",
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
                "name": "Skincare Starter Kit",
                "description": "Cleanser, toner, moisturizer trio.",
                "base_price": "8000.00",
                "sale_price": "5000.00",
                "stock": 28,
            },
            {
                "name": "Matte Lipstick Set",
                "description": "3-shade matte lipstick pack.",
                "base_price": "3500.00",
                "sale_price": "2799.00",
                "stock": 50,
            },
            {
                "name": "Hair Serum 50ml",
                "description": "Argan oil hair serum.",
                "base_price": "1800.00",
                "sale_price": None,
                "stock": 70,
            },
        ],
    },
    {
        "slug": "gift-haven",
        "name": "Gift Haven",
        "email": "merchant.gifthaven@aajhee.test",
        "category_slug": "gifts",
        "phone": "+923001111006",
        "instagram_url": "https://instagram.com/gifthaven.demo",
        "presence_mode": "hybrid",
        "branches": [
            {
                "name": "Emporium Mall",
                "street": "Jail Road",
                "house_number": "8",
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
                "name": "Personalized Mug",
                "description": "Custom printed ceramic mug.",
                "base_price": "1200.00",
                "sale_price": "899.00",
                "stock": 100,
            },
            {
                "name": "Gift Hamper Classic",
                "description": "Chocolates, candles, and a card.",
                "base_price": "4500.00",
                "sale_price": "3799.00",
                "stock": 18,
            },
            {
                "name": "Photo Frame Wood",
                "description": "A4 wooden photo frame.",
                "base_price": "1600.00",
                "sale_price": None,
                "stock": 40,
            },
        ],
    },
    {
        "slug": "medcare-plus",
        "name": "MedCare Plus",
        "email": "merchant.medcare@aajhee.test",
        "category_slug": "health",
        "phone": "+923001111007",
        "instagram_url": "https://instagram.com/medcare.demo",
        "presence_mode": "instore_only",
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
                "name": "Vitamin C Pack (30)",
                "description": "Daily vitamin C tablets.",
                "base_price": "950.00",
                "sale_price": "799.00",
                "stock": 90,
            },
            {
                "name": "Digital Thermometer",
                "description": "Fast-read digital thermometer.",
                "base_price": "1400.00",
                "sale_price": None,
                "stock": 35,
            },
            {
                "name": "First Aid Kit",
                "description": "Home first-aid essentials.",
                "base_price": "2800.00",
                "sale_price": "2399.00",
                "stock": 20,
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
            "street": "Street 12, Block B",
            "house_number": "42",
            "postal_code": "54792",
            "city": "Lahore",
            "county": "Punjab",
            "latitude": "31.469200",
            "longitude": "74.410500",
            "landmark": "Near Y Block Market",
            "delivery_instructions": "Call on arrival",
        },
    },
    {
        "email": "fatima@aajhee.test",
        "first_name": "Fatima",
        "last_name": "Khan",
        "phone": "+923001222002",
        "preferred_category_slugs": ["beauty", "fashion", "gifts"],
        "address": {
            "street": "MM Alam Road",
            "house_number": "88",
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
            "house_number": "15",
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


def _color_from_key(key: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return (
        64 + int(digest[0:2], 16) % 176,
        64 + int(digest[2:4], 16) % 176,
        64 + int(digest[4:6], 16) % 176,
    )


def _load_font(size: int):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def generate_seed_image(title: str, subtitle: str, filename: str) -> ContentFile:
    width, height = 800, 800
    background = _color_from_key(title)
    accent = _color_from_key(subtitle or title + "-accent")

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (40, 40, width - 40, height - 40),
        fill=accent,
        outline=(255, 255, 255),
        width=4,
    )
    draw.ellipse(
        (120, 120, width - 120, height - 120),
        fill=background,
        outline=(255, 255, 255),
        width=3,
    )

    title_font = _load_font(48)
    subtitle_font = _load_font(28)
    draw.multiline_text((80, 300), title, fill=(255, 255, 255), font=title_font, spacing=8)
    if subtitle:
        draw.text((80, 520), subtitle, fill=(240, 240, 240), font=subtitle_font)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return ContentFile(buffer.getvalue(), name=filename)
