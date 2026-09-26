from django.urls import path

from accounts import views

urlpatterns = [
    path("login", views.login_page, name="login"),
    path("discord/login", views.discord_login, name="discord_login"),
    path("discord/callback", views.discord_callback, name="discord_callback"),
    path("logout", views.logout_view, name="logout"),
]
