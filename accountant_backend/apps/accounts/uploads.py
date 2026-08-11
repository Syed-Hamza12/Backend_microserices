"""Shared, validated image-upload handling.

Both upload endpoints previously took the extension straight off the client's
filename (`Path(image.name).suffix`) and wrote it under MEDIA_ROOT. That let a
caller choose the stored extension: upload `x.html` or `x.svg`, and the file
comes back from the app's own domain as active content — stored XSS against
anyone who opens it, from an endpoint that only ever wanted a JPEG. There was
also no size limit and no check that the bytes were an image at all.
"""

import uuid
from pathlib import Path

from django.conf import settings
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

# Extension is chosen by us from the verified image format, never taken from the
# uploaded filename.
FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "HEIF": ".heic",
}

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Guards against decompression bombs: a small file that expands to a huge bitmap.
MAX_IMAGE_PIXELS = 50_000_000


def save_validated_image(uploaded_file, *, subdirectory):
    """Validates an uploaded image and saves it under MEDIA_ROOT/<subdirectory>.

    Returns the media-relative URL path. Raises ValidationError with a
    user-facing message if the upload isn't an acceptable image.
    """
    if uploaded_file.size > MAX_UPLOAD_BYTES:
        raise serializers.ValidationError(
            f"Image is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)."
        )

    # Verify by decoding the actual bytes, not by trusting the name or the
    # client-supplied content type.
    try:
        uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise serializers.ValidationError("That file isn't a readable image.")

    if image_format not in FORMAT_EXTENSIONS:
        raise serializers.ValidationError(
            f"Unsupported image format. Use one of: {', '.join(sorted(FORMAT_EXTENSIONS))}."
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise serializers.ValidationError("Image dimensions are too large.")

    destination_dir = Path(settings.MEDIA_ROOT) / subdirectory
    destination_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{FORMAT_EXTENSIONS[image_format]}"
    destination_path = destination_dir / filename

    uploaded_file.seek(0)
    with open(destination_path, "wb") as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)

    return f"{settings.MEDIA_URL}{subdirectory}/{filename}"


# Voice notes: extension/mime is chosen by us from a small allow-list of
# container signatures sniffed from the actual bytes, same reasoning as the
# image path above — never trust the client's filename or Content-Type.
# `record` (the Flutter package driving the new recording UI) produces M4A/AAC
# on Android and iOS; WAV/OGG are accepted too for other platforms/fallbacks.
AUDIO_MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _sniff_audio_format(head):
    """Identifies an audio container from its first bytes. Returns
    (extension, mime_type) or None if nothing recognized."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        # MPEG-4 container: M4A audio uses this box the same as MP4 video does,
        # there is no separate audio-only magic number.
        return ".m4a", "audio/mp4"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if len(head) >= 4 and head[0:4] == b"OggS":
        return ".ogg", "audio/ogg"
    if len(head) >= 4 and (head[0:3] == b"ID3" or head[0:2] in (b"\xff\xfb", b"\xff\xf3")):
        return ".mp3", "audio/mpeg"
    if len(head) >= 4 and head[0:4] == b"\x1a\x45\xdf\xa3":
        return ".webm", "audio/webm"
    return None


def save_validated_audio(uploaded_file, *, subdirectory):
    """Validates an uploaded voice-note recording and saves it under
    MEDIA_ROOT/<subdirectory>.

    Returns (media_relative_url, mime_type). Raises ValidationError with a
    user-facing message if the upload isn't an acceptable audio file.
    """
    if uploaded_file.size > AUDIO_MAX_UPLOAD_BYTES:
        raise serializers.ValidationError(
            f"Voice note is too large (limit {AUDIO_MAX_UPLOAD_BYTES // (1024 * 1024)}MB)."
        )

    uploaded_file.seek(0)
    head = uploaded_file.read(64)
    sniffed = _sniff_audio_format(head)
    if sniffed is None:
        raise serializers.ValidationError("That file isn't a readable voice note.")
    extension, mime_type = sniffed

    destination_dir = Path(settings.MEDIA_ROOT) / subdirectory
    destination_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{extension}"
    destination_path = destination_dir / filename

    uploaded_file.seek(0)
    with open(destination_path, "wb") as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)

    return f"{settings.MEDIA_URL}{subdirectory}/{filename}", mime_type
