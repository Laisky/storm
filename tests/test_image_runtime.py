"""Offline image contracts for real NumPy/Arrow interoperability and core exports."""

import unittest


class ImageRuntimeCompatibilityTests(unittest.TestCase):
    """ImageRuntimeCompatibilityTests guards image imports without provider calls."""

    def test_arrow_numpy_and_dataframe_roundtrip(self):
        """test_arrow_numpy_and_dataframe_roundtrip preserves values across real APIs."""
        import numpy as np
        import pyarrow as pa

        values = np.array([1, 2, 3], dtype=np.int64)
        arrow = pa.array(values)
        np.testing.assert_array_equal(arrow.to_numpy(), values)
        frame = pa.table(
            {"value": values, "label": ["alpha", "beta", "gamma"]}
        ).to_pandas()
        restored = pa.Table.from_pandas(frame, preserve_index=False)
        self.assertEqual(
            restored.to_pydict(),
            {"value": [1, 2, 3], "label": ["alpha", "beta", "gamma"]},
        )

    def test_core_public_engines_import_without_provider_calls(self):
        """test_core_public_engines_import_without_provider_calls checks lazy engine exports."""
        import knowledge_storm

        self.assertTrue(callable(knowledge_storm.STORMWikiRunner))
        self.assertTrue(callable(knowledge_storm.CoStormRunner))


if __name__ == "__main__":
    unittest.main()
