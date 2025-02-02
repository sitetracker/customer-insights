import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from services.jira_client import JiraAnalyzer
import logging
import json
from slack_sdk import WebClient
import openai
from threading import Lock, Thread
import requests
from helpers.downloader import download_bugs, download_impact_areas
from messaging.slack_chatter import SlackChatter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Load environment variables
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, ".env")
load_dotenv(env_path, override=True)  # Force reload

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

# Add after other global variables
response_lock = Lock()
last_response_time = {}
RESPONSE_COOLDOWN = 2  # seconds
cached_components = set()
message_tracking = {}
processed_messages = set()
processed_requests = set()


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


# Initialize Slack client
slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

# Dictionary to track the last request time for each user
user_request_times = {}


@app.route("/slack/events", methods=["POST"])
def slack_events():
    try:
        content_type = request.headers.get("Content-Type", "")

        is_button_click_communication = "application/x-www-form-urlencoded" in content_type
        if not is_button_click_communication:
            # Handle regular JSON events
            data = request.get_json()

            if "type" in data and data["type"] == "url_verification":
                return jsonify({"challenge": data["challenge"]})

            if data.get("type") == "event_callback":
                event = data.get("event", {})
                if event.get("type") == "app_home_opened":
                    handle_app_home_opened(event)
                elif event.get("type") == "app_mention":
                    handle_mention(event)
                elif (
                    event.get("type") == "message" and event.get("channel_type") == "im"
                ):
                    if "bot_id" not in event:
                        handle_message_event(event)

            return "", 200

        if is_button_click_communication:
            payload = json.loads(request.form["payload"])

            # Generate a unique request ID using trigger_id and action_ts
            request_id = (
                f"{payload.get('trigger_id', '')}_{payload.get('action_ts', '')}"
            )

            # Skip if we've seen this request before
            if request_id in processed_requests:
                return jsonify({"response_action": "clear"}), 200

            # Mark this request as processed
            processed_requests.add(request_id)

            if "actions" in payload:
                action = payload["actions"][0]
                action_id = action["action_id"]
                channel = payload["container"]["channel_id"]
                user = payload["user"]["id"]

                # Create a single SlackChatter instance for this request
                slack_chatter = SlackChatter(slack_client, channel)

                # Handle component selection from buttons
                if action_id.startswith("select_component_"):
                    component = action_id.split("select_component_")[1]
                    response_url = payload["response_url"]

                    def process_component_selection(response_url):
                        try:
                            # Send results directly to response_url
                            requests.post(
                                response_url,
                                json={
                                    "blocks": get_analysis_options_blocks(component),
                                    "replace_original": True,
                                    "response_type": "ephemeral",
                                },
                            )
                        except Exception as e:
                            logger.error(f"Error processing component selection: {e}")
                            requests.post(
                                response_url,
                                json={
                                    "text": f"❌ Error loading options: {str(e)}",
                                    "replace_original": True,
                                    "response_type": "ephemeral",
                                },
                            )

                    Thread(
                        target=process_component_selection,
                        args=(response_url,),
                    ).start()
                    return jsonify({"response_action": "clear"}), 200

                # Handle view selection
                elif action_id.startswith("view_"):
                    _, view_type, component = action_id.split("_", 2)
                    response_url = payload["response_url"]

                    def process_view_selection(response_url, chatter):
                        try:
                            # Send initial loading message
                            requests.post(
                                response_url,
                                json={
                                    "text": "🔄 Starting analysis...",
                                    "replace_original": True,
                                    "response_type": "ephemeral",
                                },
                            )

                            if view_type == "impact":
                                chatter.emit_message("📊 Fetching issues from JIRA...")
                                analysis = analyzer.get_component_analysis(component)

                                chatter.emit_message("🎯 Analyzing impact patterns...")
                                blocks = create_view_blocks(
                                    view_type, component, analysis, channel, user
                                )

                                chatter.emit_message("📝 Formatting results...")

                            elif view_type == "bugs":
                                chatter.emit_message(
                                    "🐛 Fetching customer reported issues..."
                                )
                                analysis = analyzer.get_component_analysis(component)

                                chatter.emit_message(
                                    "🤖 Generating bug summaries with AI..."
                                )
                                blocks = create_view_blocks(
                                    view_type, component, analysis, channel, user
                                )

                                chatter.emit_message(
                                    "📝 Formatting customer bug report..."
                                )

                            # Send final results through response_url
                            if blocks:
                                requests.post(
                                    response_url,
                                    json={
                                        "blocks": blocks,
                                        "text": f"Analysis results for {component}:",
                                        "replace_original": True,
                                        "response_type": "ephemeral",
                                    },
                                )
                            else:
                                requests.post(
                                    response_url,
                                    json={
                                        "text": f"No {view_type} data found for {component}",
                                        "replace_original": True,
                                        "response_type": "ephemeral",
                                    },
                                )

                        except Exception as e:
                            logger.error(f"Error processing view: {e}")
                            requests.post(
                                response_url,
                                json={
                                    "text": f"❌ Error analyzing {view_type}: {str(e)}",
                                    "replace_original": True,
                                    "response_type": "ephemeral",
                                },
                            )

                    Thread(
                        target=process_view_selection,
                        args=(response_url, slack_chatter),
                    ).start()
                    return jsonify({"response_action": "clear"}), 200

                # Handle download action
                elif action_id.startswith("download_"):
                    if action_id.startswith("download_bugs_"):
                        component = action_id.split("download_bugs_")[1]
                        return download_bugs(slack_client, analyzer, component, channel)

                    else:  # Handle regular impact areas download
                        component = action_id.split("_", 1)[1]
                        return download_impact_areas(
                            slack_client, analyzer, component, channel
                        )

            return jsonify({"response_action": "clear"}), 200

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
                channel=channel, text=f"⚠️ No analysis available for {component}."
            )


