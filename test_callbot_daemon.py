import unittest
from pathlib import Path
from unittest import mock

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
        "ftp_archive_dir": "/archive",
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
        "poll_interval_sec": 300,
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


class AnalysisDefaultsTests(unittest.TestCase):
    def test_defaults_reasoning_effort_for_gpt5_models(self):
        self.assertEqual(daemon.default_analysis_reasoning_effort("gpt-5-mini"), "low")
        self.assertEqual(daemon.default_analysis_reasoning_effort("gpt-4.1-mini"), "")


if __name__ == "__main__":
    unittest.main()
