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
import openai

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

# Initialize clients
openai_client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
analyzer = JiraAnalyzer(jira_config)

# Initialize Flask app
app = Flask(__name__)

@app.before_request
def before_request():
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                request.get_json()  # Force parse JSON to catch errors early
            except Exception as e:
                logger.error(f"Error parsing JSON: {e}")
                return jsonify({"error": "Invalid JSON"}), 400
    logger.info(f"Received request to {request.path}")
    logger.info(f"Request headers: {dict(request.headers)}")

# Initialize Slack client
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

# Dictionary to track the last request time for each user
user_request_times = {}

@app.route("/slack/events", methods=["POST"])
def slack_events():
    try:
        content_type = request.headers.get("Content-Type", "")
        
        if "application/x-www-form-urlencoded" in content_type:
            logger.info("Handling form data interaction")
            payload = json.loads(request.form["payload"])
            
            if "actions" in payload:
                action = payload["actions"][0]
                action_id = action["action_id"]
                channel = payload["container"]["channel_id"]
                user = payload["user"]["id"]
                
                # Handle Maps platform selection
                if action_id.startswith("analyze_"):
                    _, platform, component = action_id.split("_")
                    logger.info(f"Processing {platform} analysis for {component}")
                    
                    # Acknowledge button click
                    response = jsonify({"response_action": "clear"})
                    
                    # Post loading message
                    loading_msg = slack_client.chat_postMessage(
                        channel=channel,
                        text=f"🔄 Analyzing maps data...\n_This may take a few moments..._"
                    )
                    
                    try:
                        if platform == "both":
                            analyzer.platform_filter = "Platform = Core"
                            process_analysis("Maps (Web)", channel)
                            
                            analyzer.platform_filter = "Platform = Mobile"
                            process_analysis("Maps (Mobile)", channel)
                        else:
                            component_name = f"Maps ({platform.title()})"
                            analyzer.platform_filter = "Platform = Core" if platform == "web" else "Platform = Mobile"
                            process_analysis(component_name, channel)
                            
                        # Delete loading message after processing
                        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
                        return response, 200
                        
                    except Exception as e:
                        logger.error(f"Error processing analysis: {e}")
                        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
                        slack_client.chat_postMessage(
                            channel=channel,
                            text=f"❌ Error analyzing maps: {str(e)}"
                        )
                        return response, 200
                
                # Handle view selection
                elif action_id.startswith("view_"):
                    _, view_type, component = action_id.split("_", 2)
                    logger.info(f"Processing {view_type} view for {component}")
                    
                    # Acknowledge button click and clear the ephemeral message
                    response = jsonify({"response_action": "clear"})
                    
                    # Post loading message with specific text based on view type
                    loading_text = {
                        "impact": "🎯 Analyzing impact areas...",
                        "bugs": "🔍 Gathering customer bugs...",
                        "tests": "🧪 Generating test scenarios..."
                    }.get(view_type, "Loading...")
                    
                    loading_msg = slack_client.chat_postMessage(
                        channel=channel,
                        text=f"{loading_text}\n_This may take a few moments..._"
                    )
                    
                    try:
                        # Get analysis and process view
                        analysis = analyzer.get_component_analysis(component)
                        blocks = process_view(view_type, component, analysis, channel, user)
                        
                        # Delete loading message and send results
                        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
                        if blocks:  # Only send if there are blocks to send
                            slack_client.chat_postMessage(
                                channel=channel,
                                blocks=blocks
                            )
                        
                        # Return immediately after sending results
                        return response, 200
                        
                    except Exception as e:
                        logger.error(f"Error processing view: {e}")
                        # Delete loading message and show error
                        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
                        slack_client.chat_postMessage(
                            channel=channel,
                            text=f"❌ Error analyzing {view_type}: {str(e)}"
                        )
                        return response, 200
            
            return jsonify({"response_action": "clear"}), 200
            
        else:
            # Handle regular JSON events
            data = request.get_json()
            logger.info(f"Received event data: {data}")

            if "type" in data and data["type"] == "url_verification":
                return jsonify({"challenge": data["challenge"]})

            if data.get("type") == "event_callback":
                event = data.get("event", {})
                if event.get("type") == "app_home_opened":
                    handle_app_home_opened(event)
                elif event.get("type") == "app_mention":
                    handle_mention(event)
                elif event.get("type") == "message" and event.get("channel_type") == "im":
                    if "bot_id" not in event:
                        handle_message_event(event)
                        
            return "", 200
            
    except Exception as e:
        logger.error(f"Error in slack_events: {e}")
        return jsonify({"error": str(e)}), 200

