import unittest
from pathlib import Path
from unittest import mock
import tempfile

import httpx
import callbot_daemon as daemon


def make_config(**overrides):
    data = {
        "ftp_enabled": True,
        "ftp_protocol": "sftp",
        "ftp_host": "example.com",
        "ftp_port": 22,
        "ftp_user": "user",
        "ftp_password": "pass",
        "ftp_encoding": "utf-8",
        "ftp_encoding_fallbacks": ["cp1251"],
        "ftp_remote_root": "/recordings",
        "ftp_remote_roots": ["/recordings"],
        "ftp_archive_dir": "/recordings/archive",
        "ftp_archive_dir_explicit": True,
        "ftp_delete_after_success": False,
        "ftp_move_to_archive_after_success": True,
        "ftp_use_tls": False,
        "ftp_timeout_sec": 60,
        "ftp_connect_attempts": 2,
        "ftp_retry_delay_sec": 5.0,
        "yandex_disk_enabled": False,
        "yandex_disk_oauth_token": "",
        "yandex_disk_timeout_sec": 120,
        "yandex_disk_remote_root": "disk:/recordings",
        "yandex_disk_remote_roots": [],
        "yandex_disk_archive_dir": "disk:/recordings/archive",
        "yandex_disk_archive_dir_explicit": True,
        "yandex_disk_delete_after_success": False,
        "yandex_disk_move_to_archive_after_success": False,
        "openai_api_key": "sk-test",
        "openai_base_url": "",
        "openai_proxy": "",
        "openai_timeout_sec": 600.0,
        "openai_connect_timeout_sec": 30.0,
        "openai_route_probe_timeout_sec": 15.0,
        "openai_route_probe_connect_timeout_sec": 5.0,
        "openai_request_attempts": 2,
        "openai_retry_delay_sec": 2.0,
        "openai_retry_backoff": 2.0,
        "openai_proxy_failure_cooldown_sec": 300.0,
        "openai_proxy_direct_fallback": False,
        "transcribe_model": "gpt-4o-transcribe-diarize",
        "transcribe_language": "ru",
        "transcribe_chunking_strategy": "auto",
        "analysis_model": "gpt-5-mini",
        "analysis_reasoning_effort": "",
        "analysis_store": False,
        "analysis_max_output_tokens": 1800,
        "instruction_json_path": Path("instructions.json"),
        "state_path": Path("state.json"),
        "work_root": Path("work"),
        "telegram_bot_token": "123:test",
        "telegram_chat_id": "-1001",
        "telegram_message_thread_id": None,
        "telegram_proxy": "",
        "poll_interval_sec": 60,
        "min_stable_polls": 2,
        "min_audio_bytes": 102400,
        "min_dialogue_words": 30,
        "min_duration_min": 0.5,
        "split_threshold_bytes": 4194304,
        "target_part_max_bytes": 4194304,
        "part_export_bitrate": "64k",
        "part_export_frame_rate": 16000,
        "part_export_channels": 1,
        "max_transcribe_bytes": 26214400,
    }
    data.update(overrides)
    if "ftp_remote_roots" not in overrides:
        data["ftp_remote_roots"] = [data["ftp_remote_root"]]
    return daemon.Config(**data)


class InspectResponseOutputTests(unittest.TestCase):
    def test_collects_output_text_and_metadata(self):
        response = {
            "id": "resp_123",
            "status": "completed",
            "output": [
                {"type": "reasoning", "id": "rs_123"},
                {
                    "type": "message",
                    "status": "completed",
                    "phase": "final_answer",
                    "content": [
                        {"type": "output_text", "text": " Итоговое сообщение "},
                    ],
                },
            ],
            "usage": {"output_tokens": 42},
        }

        info = daemon.inspect_response_output(response)

        self.assertEqual(info["text"], "Итоговое сообщение")
        self.assertEqual(info["response_id"], "resp_123")
        self.assertEqual(info["status"], "completed")
        self.assertEqual(info["output_types"], ["reasoning", "message"])
        self.assertEqual(info["message_statuses"], ["completed"])
        self.assertEqual(info["phases"], ["final_answer"])

    def test_collects_refusal_text(self):
        response = {
            "id": "resp_refusal",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [
                        {"type": "refusal", "refusal": "Не могу помочь с этим."},
                    ],
                }
            ],
        }

        info = daemon.inspect_response_output(response)

        self.assertEqual(info["text"], "")
        self.assertEqual(info["refusal"], "Не могу помочь с этим.")

    def test_extract_saved_telegram_message_uses_existing_analysis(self):
        transcript_doc = {
            "source": {"ftp_path_audio": "/recordings/call.mp3"},
            "analysis": {"telegram_message": "Готовое сообщение"},
            "telegram": {"sent": False},
        }

        message = daemon.extract_saved_telegram_message(
            transcript_doc,
            "/recordings/call.mp3",
        )

        self.assertEqual(message, "Готовое сообщение")

    def test_extract_saved_telegram_message_skips_already_sent(self):
        transcript_doc = {
            "source": {"ftp_path_audio": "/recordings/call.mp3"},
            "analysis": {"telegram_message": "Готовое сообщение"},
            "telegram": {"sent": True},
        }

        message = daemon.extract_saved_telegram_message(
            transcript_doc,
            "/recordings/call.mp3",
        )

        self.assertEqual(message, "")


