"""TikTok failures. Messages are plain English and never contain cookie values."""


class TikTokError(Exception):
    """Something went wrong talking to TikTok."""


class SessionExpired(TikTokError):
    """TikTok no longer accepts the saved login."""


class RecipientNotFound(TikTokError):
    """The recipient's profile doesn't exist."""


class UiChanged(TikTokError):
    """An expected element wasn't found; TikTok probably changed its web UI."""
