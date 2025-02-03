from datetime import datetime
from flask import jsonify
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def download_bugs(slack_client, analyzer, component, channel):
    loading_msg = slack_client.chat_postMessage(
        channel=channel, text="🔄 Starting bugs CSV export..."
    )
    try:
        slack_client.chat_update(
            channel=channel, ts=loading_msg["ts"], text="📊 Analyzing customer bugs..."
        )
        analysis = analyzer.get_component_analysis(component)
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text="📝 Formatting bug data for export...",
        )
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"customer_bugs_{component}_{timestamp}.csv"
        csv_content = (
            '"Number","Component","Customer","Priority","Impact","Fix","Test"\n'
        )
        row_number = 1
        for customer, priorities in analysis.items():
            for priority, flows in priorities.items():
                for flow in flows:
                    impact = ""
                    fix = ""
                    test = ""
                    if "*Impact:*" in flow:
                        parts = flow.split("*Impact:*", 1)
                        impact_part = parts[1]
                        if "*Fix:*" in impact_part:
                            impact = impact_part.split("*Fix:*", 0)[0].strip()
                        if "*Fix:*" in flow:
                            fix_part = flow.split("*Fix:*", 1)[1]
                            if "*Test:*" in fix_part:
                                fix = fix_part.split("*Test:*", 0)[0].strip()
                                test = flow.split("*Test:*", 1)[1].strip()
                            else:
                                fix = fix_part.strip()
                    safe_impact = impact.replace('"', '""')
                    safe_fix = fix.replace('"', '""')
                    safe_test = test.replace('"', '""')
                    safe_customer = customer.replace('"', '""')
                    csv_content += f'{row_number},"{component}","{safe_customer}","{priority}","{safe_impact}","{safe_fix}","{safe_test}"\n'
                    row_number += 1
        slack_client.chat_update(
            channel=channel, ts=loading_msg["ts"], text="📤 Uploading CSV file..."
        )
        response = slack_client.files_upload_v2(
            channel=channel,
            content=csv_content,
            filename=filename,
            title=f"Customer Bugs - {component}",
            initial_comment=f"📥 Here's your customer bugs CSV export for {component}",
        )
        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
        return response
    except Exception as e:
        logger.error(f"Error downloading bugs CSV: {e}")
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text=f"❌ Error downloading bugs CSV: {str(e)}",
        )
        return


def download_impact_areas(slack_client, analyzer, component, channel):
    loading_msg = slack_client.chat_postMessage(
        channel=channel, text="🔄 Starting CSV export process..."
    )
    try:
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text="📊 Analyzing component data...",
        )
        analysis = analyzer.get_component_analysis(component)
        impacts = []
        for customer, priority_flows in analysis.items():
            for priority, flows in priority_flows.items():
                for flow in flows:
                    if "*Impact:*" in flow:
                        impact = flow.split("*Impact:*", 1)[1]
                        if "*Fix:*" in impact:
                            impact = impact.split("*Fix:*", 0)[0]
                        if "*Test:*" in impact:
                            impact = impact.split("*Test:*", 0)[0]
                        impact = impact.strip()
                        if impact and impact not in impacts:
                            impacts.append(impact)
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text="📝 Formatting data for export...",
        )
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"impact_areas_{component}_{timestamp}.csv"
        csv_content = '"Number","Component","Impact Summary"\n'
        for i, impact in enumerate(impacts, 1):
            safe_impact = impact.replace('"', '""')
            csv_content += f'{i},"{component}","{safe_impact}"\n'
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text="📤 Uploading CSV file...",
        )
        response = slack_client.files_upload_v2(
            channel=channel,
            content=csv_content,
            filename=filename,
            title=f"Impact Areas - {component}",
            initial_comment=f"📥 Here's your CSV export for {component}",
        )
        slack_client.chat_delete(channel=channel, ts=loading_msg["ts"])
        return response
    except Exception as e:
        logger.error(f"Error downloading CSV: {e}")
        slack_client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text=f"❌ Error downloading CSV: {str(e)}",
        )
        return