class AnalysisRetryTests(unittest.TestCase):
    def test_builds_retry_settings_for_token_exhaustion(self):
        cfg = make_config(analysis_reasoning_effort="", analysis_max_output_tokens=1800)

        retry_settings = daemon.build_analysis_retry_settings(
            cfg,
            {
                "text": "",
                "refusal": "",
                "status": "incomplete",
                "incomplete_reason": "max_output_tokens",
            },
        )

        self.assertEqual(
            retry_settings,
            {
                "reasoning_effort": "low",
                "max_output_tokens": 3600,
            },
        )

    def test_retries_once_and_returns_text(self):
        cfg = make_config(analysis_reasoning_effort="", analysis_max_output_tokens=1800)
        responses = iter(
            [
                {
                    "id": "resp_1",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "id": "rs_1"}],
                },
                {
                    "id": "resp_2",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "Готовый итог"},
                            ],
                        }
                    ],
                },
            ]
        )

        with mock.patch(
            "callbot_daemon.run_openai_request",
            side_effect=lambda *args, **kwargs: next(responses),
        ) as mocked_run:
            result = daemon.analyze_transcript(
                client=mock.Mock(),
                instruction_text="Сделай короткую сводку",
                transcript_doc={"transcription": {"dialogue_text": "Привет"}},
                cfg=cfg,
            )

        self.assertEqual(result, "Готовый итог")
        self.assertEqual(mocked_run.call_count, 2)
        self.assertEqual(mocked_run.call_args_list[0].args[1], "analysis request")
        self.assertEqual(mocked_run.call_args_list[1].args[1], "analysis retry")

    def test_raises_helpful_error_on_refusal(self):
        cfg = make_config()

        with mock.patch(
            "callbot_daemon.run_openai_request",
            return_value={
                "id": "resp_refusal",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [
                            {"type": "refusal", "refusal": "Отказываюсь отвечать."},
                        ],
                    }
                ],
            },
        ):
            with self.assertRaises(RuntimeError) as exc_info:
                daemon.analyze_transcript(
                    client=mock.Mock(),
                    instruction_text="Сделай сводку",
                    transcript_doc={"transcription": {"dialogue_text": "Тест"}},
                    cfg=cfg,
                )

        self.assertIn("returned a refusal", str(exc_info.exception))


