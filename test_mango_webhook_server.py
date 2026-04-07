import json
import threading
import unittest
from unittest import mock

import requests

import mango_webhook_server as mango


def http_request(method, url, **kwargs):
    session = requests.Session()
    session.trust_env = False
    try:
        return session.request(method, url, **kwargs)
    finally:
        session.close()


def make_config(**overrides):
    data = {
        "db_enabled": True,
        "db_host": "127.0.0.1",
        "db_port": 5432,
        "db_name": "call_brief_ai_test",
        "db_user": "postgres",
        "db_password": "postgres",
        "db_sslmode": "disable",
        "db_connect_timeout_sec": 10,
        "telegram_bot_token": "123:test",
        "telegram_chat_id": "-1001",
        "telegram_message_thread_id": None,
        "telegram_proxy": "",
        "mango_enabled": True,
        "mango_http_host": "127.0.0.1",
        "mango_http_port": 8081,
        "mango_webhook_path": "/events/summary",
        "mango_api_key": "key",
        "mango_api_salt": "salt",
        "mango_allowed_ips": [],
        "mango_display_timezone": "Asia/Novosibirsk",
        "mango_retry_enabled": True,
        "mango_retry_interval_sec": 60,
        "mango_retry_batch_size": 20,
        "mango_log_payloads": False,
        "log_level": "INFO",
    }
    data.update(overrides)
    return mango.MangoConfig(**data)


def sample_payload(**overrides):
    payload = {
        "entry_id": "entry-001",
        "call_direction": 1,
        "entry_result": 0,
        "create_time": "2026-04-07T10:15:30+07:00",
        "end_time": "2026-04-07T10:16:00+07:00",
        "disconnect_reason": "1111",
        "line_number": "+73833880000",
        "from": {"number": "+79277105901"},
        "to": {"extension": "103", "number": "+73832112233"},
    }
    payload.update(overrides)
    return payload


class FakeStore:
    def __init__(self):
        self.records = {}

    def insert_missed_call(self, record):
        entry_id = record["entry_id"]
        if entry_id in self.records:
            return False
        stored = dict(record)
        stored["telegram_sent"] = False
        stored["telegram_message_id"] = None
        stored["telegram_retry_count"] = 0
        stored["telegram_error"] = None
        self.records[entry_id] = stored
        return True

    def mark_telegram_sent(self, entry_id, telegram_message_id):
        self.records[entry_id]["telegram_sent"] = True
        self.records[entry_id]["telegram_message_id"] = telegram_message_id
        self.records[entry_id]["telegram_error"] = None

    def mark_telegram_failed(self, entry_id, error_text):
        self.records[entry_id]["telegram_sent"] = False
        self.records[entry_id]["telegram_error"] = error_text
        self.records[entry_id]["telegram_retry_count"] += 1

    def list_pending_retries(self, retry_interval_sec, batch_size):
        pending = [
            dict(record)
            for record in self.records.values()
            if not record.get("telegram_sent")
        ]
        return pending[:batch_size]

    def get_missed_call(self, entry_id):
        return self.records.get(entry_id)


class SignTests(unittest.TestCase):
    def test_build_and_verify_sign(self):
        json_str = '{"entry_id":"123"}'
        sign = mango.build_mango_sign("key", json_str, "salt")

        self.assertTrue(mango.verify_mango_sign("key", json_str, "salt", sign))
        self.assertFalse(mango.verify_mango_sign("key", json_str, "salt", "bad"))


class ParsingTests(unittest.TestCase):
    def test_extract_json_payload_and_sign_from_json_request(self):
        json_str = '{"entry_id":"123"}'
        extracted_json, sign = mango.extract_json_payload_and_sign(
            "/events/summary?sign=abc",
            {"Content-Type": "application/json"},
            json_str.encode("utf-8"),
        )

        self.assertEqual(extracted_json, json_str)
        self.assertEqual(sign, "abc")

    def test_extract_json_payload_and_sign_from_form_request(self):
        body = (
            "json=%7B%22entry_id%22%3A%22123%22%7D"
            "&sign=form-sign"
        ).encode("utf-8")
        extracted_json, sign = mango.extract_json_payload_and_sign(
            "/events/summary",
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        )

        self.assertEqual(extracted_json, '{"entry_id":"123"}')
        self.assertEqual(sign, "form-sign")


