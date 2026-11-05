"""Read-only access to the user's iCloud Photos library (used on Windows).

We only *read* the library: to find out which photos are already there
(so they are not sent again) and to confirm that what we sent actually
arrived. Uploading is done by Apple's own "iCloud for Windows" app.

Login uses pyicloud (https://github.com/timlaing/pyicloud), the same
sign-in as iCloud.com including two-factor authentication. The password
is never stored; the session is kept in <mappe>/.icloud-økt so the user
does not have to log in every time. If "Advanced Data Protection" is on,
Apple blocks this kind of access, and the user is told so.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterator

log = logging.getLogger("minsky")


class NeedLogin(Exception):
    """The iCloud session is missing or expired."""


class Asset:
    __slots__ = ("id", "filename", "size", "created")

    def __init__(self, id: str, filename: str, size: int | None, created=None):
        self.id, self.filename, self.size, self.created = id, filename, size, created


class ICloud:
    """Thin wrapper so the rest of the program does not depend on pyicloud details."""

    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.api = None
        self.apple_id: str | None = None
        self.needs_code = False
        self.guard = threading.Lock()
        self._meta = session_dir / "konto.json"

    # ---------------------------------------------------------------- login
    def _service(self, apple_id: str, password: str | None):
        from pyicloud import PyiCloudService  # imported lazily: only needed on Windows

        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.session_dir, 0o700)
        except OSError:
            pass
        return PyiCloudService(apple_id, password, cookie_directory=str(self.session_dir))

    def login(self, apple_id: str, password: str) -> dict:
        with self.guard:
            try:
                api = self._service(apple_id.strip(), password)
            except Exception as e:  # wrong password, locked account, network
                raise NeedLogin(_friendly(e)) from e
            self.api, self.apple_id = api, apple_id.strip()
            self.needs_code = bool(api.requires_2fa or getattr(api, "requires_2sa", False))
            self._save_meta()
            if not self.needs_code:
                self._check_photos_access()
            return self.state()

    def submit_code(self, code: str) -> dict:
        with self.guard:
            if not self.api:
                raise NeedLogin("Logg inn på nytt.")
            if not self.api.validate_2fa_code(code.strip().replace(" ", "")):
                raise NeedLogin("Feil kode. Prøv igjen.")
            try:
                self.api.trust_session()
            except Exception:
                pass
            self.needs_code = False
            self._check_photos_access()
            return self.state()

    def resume(self) -> bool:
        """Reuse a saved session without asking for the password again."""
        try:
            meta = json.loads(self._meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        try:
            api = self._service(meta["apple_id"], None)
        except Exception as e:
            log.info("iCloud-økten må fornyes: logg inn igjen (%s).", _friendly(e))
            return False
        if api.requires_2fa:
            return False
        self.api, self.apple_id, self.needs_code = api, meta["apple_id"], False
        return True

    def logout(self) -> None:
        with self.guard:
            self.api = None
            self.needs_code = False
            for p in self.session_dir.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass

    def _save_meta(self):
        self._meta.write_text(json.dumps({"apple_id": self.apple_id}), encoding="utf-8")

    def _check_photos_access(self):
        try:
            _ = self.api.photos.all  # noqa: B018 - touches the Photos service
        except Exception as e:
            msg = str(e)
            if "PCS" in msg or "Advanced Data Protection" in msg or "webaccess" in msg.lower():
                raise NeedLogin(
                    "Apple sperrer tilgangen fordi «Avansert databeskyttelse» er på, eller fordi "
                    "«Tilgang til iCloud-data på nettet» er slått av. Slå på «Tilgang til iCloud-data "
                    "på nettet» på iPhone: Innstillinger → [navnet ditt] → iCloud."
                ) from e
            raise NeedLogin(_friendly(e)) from e

    @property
    def ready(self) -> bool:
        return self.api is not None and not self.needs_code

    def state(self) -> dict:
        return {"logged_in": self.ready, "needs_code": self.needs_code, "apple_id": self.apple_id}

    # ------------------------------------------------------------- reading
    def scan(self) -> Iterator[Asset]:
        """Every photo and video in the iCloud library (newest first)."""
        if not self.ready:
            raise NeedLogin("Logg inn i iCloud for å fortsette.")
        try:
            for a in self.api.photos.all:
                try:
                    created = a.created  # capture time (UTC)
                except Exception:
                    created = None
                yield Asset(a.id, a.filename or "", a.size, created)
        except NeedLogin:
            raise
        except Exception as e:
            if "401" in str(e) or "421" in str(e) or "auth" in str(e).lower():
                self.api = None
                raise NeedLogin("iCloud-økten er utløpt. Logg inn igjen.") from e
            raise


def _friendly(e: Exception) -> str:
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if "invalid" in low and ("password" in low or "credentials" in low) or "401" in low:
        return "Feil Apple-ID eller passord."
    if "locked" in low:
        return "Apple-ID-en er låst. Lås den opp på iforgot.apple.com."
    if "connection" in low or "timed out" in low or "resolve" in low:
        return "Får ikke kontakt med iCloud. Sjekk internettforbindelsen."
    return s
