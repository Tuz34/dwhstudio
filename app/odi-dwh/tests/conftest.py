"""
pytest Fixtures
===============

Shared fixtures for all test files.
"""

import sys
import os
import pytest

# Add the project root to the Python path.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def app_client():
    """Create a Flask test client."""
    from app import app, limiter
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # Disable CSRF during tests.
    app.config["RATELIMIT_ENABLED"] = False  # The test suite calls the same endpoint rapidly.
    old_limiter_enabled = limiter.enabled
    limiter.enabled = False
    try:
        with app.test_client() as client:
            yield client
    finally:
        limiter.enabled = old_limiter_enabled


@pytest.fixture
def dqm():
    """Create and then close a DataQualityManager instance."""
    from data_quality import DataQualityManager
    manager = DataQualityManager()
    yield manager
    manager.close()


@pytest.fixture
def wm():
    """Create and then close a WatermarkManager instance."""
    from watermark_manager import WatermarkManager
    manager = WatermarkManager()
    yield manager
    manager.close()
