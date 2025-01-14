import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from jira_client import JiraAnalyzer
import logging
import time
import http.server
import json
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
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

# Initialize Slack client
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

# Dictionary to track the last request time for each user
user_request_times = {}


@app.route("/slack/events", methods=["POST"])
def slack_events():
    logger.info("Received request to /slack/events")
    
    logger.info(f"Request headers: {dict(request.headers)}")
    
    data = request.json
    logger.info(f"Received event data: {data}")

    # Handle URL verification challenge
    if "type" in data and data["type"] == "url_verification":
        logger.info(f"Handling verification challenge: {data['challenge']}")
        response = jsonify({"challenge": data["challenge"]})
        logger.info(f"Sending challenge response: {response.get_data()}")
        return response

    if data.get("type") == "event_callback":
        logger.info(f"Received event callback: {data.get('event', {})}")
        event = data.get("event", {})
        
        # Handle both app_mention and direct messages
        if event.get("type") == "app_mention":
            handle_mention(event)
        elif event.get("type") == "message" and event.get("channel_type") == "im":
            # Avoid infinite loops by ignoring bot messages
            if "bot_id" not in event:
                handle_message_event(event)
                
    return "", 200

def handle_message_event(event):
    """Handle incoming message events"""
    if "bot_id" in event or "text" not in event:
        return
        
    text = event["text"].strip()
    channel = event["channel"]
    
    # Handle direct messages
    if event.get("channel_type") in ["im", "group"]:
        # Initial greeting
        if text.lower() in ['hi', 'hello', 'hey']:
            slack_client.chat_postMessage(
                channel=channel,
                text="Hey there! 👋 I'm Customer Insights Bot. I can help you analyze customer issues and provide insights. Just tell me which component you'd like to analyze!"
            )
            return
            
        # Help command
        if text.lower() in ['help', '?']:
            help_text = """Here's how you can use me:
• Just type a component name to analyze it
• Type 'list' to see available components
• Type 'help' to see this message again"""
            slack_client.chat_postMessage(channel=channel, text=help_text)
            return
            
        # List components
        if text.lower() == 'list':
            components = analyzer.get_available_components()
            if components:
                slack_client.chat_postMessage(
                    channel=channel,
                    text=f"Here are the available components:\n• " + "\n• ".join(components)
                )
            else:
                slack_client.chat_postMessage(
                    channel=channel,
                    text="No components found. Please check the JIRA configuration."
                )
            return
            
        # Handle component analysis
        handle_strategy_request(text, channel)

def handle_app_home_opened(event):
    """Handle app home opened events"""
    if event.get("tab") == "messages":
        channel = event["channel"]
        welcome_text = """👋 Hi there! I'm Customer Insights Bot.

I can help you analyze customer issues and provide insights about different components. Here's how to use me:

• Just type a component name to analyze it
• Type 'list' to see available components
• Type 'help' to see this message again

What component would you like to analyze?"""
        slack_client.chat_postMessage(channel=channel, text=welcome_text)


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


def handle_strategy_request(text, channel):
    logger.info(f"Handling strategy request: {text}")
    try:
        if not text:
            return

        component = text.lower()
        if "<@" in component:
            component = component.split(">", 1)[-1]
        component = component.strip("/:- \n\t")

        # Debounce logic: Check if a request has been made for the same component in the channel in the last minute
        now = datetime.now()
        key = (channel, component)
        if key in user_request_times:
            last_request_time = user_request_times[key]
            if now - last_request_time < timedelta(minutes=1):
                logger.info(
                    f"Skipping request for channel {channel} and component {component} due to debounce."
                )
                return
        # Update the last request time
        user_request_times[key] = now

        available_components = analyzer.get_available_components()
        print(f"Available components: {available_components}")

        if not component:
            if available_components:
                slack_client.chat_postMessage(
                    channel=channel,
                    text=f"Please specify a component name. Available components:\n"
                    + f"{', '.join(available_components)}",
                )
            else:
                slack_client.chat_postMessage(
                    channel=channel,
                    text="No components found in JIRA. Please check your JIRA configuration.",
                )
            return

        try:
            loading_msg = slack_client.chat_postMessage(
                channel=channel, text=f"📊 Fetching JIRA data for {component}..."
            )
        except slack_sdk.errors.SlackApiError as e:
            logger.error(f"Slack API error: {e.response['error']}")
            slack_client.chat_postMessage(
                channel=channel, text=f"❌ Error posting message: {e.response['error']}"
            )
            return

        component_map = {c.lower(): c for c in available_components}
        if component.lower() not in component_map:
            slack_client.chat_update(
                channel=channel,
                ts=loading_msg["ts"],
                text=f"❌ Component '{component}' not found.\nAvailable components:\n"
                + f"{', '.join(available_components)}",
            )
            return

        actual_component = component_map[component.lower()]
        analysis = analyzer.get_component_analysis(actual_component, force_refresh=True)

        if not analysis:
            comps = analyzer.get_component_analysis("", force_refresh=True)
            if isinstance(comps, dict) and "components" in comps:
                slack_client.chat_update(
                    channel=channel,
                    ts=loading_msg["ts"],
                    text=f"❌ Component '{component}' not found.\nAvailable components:\n"
                    + f"{', '.join(comps['components'])}",
                )
            return

        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text=f"🧠 Processing insights for {component}...",
        )

        blocks_batches = analyzer.format_slack_message(analysis)
        if blocks_batches:
            slack_client.chat_update(
                channel=channel,
                ts=loading_msg["ts"],
                text=f"📝 Preparing results for {component}...",
            )
            time.sleep(1)
            slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
            for blocks in blocks_batches:
                slack_client.chat_postMessage(channel=channel, blocks=blocks)
        else:
            slack_client.chat_update(
                channel=channel,
                ts=loading_msg["ts"],
                text=f"⚠️ No analysis available for {component}.",
            )

    except Exception as e:
        print(f"ERROR: {e}")
        try:
            slack_client.chat_update(
                channel=channel,
                ts=loading_msg["ts"],
                text=f"❌ Error analyzing {component}: {e}",
            )
        except:
            slack_client.chat_postMessage(
                channel=channel, text=f"Sorry, I encountered an error: {e}"
            )


def handle_mention(event):
    """Handle when the bot is mentioned in a channel"""
    logger.info(f"Handling mention event: {event}")
    
    # Extract the text, removing the bot mention
    text = event.get("text", "")
    if "<@" in text:
        text = text.split(">", 1)[-1].strip()
    
    channel = event.get("channel")
    handle_strategy_request(text, channel)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)