class TelegramTests(unittest.TestCase):
    def test_send_telegram_message_includes_api_description(self):
        cfg = make_config()
        response = mock.Mock()
        response.status_code = 400
        response.reason = "Bad Request"
        response.text = '{"ok":false,"description":"Bad Request: message thread not found"}'
        response.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message thread not found",
        }

        with mock.patch("callbot_daemon.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as exc_info:
                daemon.send_telegram_message(cfg, "Тест")

        self.assertIn("message thread not found", str(exc_info.exception))

    def test_process_remote_audio_reuses_saved_analysis_for_telegram_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(
                state_path=Path(temp_dir) / "state.json",
                work_root=Path(temp_dir),
            )
            state = {"files": {}}
            remote_file = {
                "path": "/recordings/call.mp3",
                "name": "call.mp3",
                "size": 123456,
                "modify": "20260323070000",
            }
            saved_doc = {
                "generated_at": "2026-03-23T00:00:00+00:00",
                "stage": "error",
                "source": {"ftp_path_audio": "/recordings/call.mp3"},
                "analysis": {"telegram_message": "Готовое сообщение"},
                "telegram": {"sent": False, "reason": "processing failed"},
            }

            with (
                mock.patch("callbot_daemon.remote_load_json", return_value=saved_doc),
                mock.patch(
                    "callbot_daemon.send_telegram_message",
                    return_value=[{"result": {"message_id": 101}}],
                ) as mocked_send,
                mock.patch("callbot_daemon.remote_upload_json") as mocked_upload,
                mock.patch("callbot_daemon.remote_archive_or_delete") as mocked_archive,
                mock.patch("callbot_daemon.remote_download_file") as mocked_download,
                mock.patch("callbot_daemon.prepare_audio_parts") as mocked_prepare,
                mock.patch("callbot_daemon.transcribe_part") as mocked_transcribe,
                mock.patch("callbot_daemon.analyze_transcript") as mocked_analyze,
            ):
                daemon.process_remote_audio(
                    cfg=cfg,
                    client=mock.Mock(),
                    instruction_text="Инструкция",
                    state=state,
                    remote_file=remote_file,
                )

            mocked_send.assert_called_once_with(cfg, "Готовое сообщение")
            mocked_upload.assert_called_once()
            mocked_archive.assert_called_once_with(
                cfg,
                "/recordings/call.mp3",
                backend=daemon.REMOTE_BACKEND_FTP,
            )
            mocked_download.assert_not_called()
            mocked_prepare.assert_not_called()
            mocked_transcribe.assert_not_called()
            mocked_analyze.assert_not_called()
            self.assertEqual(
                state["files"]["/recordings/call.mp3"]["stage"],
                "done",
            )
            self.assertEqual(saved_doc["telegram"]["sent"], True)
            self.assertEqual(saved_doc["stage"], "done")


class FilenameMetadataTests(unittest.TestCase):
    def test_parse_filename_metadata_supports_phone_manager_pattern(self):
        data = daemon.parse_filename_metadata(
            "2024-11-02__16-56-29__79621625788__Manager_Name_677.mp3"
        )

        self.assertEqual(data["file_date"], "2024-11-02")
        self.assertEqual(data["file_time"], "16-56-29")
        self.assertEqual(data["file_phone"], "79621625788")
        self.assertEqual(data["manager_name"], "Manager Name")
        self.assertEqual(data["call_suffix"], "677")
        self.assertIsNone(data["counterparty_name"])

    def test_parse_filename_metadata_supports_manager_phone_pattern(self):
        data = daemon.parse_filename_metadata(
            "2024-11-06__07-41-19__Manager_Name__73472681128_672.mp3"
        )

        self.assertEqual(data["manager_name"], "Manager Name")
        self.assertEqual(data["file_phone"], "73472681128")
        self.assertEqual(data["call_suffix"], "672")
        self.assertIsNone(data["counterparty_name"])

    def test_parse_filename_metadata_supports_counterparty_pattern(self):
        data = daemon.parse_filename_metadata(
            "2025-03-21__12-07-27__Manager_Name__Counterparty_User_384.mp3"
        )

        self.assertEqual(data["manager_name"], "Manager Name")
        self.assertEqual(data["counterparty_name"], "Counterparty User")
        self.assertEqual(data["call_suffix"], "384")
        self.assertIsNone(data["file_phone"])


class ExistingRemoteJsonDecisionTests(unittest.TestCase):
    def test_should_process_file_skips_when_remote_json_is_completed(self):
        cfg = make_config(min_stable_polls=1)
        state = {"files": {}}
        remote_file = {
            "path": "/recordings/call.mp3",
            "name": "call.mp3",
            "size": 123456,
            "modify": "20260323070000",
        }
        remote_json_meta = {
            "path": "/recordings/call.json",
            "name": "call.json",
            "size": 100,
            "modify": "20260323070001",
        }
        saved_doc = {
            "stage": "done",
            "status": "ok",
            "source": {"ftp_path_audio": "/recordings/call.mp3"},
        }

        with mock.patch("callbot_daemon.remote_load_json", return_value=saved_doc):
            result = daemon.should_process_file(
                remote_file,
                {
                    remote_file["path"]: remote_file,
                    remote_json_meta["path"]: remote_json_meta,
                },
                state,
                cfg,
            )

        self.assertFalse(result)
        self.assertEqual(state["files"]["/recordings/call.mp3"]["stage"], "done")
        self.assertEqual(
            state["files"]["/recordings/call.mp3"]["skip_reason"],
            "remote json already completed",
        )

    def test_should_process_file_retries_when_remote_json_is_error(self):
        cfg = make_config(min_stable_polls=1)
        state = {"files": {}}
        remote_file = {
            "path": "/recordings/call.mp3",
            "name": "call.mp3",
            "size": 123456,
            "modify": "20260323070000",
        }
        remote_json_meta = {
            "path": "/recordings/call.json",
            "name": "call.json",
            "size": 100,
            "modify": "20260323070001",
        }
        saved_doc = {
            "stage": "error",
            "status": "error",
            "source": {"ftp_path_audio": "/recordings/call.mp3"},
        }

        with mock.patch("callbot_daemon.remote_load_json", return_value=saved_doc):
            result = daemon.should_process_file(
                remote_file,
                {
                    remote_file["path"]: remote_file,
                    remote_json_meta["path"]: remote_json_meta,
                },
                state,
                cfg,
            )

        self.assertTrue(result)
        self.assertNotEqual(
            state["files"]["/recordings/call.mp3"].get("stage"),
            "done",
        )


class ResumeProcessingTests(unittest.TestCase):
    def test_process_remote_audio_reuses_saved_transcription_for_analysis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(
                state_path=Path(temp_dir) / "state.json",
                work_root=Path(temp_dir),
                min_dialogue_words=1,
            )
            state = {"files": {}}
            remote_file = {
                "path": "/recordings/call.mp3",
                "name": "call.mp3",
                "size": 123456,
                "modify": "20260323070000",
            }
            saved_doc = {
                "generated_at": "2026-03-23T00:00:00+00:00",
                "stage": "error",
                "status": "error",
                "source": {"ftp_path_audio": "/recordings/call.mp3"},
                "transcription": {
                    "dialogue_text": "Person 1: test",
                    "full_text": "test",
                    "word_count": 1,
                    "duration_min_total": 1.0,
                    "parts_planned": 1,
                    "parts_completed": 1,
                    "parts_failed": 0,
                    "parts": [{"status": "ok"}],
                },
            }

            with (
                mock.patch("callbot_daemon.remote_load_json", return_value=saved_doc),
                mock.patch(
                    "callbot_daemon.analyze_transcript",
                    return_value="Ready message",
                ) as mocked_analyze,
                mock.patch(
                    "callbot_daemon.send_telegram_message",
                    return_value=[{"result": {"message_id": 7}}],
                ),
                mock.patch("callbot_daemon.remote_upload_json") as mocked_upload,
                mock.patch("callbot_daemon.remote_archive_or_delete") as mocked_archive,
                mock.patch("callbot_daemon.remote_download_file") as mocked_download,
                mock.patch("callbot_daemon.prepare_audio_parts") as mocked_prepare,
                mock.patch("callbot_daemon.transcribe_part") as mocked_transcribe,
            ):
                daemon.process_remote_audio(
                    cfg=cfg,
                    client=mock.Mock(),
                    instruction_text="Instruction",
                    state=state,
                    remote_file=remote_file,
                )

            mocked_download.assert_not_called()
            mocked_prepare.assert_not_called()
            mocked_transcribe.assert_not_called()
            mocked_analyze.assert_called_once()
            mocked_archive.assert_called_once_with(
                cfg,
                "/recordings/call.mp3",
                backend=daemon.REMOTE_BACKEND_FTP,
            )
            self.assertGreaterEqual(mocked_upload.call_count, 2)
            self.assertEqual(saved_doc["stage"], "done")
            self.assertEqual(saved_doc["status"], "ok")


class TranscriptDocumentTests(unittest.TestCase):
    def test_build_transcript_document_aggregates_usage_and_part_metadata(self):
        cfg = make_config()
        remote_file = {
            "path": "/recordings/2024-11-06__07-41-19__Manager_Name__73472681128_672.mp3",
            "name": "2024-11-06__07-41-19__Manager_Name__73472681128_672.mp3",
            "size": 123456,
            "modify": "20260323070000",
        }
        part_results = [
            {
                "part_index": 1,
                "parts_total": 2,
                "part_name": "part_001.mp3",
                "size_bytes": 111,
                "duration_sec": 10.0,
                "start_offset_sec": 0.0,
                "status": "ok",
                "error": None,
                "api_sent_at_utc": "2026-03-23T00:00:00+00:00",
                "api_finished_at_utc": "2026-03-23T00:00:01+00:00",
                "api_elapsed_sec": 1.2,
                "full_text": "Hello",
                "dialogue_text": "Person 1: Hello",
                "segments": [{"speaker": "A", "text": "Hello", "start": 0.0, "end": 1.0}],
                "usage": {
                    "type": "tokens",
                    "input_token_details": {"audio_tokens": 100, "text_tokens": 5},
                    "output_tokens": 10,
                },
            },
            {
                "part_index": 2,
                "parts_total": 2,
                "part_name": "part_002.mp3",
                "size_bytes": 222,
                "duration_sec": 12.0,
                "start_offset_sec": 10.0,
                "status": "ok",
                "error": None,
                "api_sent_at_utc": "2026-03-23T00:00:02+00:00",
                "api_finished_at_utc": "2026-03-23T00:00:03+00:00",
                "api_elapsed_sec": 1.5,
                "full_text": "World",
                "dialogue_text": "Person 2: World",
                "segments": [{"speaker": "B", "text": "World", "start": 0.5, "end": 2.0}],
                "usage": {
                    "type": "tokens",
                    "input_token_details": {"audio_tokens": 200, "text_tokens": 7},
                    "output_tokens": 20,
                },
            },
        ]

        doc = daemon.build_transcript_document(
            remote_file,
            part_results,
            cfg,
            planned_parts_count=2,
            split_applied=True,
        )

        self.assertEqual(doc["status"], "transcribed")
        self.assertTrue(doc["transcription"]["split_applied"])
        self.assertEqual(doc["transcription"]["parts_planned"], 2)
        self.assertEqual(doc["transcription"]["parts_completed"], 2)
        self.assertEqual(doc["transcription"]["parts_failed"], 0)
        self.assertEqual(doc["transcription"]["api_elapsed_sec_total"], 2.7)
        self.assertEqual(doc["transcription"]["usage"]["type"], "tokens")
        self.assertEqual(
            doc["transcription"]["usage"]["input_token_details"]["audio_tokens"],
            300,
        )
        self.assertEqual(
            doc["transcription"]["usage"]["input_token_details"]["text_tokens"],
            12,
        )
        self.assertEqual(doc["transcription"]["usage"]["output_tokens"], 30)
        self.assertEqual(doc["source"]["file_metadata"]["manager_name"], "Manager Name")
        self.assertEqual(doc["source"]["file_metadata"]["file_phone"], "73472681128")
        self.assertEqual(doc["source"]["file_metadata"]["call_suffix"], "672")
        self.assertEqual(doc["transcription"]["segments"][1]["start"], 10.5)


class AnalysisDefaultsTests(unittest.TestCase):
    def test_defaults_reasoning_effort_for_gpt5_models(self):
        self.assertEqual(daemon.default_analysis_reasoning_effort("gpt-5-mini"), "low")
        self.assertEqual(daemon.default_analysis_reasoning_effort("gpt-4.1-mini"), "")


class RouteProbeTests(unittest.TestCase):
    def test_run_openai_request_does_not_pause_proxy_for_route_probe(self):
        cfg = make_config(openai_proxy="http://proxy.example:8888")
        clients = daemon.OpenAIClients(
            primary=mock.Mock(),
            direct_fallback=None,
            proxy_enabled=True,
            proxy_failure_cooldown_sec=60,
        )

        with mock.patch(
            "callbot_daemon.execute_openai_request",
            side_effect=httpx.ConnectError("Connection error"),
        ):
            with self.assertRaises(httpx.ConnectError):
                daemon.run_openai_request(
                    clients,
                    "route probe",
                    cfg,
                    lambda _: {"ok": True},
                )

        self.assertFalse(daemon.openai_proxy_route_is_in_cooldown(clients))

    def test_route_probe_connection_error_is_advisory_without_fallback(self):
        cfg = make_config(openai_proxy="http://proxy.example:8888")
        clients = daemon.OpenAIClients(
            primary=mock.Mock(),
            direct_fallback=None,
            proxy_enabled=True,
            proxy_failure_cooldown_sec=60,
        )

        with mock.patch(
            "callbot_daemon.run_openai_request",
            side_effect=httpx.ConnectError("Connection error"),
        ):
            result = daemon.verify_openai_route_before_processing(cfg, clients)

        self.assertTrue(result)
        self.assertFalse(daemon.openai_proxy_route_is_in_cooldown(clients))

    def test_route_probe_connection_error_switches_to_direct_fallback(self):
        cfg = make_config(
            openai_proxy="http://proxy.example:8888",
            openai_proxy_direct_fallback=True,
        )
        clients = daemon.OpenAIClients(
            primary=mock.Mock(),
            direct_fallback=mock.Mock(),
            proxy_enabled=True,
            proxy_failure_cooldown_sec=60,
        )

        with mock.patch(
            "callbot_daemon.run_openai_request",
            side_effect=httpx.ConnectError("Connection error"),
        ):
            result = daemon.verify_openai_route_before_processing(cfg, clients)

        self.assertTrue(result)
        self.assertTrue(daemon.openai_proxy_route_is_in_cooldown(clients))


class ConfigDefaultsTests(unittest.TestCase):
    def test_from_env_defaults_poll_interval_and_archive_dir_from_remote_root(self):
        env = {
            "OPENAI_API_KEY": "sk-test",
            "FTP_HOST": "example.com",
            "FTP_USER": "user",
            "FTP_PASSWORD": "pass",
            "TELEGRAM_BOT_TOKEN": "123:test",
            "TELEGRAM_CHAT_ID": "-1001",
            "FTP_REMOTE_ROOT": "/recordings",
        }

        with mock.patch.dict("os.environ", env, clear=True):
            cfg = daemon.Config.from_env()

        self.assertEqual(cfg.poll_interval_sec, 60)
        self.assertEqual(cfg.ftp_remote_roots, ["/recordings"])
        self.assertEqual(cfg.ftp_archive_dir, "/recordings/archive")
        self.assertFalse(cfg.ftp_archive_dir_explicit)

    def test_from_env_supports_multiple_remote_roots(self):
        env = {
            "OPENAI_API_KEY": "sk-test",
            "FTP_HOST": "example.com",
            "FTP_USER": "user",
            "FTP_PASSWORD": "pass",
            "TELEGRAM_BOT_TOKEN": "123:test",
            "TELEGRAM_CHAT_ID": "-1001",
            "FTP_REMOTE_ROOTS": "/recordings/sales, /recordings/support, /recordings/sales",
        }

        with mock.patch.dict("os.environ", env, clear=True):
            cfg = daemon.Config.from_env()

        self.assertEqual(
            cfg.ftp_remote_roots,
            ["/recordings/sales", "/recordings/support"],
        )
        self.assertEqual(cfg.ftp_remote_root, "/recordings/sales")
        self.assertEqual(cfg.ftp_archive_dir, "/recordings/sales/archive")
        self.assertFalse(cfg.ftp_archive_dir_explicit)

    def test_from_env_supports_yandex_disk_without_ftp(self):
        env = {
            "OPENAI_API_KEY": "sk-test",
            "TELEGRAM_BOT_TOKEN": "123:test",
            "TELEGRAM_CHAT_ID": "-1001",
            "YANDEX_DISK_OAUTH_TOKEN": "y0_test_token",
            "YANDEX_DISK_REMOTE_ROOT": "/mango/records",
        }

        with mock.patch.dict("os.environ", env, clear=True):
            cfg = daemon.Config.from_env()

        self.assertFalse(cfg.ftp_enabled)
        self.assertTrue(cfg.yandex_disk_enabled)
        self.assertEqual(cfg.yandex_disk_remote_roots, ["disk:/mango/records"])
        self.assertEqual(cfg.yandex_disk_remote_root, "disk:/mango/records")


class RemoteScanExclusionTests(unittest.TestCase):
    def test_skips_archive_directory_when_archive_is_enabled(self):
        cfg = make_config(
            ftp_remote_root="/recordings",
            ftp_archive_dir="/recordings/archive",
            ftp_move_to_archive_after_success=True,
        )

        self.assertTrue(
            daemon.should_skip_remote_scan_path(
                cfg, "/recordings/archive/call.mp3"
            )
        )
        self.assertTrue(
            daemon.should_skip_remote_scan_path(
                cfg, "/recordings/archive/nested/call.mp3"
            )
        )
        self.assertFalse(
            daemon.should_skip_remote_scan_path(
                cfg, "/recordings/call.mp3"
            )
        )

    def test_does_not_skip_all_files_if_archive_equals_root(self):
        cfg = make_config(
            ftp_remote_root="/recordings",
            ftp_archive_dir="/recordings",
            ftp_move_to_archive_after_success=True,
        )

        self.assertFalse(
            daemon.should_skip_remote_scan_path(
                cfg, "/recordings/call.mp3"
            )
        )

    def test_skips_implicit_archive_dirs_for_each_remote_root(self):
        cfg = make_config(
            ftp_remote_root="/recordings/sales",
            ftp_remote_roots=["/recordings/sales", "/recordings/support"],
            ftp_archive_dir="/recordings/sales/archive",
            ftp_archive_dir_explicit=False,
            ftp_move_to_archive_after_success=True,
        )

        self.assertTrue(
            daemon.should_skip_remote_scan_path(
                cfg, "/recordings/support/archive/call.mp3"
            )
        )
        self.assertFalse(
            daemon.should_skip_remote_scan_path(
                cfg, "/recordings/support/call.mp3"
            )
        )


class ArchiveResolutionTests(unittest.TestCase):
    def test_resolves_archive_dir_from_matching_remote_root(self):
        cfg = make_config(
            ftp_remote_root="/recordings/sales",
            ftp_remote_roots=["/recordings/sales", "/recordings/support"],
            ftp_archive_dir="/recordings/sales/archive",
            ftp_archive_dir_explicit=False,
        )

        self.assertEqual(
            daemon.resolve_archive_dir_for_path(
                cfg, "/recordings/support/2026/call.mp3"
            ),
            "/recordings/support/archive",
        )

    def test_explicit_archive_dir_is_shared_for_all_roots(self):
        cfg = make_config(
            ftp_remote_root="/recordings/sales",
            ftp_remote_roots=["/recordings/sales", "/recordings/support"],
            ftp_archive_dir="/archive/common",
            ftp_archive_dir_explicit=True,
        )

        self.assertEqual(
            daemon.resolve_archive_dir_for_path(
                cfg, "/recordings/support/2026/call.mp3"
            ),
            "/archive/common",
        )


class RemoteWalkTests(unittest.TestCase):
    def test_sftp_walk_skips_unreadable_subdirectories(self):
        cfg = make_config(
            ftp_remote_root="/",
            ftp_remote_roots=["/"],
            ftp_archive_dir="/recordings/archive",
        )
        root_entries = [
            mock.Mock(
                filename="recordings",
                st_mode=daemon.stat.S_IFDIR,
                st_size=0,
                st_mtime=None,
            ),
            mock.Mock(
                filename="lost+found",
                st_mode=daemon.stat.S_IFDIR,
                st_size=0,
                st_mtime=None,
            ),
        ]
        recordings_entries = [
            mock.Mock(
                filename="call.mp3",
                st_mode=daemon.stat.S_IFREG,
                st_size=128,
                st_mtime=1_742_000_000,
            ),
            mock.Mock(
                filename="archive",
                st_mode=daemon.stat.S_IFDIR,
                st_size=0,
                st_mtime=None,
            ),
        ]

        sftp = mock.Mock()
        transport = mock.Mock()

        def listdir_attr(path: str):
            if path == "/":
                return root_entries
            if path == "/recordings":
                return recordings_entries
            if path == "/lost+found":
                raise PermissionError(13, "Permission denied")
            raise AssertionError(f"Unexpected path: {path}")

        sftp.listdir_attr.side_effect = listdir_attr

        with mock.patch("callbot_daemon.sftp_connect", return_value=(transport, sftp)):
            files = daemon.sftp_walk(cfg, "/")

        self.assertEqual([item["path"] for item in files], ["/recordings/call.mp3"])
        sftp.close.assert_called_once()
        transport.close.assert_called_once()

    def test_sftp_remote_walk_scans_all_roots_and_deduplicates(self):
        cfg = make_config(
            ftp_protocol="sftp",
            ftp_remote_root="/recordings",
            ftp_remote_roots=["/recordings", "/recordings/nested"],
        )

        root_results = {
            "/recordings": [
                {
                    "path": "/recordings/a.mp3",
                    "name": "a.mp3",
                    "size": 1,
                    "modify": "20260323070000",
                },
                {
                    "path": "/recordings/nested/b.mp3",
                    "name": "b.mp3",
                    "size": 2,
                    "modify": "20260323070100",
                },
            ],
            "/recordings/nested": [
                {
                    "path": "/recordings/nested/b.mp3",
                    "name": "b.mp3",
                    "size": 2,
                    "modify": "20260323070100",
                }
            ],
        }

        with mock.patch(
            "callbot_daemon.sftp_walk",
            side_effect=lambda cfg_arg, root: root_results[root],
        ) as mocked_walk:
            files = daemon.remote_walk(cfg)

        self.assertEqual(
            [item["path"] for item in files],
            ["/recordings/a.mp3", "/recordings/nested/b.mp3"],
        )
        mocked_walk.assert_has_calls(
            [
                mock.call(cfg, "/recordings"),
                mock.call(cfg, "/recordings/nested"),
            ]
        )

    def test_remote_walk_merges_ftp_and_yandex_sources(self):
        cfg = make_config(
            ftp_enabled=True,
            ftp_protocol="sftp",
            ftp_remote_root="/recordings",
            ftp_remote_roots=["/recordings"],
            yandex_disk_enabled=True,
            yandex_disk_oauth_token="y0_test_token",
            yandex_disk_remote_root="disk:/recordings",
            yandex_disk_remote_roots=["disk:/recordings"],
        )
        ftp_file = {
            "backend": daemon.REMOTE_BACKEND_FTP,
            "path": "/recordings/a.mp3",
            "name": "a.mp3",
            "size": 1,
            "modify": "20260323070000",
        }
        yadisk_file = {
            "backend": daemon.REMOTE_BACKEND_YANDEX_DISK,
            "path": "disk:/recordings/a.mp3",
            "name": "a.mp3",
            "size": 2,
            "modify": "2026-03-23T07:01:00+00:00",
        }

        with (
            mock.patch("callbot_daemon.sftp_walk", return_value=[ftp_file]),
            mock.patch("callbot_daemon.yandex_disk_walk", return_value=[yadisk_file]),
        ):
            files = daemon.remote_walk(cfg)

        self.assertEqual(
            [daemon.remote_file_lookup_key(item) for item in files],
            ["/recordings/a.mp3", "yandex_disk:disk:/recordings/a.mp3"],
        )

    def test_remote_walk_continues_with_yandex_when_ftp_scan_fails(self):
        cfg = make_config(
            ftp_enabled=True,
            ftp_protocol="sftp",
            ftp_remote_root="/recordings",
            ftp_remote_roots=["/recordings"],
            yandex_disk_enabled=True,
            yandex_disk_oauth_token="y0_test_token",
            yandex_disk_remote_root="disk:/recordings",
            yandex_disk_remote_roots=["disk:/recordings"],
        )
        yadisk_file = {
            "backend": daemon.REMOTE_BACKEND_YANDEX_DISK,
            "path": "disk:/recordings/b.mp3",
            "name": "b.mp3",
            "size": 2,
            "modify": "2026-03-23T07:01:00+00:00",
        }

        with (
            mock.patch("callbot_daemon.sftp_walk", side_effect=RuntimeError("FTP down")),
            mock.patch("callbot_daemon.yandex_disk_walk", return_value=[yadisk_file]),
        ):
            files = daemon.remote_walk(cfg)

        self.assertEqual(files, [yadisk_file])


class YandexDispatchTests(unittest.TestCase):
    def test_remote_download_file_uses_yandex_backend(self):
        cfg = make_config(
            yandex_disk_enabled=True,
            yandex_disk_oauth_token="y0_test_token",
            yandex_disk_remote_root="disk:/recordings",
            yandex_disk_remote_roots=["disk:/recordings"],
        )
        local_path = Path("sample.mp3")

        with (
            mock.patch("callbot_daemon.yandex_disk_download_file") as mocked_yadisk,
            mock.patch("callbot_daemon.sftp_download_file") as mocked_sftp,
        ):
            daemon.remote_download_file(
                cfg,
                "disk:/recordings/sample.mp3",
                local_path,
                backend=daemon.REMOTE_BACKEND_YANDEX_DISK,
            )

        mocked_yadisk.assert_called_once_with(
            cfg,
            "disk:/recordings/sample.mp3",
            local_path,
        )
        mocked_sftp.assert_not_called()


class ScanCycleTests(unittest.TestCase):
    def test_scan_cycle_skips_route_probe_when_no_files_need_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(
                openai_proxy="http://proxy.example:8888",
                state_path=Path(temp_dir) / "state.json",
                work_root=Path(temp_dir),
            )
            state = {
                "files": {
                    "/recordings/call.mp3": {
                        "stage": "done",
                        "processed_sig": "123456:20260323070000",
                    }
                }
            }
            clients = daemon.OpenAIClients(
                primary=mock.Mock(),
                direct_fallback=None,
                proxy_enabled=True,
                proxy_failure_cooldown_sec=60,
            )
            remote_file = {
                "path": "/recordings/call.mp3",
                "name": "call.mp3",
                "size": 123456,
                "modify": "20260323070000",
            }

            with (
                mock.patch("callbot_daemon.load_instruction_text", return_value="Instruction"),
                mock.patch("callbot_daemon.remote_walk", return_value=[remote_file]),
                mock.patch("callbot_daemon.verify_openai_route_before_processing") as mocked_probe,
                mock.patch("callbot_daemon.process_remote_audio") as mocked_process,
            ):
                daemon.scan_cycle(cfg, clients, state)

            mocked_probe.assert_not_called()
            mocked_process.assert_not_called()


class TranscriptionRequestTests(unittest.TestCase):
    def test_transcribe_part_uses_requests_endpoint_with_proxy(self):
        cfg = make_config(openai_proxy="http://proxy.example:8888")
        clients = daemon.OpenAIClients(
            primary=mock.Mock(),
            direct_fallback=None,
            proxy_enabled=True,
            proxy_failure_cooldown_sec=60,
        )
        response = mock.Mock()
        response.json.return_value = {
            "text": "test",
            "segments": [],
            "duration": 1.0,
            "usage": {},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.mp3"
            audio_path.write_bytes(b"fake-audio")

            with (
                mock.patch(
                    "callbot_daemon.run_openai_request",
                    side_effect=lambda openai_clients, operation, cfg_arg, fn: fn(
                        openai_clients.primary
                    ),
                ),
                mock.patch(
                    "callbot_daemon.requests.post",
                    return_value=response,
                ) as mocked_post,
            ):
                result = daemon.transcribe_part(clients, audio_path, cfg)

        self.assertEqual(result["full_text"], "test")
        self.assertEqual(
            mocked_post.call_args.args[0],
            "https://api.openai.com/v1/audio/transcriptions",
        )
        self.assertEqual(
            mocked_post.call_args.kwargs["proxies"],
            {
                "http": "http://proxy.example:8888",
                "https": "http://proxy.example:8888",
            },
        )

    def test_analyze_transcript_uses_requests_responses_endpoint_with_proxy(self):
        cfg = make_config(openai_proxy="http://proxy.example:8888")
        clients = daemon.OpenAIClients(
            primary=mock.Mock(),
            direct_fallback=None,
            proxy_enabled=True,
            proxy_failure_cooldown_sec=60,
        )
        response = mock.Mock()
        response.json.return_value = {
            "id": "resp_123",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "Готово"},
                    ],
                }
            ],
        }

        with (
            mock.patch(
                "callbot_daemon.run_openai_request",
                side_effect=lambda openai_clients, operation, cfg_arg, fn: fn(
                    openai_clients.primary
                ),
            ),
            mock.patch(
                "callbot_daemon.requests.post",
                return_value=response,
            ) as mocked_post,
        ):
            result = daemon.analyze_transcript(
                clients,
                "Сделай сводку",
                {"transcription": {"dialogue_text": "Тест"}},
                cfg,
            )

        self.assertEqual(result, "Готово")
        self.assertEqual(
            mocked_post.call_args.args[0],
            "https://api.openai.com/v1/responses",
        )
        self.assertEqual(
            mocked_post.call_args.kwargs["proxies"],
            {
                "http": "http://proxy.example:8888",
                "https": "http://proxy.example:8888",
            },
        )


if __name__ == "__main__":
    unittest.main()
