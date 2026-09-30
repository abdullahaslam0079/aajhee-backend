from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .auth_utils import blacklist_user_tokens, logout_response_message
from .password_reset import request_password_reset
from .phone_auth import (
    get_or_create_consumer_from_firebase_claims,
    verify_firebase_id_token,
)
from .serializers import (
    AddressSerializer,
    ForgotPasswordSerializer,
    LoginUserSerializer,
    PhoneAuthSerializer,
    LoginTokenObtainPairSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
)

User = get_user_model()


class AuthScopedThrottleMixin:
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class LoginAPIView(AuthScopedThrottleMixin, TokenObtainPairView):
    authentication_classes = []
    serializer_class = LoginTokenObtainPairSerializer


class ThrottledTokenRefreshView(AuthScopedThrottleMixin, TokenRefreshView):
    authentication_classes = []


class FirebaseAuthAPIView(AuthScopedThrottleMixin, APIView):
    """Exchange a Firebase Auth ID token (phone/Google/Apple) for Django JWTs."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PhoneAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            claims = verify_firebase_id_token(serializer.validated_data["id_token"])
            user = get_or_create_consumer_from_firebase_claims(claims)
        except AuthenticationFailed as exc:
            return Response(
                {
                    "message": str(exc.detail),
                    "errors": {"id_token": [str(exc.detail)]},
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {
                    "message": "This account is inactive.",
                    "errors": {"detail": ["This account is inactive."]},
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": LoginUserSerializer(user).data,
                "addresses": AddressSerializer(
                    user.addresses.order_by("-is_default", "id"), many=True
                ).data,
            },
            status=status.HTTP_200_OK,
        )


# Backwards-compatible alias for phone clients.
PhoneAuthAPIView = FirebaseAuthAPIView


class LogoutAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.account_type != User.AccountType.CONSUMER:
            return Response(
                {
                    "message": "Consumer account required.",
                    "errors": {"detail": ["Consumer account required."]},
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = request.data.get("refresh")
        try:
            blacklist_user_tokens(request.user, refresh=refresh or None)
        except TokenError:
            return Response(
                {
                    "message": "Invalid or expired token.",
                    "errors": {"refresh": ["Invalid or expired token."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": logout_response_message(refresh),
                "errors": {},
            },
            status=status.HTTP_200_OK,
        )


class RegisterAPIView(AuthScopedThrottleMixin, generics.CreateAPIView):
    """Create a user account."""

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"message": "Account created successfully.", "errors": {}},
            status=status.HTTP_201_CREATED,
        )


class ForgotPasswordAPIView(AuthScopedThrottleMixin, generics.GenericAPIView):
    serializer_class = ForgotPasswordSerializer
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            message = request_password_reset(serializer.validated_data["email"])
        except Exception:
            return Response(
                {
                    "message": "Unable to send password reset email. Please try again later.",
                    "errors": {},
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"message": message, "errors": {}})


class ResetPasswordAPIView(AuthScopedThrottleMixin, generics.GenericAPIView):
    serializer_class = ResetPasswordSerializer
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"message": "Password reset successfully.", "errors": {}},
            status=status.HTTP_200_OK,
        )
