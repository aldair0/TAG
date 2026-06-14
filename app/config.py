from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(
        default="sqlite:///./data/tag_inventory.db",
        alias="DATABASE_URL",
    )

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

    # Seller identity on TCGPlayer's public marketplace. Both IDs travel
    # together — SellerId is internal/numeric; SellerKey is what the
    # mp-search-api filters on (listingSearch.filters.term.sellerKey).
    # Pulled from the SellerFilter cookie when logged in as the seller.
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

    @property
    def sqlite_path(self) -> Path | None:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///")).resolve()
        return None


settings = Settings()
