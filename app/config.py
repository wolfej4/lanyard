"""All settings come from environment variables so the same image runs anywhere."""
import os
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.site_name = os.getenv("SITE_NAME", "Lanyard")
        self.base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./data/lanyard.db")
        self.secret_key = os.getenv("SECRET_KEY", "")
        self.admin_emails = {
            e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
        }
        # Email
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = _int("SMTP_PORT", 587)
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_starttls = _bool("SMTP_STARTTLS", True)
        self.smtp_ssl = _bool("SMTP_SSL", False)
        self.mail_from = os.getenv("MAIL_FROM", f"{self.site_name} <noreply@localhost>")
        # Payments
        self.stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "")
        self.stripe_webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        self.currency = os.getenv("CURRENCY", "usd").lower()
        self.platform_fee_percent = float(os.getenv("PLATFORM_FEE_PERCENT", "0") or 0)
        self.dev_fake_payments = _bool("DEV_FAKE_PAYMENTS", False)
        # Limits
        self.free_capacity_limit = _int("FREE_CAPACITY_LIMIT", 20)
        self.max_capacity = _int("MAX_CAPACITY", 2000)
        self.checkout_hold_minutes = max(30, _int("CHECKOUT_HOLD_MINUTES", 30))
        self.announcement_emails_per_day = _int("ANNOUNCEMENT_EMAILS_PER_DAY", 5)
        self.run_worker = _bool("RUN_WORKER", True)
        # Legal pages
        self.operator_name = os.getenv("OPERATOR_NAME", self.site_name)
        self.contact_email = os.getenv("CONTACT_EMAIL", "")
        self.governing_law = os.getenv("GOVERNING_LAW", "")
        self.legal_updated = os.getenv("LEGAL_UPDATED", "October 4, 2026")
        self.min_age = _int("MIN_AGE", 13)

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def origin(self) -> str:
        p = urlparse(self.base_url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def payments_enabled(self) -> bool:
        return bool(self.stripe_secret_key) or self.dev_fake_payments

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgres")


settings = Settings()