def handle_message_event(event):
    """Handle incoming message events"""
    if "bot_id" in event or "text" not in event:
        return

    text = event["text"].strip()
    channel = event["channel"]
    user = event.get("user")
    ts = event.get("ts", "")

    # Generate a unique key for this message
    message_key = f"{channel}_{user}_{ts}"

    # Skip if we've seen this message before
    if message_key in processed_messages:
        logger.info(f"Skipping duplicate message: {message_key}")
        return

    # Mark this message as processed
    processed_messages.add(message_key)

    # Handle direct messages
    if event.get("channel_type") in ["im", "group"]:
        if text.lower() in ["hi", "hello", "hey"]:
            slack_client.chat_postMessage(
                channel=channel,
                text="Hey there! 👋 I'm Customer Insights Bot. I can help you analyze customer issues and provide insights. Just tell me which component you'd like to analyze!",
            )
            return

        if text.lower() in ["help", "?"]:
            help_text = """Here's how you can use me:
• Just type a component name to analyze it
• Type 'help' to see this message again"""
            slack_client.chat_postMessage(channel=channel, text=help_text)
            return

        # Process the request and return immediately
        handle_strategy_request(text, channel, user)


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
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "I help you analyze customer issues and provide insights about different components in your system. Get quick summaries of bugs, their impact, and proposed solutions.",
                    },
                },
                {"type": "divider"},
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📚 How to Use",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*1️⃣ Direct Message (DM)*\n• Open a DM with @Customer-Insights\n• Type a component name (e.g. `Job Scheduler`)\n\n*2️⃣ Channel Mention*\n• Type `@Customer-Insights analyze [component]`\n\n*3️⃣ Quick Commands*\n• Type `help` for assistance\n• Type `components` to see available components",
                    },
                },
                {"type": "divider"},
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "✨ Features",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "• *Bug Analysis*: Get summaries of customer-reported issues\n• *Impact Assessment*: Understand how issues affect customers\n• *Solution Tracking*: View proposed fixes and test scenarios\n• *Component Insights*: Analyze specific components of your system",
                    },
                },
            ],
        }

        # Publish the home view
        slack_client.views_publish(user_id=user_id, view=home_view)

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

        # Post initial loading message
        loading_msg = slack_client.chat_postMessage(
            channel=channel, text="🤔 Let me look through our component list..."
        )

        search_term = text.lower().strip()
        if "<@" in search_term:
            search_term = search_term.split(">", 1)[-1].strip()

        # Update loading message while checking cache
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text="🔍 Analyzing available components...",
        )

        # Use cached components first for quick response
        global cached_components
        if not cached_components:
            slack_client.chat_update(
                channel=channel,
                ts=loading_msg["ts"],
                text="🔄 Refreshing component list from JIRA...",
            )
            cached_components = set(analyzer.get_available_components())

        # Update message while matching components
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text="🎯 Finding matches for your request...",
        )

        # Enhanced wildcard matching for components
        matching_components = set()
        search_words = search_term.lower().split()
        for comp in cached_components:
            comp_lower = comp.lower()
            comp_words = comp_lower.split()

            # Match if:
            # 1. Search term appears anywhere in component name
            # 2. Component name contains any search word
            # 3. Any word in component starts with search term
            # 4. Search term starts with any word in component
            if any(
                sw in comp_lower  # Full word match
                or comp_lower in sw  # Component is part of search word
                or any(
                    word.startswith(sw) or sw.startswith(word) for word in comp_words
                )  # Prefix match
                or any(sw in word for word in comp_words)  # Partial word match
                for sw in search_words
            ):
                matching_components.add(comp)

        matching_components = sorted(matching_components)

        # Delete the loading message
        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])

        if not matching_components:
            if user:
                slack_client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    text=f"❌ No components found matching: '{text}'",
                )
            else:
                slack_client.chat_postMessage(
                    channel=channel, text=f"❌ No components found matching: '{text}'"
                )
            return

        # Create a single message with all matching components
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📋 *Found {len(matching_components)} matching component{'s' if len(matching_components) > 1 else ''}:*",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": comp, "emoji": True},
                        "value": comp,
                        "action_id": f"select_component_{comp}",
                    }
                    for comp in matching_components
                ],
            },
        ]

        # Send a single message and return immediately
        if user:
            slack_client.chat_postEphemeral(
                channel=channel,
                user=user,
                blocks=blocks,
                text="Found matching components",  # Fallback text
            )
        else:
            slack_client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text="Found matching components",  # Fallback text
            )

    except Exception as e:
        logger.error(f"Error in handle_strategy_request: {e}")
        if user:
            slack_client.chat_postEphemeral(
                channel=channel, user=user, text=f"Sorry, I encountered an error: {e}"
            )
        else:
            slack_client.chat_postMessage(
                channel=channel, text=f"Sorry, I encountered an error: {e}"
            )


