"""The `Server-Timing` header counts the database queries a request runs."""
import re

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


def test_server_timing_counts_queries():
    Base.metadata.create_all(engine)
    try:
        response = TestClient(app).get("/api/scenarios")
        header = response.headers["server-timing"]
        assert int(re.search(r'db;desc="(\d+) queries"', header).group(1)) >= 1
        assert "total;dur=" in header
    finally:
        Base.metadata.drop_all(engine)
