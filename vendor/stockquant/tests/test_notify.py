import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stockquant.assistant import notify
from stockquant.assistant.notify import (
    NotifyConfig,
    dispatch,
    format_alert_text,
    load_notify_config,
    send_serverchan,
    send_wecom,
    sort_alerts,
)

ALERTS = [
    {"severity": "opportunity", "title": "000651.SZ 出现可入信号", "message": "现价 38.28"},
    {"severity": "critical", "title": "600309.SH 触发止损", "message": "建议尽快减仓"},
]


class _Resp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body


class NotifyConfigTests(unittest.TestCase):
    def test_load_from_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "notify.env"
            path.write_text(
                "STOCKQUANT_NOTIFY_MACOS=0\n"
                "STOCKQUANT_WECOM_WEBHOOK=https://example.com/hook?key=abc\n"
                "STOCKQUANT_SERVERCHAN_KEY=SCT123\n"
                "STOCKQUANT_SMTP_HOST=smtp.qq.com\n"
                "STOCKQUANT_SMTP_PORT=465\n"
                "STOCKQUANT_SMTP_USER=u@qq.com\n"
                "STOCKQUANT_SMTP_PASSWORD=secret\n"
                "STOCKQUANT_SMTP_TO=t@qq.com\n",
                encoding="utf-8",
            )
            cfg = load_notify_config(str(path))
        self.assertFalse(cfg.macos)
        self.assertEqual(cfg.smtp_port, 465)
        self.assertEqual(set(cfg.channels()), {"wecom", "serverchan", "email"})

    def test_missing_file_is_safe(self):
        cfg = load_notify_config("/nonexistent/path/notify.env")
        self.assertEqual(cfg.channels(), ["macos"])


class FormatTests(unittest.TestCase):
    def test_sorted_critical_first(self):
        ordered = sort_alerts(ALERTS)
        self.assertEqual(ordered[0]["severity"], "critical")

    def test_text_contains_all_alerts(self):
        text = format_alert_text(ALERTS)
        self.assertIn("600309.SH", text)
        self.assertIn("000651.SZ", text)
        self.assertIn("紧急", text)
        self.assertIn("机会", text)


class ChannelTests(unittest.TestCase):
    def test_wecom_success(self):
        ok = send_wecom("https://example.com/hook", "hello", opener=lambda req, timeout=10: _Resp('{"errcode":0,"errmsg":"ok"}'))
        self.assertTrue(ok)

    def test_wecom_failure_is_swallowed(self):
        def boom(req, timeout=10):
            raise RuntimeError("network down")
        self.assertFalse(send_wecom("https://example.com/hook", "hello", opener=boom))

    def test_serverchan_success(self):
        ok = send_serverchan("SCT123", "title", "body", opener=lambda req, timeout=10: _Resp('{"code":0}'))
        self.assertTrue(ok)

    @mock.patch.object(notify, "send_macos", return_value=True)
    @mock.patch.object(notify, "send_wecom", return_value=True)
    def test_dispatch_fans_out_to_configured_channels(self, _wecom, _macos):
        cfg = NotifyConfig(macos=True, wecom_webhook="https://example.com/hook")
        result = dispatch(ALERTS, cfg)
        self.assertEqual(result, {"macos": True, "wecom": True})

    @mock.patch.object(notify, "send_macos", return_value=True)
    def test_dispatch_empty_alerts_is_noop(self, _macos):
        self.assertEqual(dispatch([], NotifyConfig()), {})
        _macos.assert_not_called()


if __name__ == "__main__":
    unittest.main()