class MessageFormattingTests(unittest.TestCase):
    def test_build_message_uses_extension_number_priority(self):
        cfg = make_config()
        record = mango.normalize_missed_call_payload(sample_payload())
        message = mango.build_missed_call_message(cfg, record)

        self.assertIn("‼️‼️‼️ ПРОПУЩЕННЫЙ ЗВОНОК", message)
        self.assertIn("Входящий (пропущенный) номер: +79277105901", message)
        self.assertIn("Вызываемый абонент: 103 / +73832112233", message)

    def test_build_message_falls_back_to_line_number(self):
        cfg = make_config()
        record = mango.normalize_missed_call_payload(
            sample_payload(to={}, line_number="+73833889999")
        )
        message = mango.build_missed_call_message(cfg, record)

        self.assertIn("Вызываемый абонент: +73833889999", message)


class ProcessSummaryTests(unittest.TestCase):
    def test_process_summary_event_saves_and_sends(self):
        cfg = make_config()
        store = FakeStore()
        sent_messages = []

        def sender(cfg_arg, text):
            sent_messages.append(text)
            return [{"ok": True, "result": {"message_id": 777}}]

        result = mango.process_summary_event(
            cfg,
            store,
            sample_payload(),
            telegram_sender=sender,
        )

        self.assertEqual(result.status, "sent")
        self.assertEqual(result.telegram_message_id, 777)
        self.assertEqual(len(sent_messages), 1)
        self.assertTrue(store.get_missed_call("entry-001")["telegram_sent"])

    def test_process_summary_event_skips_non_missed_call(self):
        cfg = make_config()
        store = FakeStore()

        result = mango.process_summary_event(
            cfg,
            store,
            sample_payload(entry_result=1),
            telegram_sender=lambda *_: [{"ok": True, "result": {"message_id": 1}}],
        )

        self.assertEqual(result.status, "skipped")
        self.assertIsNone(store.get_missed_call("entry-001"))

    def test_process_summary_event_deduplicates_entry_id(self):
        cfg = make_config()
        store = FakeStore()
        send_calls = []

        def sender(cfg_arg, text):
            send_calls.append(text)
            return [{"ok": True, "result": {"message_id": 5}}]

        first = mango.process_summary_event(
            cfg, store, sample_payload(), telegram_sender=sender
        )
        second = mango.process_summary_event(
            cfg, store, sample_payload(), telegram_sender=sender
        )

        self.assertEqual(first.status, "sent")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(len(send_calls), 1)

    def test_process_summary_event_marks_telegram_failure_for_retry(self):
        cfg = make_config()
        store = FakeStore()

        result = mango.process_summary_event(
            cfg,
            store,
            sample_payload(),
            telegram_sender=lambda *_: (_ for _ in ()).throw(RuntimeError("telegram down")),
        )

        self.assertEqual(result.status, "telegram_failed")
        record = store.get_missed_call("entry-001")
        self.assertFalse(record["telegram_sent"])
        self.assertEqual(record["telegram_retry_count"], 1)
        self.assertIn("telegram down", record["telegram_error"])

    def test_retry_pending_notifications_resends_failed_rows(self):
        cfg = make_config()
        store = FakeStore()
        mango.process_summary_event(
            cfg,
            store,
            sample_payload(),
            telegram_sender=lambda *_: (_ for _ in ()).throw(RuntimeError("telegram down")),
        )

        sent_count = mango.retry_pending_notifications(
            cfg,
            store,
            telegram_sender=lambda *_: [{"ok": True, "result": {"message_id": 909}}],
        )

        self.assertEqual(sent_count, 1)
        record = store.get_missed_call("entry-001")
        self.assertTrue(record["telegram_sent"])
        self.assertEqual(record["telegram_message_id"], 909)


