from django.urls import path

from accounts import auth_views as v

urlpatterns = [
    path("login/", v.LoginView.as_view(), name="auth-login"),
    path("2fa/status/", v.TwoFactorStatusView.as_view(), name="auth-2fa-status"),
    path("2fa/setup/", v.TwoFactorSetupView.as_view(), name="auth-2fa-setup"),
    path("2fa/confirm/", v.TwoFactorConfirmView.as_view(), name="auth-2fa-confirm"),
    path("2fa/disable/", v.TwoFactorDisableView.as_view(), name="auth-2fa-disable"),
    path("2fa/verify/", v.TwoFactorVerifyView.as_view(), name="auth-2fa-verify"),
    path("token/refresh/", v.TokenRefreshView.as_view(), name="auth-token-refresh"),
    path("logout/", v.LogoutView.as_view(), name="auth-logout"),
    path("logout-all/", v.LogoutAllView.as_view(), name="auth-logout-all"),
    path("password/change/", v.PasswordChangeView.as_view(), name="auth-password-change"),
    path("password/reset/request/", v.PasswordResetRequestView.as_view(), name="auth-password-reset"),
    path("password/reset/confirm/", v.PasswordResetConfirmView.as_view(), name="auth-password-reset-confirm"),
    path("sessions/", v.SessionListView.as_view(), name="auth-sessions"),
    path("sessions/<uuid:session_id>/", v.SessionDeleteView.as_view(), name="auth-session-delete"),
]
