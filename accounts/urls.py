from django.urls import path

from accounts import views as v

urlpatterns = [
    path("me/", v.MeView.as_view(), name="me"),
    path("me/preferences/", v.MePreferencesView.as_view(), name="me-preferences"),
    path("me/avatar/", v.MeAvatarView.as_view(), name="me-avatar"),
    path("users/", v.UserListCreateView.as_view(), name="user-list"),
    path("users/<uuid:user_id>/", v.UserDetailView.as_view(), name="user-detail"),
    path("users/<uuid:user_id>/invite/", v.UserInviteView.as_view(), name="user-invite"),
    path("users/<uuid:user_id>/suspend/", v.UserSuspendView.as_view(), name="user-suspend"),
    path("users/<uuid:user_id>/roles/", v.UserRolesView.as_view(), name="user-roles"),
    path("users/<uuid:user_id>/service-access/", v.UserServiceAccessView.as_view(),
         name="user-service-access"),
    path("users/<uuid:user_id>/sla/", v.UserSlaView.as_view(), name="user-sla"),
    path("roles/", v.RoleListView.as_view(), name="role-list"),
]