class WebhookHandlerTests(unittest.TestCase):
    def start_server(self, cfg, store, sender):
        server = mango.MangoWebhookServer(
            (cfg.mango_http_host, 0),
            mango.MangoWebhookRequestHandler,
            cfg,
            store,
            sender,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return server

    def signed_url(self, server, payload, cfg, extra_query=""):
        json_str = json.dumps(payload, ensure_ascii=False)
        sign = mango.build_mango_sign(cfg.mango_api_key, json_str, cfg.mango_api_salt)
        suffix = f"?sign={sign}"
        if extra_query:
            suffix += f"&{extra_query}"
        return (
            f"http://127.0.0.1:{server.server_address[1]}"
            f"{cfg.mango_webhook_path}{suffix}"
        ), json_str

    def test_healthz(self):
        cfg = make_config()
        server = self.start_server(cfg, FakeStore(), lambda *_: [])

        response = http_request(
            "GET",
            f"http://127.0.0.1:{server.server_address[1]}/healthz",
            timeout=5,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_post_summary_saves_and_sends(self):
        cfg = make_config()
        store = FakeStore()
        sent = []
        server = self.start_server(
            cfg,
            store,
            lambda cfg_arg, text: sent.append(text)
            or [{"ok": True, "result": {"message_id": 555}}],
        )
        url, json_str = self.signed_url(server, sample_payload(), cfg)

        response = http_request(
            "POST",
            url,
            data=json_str.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        self.assertEqual(len(sent), 1)
        self.assertTrue(store.get_missed_call("entry-001")["telegram_sent"])

    def test_post_summary_returns_duplicate_on_second_webhook(self):
        cfg = make_config()
        store = FakeStore()
        send_count = {"value": 0}

        def sender(cfg_arg, text):
            send_count["value"] += 1
            return [{"ok": True, "result": {"message_id": 10}}]

        server = self.start_server(cfg, store, sender)
        url, json_str = self.signed_url(server, sample_payload(), cfg)

        first = http_request(
            "POST",
            url,
            data=json_str.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        second = http_request(
            "POST",
            url,
            data=json_str.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )

        self.assertEqual(first.json()["status"], "sent")
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(send_count["value"], 1)

    def test_post_summary_ignores_invalid_sign(self):
        cfg = make_config()
        store = FakeStore()
        server = self.start_server(cfg, store, lambda *_: [])
        json_str = json.dumps(sample_payload(), ensure_ascii=False)

        response = http_request(
            "POST",
            f"http://127.0.0.1:{server.server_address[1]}{cfg.mango_webhook_path}?sign=bad",
            data=json_str.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        self.assertEqual(response.json()["reason"], "invalid_sign")
        self.assertEqual(store.records, {})

    def test_ip_allowlist_helper(self):
        self.assertTrue(mango.is_allowed_ip("127.0.0.1", []))
        self.assertTrue(mango.is_allowed_ip("10.10.10.10", ["10.10.10.10"]))
        self.assertFalse(mango.is_allowed_ip("127.0.0.1", ["10.10.10.10"]))


class MangoStoreTests(unittest.TestCase):
    @mock.patch("mango_webhook_server.psycopg.connect")
    def test_initialize_creates_missed_calls_table(self, connect_mock):
        connect_cm = mock.MagicMock()
        connection = mock.MagicMock()
        cursor_cm = mock.MagicMock()
        cursor = mock.MagicMock()
        connect_mock.return_value = connect_cm
        connect_cm.__enter__.return_value = connection
        connection.cursor.return_value = cursor_cm
        cursor_cm.__enter__.return_value = cursor

        store = mango.MangoStore(make_config())
        store.initialize()

        executed_sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("CREATE TABLE IF NOT EXISTS missed_calls", executed_sql)
        self.assertIn("telegram_retry_count INTEGER NOT NULL DEFAULT 0", executed_sql)
        self.assertIn("CREATE TRIGGER trg_missed_calls_set_updated_at", executed_sql)


if __name__ == "__main__":
    unittest.main()
