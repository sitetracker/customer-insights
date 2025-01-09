import os
import pytest
from dotenv import load_dotenv
from bot import handle_strategy_request

# Load environment variables
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(env_path, override=True)  # Force reload

# Verify environment variables are loaded
print("Environment check:")
print(f"JIRA_SERVER loaded: {bool(os.environ.get('JIRA_SERVER'))}")
print(f"JIRA_EMAIL loaded: {bool(os.environ.get('JIRA_EMAIL'))}")
print(f"JIRA_API_TOKEN loaded: {bool(os.environ.get('JIRA_API_TOKEN'))}")


class MockSay:
    def __init__(self):
        self.last_message = None
        self.last_blocks = None

    def __call__(self, text=None, blocks=None):
        self.last_message = text
        self.last_blocks = blocks


def test_bot_component_analysis():
    mock_say = MockSay()
    handle_strategy_request("test strategy for Job Scheduler", mock_say)
    assert mock_say.last_blocks is not None
    print("\nBot response blocks:")
    print(mock_say.last_blocks)
