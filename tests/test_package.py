import importlib.metadata

import hardcharger


def test_version_matches_metadata():
    assert hardcharger.__version__ == importlib.metadata.version(distribution_name="hardcharger")
