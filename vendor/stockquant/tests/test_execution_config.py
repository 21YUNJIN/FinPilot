import os
import tempfile
import unittest
from pathlib import Path

from stockquant.execution.config import load_env_file, parse_env_file, validate_broker_config


def write_config(path: Path, *, simulated: int, mode: int = 0o600) -> None:
    path.write_text(
        "\n".join(
            [
                "STOCKQUANT_API_KEY=key",
                "STOCKQUANT_API_SECRET=secret",
                "STOCKQUANT_API_PASSPHRASE=passphrase",
                f"STOCKQUANT_OKX_SIMULATED={simulated}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(path, mode)


class ExecutionConfigTests(unittest.TestCase):
    def test_parse_and_load_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "execution.env"
            write_config(path, simulated=1)
            self.assertEqual(parse_env_file(str(path))["STOCKQUANT_OKX_SIMULATED"], "1")
            environ = {"STOCKQUANT_API_KEY": "already-set"}
            load_env_file(str(path), environ)
            self.assertEqual(environ["STOCKQUANT_API_KEY"], "already-set")
            self.assertEqual(environ["STOCKQUANT_API_SECRET"], "secret")

    def test_simulation_config_passes_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "execution.env"
            write_config(path, simulated=1)
            report = validate_broker_config(str(path), live=False, environ={})
            self.assertTrue(report["ok"])
            self.assertTrue(report["simulation"])
            self.assertEqual(report["warnings"], [])

    def test_simulation_marker_is_required_in_simulation_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "execution.env"
            write_config(path, simulated=0)
            report = validate_broker_config(str(path), live=False, environ={})
            self.assertFalse(report["ok"])
            self.assertIn("simulation mode marker", " ".join(report["errors"]))

    def test_live_mode_rejects_simulation_marker(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "execution.env"
            write_config(path, simulated=1)
            report = validate_broker_config(str(path), live=True, environ={})
            self.assertFalse(report["ok"])
            self.assertIn("simulation mode", " ".join(report["errors"]))

    def test_insecure_permissions_warn_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "execution.env"
            write_config(path, simulated=1, mode=0o644)
            report = validate_broker_config(str(path), live=False, environ={})
            self.assertTrue(report["ok"])
            self.assertTrue(report["warnings"])
            self.assertNotIn("secret", str(report))


if __name__ == "__main__":
    unittest.main()


class TushareTokenTests(unittest.TestCase):
    def test_resolve_precedence(self):
        from stockquant.execution.config import resolve_tushare_token

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tushare.env"
            path.write_text("TUSHARE_TOKEN=from_file\n", encoding="utf-8")
            # explicit wins
            self.assertEqual(resolve_tushare_token("explicit", str(path), environ={}), "explicit")
            # env wins over file
            self.assertEqual(
                resolve_tushare_token(None, str(path), environ={"TUSHARE_TOKEN": "from_env"}),
                "from_env",
            )
            # file fallback
            self.assertEqual(resolve_tushare_token(None, str(path), environ={}), "from_file")
            # nothing configured
            self.assertEqual(resolve_tushare_token(None, str(path), environ={"OTHER": "x"}), "from_file")
