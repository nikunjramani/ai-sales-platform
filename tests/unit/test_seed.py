import pytest
from pydantic import SecretStr

from sales_platform.platform.config import Settings
from sales_platform.seed import seed_demo_tenants


def test_production_seed_is_rejected_before_connecting() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        mailer="real_mailer",
        mail_from="no-reply@quotes.example",
        public_portal_url="https://quotes.example/portal/",
        public_base_url="https://quotes.example",
        chat_ip_key="0123456789abcdef0123456789abcdef",
        database_url=SecretStr("postgresql+psycopg://app:private@localhost/db"),
    )
    with pytest.raises(ValueError, match="disabled"):
        seed_demo_tenants(settings)
