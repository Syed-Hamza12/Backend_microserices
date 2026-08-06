from .models import JobTask


def enqueue(*, business, type, payload):
    return JobTask.objects.create(business=business, type=type, payload=payload)
