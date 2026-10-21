"""Session state model (L2)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionState:
    """Cookies and CSRF-ish tokens required by Facebook requests.

    ``cookies`` holds raw name -> value pairs; ``fb_dtsg`` / ``lsd`` are
    extracted from page HTML and are required for GraphQL POSTs.
    """

    cookies: dict[str, str] = field(default_factory=dict)
    fb_dtsg: str | None = None
    lsd: str | None = None
    user_agent: str | None = None

    @property
    def user_id(self) -> str | None:
        return self.cookies.get("c_user")

    @property
    def impersonator_id(self) -> str | None:
        """The personal id behind ``c_user`` while acting as another identity.

        Facebook hands out ``i_user`` (the admin's own id) alongside ``c_user``
        (the Page id) once a session is switched into a Page.
        """
        return self.cookies.get("i_user")

    @property
    def acting_as_page(self) -> bool:
        """Whether the session is switched into a Page identity.

        Pages behave differently in the UI: they get a page inbox instead of
        Messenger, and Facebook does not render the profile "Message" button
        for them (a Page cannot start a conversation with a stranger).
        """
        return bool(self.impersonator_id)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.cookies.get("c_user") and self.cookies.get("xs"))

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def as_headers(self) -> dict[str, str]:
        """Headers that carry the session into a plain HTTP request."""
        headers: dict[str, str] = {}
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        return headers

    def merge_tokens(self, other: SessionState) -> None:
        """Fill in learning from a page fetch (tokens, discovered cookies)."""
        if other.fb_dtsg:
            self.fb_dtsg = other.fb_dtsg
        if other.lsd:
            self.lsd = other.lsd
        for key, value in other.cookies.items():
            self.cookies.setdefault(key, value)
