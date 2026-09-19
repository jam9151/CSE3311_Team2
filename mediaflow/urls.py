from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import include, path

from planner.forms import LoginForm

urlpatterns = [
    path("admin/", admin.site.urls),
    # Ahead of auth.urls so our login form (no label colons) wins.
    path("accounts/login/", LoginView.as_view(authentication_form=LoginForm), name="login"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("planner.urls")),
]
