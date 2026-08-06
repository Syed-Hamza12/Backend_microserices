from unittest import mock

from django.test import SimpleTestCase

from . import google_genai_client as g


class ModelLadderTests(SimpleTestCase):
    def test_dedupes_and_drops_empty(self):
        self.assertEqual(g.model_ladder("a", ["a", "b", "", None, "b"]), ["a", "b"])

    def test_no_fallbacks_gives_single_item_list(self):
        self.assertEqual(g.model_ladder("only-model"), ["only-model"])


class GenerateRotationTests(SimpleTestCase):
    """The canonical rotation logic every Google-model caller in this
    codebase shares (vision OCR, chat's Gemma planner) — verified once
    here rather than re-verified per caller."""

    def setUp(self):
        g._clients.clear()
        self.addCleanup(g._clients.clear)

    def test_raises_cleanly_when_no_keys_configured(self):
        with self.assertRaises(RuntimeError):
            g.generate([], ["some-model"], ["hello"])

    def test_first_working_key_wins_key_stays_at_index_zero_next_call(self):
        """Every call starts back at key 0 — a key whose quota has since
        reset comes back into rotation automatically, with no separate
        'has it reset yet' bookkeeping (same reasoning as
        apps.chat.groq_client.call_groq's key-rotation docstring)."""
        good_client = mock.Mock()
        good_client.models.generate_content.return_value = "ok-response"
        bad_client = mock.Mock()
        bad_client.models.generate_content.side_effect = RuntimeError("quota exceeded")

        def fake_client_for(key):
            return good_client if key == "key-good" else bad_client

        with mock.patch.object(g, "client_for", side_effect=fake_client_for):
            result = g.generate(["key-bad", "key-good"], ["model-a"], ["hi"])
        self.assertEqual(result, "ok-response")

        # A second call, same key order — must try "key-bad" (index 0)
        # again rather than remembering last call's winner.
        with mock.patch.object(g, "client_for", side_effect=fake_client_for) as spy:
            g.generate(["key-bad", "key-good"], ["model-a"], ["hi"])
        first_call_key = spy.call_args_list[0].args[0]
        self.assertEqual(first_call_key, "key-bad")

    def test_model_is_retried_before_exhausting_every_key(self):
        """A 503-style 'model busy' failure should move to the NEXT MODEL
        before cycling through every remaining key on a model that's
        already known to be struggling."""
        call_log = []

        def fake_client_for(key):
            client = mock.Mock()

            def side_effect(*, model, contents, config):
                call_log.append((key, model))
                if model == "busy-model":
                    raise RuntimeError("503 model busy")
                return f"response-from-{model}"

            client.models.generate_content.side_effect = side_effect
            return client

        with mock.patch.object(g, "client_for", side_effect=fake_client_for):
            result = g.generate(["key-1", "key-2"], ["busy-model", "backup-model"], ["hi"])

        self.assertEqual(result, "response-from-backup-model")
        # Both keys were tried against the busy model before the ladder
        # moved on to the backup model — model-first, not key-first.
        self.assertEqual(call_log, [
            ("key-1", "busy-model"), ("key-2", "busy-model"), ("key-1", "backup-model"),
        ])

    def test_raises_the_last_error_once_everything_fails(self):
        def fake_client_for(key):
            client = mock.Mock()
            client.models.generate_content.side_effect = RuntimeError(f"failed for {key}")
            return client

        with mock.patch.object(g, "client_for", side_effect=fake_client_for):
            with self.assertRaisesMessage(RuntimeError, "failed for key-2"):
                g.generate(["key-1", "key-2"], ["model-a"], ["hi"])
