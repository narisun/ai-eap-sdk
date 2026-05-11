import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Register shared cloud-mocking fixtures (mock_boto3_client,
# mock_google_aiplatform, real_runtimes_enabled). Test files just take
# the fixture by name — no explicit import needed.
pytest_plugins = ["tests._cloud_mocks"]
