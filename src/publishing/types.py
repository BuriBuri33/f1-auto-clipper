"""Shared types for the publishing stage (spec sections 19-20).

Each platform module (``youtube``, ``instagram``) raises its own error
subclass on failure and returns its own small result dataclass on success —
no shared "Publisher" interface, because the two platforms' actual protocols
(resumable byte upload vs. fetch-from-URL-then-poll) have almost nothing in
common to abstract over. ``publishing.runner`` is where the two are treated
uniformly, as "did this clip reach this platform or not".
"""

from __future__ import annotations


class PublishError(Exception):
    """A publish attempt could not be completed. Retryable by default."""


class PublishAuthError(PublishError):
    """The credentials themselves are wrong — retrying without fixing them
    will fail identically, so this is classified separately from a
    transient network/server error."""
