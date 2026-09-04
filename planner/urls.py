from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"), path("register/", views.register, name="register"),
    path("dashboard/", views.dashboard, name="dashboard"), path("media/add/", views.add_media, name="add_media"),
    path("availability/add/", views.add_availability, name="add_availability"),
    path("commitments/add/", views.add_commitment, name="add_commitment"), path("schedule/build/", views.build_schedule, name="build_schedule"),
]
