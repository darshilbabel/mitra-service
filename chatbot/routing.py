from django.urls import re_path
from .consumers.async_consumer import AsyncSocketConsumer


websocket_urlpatterns = [
    re_path(r"ws/common/$", AsyncSocketConsumer.as_asgi()),
]