def process_analysis(component, channel):
    """Process component analysis and send results"""
    analysis = analyzer.get_component_analysis(component)
    if analysis:
        blocks_batches = analyzer.format_slack_message(analysis)
        if blocks_batches:
            for blocks in blocks_batches:
                slack_client.chat_postMessage(channel=channel, blocks=blocks)
        else:
            slack_client.chat_postMessage(
                channel=channel,
                text=f"⚠️ No analysis available for {component}."
            )

def handle_message_event(event):
    """Handle incoming message events"""
    if "bot_id" in event or "text" not in event:
        return
        
    text = event["text"].strip()
    channel = event["channel"]
    user = event.get("user")
    
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
• Type 'help' to see this message again"""
            slack_client.chat_postMessage(channel=channel, text=help_text)
            return
            
        # Check if this is a duplicate message
        current_time = time.time()
        last_request_time = user_request_times.get(user, 0)
        
        if current_time - last_request_time < 1:  # Debounce threshold of 1 second
            logger.info(f"Debouncing request from user {user}")
            return
            
        user_request_times[user] = current_time
        
        # Handle component analysis with user ID
        handle_strategy_request(text, channel, user)
        return

def handle_app_home_opened(event):
    """Handle app home opened events"""
    try:
        user_id = event["user"]
        
        # Create the home view
        home_view = {
            "type": "home",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🔍 Welcome to Customer Insights!",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "I help you analyze customer issues and provide insights about different components in your system. Get quick summaries of bugs, their impact, and proposed solutions."
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📚 How to Use",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*1️⃣ Direct Message (DM)*\n• Open a DM with @Customer-Insights\n• Type a component name (e.g. `Job Scheduler`)\n\n*2️⃣ Channel Mention*\n• Type `@Customer-Insights analyze [component]`\n\n*3️⃣ Quick Commands*\n• Type `help` for assistance\n• Type `components` to see available components"
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "✨ Features",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "• *Bug Analysis*: Get summaries of customer-reported issues\n• *Impact Assessment*: Understand how issues affect customers\n• *Solution Tracking*: View proposed fixes and test scenarios\n• *Component Insights*: Analyze specific components of your system"
                    }
                }
            ]
        }
        
        # Publish the home view
        slack_client.views_publish(
            user_id=user_id,
            view=home_view
        )
        
    except Exception as e:
        logger.error(f"Error publishing home view: {e}")

def handle_mention(event):
    """Handle when the bot is mentioned in a channel"""
    logger.info(f"Handling mention event: {event}")
    
    # Extract the text, removing the bot mention
    text = event.get("text", "")
    if "<@" in text:
        text = text.split(">", 1)[-1].strip()
    
    channel = event.get("channel")
    user = event.get("user")
    handle_strategy_request(text, channel, user)

def handle_strategy_request(text, channel, user=None):
    """Handle component analysis requests"""
    logger.info(f"Handling strategy request: {text}")
    try:
        if not text:
            return

        component = text.lower().strip()
        if "<@" in component:
            component = component.split(">", 1)[-1].strip()
            
        # Special handling for Maps component
        if component == "maps":
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🚀 *Select platform for Maps*"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "💻 Web",
                                "emoji": True
                            },
                            "style": "primary",
                            "action_id": f"analyze_web_maps"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📱 Mobile",
                                "emoji": True
                            },
                            "style": "primary", 
                            "action_id": f"analyze_mobile_maps"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🔄 Both",
                                "emoji": True
                            },
                            "action_id": f"analyze_both_maps"
                        }
                    ]
                }
            ]
            # Send as ephemeral message so it disappears after selection
            if user:
                slack_client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    blocks=blocks
                )
            else:
                slack_client.chat_postMessage(
                    channel=channel,
                    blocks=blocks
                )
            return

        # For all other components, show view options immediately
        # Only validate component name format, don't do any processing yet
        if not component.replace(' ', '').isalnum():
            slack_client.chat_postMessage(
                channel=channel,
                text=f"❌ Invalid component name: '{component}'"
            )
            return

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🎯 *Select analysis view for {component}*"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🎯 Impact Areas",
                            "emoji": True
                        },
                        "style": "primary",
                        "action_id": f"view_impact_{component}"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "👥 Customer Bugs",
                            "emoji": True
                        },
                        "style": "primary",
                        "action_id": f"view_bugs_{component}"
                    }
                ]
            }
        ]
        # Send as ephemeral message so it disappears after selection
        if user:
            slack_client.chat_postEphemeral(
                channel=channel,
                user=user,
                blocks=blocks
            )
        else:
            slack_client.chat_postMessage(
                channel=channel,
                blocks=blocks
            )

    except Exception as e:
        logger.error(f"Error in handle_strategy_request: {e}")
        slack_client.chat_postMessage(
            channel=channel, 
            text=f"Sorry, I encountered an error: {e}"
        )

def process_view(view_type, component, analysis, channel, user=None):
    """Process different view types and return formatted blocks"""
    logger.info(f"Processing view type: {view_type} for component: {component}")
    
    # Handle empty analysis case first
    if not analysis:
        return [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"No {view_type} found for this component."
            }
        }]
    
    if view_type == "impact":
        impacts = []
        try:
            for customer, priority_flows in analysis.items():
                for priority, flows in priority_flows.items():
                    for flow in flows:
                        if "*Impact:*" in flow:
                            impact = flow.split("*Impact:*")[1]
                            if "*Fix:*" in impact:
                                impact = impact.split("*Fix:*")[0]
                            if "*Test:*" in impact:
                                impact = impact.split("*Test:*")[0]
                            impact = impact.strip()
                            if impact and impact not in impacts:
                                impacts.append(impact)
            
            # Sort impacts by length for better readability
            impacts.sort(key=len)
            
            # Create a simple, clean message
            message = f"🎯 *Impact Areas for {component}*\n\n"
            if impacts:
                for i, impact in enumerate(impacts, 1):
                    message += f"{i}. {impact}\n"
            else:
                message += "No impact areas found."
                
            message += "\n_For detailed analysis including fixes and test scenarios, check the Customer Bugs view._"
            
            blocks = [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message
                }
            }]
            
            # Send as ephemeral if user is provided
            if user:
                slack_client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    text=message  # Fallback to plain text if blocks fail
                )
                return []  # Return empty blocks since we've already sent the message
            return blocks
            
        except Exception as e:
            logger.error(f"Error processing impact view: {str(e)}")
            error_message = f"Error analyzing impact areas: {str(e)}"
            if user:
                slack_client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    text=error_message
                )
                return []
            return [{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": error_message
                }
            }]
    
    elif view_type == "bugs":
        # Get all blocks for bugs view
        blocks_batches = analyzer.format_slack_message(analysis)
        if blocks_batches:
            # Send each batch in sequence
            for i, blocks in enumerate(blocks_batches):
                if user:
                    slack_client.chat_postEphemeral(
                        channel=channel,
                        user=user,
                        blocks=blocks
                    )
                else:
                    slack_client.chat_postMessage(
                        channel=channel,
                        blocks=blocks
                    )
            # Return empty blocks since we've already sent the messages
            return []
        return [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "No bugs found for this component."
            }
        }]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)
