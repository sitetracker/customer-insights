import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv
from jira_client import JiraAnalyzer
import logging
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(env_path, override=True)  # Force reload

# Verify environment variables
print("Bot environment check:")
print(f"JIRA_SERVER: {os.environ.get('JIRA_SERVER')}")
print(f"JIRA_EMAIL: {os.environ.get('JIRA_EMAIL')}")
print(f"JIRA_API_TOKEN length: {len(os.environ.get('JIRA_API_TOKEN', ''))}")

# Initialize JIRA client
jira_config = {
    "server": os.environ.get("JIRA_SERVER"),
    "email": os.environ.get("JIRA_EMAIL"),
    "api_token": os.environ.get("JIRA_API_TOKEN"),
}

analyzer = JiraAnalyzer(jira_config)

# Initialize Slack app with token
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))


def clean_component_name(text):
    """Clean and extract component name from various input formats"""
    # Remove common prefixes and extra whitespace
    text = text.lower().strip()
    prefixes_to_remove = ['/customer', '/insights', 'customer', 'insights', 'for', 'analyze']
    
    for prefix in prefixes_to_remove:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    
    # Remove any leading/trailing special characters
    text = text.strip('/:- ')
    return text.strip()

def handle_strategy_request(text, say):
    """Common handler for both mentions and messages"""
    try:
        # Skip if text is empty
        if not text:
            return

        # Clean and extract component name by removing bot mention and extra spaces
        component = text.lower()
        
        # Remove bot mention if present
        if '<@' in component:
            component = component.split('>', 1)[-1]
        
        # Clean up any extra spaces or special characters
        component = component.strip('/:- \n\t')
        
        # Get available components first
        available_components = analyzer.get_available_components()
        print(f"Available components: {available_components}")  # Debug print
        
        if not component:
            if available_components:
                say(f"Please specify a component name. Available components:\n" + 
                    f"{', '.join(available_components)}")
            else:
                say("No components found in JIRA. Please check your JIRA configuration.")
            return
        
        # Start with JIRA fetch status
        loading_msg = say(f"📊 Fetching JIRA data for {component}...")

        # Case-insensitive component matching
        component_map = {c.lower(): c for c in available_components}
        if component.lower() not in component_map:
            app.client.chat_update(
                channel=loading_msg['channel'],
                ts=loading_msg['ts'],
                text=f"❌ Component '{component}' not found.\nAvailable components:\n" +
                     f"{', '.join(available_components)}"
            )
            return

        # Use the correctly cased component name
        actual_component = component_map[component.lower()]
        analysis = analyzer.get_component_analysis(actual_component, force_refresh=True)

        if not analysis:
            comps = analyzer.get_component_analysis("", force_refresh=True)
            if isinstance(comps, dict) and "components" in comps:
                app.client.chat_update(
                    channel=loading_msg['channel'],
                    ts=loading_msg['ts'],
                    text=f"❌ Component '{component}' not found.\nAvailable components:\n" +
                         f"{', '.join(comps['components'])}"
                )
            return

        # Update status for processing
        app.client.chat_update(
            channel=loading_msg['channel'],
            ts=loading_msg['ts'],
            text=f"🧠 Processing insights for {component}..."
        )

        blocks_batches = analyzer.format_slack_message(analysis)
        if blocks_batches:
            # Update status before showing results
            app.client.chat_update(
                channel=loading_msg['channel'],
                ts=loading_msg['ts'],
                text=f"📝 Preparing results for {component}..."
            )
            
            # Short delay to show the preparing message
            time.sleep(1)
            
            # Delete the loading message
            app.client.chat_delete(
                channel=loading_msg['channel'],
                ts=loading_msg['ts']
            )
            
            # Send results in batches
            for blocks in blocks_batches:
                say(blocks=blocks)
        else:
            app.client.chat_update(
                channel=loading_msg['channel'],
                ts=loading_msg['ts'],
                text=f"⚠️ No analysis available for {component}."
            )

    except Exception as e:
        print(f"ERROR: {e}")
        try:
            app.client.chat_update(
                channel=loading_msg['channel'],
                ts=loading_msg['ts'],
                text=f"❌ Error analyzing {component}: {e}"
            )
        except:
            say(f"Sorry, I encountered an error: {e}")


@app.event("app_mention")
def handle_mention(event, say):
    handle_strategy_request(event["text"], say)


@app.event("message")
def handle_message(event, say):
    """Handle regular channel messages"""
    # Only process messages that are not from bots and contain text
    if "text" in event and not event.get("bot_id"):
        # Check if it's a direct message to the bot
        if event.get("channel_type") == "im":
            handle_strategy_request(event["text"], say)


# Add message_changed event handler for edited messages
@app.event("message_changed")
def handle_message_changed(event, say):
    """Handle edited messages"""
    if "message" in event and "text" in event["message"]:
        if event.get("channel_type") == "im":
            handle_strategy_request(event["message"]["text"], say)


if __name__ == "__main__":
    try:
        handler = SocketModeHandler(
            app=app, app_token=os.environ.get("SLACK_APP_TOKEN"), logger=logger
        )
        logger.info("⚡️ CIntel is starting in Socket Mode...")
        handler.start()
    except Exception as e:
        logger.error(f"Error starting app: {e}")