def create_view_blocks(view_type, component, analysis, channel, user=None):
    """Process different view types and return formatted blocks"""
    logger.info(f"Processing view type: {view_type} for component: {component}")

    # Handle empty analysis case first
    if not analysis:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"No {view_type} found for this component.",
                },
            }
        ]

    if view_type == "impact":
        try:
            # Extract impacts by class
            impacts_by_class = {"Class 1": [], "Class 2": [], "Class 3": []}

            # First collect all impacts by class
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

                            # Add to appropriate class based on priority
                            if (
                                priority == "Class 1"
                                and impact not in impacts_by_class["Class 1"]
                            ):
                                impacts_by_class["Class 1"].append(impact)
                            elif (
                                priority == "Class 2"
                                and impact not in impacts_by_class["Class 2"]
                            ):
                                impacts_by_class["Class 2"].append(impact)
                            elif (
                                priority == "Class 3"
                                and impact not in impacts_by_class["Class 3"]
                            ):
                                impacts_by_class["Class 3"].append(impact)

            # Create blocks with proper structure
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"Key Impacts for {component}",  # Updated title
                        "emoji": True,
                    },
                }
            ]

            # Process each class
            for class_name, impacts in impacts_by_class.items():
                if impacts:  # Only add section if there are impacts
                    # Add class header
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*{class_name} Impacts:*",
                            },
                        }
                    )

                    # Format all impacts as a single string
                    impacts_text = ""
                    current_length = 0
                    current_batch = []

                    for i, impact in enumerate(impacts, 1):
                        impact_line = f"{i}. {impact}\n"
                        if (
                            current_length + len(impact_line) > 2800
                        ):  # Leave room for formatting
                            # Add current batch as a block
                            if current_batch:
                                blocks.append(
                                    {
                                        "type": "section",
                                        "text": {
                                            "type": "mrkdwn",
                                            "text": f"```{''.join(current_batch)}```",
                                        },
                                    }
                                )
                            current_batch = [impact_line]
                            current_length = len(impact_line)
                        else:
                            current_batch.append(impact_line)
                            current_length += len(impact_line)

                    # Add remaining impacts if any
                    if current_batch:
                        blocks.append(
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"```{''.join(current_batch)}```",
                                },
                            }
                        )

            # Add download button if there are any impacts
            if any(impacts_by_class.values()):
                blocks.append(
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "📥 Download CSV",
                                    "emoji": True,
                                },
                                "action_id": f"download_{component}",
                            }
                        ],
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "No impact areas found."},
                    }
                )

            return blocks

        except Exception as e:
            logger.error(f"Error processing impact view: {str(e)}")
            return [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Error analyzing impact areas: {str(e)}",
                    },
                }
            ]

    elif view_type == "bugs":
        # Get all blocks for bugs view
        blocks_batches = analyzer.format_slack_message(analysis)
        if blocks_batches:
            # Send all batches except the last one
            for i, blocks in enumerate(blocks_batches[:-1]):
                if user:
                    slack_client.chat_postEphemeral(
                        channel=channel,
                        user=user,
                        blocks=blocks,
                        replace_original=(i == 0),
                    )
                else:
                    slack_client.chat_postMessage(channel=channel, blocks=blocks)

            # Add download button to the last batch
            last_batch = blocks_batches[-1]
            last_batch.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📥 Download Bugs CSV",
                                "emoji": True,
                            },
                            "action_id": f"download_bugs_{component}",
                        }
                    ],
                }
            )

            # Send the last batch with download button
            if user:
                slack_client.chat_postEphemeral(
                    channel=channel,
                    user=user,
                    blocks=last_batch,
                    replace_original=False,
                )
            else:
                slack_client.chat_postMessage(channel=channel, blocks=last_batch)
            # Return empty blocks since we've already sent the messages
            return []
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "No bugs found for this component."},
            }
        ]


def get_analysis_options_blocks(component):
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🎯 *Select analysis view for {component}*",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🎯 Key Impacts",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": f"view_impact_{component}",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🐛 Bug Insights",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": f"view_bugs_{component}",
                },
            ],
        },
    ]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)
