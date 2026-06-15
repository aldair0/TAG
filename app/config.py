from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import data_dir, env_file

# Default DB lives under TAG_HOME/data (absolute), so a frozen binary writes
# to a stable location regardless of the working directory it's launched from.
_DEFAULT_DB_URL = f"sqlite:///{(data_dir() / 'tag_inventory.db').as_posix()}"


class Settings(BaseSettings):
    # Resolve .env to an absolute path anchored beside the program, so a
    # frozen (PyInstaller) binary loads config regardless of the working
    # directory it was launched from. In dev this is just <repo>/.env.
    # os.environ still overrides the file (used by tests).
    model_config = SettingsConfigDict(
        env_file=str(env_file()),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(default=_DEFAULT_DB_URL, alias="DATABASE_URL")

    # --- Server bind + HTTPS (direct uvicorn TLS, no reverse proxy) ---
    # HTTPS is served by uvicorn itself using a self-signed cert that
    # auto-generates under TAG_HOME/certs on first run. Cert/key paths can be
    # overridden to use your own. Empty paths => the TAG_HOME defaults.
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    ssl_enabled: bool = Field(default=True, alias="SSL_ENABLED")
    ssl_certfile: str = Field(default="", alias="SSL_CERTFILE")
    ssl_keyfile: str = Field(default="", alias="SSL_KEYFILE")

    # --- Logging (rotating file logs under TAG_HOME/logs) ---
    log_max_bytes: int = Field(default=5_000_000, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=7, alias="LOG_BACKUP_COUNT")

    # --- Local backups (end-of-day, retain 2 weeks) ---
    backup_enabled: bool = Field(default=True, alias="BACKUP_ENABLED")
    backup_time: str = Field(default="23:30", alias="BACKUP_TIME")  # store-tz HH:MM
    backup_retention_days: int = Field(default=14, alias="BACKUP_RETENTION_DAYS")

    # --- Tech-support alerting (outbound email — wired in Wave 3) ---
    # The app is otherwise inbound-only; this is the one outbound channel so
    # it can report critical failures it can't otherwise surface.
    support_email: str = Field(default="alexander.smith319@protonmail.com", alias="SUPPORT_EMAIL")

    # --- Update check (inbound support: notify when a new build is released) ---
    # No network call until GITHUB_REPO is set (e.g. "owner/tag-inventory").
    # GITHUB_TOKEN is a scoped read-only token for a private repo's releases.
    update_check_enabled: bool = Field(default=True, alias="UPDATE_CHECK_ENABLED")
    update_check_interval_hours: int = Field(default=24, alias="UPDATE_CHECK_INTERVAL_HOURS")
    github_repo: str = Field(default="", alias="GITHUB_REPO")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    alert_smtp_host: str = Field(default="smtp.gmail.com", alias="ALERT_SMTP_HOST")
    alert_smtp_port: int = Field(default=587, alias="ALERT_SMTP_PORT")
    alert_smtp_username: str = Field(default="", alias="ALERT_SMTP_USERNAME")
    alert_smtp_password: str = Field(default="", alias="ALERT_SMTP_PASSWORD")

    shopify_shop_domain: str = Field(default="", alias="SHOPIFY_SHOP_DOMAIN")
    shopify_admin_api_token: str = Field(default="", alias="SHOPIFY_ADMIN_API_TOKEN")
    shopify_api_key: str = Field(default="", alias="SHOPIFY_API_KEY")
    shopify_api_secret: str = Field(default="", alias="SHOPIFY_API_SECRET")
    shopify_webhook_secret: str = Field(default="", alias="SHOPIFY_WEBHOOK_SECRET")

    ebay_env: str = Field(default="sandbox", alias="EBAY_ENV")
    ebay_app_id: str = Field(default="", alias="EBAY_APP_ID")
    ebay_cert_id: str = Field(default="", alias="EBAY_CERT_ID")
    ebay_dev_id: str = Field(default="", alias="EBAY_DEV_ID")
    ebay_user_refresh_token: str = Field(default="", alias="EBAY_USER_REFRESH_TOKEN")
    ebay_sandbox_username: str = Field(default="", alias="EBAY_SANDBOX_USERNAME")

    tcgplayer_pro_username: str = Field(default="", alias="TCGPLAYER_PRO_USERNAME")
    tcgplayer_pro_password: str = Field(default="", alias="TCGPLAYER_PRO_PASSWORD")

    # Sale-signal endpoint auth (A1). When set, POST /admin/sold-online/signal
    # requires a matching X-Signal-Token header. Empty = open (LAN-only,
    # backward-compatible) — set this if the shop LAN is not fully trusted.
    signal_token: str = Field(default="", alias="SIGNAL_TOKEN")

    # Seller identity on TCGPlayer's PUBLIC marketplace. These are NOT secrets —
    # the SellerKey is the value that appears in the public storefront URL /
    # mp-search-api filter (listingSearch.filters.term.sellerKey); SellerId is
    # the public numeric id. Defaulted for convenience; override via .env.
    tcgplayer_seller_id: int = Field(default=734972, alias="TCGPLAYER_SELLER_ID")
    tcgplayer_seller_key: str = Field(default="1d1b3bf6", alias="TCGPLAYER_SELLER_KEY")
    tcgplayer_seller_name: str = Field(default="Tag Collects", alias="TCGPLAYER_SELLER_NAME")

    # Portal-download timing knobs. ``login_wait_sec`` is how long the
    # browser stays open waiting for the user to authenticate before
    # giving up. ``download_timeout_sec`` is how long we wait for the
    # CSV to appear after clicking "Export From Live".
    tcgplayer_portal_login_wait_sec: int = Field(
        default=900, alias="TCGPLAYER_PORTAL_LOGIN_WAIT_SEC"
    )
    tcgplayer_portal_download_timeout_sec: int = Field(
        default=180, alias="TCGPLAYER_PORTAL_DOWNLOAD_TIMEOUT_SEC"
    )
    # For automated (headless) scheduler downloads: shorter wait since no
    # human can log in. If the session has expired the download fails and
    # the scheduler falls back to the existing CSV on disk.
    tcgplayer_portal_auto_login_wait_sec: int = Field(
        default=90, alias="TCGPLAYER_PORTAL_AUTO_LOGIN_WAIT_SEC"
    )

    # POS pricing parameters. Override via .env if rates change.
    pos_tax_rate: float = Field(default=0.07, alias="POS_TAX_RATE")
    pos_card_surcharge: float = Field(default=0.029, alias="POS_CARD_SURCHARGE")
    pos_cash_discount: float = Field(default=0.07, alias="POS_CASH_DISCOUNT")

    # Store hours used by the TCGPlayer auto-sync scheduler. The on/off
    # flag itself lives in app_setting (so it can be toggled at runtime).
    store_timezone: str = Field(default="America/New_York", alias="STORE_TIMEZONE")
    store_open_time: str = Field(default="12:00", alias="STORE_OPEN_TIME")
    store_close_time: str = Field(default="20:00", alias="STORE_CLOSE_TIME")
    store_open_days: str = Field(
        default="mon,tue,wed,thu,fri,sat,sun", alias="STORE_OPEN_DAYS"
    )
    tcgplayer_sync_interval_min: int = Field(
        default=30, alias="TCGPLAYER_SYNC_INTERVAL_MIN"
    )

    # --- Sale-notification email receiver (app/inbound_email) ---
    # An always-on IMAP IDLE connection to the store inbox that receives
    # "items have sold" emails from TCGPlayer. Each new mail is parsed and
    # matching inventory units are flagged "sold online" (POS-blocked until
    # the authoritative CSV sync decrements them, or staff dismisses).
    #
    # GMAIL_APP_PASSWORD is a 16-char Google App Password (requires 2-Step
    # Verification on the account). Not a regular account password.
    email_receiver_enabled: bool = Field(default=False, alias="EMAIL_RECEIVER_ENABLED")
    imap_host: str = Field(default="imap.gmail.com", alias="IMAP_HOST")
    imap_port: int = Field(default=993, alias="IMAP_PORT")
    imap_username: str = Field(default="", alias="IMAP_USERNAME")
    imap_app_password: str = Field(default="", alias="GMAIL_APP_PASSWORD")
    imap_mailbox: str = Field(default="INBOX", alias="IMAP_MAILBOX")
    # How long to hold one IDLE before refreshing the connection. Gmail
    # drops idle sockets at ~30 min; refresh comfortably under that.
    imap_idle_refresh_sec: int = Field(default=1500, alias="IMAP_IDLE_REFRESH_SEC")
    # Which inbox messages count as a sale notification. A message is acted
    # on only when its From header contains SALE_EMAIL_FROM *and* its
    # Subject contains SALE_EMAIL_SUBJECT_CONTAINS (both case-insensitive).
    # Defaults recognize TCGPlayer's "items have sold" emails. An empty
    # value disables that half of the check (matches anything).
    sale_email_from: str = Field(default="tcgplayer", alias="SALE_EMAIL_FROM")
    sale_email_subject_contains: str = Field(
        default="have sold", alias="SALE_EMAIL_SUBJECT_CONTAINS"
    )

    @property
    def sqlite_path(self) -> Path | None:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///")).resolve()
        return None


settings = Settings()
