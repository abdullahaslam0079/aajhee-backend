from __future__ import annotations

from decimal import Decimal

from .models import Product


def apply_discount_percent(product: Product, percent: Decimal) -> Product:
    product.apply_percent_discount(Decimal(percent))
    product.save(update_fields=["discount_percent", "sale_price", "updated_at"])
    return product


def apply_sale_price(product: Product, sale_price: Decimal) -> Product:
    product.apply_sale_price(Decimal(sale_price))
    product.save(update_fields=["discount_percent", "sale_price", "updated_at"])
    return product


def clear_discount(product: Product) -> Product:
    product.discount_percent = None
    product.sale_price = None
    product.save(update_fields=["discount_percent", "sale_price", "updated_at"])
    return product


def bulk_apply_percent(
    products, percent: Decimal
) -> int:
    updated = 0
    for product in products:
        apply_discount_percent(product, percent)
        updated += 1
    return updated
