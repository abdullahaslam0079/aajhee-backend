"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import TokenVerifyView

from discounts.views_auth import (
    ForgotPasswordAPIView,
    FirebaseAuthAPIView,
    LoginAPIView,
    LogoutAPIView,
    PhoneAuthAPIView,
    RegisterAPIView,
    ResetPasswordAPIView,
    ThrottledTokenRefreshView,
)
from discounts.views_health import HealthCheckAPIView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthCheckAPIView.as_view(), name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/auth/register", RegisterAPIView.as_view(), name="auth-register"),
    path("api/auth/token", LoginAPIView.as_view(), name="auth-token"),
    path("api/auth/phone", PhoneAuthAPIView.as_view(), name="auth-phone"),
    path("api/auth/firebase", FirebaseAuthAPIView.as_view(), name="auth-firebase"),
    path("api/auth/logout", LogoutAPIView.as_view(), name="auth-logout"),
    path(
        "api/auth/token/refresh",
        ThrottledTokenRefreshView.as_view(),
        name="auth-token-refresh",
    ),
    path("api/auth/token/verify", TokenVerifyView.as_view(), name="auth-token-verify"),
    path(
        "api/auth/password/forgot",
        ForgotPasswordAPIView.as_view(),
        name="auth-password-forgot",
    ),
    path(
        "api/auth/password/reset",
        ResetPasswordAPIView.as_view(),
        name="auth-password-reset",
    ),
    path("api/", include("discounts.urls")),
]


def _uses_local_media_storage() -> bool:
    return (
        settings.STORAGES["default"]["BACKEND"]
        == "django.core.files.storage.FileSystemStorage"
    )


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif _uses_local_media_storage():
    # Render runs with DEBUG=False; uploaded media is served from the local disk.
    urlpatterns += [
        re_path(
            r"^media/(?P<path>.*)$",
            serve,
            {"document_root": settings.MEDIA_ROOT},
        ),
    ]
