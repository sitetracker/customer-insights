import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from jira_client import JiraAnalyzer
import logging
import time
import http.server
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(env_path, override=True)  # Force reload

# Verify environment variables
logger.info("Bot environment check:")
logger.info(f"JIRA_SERVER: {os.environ.get('JIRA_SERVER')}")
logger.info(f"JIRA_EMAIL: {os.environ.get('JIRA_EMAIL')}")
logger.info(f"JIRA_API_TOKEN length: {len(os.environ.get('JIRA_API_TOKEN', ''))}")

# Initialize JIRA client
jira_config = {
    "server": os.environ.get("JIRA_SERVER"),
    "email": os.environ.get("JIRA_EMAIL"),
    "api_token": os.environ.get("JIRA_API_TOKEN"),
}

analyzer = JiraAnalyzer(jira_config)

app = Flask(__name__)


@app.route("/", methods=["POST"])
def slack_events():
    data = request.json
    logger.info(f"Received event: {data}")

    if "challenge" in data:
        logger.info("Received challenge request")
        return jsonify({"challenge": data["challenge"]})

    if data.get("type") == "event_callback":
        logger.info(f"Received event callback: {data.get('event', {})}")
        event = data.get("event", {})
        if event.get("type") == "app_mention":
            handle_mention(event)
        elif event.get("type") == "message":
            handle_message(event)
        elif event.get("type") == "message_changed":
            handle_message_changed(event)
    return "", 200


PORT = int(os.environ.get("PORT", 8000))


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Server is running</h1></body></html>")

    def do_POST(self):
        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data)
        if "challenge" in data:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(data["challenge"].encode())
        else:
            self.send_response(404)
            self.end_headers()


def clean_component_name(text):
    """Clean and extract component name from various input formats"""
    logger.info(f"Cleaning component name: {text}")
    # Remove common prefixes and extra whitespace
    text = text.lower().strip()
    prefixes_to_remove = [
        "/customer",
        "/insights",
        "customer",
        "insights",
        "for",
        "analyze",
    ]

    for prefix in prefixes_to_remove:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()

    # Remove any leading/trailing special characters
    text = text.strip("/:- ")
    logger.info(f"Cleaned component name: {text}")
    return text.strip()


def handle_strategy_request(text, say):
    """Common handler for both mentions and messages"""
    logger.info(f"Handling strategy request: {text}")
    try:
        # Skip if text is empty
        if not text:
            return

        # Clean and extract component name by removing bot mention and extra spaces
        component = text.lower()

        # Remove bot mention if present
        if "<@" in component:
            component = component.split(">", 1)[-1]

        # Clean up any extra spaces or special characters
        component = component.strip("/:- \n\t")

        # Get available components first
        available_components = analyzer.get_available_components()
        print(f"Available components: {available_components}")  # Debug print

        if not component:
            if available_components:
                say(
                    f"Please specify a component name. Available components:\n"
                    + f"{', '.join(available_components)}"
                )
            else:
                say(
                    "No components found in JIRA. Please check your JIRA configuration."
                )
            return

        # Start with JIRA fetch status
        loading_msg = say(f"📊 Fetching JIRA data for {component}...")

        # Case-insensitive component matching
        component_map = {c.lower(): c for c in available_components}
        if component.lower() not in component_map:
            app.client.chat_update(
                channel=loading_msg["channel"],
                ts=loading_msg["ts"],
                text=f"❌ Component '{component}' not found.\nAvailable components:\n"
                + f"{', '.join(available_components)}",
            )
            return

        # Use the correctly cased component name
        actual_component = component_map[component.lower()]
        analysis = analyzer.get_component_analysis(actual_component, force_refresh=True)

        if not analysis:
            comps = analyzer.get_component_analysis("", force_refresh=True)
            if isinstance(comps, dict) and "components" in comps:
                app.client.chat_update(
                    channel=loading_msg["channel"],
                    ts=loading_msg["ts"],
                    text=f"❌ Component '{component}' not found.\nAvailable components:\n"
                    + f"{', '.join(comps['components'])}",
                )
            return

        # Update status for processing
        app.client.chat_update(
            channel=loading_msg["channel"],
            ts=loading_msg["ts"],
            text=f"🧠 Processing insights for {component}...",
        )

        blocks_batches = analyzer.format_slack_message(analysis)
        if blocks_batches:
            # Update status before showing results
            app.client.chat_update(
                channel=loading_msg["channel"],
                ts=loading_msg["ts"],
                text=f"📝 Preparing results for {component}...",
            )

            # Short delay to show the preparing message
            time.sleep(1)

            # Delete the loading message
            app.client.chat_delete(channel=loading_msg["channel"], ts=loading_msg["ts"])

            # Send results in batches
            for blocks in blocks_batches:
                say(blocks=blocks)
        else:
            app.client.chat_update(
                channel=loading_msg["channel"],
                ts=loading_msg["ts"],
                text=f"⚠️ No analysis available for {component}.",
            )

    except Exception as e:
        print(f"ERROR: {e}")
        try:
            app.client.chat_update(
                channel=loading_msg["channel"],
                ts=loading_msg["ts"],
                text=f"❌ Error analyzing {component}: {e}",
            )
        except:
            say(f"Sorry, I encountered an error: {e}")


def handle_mention(event):
    text = event.get("text", "")
    logger.info(f"App mentioned with text: {text}")
    # Add logic to handle mention


def handle_message(event):
    text = event.get("text", "")
    logger.info(f"Message received: {text}")
    # Add logic to handle message


def handle_message_changed(event):
    message = event.get("message", {}).get("text", "")
    logger.info(f"Message changed: {message}")
    # Add logic to handle message change


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)
