"""Offline embedding contracts: batch limits, ordered results and safe fallback."""

import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock, patch

import plan


EMBEDDING_MODEL = "qwen3.7-text-embedding"
CHAT_MODEL = "deepseek-v4.1-flash"


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(
            plan.os.environ,
            {
                "OPENAI_API_KEY": "chat-key-for-offline-tests",
                "OPENAI_MODEL": CHAT_MODEL,
                "EMBEDDING_BASE_URL": "https://embedding.example.invalid/v1",
                "EMBEDDING_MODEL": EMBEDDING_MODEL,
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        singleton = patch.object(plan, "_embedding_client", None)
        singleton.start()
        self.addCleanup(singleton.stop)

    @staticmethod
    def client(side_effect):
        return SimpleNamespace(
            embeddings=SimpleNamespace(create=Mock(side_effect=side_effect))
        )

    @staticmethod
    def response_for_texts(*, model, input, encoding_format="float"):
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(text), 1.0])
                for index, text in enumerate(input)
            ]
        )

    def test_more_than_twenty_texts_are_batched_without_loss(self):
        client = self.client(self.response_for_texts)
        texts = [str(index) for index in range(45)]

        vectors = plan._embed_texts(client, texts)

        self.assertEqual(vectors, [[float(index), 1.0] for index in range(45)])
        calls = client.embeddings.create.call_args_list
        self.assertEqual([len(call.kwargs["input"]) for call in calls], [20, 20, 5])
        self.assertEqual(
            [text for call in calls for text in call.kwargs["input"]], texts
        )
        for call in calls:
            self.assertEqual(call.kwargs["model"], EMBEDDING_MODEL)

    def test_unsorted_batch_results_follow_input_order(self):
        def reversed_response(**kwargs):
            response = self.response_for_texts(**kwargs)
            response.data.reverse()
            return response

        client = self.client(reversed_response)

        vectors = plan._embed_texts(client, [str(index) for index in range(23)])

        self.assertEqual(vectors, [[float(index), 1.0] for index in range(23)])
        self.assertEqual(client.embeddings.create.call_count, 2)

    def test_incomplete_or_invalid_indices_fail_without_partial_vectors(self):
        for indices in ([0], [0, 0], [0, 2], [-1, 0]):
            with self.subTest(indices=indices):
                response = SimpleNamespace(
                    data=[
                        SimpleNamespace(index=index, embedding=[1.0, 0.0])
                        for index in indices
                    ]
                )
                client = self.client(lambda **kwargs: response)

                self.assertIsNone(plan._embed_texts(client, ["0", "1"]))

    def test_middle_batch_failure_discards_previous_vectors(self):
        client = self.client(
            [
                self.response_for_texts(
                    model=EMBEDDING_MODEL, input=[str(index) for index in range(20)]
                ),
                RuntimeError("simulated embedding quota failure"),
            ]
        )

        result = plan._embed_texts(client, [str(index) for index in range(41)])

        self.assertIsNone(result)
        self.assertEqual(client.embeddings.create.call_count, 2)

    def test_empty_input_does_not_call_embedding_api(self):
        client = self.client(RuntimeError("must not call"))

        self.assertEqual(plan._embed_texts(client, []), [])
        client.embeddings.create.assert_not_called()

    def test_embedding_default_is_independent_of_chat_model(self):
        plan.os.environ.pop("EMBEDDING_MODEL")
        client = self.client(self.response_for_texts)

        self.assertEqual(plan._embed_texts(client, ["0"]), [[0.0, 1.0]])

        client.embeddings.create.assert_called_once_with(
            model=EMBEDDING_MODEL, input=["0"], encoding_format="float"
        )
        self.assertEqual(plan.os.environ["OPENAI_MODEL"], CHAT_MODEL)
        self.assertEqual(plan._model_options(), {"extra_body": {"enable_thinking": False}})

    def test_unconfigured_base_url_does_not_create_client(self):
        plan.os.environ.pop("EMBEDDING_BASE_URL")
        with patch.object(plan, "OpenAI") as constructor:
            self.assertIsNone(plan._embedding_client_instance())
        constructor.assert_not_called()

    def test_missing_all_keys_does_not_create_client(self):
        plan.os.environ.pop("OPENAI_API_KEY")
        with patch.object(plan, "OpenAI") as constructor:
            self.assertIsNone(plan._embedding_client_instance())
        constructor.assert_not_called()

    def test_embedding_client_can_reuse_chat_api_key_and_is_cached(self):
        with patch.object(plan, "OpenAI") as constructor:
            first = plan._embedding_client_instance()
            second = plan._embedding_client_instance()

        self.assertIs(first, constructor.return_value)
        self.assertIs(second, first)
        constructor.assert_called_once_with(
            api_key="chat-key-for-offline-tests",
            base_url="https://embedding.example.invalid/v1",
            max_retries=0,
            timeout=20,
        )

    def test_independent_embedding_key_overrides_chat_key(self):
        plan.os.environ["EMBEDDING_API_KEY"] = "embedding-key-for-offline-tests"
        with patch.object(plan, "OpenAI") as constructor:
            plan._embedding_client_instance()

        constructor.assert_called_once_with(
            api_key="embedding-key-for-offline-tests",
            base_url="https://embedding.example.invalid/v1",
            max_retries=0,
            timeout=20,
        )

    def test_embedding_failure_keeps_keyword_scores_and_does_not_write_cache(self):
        for response in (
            RuntimeError("simulated embedding request failure"),
            SimpleNamespace(data=[]),
        ):
            with self.subTest(response_type=type(response).__name__):
                client = self.client([response])
                pois = [
                    {"name": "海边雕塑园", "category": "艺术"},
                    {"name": "历史博物馆", "category": "历史"},
                ]
                preferences = {"liked": ["艺术"]}
                expected = [
                    plan._score_poi_match(poi, {}, preferences, {}) for poi in pois
                ]
                with (
                    patch.object(plan, "_embedding_client_instance", return_value=client),
                    patch.object(plan, "_load_embedding_cache", return_value=OrderedDict()),
                    patch.object(plan, "_save_embedding_cache") as save,
                ):
                    plan._apply_match_scores(pois, {}, preferences, {})

                self.assertEqual([poi["match_score"] for poi in pois], expected)
                save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
