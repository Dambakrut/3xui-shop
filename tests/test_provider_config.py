"""Actual environs/load_config checks; no gateway initialization or network."""
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

from test_runtime import dummy_env

AVAILABLE = importlib.util.find_spec("environs") is not None


@unittest.skipUnless(AVAILABLE, "Install locked config dependencies")
class ProviderConfigTests(unittest.TestCase):
    PROVIDERS = {
        "YOOKASSA": ("yookassa", {"YOOKASSA_TOKEN": ("TOKEN", "dummy-token"),
                                 "YOOKASSA_SHOP_ID": ("SHOP_ID", 12345)}),
        "YOOMONEY": ("yoomoney", {"YOOMONEY_WALLET_ID": ("WALLET_ID", "4100112345"),
                                 "YOOMONEY_NOTIFICATION_SECRET": ("NOTIFICATION_SECRET", "dummy-secret")}),
        "CRYPTOMUS": ("cryptomus", {"CRYPTOMUS_API_KEY": ("API_KEY", "dummy-key"),
                                   "CRYPTOMUS_MERCHANT_ID": ("MERCHANT_ID", "dummy-merchant")}),
        "HELEKET": ("heleket", {"HELEKET_API_KEY": ("API_KEY", "dummy-key"),
                               "HELEKET_MERCHANT_ID": ("MERCHANT_ID", "dummy-merchant")}),
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = dummy_env(directory.name)

    def load(self, env):
        from app.config import load_config
        with patch.dict(os.environ, env, clear=True), patch("environs.Env.read_env"):
            return load_config()

    def check_enabled(self, provider):
        from environs import EnvError
        section_name, fields = self.PROVIDERS[provider]
        env = dict(self.env)
        # Also prove successful use without Stars silently replacing this gateway.
        env["SHOP_PAYMENT_STARS_ENABLED"] = "false"
        env[f"SHOP_PAYMENT_{provider}_ENABLED"] = "true"
        env.update({name: str(value) for name, (_, value) in fields.items()})
        config = self.load(env)
        self.assertTrue(getattr(config.shop, f"PAYMENT_{provider}_ENABLED"))
        self.assertFalse(config.shop.PAYMENT_STARS_ENABLED)
        section = getattr(config, section_name)
        for name, (attribute, value) in fields.items():
            self.assertEqual(getattr(section, attribute), value)
            self.assertIs(type(getattr(section, attribute)), type(value))
            for invalid in (None, "", "   "):
                with self.subTest(provider=provider, field=name, invalid=invalid):
                    broken = dict(env)
                    if invalid is None:
                        broken.pop(name)
                    else:
                        broken[name] = invalid
                    with self.assertRaises((EnvError, ValueError)) as error:
                        self.load(broken)
                    self.assertIn(name, str(error.exception))

    def test_yookassa_enabled_credentials_and_required_values(self):
        self.check_enabled("YOOKASSA")

    def test_yoomoney_enabled_credentials_and_required_values(self):
        self.check_enabled("YOOMONEY")

    def test_cryptomus_enabled_credentials_and_required_values(self):
        self.check_enabled("CRYPTOMUS")

    def test_heleket_enabled_credentials_and_required_values(self):
        self.check_enabled("HELEKET")

    def test_yookassa_invalid_shop_id_fails(self):
        from environs import EnvError
        env = dict(self.env, SHOP_PAYMENT_YOOKASSA_ENABLED="true", YOOKASSA_TOKEN="dummy-token")
        for invalid in ("not-an-integer", "1.5", "0", "-1"):
            with self.subTest(shop_id=invalid):
                env["YOOKASSA_SHOP_ID"] = invalid
                with self.assertRaises(EnvError):
                    self.load(env)

    def test_each_disabled_provider_ignores_missing_empty_and_garbage_credentials(self):
        for provider, (section_name, fields) in self.PROVIDERS.items():
            for invalid in (None, "", "   ", "not-an-integer"):
                with self.subTest(provider=provider, invalid=invalid):
                    env = dict(self.env)
                    if invalid is not None:
                        env.update({name: invalid for name in fields})
                    config = self.load(env)
                    self.assertTrue(config.shop.PAYMENT_STARS_ENABLED)
                    self.assertFalse(getattr(config.shop, f"PAYMENT_{provider}_ENABLED"))
                    section = getattr(config, section_name)
                    for attribute, _ in fields.values():
                        self.assertIsNone(getattr(section, attribute))


if __name__ == "__main__":
    unittest.main()
