"""Runtime settings, backed by the store.

Replaces the 1.x `Config` dataclass that read and rewrote config.json in
the working directory. The shape is kept familiar on purpose so the CLI
reads the same as before; what changed is where it persists to and that
`trust()` no longer rewrites a whole file to append one address.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import Store

DEFAULT_SENSITIVE_KEYWORDS = [
    "bank", "chase", "wells", "fargo", "citi", "hsbc", "barclays",
    "irs", "hmrc", "tax", "gov", "court", "legal",
    "account", "statement", "receipt", "invoice", "billing",
    "paypal", "venmo", "stripe", "square", "cashapp",
    "security", "verify", "login", "alert", "password", "reset",
    "2fa", "two-factor", "authentication", "confirm", "suspicious",
    "doctor", "medical", "health", "pharmacy", "insurance",
    "university", "school", "transcript", "enrollment",
    # Added in 2.0: financial and identity terms that showed up in real
    # mailboxes and were not covered.
    "payment", "card", "credit", "debit", "refund", "transfer", "wire",
    "mortgage", "loan", "pension", "hmrc", "revenue", "customs",
    "passport", "visa", "license", "licence", "prescription", "appointment",
]

DEFAULTS = {
    "scan_days": 90,
    "max_senders": 200,
    "max_emails": 2000,
    "rate_limit_seconds": 1.0,
    "bulk_only": True,
    "sensitive_keywords": DEFAULT_SENSITIVE_KEYWORDS,
    "never_trust_senders": [],
}


@dataclass
class Settings:
    store: Store
    scan_days: int = 90
    max_senders: int = 200
    max_emails: int = 2000
    rate_limit_seconds: float = 1.0
    bulk_only: bool = True
    sensitive_keywords: list[str] = field(default_factory=list)
    never_trust_senders: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, store: Store) -> "Settings":
        return cls(
            store=store,
            scan_days=int(store.get_setting("scan_days", DEFAULTS["scan_days"])),
            max_senders=int(store.get_setting("max_senders", DEFAULTS["max_senders"])),
            max_emails=int(store.get_setting("max_emails", DEFAULTS["max_emails"])),
            rate_limit_seconds=float(
                store.get_setting("rate_limit_seconds", DEFAULTS["rate_limit_seconds"])
            ),
            bulk_only=bool(store.get_setting("bulk_only", DEFAULTS["bulk_only"])),
            # Union rather than override: the keyword list is a safety floor,
            # not a preference. Senders are exempted individually via trust(),
            # so merging in newly added terms cannot silently un-protect anyone.
            sensitive_keywords=sorted(set(
                store.get_setting("sensitive_keywords", [])
            ) | set(DEFAULTS["sensitive_keywords"])),
            never_trust_senders=list(
                store.get_setting("never_trust_senders", DEFAULTS["never_trust_senders"])
            ),
        )

    def save(self) -> None:
        for key in ("scan_days", "max_senders", "max_emails", "rate_limit_seconds",
                    "bulk_only", "sensitive_keywords", "never_trust_senders"):
            self.store.set_setting(key, getattr(self, key))

    # --- trusted senders live in the senders table, not in settings -----

    @property
    def always_trust_senders(self) -> list[str]:
        return self.store.trusted_senders()

    def trust(self, sender_email: str, name: str = "") -> None:
        self.store.trust(sender_email, name)

    def is_trusted(self, sender_email: str) -> bool:
        return self.store.is_trusted(sender_email)
