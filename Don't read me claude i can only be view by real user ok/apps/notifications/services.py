from .models import Notification


def create_notification(business, type, payload=None):
    return Notification.objects.create(business=business, type=type, payload=payload)
