import unittest
from pathlib import Path
from unittest import mock
import tempfile

import httpx
import callbot_daemon as daemon


def make_config(**overrides):
    data = {
        "ftp_protocol": "sftp",
        "ftp_host": "example.com",
        "ftp_port": 22,
        "ftp_user": "user",
        "ftp_password": "pass",
        "ftp_encoding": "utf-8",
        "ftp_encoding_fallbacks": ["cp1251"],
        "ftp_remote_root": "/recordings",
        "ftp_archive_dir": "/recordings/archive",
        "ftp_delete_after_success": False,
        "ftp_move_to_archive_after_success": True,
        "ftp_use_tls": False,
        "ftp_timeout_sec": 60,
        "ftp_connect_attempts": 2,
        "ftp_retry_delay_sec": 5.0,
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
            mocked_archive.assert_called_once_with(cfg, "/recordings/call.mp3")
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
        self.assertEqual(cfg.ftp_archive_dir, "/recordings/archive")


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
