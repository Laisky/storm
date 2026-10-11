import unittest
from unittest.mock import patch

import numpy as np

from knowledge_storm.interface import Information
from knowledge_storm.storm_wiki.modules.storm_dataclass import DialogueTurn, StormInformationTable


class StormInformationTableTests(unittest.TestCase):
    """Exercise StormInformationTable retrieval pipeline with a stub encoder."""

    def test_prepare_and_retrieve_information(self) -> None:
        """Ensure prepare_table_for_retrieval configures the encoder and retrieves snippets."""
        info_primary = Information(
            url="https://example.com/primary",
            description="Primary description",
            snippets=["primary snippet"],
            title="Primary",
        )
        info_secondary = Information(
            url="https://example.com/secondary",
            description="Secondary description",
            snippets=["secondary snippet"],
            title="Secondary",
        )
        turn = DialogueTurn(
            agent_utterance="agent",
            user_utterance="user",
            search_results=[info_primary, info_secondary],
        )
        table = StormInformationTable(conversations=[("persona", [turn])])

        def _fake_encode(value):
            if isinstance(value, list):
                return np.array([[1.0, 0.0], [0.0, 1.0]])
            return np.array([1.0, 0.0])

        with patch(
            "knowledge_storm.storm_wiki.modules.storm_dataclass.SentenceTransformer"
        ) as mocked_encoder:
            mocked_encoder.return_value.encode.side_effect = _fake_encode

            table.prepare_table_for_retrieval()
            results = table.retrieve_information("topic", search_top_k=1)

        mocked_encoder.assert_called_once_with("paraphrase-MiniLM-L6-v2")
        self.assertListEqual(table.collected_urls, [info_primary.url, info_secondary.url])
        self.assertListEqual(
            table.collected_snippets,
            [info_primary.snippets[0], info_secondary.snippets[0]],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, info_primary.url)
        self.assertListEqual(results[0].snippets, [info_primary.snippets[0]])


if __name__ == "__main__":
    unittest.main()
