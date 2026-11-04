"""
pytest Fixture'lari
━━━━━━━━━━━━━━━━━━━

Tum test dosyalari icin ortak fixture'lar.
"""

import sys
import os
import pytest

# Proje kokunu Python path'ine ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def app_client():
    """Flask test client'i olusturur."""
    from app import app, limiter
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # Test sirasinda CSRF devre disi
    app.config["RATELIMIT_ENABLED"] = False  # Test suite ayni endpoint'e hizli vurur
    old_limiter_enabled = limiter.enabled
    limiter.enabled = False
    try:
        with app.test_client() as client:
            yield client
    finally:
        limiter.enabled = old_limiter_enabled


@pytest.fixture
def dqm():
    """DataQualityManager instance'i olusturur ve sonra kapatir."""
    from data_quality import DataQualityManager
    manager = DataQualityManager()
    yield manager
    manager.close()


@pytest.fixture
def wm():
    """WatermarkManager instance'i olusturur ve sonra kapatir."""
    from watermark_manager import WatermarkManager
    manager = WatermarkManager()
    yield manager
    manager.close()
