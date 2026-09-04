"""Configuration loader with sensible defaults."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


DEFAULT_CONFIG = {
    "scan_days": 90,
    "max_senders": 200,
    "max_emails": 2000,
    "sensitive_keywords": [
        "bank", "chase", "wells", "fargo", "citi", "hsbc", "barclays",
        "irs", "hmrc", "tax", "gov", "court", "legal",
        "account", "statement", "receipt", "invoice", "billing",
        "paypal", "venmo", "stripe", "square", "cashapp",
        "security", "verify", "login", "alert", "password", "reset",
        "2fa", "two-factor", "authentication", "confirm", "suspicious",
        "doctor", "medical", "health", "pharmacy", "insurance",
        "university", "school", "transcript", "enrollment",
    ],
    "always_trust_senders": [],
    "never_trust_senders": [],
    "rate_limit_seconds": 1.0,
}


@dataclass
class Config:
    scan_days: int = 90
    max_senders: int = 200
    max_emails: int = 2000
    sensitive_keywords: List[str] = field(default_factory=list)
    always_trust_senders: List[str] = field(default_factory=list)
    never_trust_senders: List[str] = field(default_factory=list)
    rate_limit_seconds: float = 1.0

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = dict(DEFAULT_CONFIG)
        if path:
            p = Path(path)
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                cfg.update(user_cfg)
        else:
            default_path = Path("config.json")
            if default_path.exists():
                with default_path.open("r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                cfg.update(user_cfg)
        return cls(
            scan_days=int(cfg["scan_days"]),
            max_senders=int(cfg["max_senders"]),
            max_emails=int(cfg["max_emails"]),
            sensitive_keywords=list(cfg["sensitive_keywords"]),
            always_trust_senders=list(cfg["always_trust_senders"]),
            never_trust_senders=list(cfg["never_trust_senders"]),
            rate_limit_seconds=float(cfg["rate_limit_seconds"]),
        )

    def trust(self, sender_email: str) -> None:
        email = sender_email.lower().strip()
        if email and email not in self.always_trust_senders:
            self.always_trust_senders.append(email)
            self._persist()

    def _persist(self) -> None:
        p = Path("config.json")
        try:
            with p.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "scan_days": self.scan_days,
                        "max_senders": self.max_senders,
                        "max_emails": self.max_emails,
                        "sensitive_keywords": self.sensitive_keywords,
                        "always_trust_senders": self.always_trust_senders,
                        "never_trust_senders": self.never_trust_senders,
                        "rate_limit_seconds": self.rate_limit_seconds,
                    },
                    f,
                    indent=2,
                )
        except OSError:
            pass
