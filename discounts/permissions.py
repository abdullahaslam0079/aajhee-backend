from rest_framework import permissions


class IsBusinessAccount(permissions.BasePermission):
    message = "Business account required."

    def has_permission(self, request, view):
        user = request.user
        return (
            user
            and user.is_authenticated
            and user.is_business_account
            and hasattr(user, "business_profile")
        )


class IsConsumerAccount(permissions.BasePermission):
    message = "Consumer account required."

    def has_permission(self, request, view):
        user = request.user
        return (
            user
            and user.is_authenticated
            and user.account_type == user.AccountType.CONSUMER
        )


class IsAdminAccount(permissions.BasePermission):
    message = "Admin account required."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)


class IsAdminOwner(permissions.BasePermission):
    """Owner-only mutations (verify, suspend, delete, bulk price, categories)."""

    message = "Owner admin role required."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff and user.is_admin_owner)


def admin_can_write_orders_reports(user) -> bool:
    """Owner and Support may mutate orders and customer reports."""
    return bool(user and user.is_authenticated and user.is_staff)


def admin_can_mutate_platform(user) -> bool:
    """Owner-only for verify/suspend/delete/price/bulk/categories."""
    return bool(user and user.is_authenticated and user.is_staff and user.is_admin_owner)


def deny_support_write(request) -> bool:
    """True when Support attempts a non-safe method outside orders/reports."""
    if request.method in permissions.SAFE_METHODS:
        return False
    user = request.user
    return bool(user and user.is_authenticated and user.is_staff and user.is_admin_support)
