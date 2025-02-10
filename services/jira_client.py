from jira import JIRA
from datetime import datetime
import re
import openai
import concurrent.futures
import logging
import time

logger = logging.getLogger(__name__)


class JiraAnalyzer:
    def __init__(self, jira_config):

        self.max_retries = 3
        self.timeout = 30  # 30 seconds timeout
        try:
            self.jira = JIRA(
                server=jira_config["server"],
                basic_auth=(jira_config["email"], jira_config["api_token"]),
                validate=True,
                options={
                    "verify": True,
                    "headers": {"Accept": "application/json"},
                    "timeout": self.timeout,
                },
            )

            # Test connection
            myself = self.jira.myself()
            print(f"Successfully connected as: {myself['displayName']}")

            # Get project info
            projects = self.jira.projects()
            print("\nAvailable projects:")
            for project in projects:
                print(f"Project: {project.key} - {project.name}")

            # Get issue
            issue_types = self.jira.issue_types()
            print("\nAvailable issue types:")
            for issue_type in issue_types:
                print(f"Type: {issue_type.name} (id: {issue_type.id})")

        except Exception as e:
            print(f"Failed to connect to Jira: {str(e)}")
            if hasattr(e, "response"):
                print(f"Response status: {e.response.status_code}")
                print(f"Response body: {e.response.text}")
            raise

        self.openai_client = openai.OpenAI()

        # Fields we want to analyze
        self.fields_to_analyze = [
            "summary",
            "components",
            "customfield_11602",  # Customer field
            "description",
            "gpt_summary",  # Add new field
        ]

        # Cache for component data
        self.component_cache = {}
        self.last_refresh = None
        self.CACHE_DURATION = 3600  # 1 hour in seconds
        self.platform_filter = None  # Initialize platform filter

    ####
    # We're missing data in the issues, specifically the Customer field is a Date.
    # Need to figure out how to get the right data out of the Issue.
    ####
    def process_production_issues(self, component_name):
        """Process production issues for a component"""
        try:
            # First, verify the component exists and get its exact name
            all_components = self.get_available_components()
            matching_component = next(
                (c for c in all_components if c.lower() == component_name.lower()), None
            )

            if matching_component:
                component_name = matching_component

            # Construct JQL query with less restrictions
            jql = f"""
                type in (Bug, "Production Issue", Defect)
                AND component = "{component_name}"
                ORDER BY created DESC
            """
            # Search for issues
            issues = self.jira.search_issues(jql)
            total_issues = len(issues) if issues else 0
            if total_issues > 0:
                first_issue = issues[0]

            else:
                return []

            # Initialize data list before using it
            data = []

            def summarize_issue(issue):
                prompt = f"""
                Provide a bug summary with each section on a new line in format:
                Impact: [customer impact]
                Fix: [solution]
                Test: [key test scenario]

                Bug info: 
                Summary: {getattr(issue.fields, 'summary', '')}
                Description: {getattr(issue.fields, 'description', '')}
                Root Cause: {getattr(issue.fields, 'customfield_11554', '')}
                Resolution: {getattr(issue.fields, 'customfield_11596', '')}
                """
                title_link = f"*{getattr(issue.fields, 'summary', '')}*\n<{self.jira._options['server']}/browse/{issue.key}|View in Jira>"
                try:
                    r = self.openai_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=300,
                        temperature=0.7,
                    )
                    gpt_text = r.choices[0].message.content.strip()
                    # Add line breaks between sections and make labels bold
                    gpt_text = gpt_text.replace("Impact:", "\n*Impact:*")
                    gpt_text = gpt_text.replace("Fix:", "\n*Fix:*")
                    gpt_text = gpt_text.replace("Test:", "\n*Test:*")

                    # Add priority information with appropriate emoji
                    priority = getattr(issue.fields, "priority", None)
                    priority_text = ""
                    if priority:
                        if "Class 1" in priority.name:
                            priority_text = "🔴 *Class 1*"
                        elif "Class 2" in priority.name:
                            priority_text = "🟧 *Class 2*"
                        elif "Class 3" in priority.name:
                            priority_text = "🟡 *Class 3*"

                    title_link = f"*{getattr(issue.fields, 'summary', '')}*\n<{self.jira._options['server']}/browse/{issue.key}|View in Jira>"
                    if priority_text:
                        title_link = f"{priority_text} | {title_link}"

                    final_gpt_summary = (
                        f"{title_link}\n{gpt_text}\n"  # Added extra newline at end
                    )
                    
                    # Safely handle customer field which could be a list or single value
                    customer_field = getattr(issue.fields, "customfield_11602", None)
                    customer_value = None
                    if customer_field:
                        if isinstance(customer_field, list) and len(customer_field) > 0:
                            customer_value = customer_field[0].value
                        elif hasattr(customer_field, 'value'):
                            customer_value = customer_field.value
                        
                    return (
                        issue.key,
                        getattr(issue.fields, "summary", ""),
                        [
                            c.name
                            for c in getattr(issue.fields, "components", [])
                            if hasattr(c, "name")
                        ],
                        customer_value,
                        getattr(issue.fields, "description", None),
                        final_gpt_summary,
                    )
                except:
                    final_gpt_summary = f"{title_link}\n"
                    
                    # Safely handle customer field which could be a list or single value
                    customer_field = getattr(issue.fields, "customfield_11602", None)
                    customer_value = None
                    if customer_field:
                        if isinstance(customer_field, list) and len(customer_field) > 0:
                            customer_value = customer_field[0].value
                        elif hasattr(customer_field, 'value'):
                            customer_value = customer_field.value
                            
                    return (
                        issue.key,
                        getattr(issue.fields, "summary", ""),
                        [
                            c.name
                            for c in getattr(issue.fields, "components", [])
                            if hasattr(c, "name")
                        ],
                        customer_value,
                        getattr(issue.fields, "description", None),
                        final_gpt_summary,
                    )

            with concurrent.futures.ThreadPoolExecutor() as executor:
                results = list(executor.map(summarize_issue, issues))

            for key, summary, components, customer, desc, gpt_summary in results:
                data.append(
                    {
                        "key": key,
                        "summary": summary,
                        "components": components,
                        "customer": customer,
                        "description": desc,
                        "gpt_summary": gpt_summary,
                    }
                )

            return data

        except Exception as e:
            if hasattr(e, "response"):
                print(f"Response status: {e.response.status_code}")
                print(f"Response body: {e.response.text}")
            raise

    def extract_flows(self, text, flow_type="general"):
        """Extract meaningful flows from text"""
        if not isinstance(text, str):
            return []

        flows = []

        # Different extraction strategies based on field type
        if flow_type == "steps":
            # Extract numbered steps or bullet points
            steps = re.split(r"\d+\.|•|\*|\n-", text)
            flows.extend([s.strip() for s in steps if len(s.strip()) > 10])

        elif flow_type == "root_cause":
            # Look for cause-effect patterns
            sentences = text.split(".")
            for sentence in sentences:
                if any(
                    word in sentence.lower()
                    for word in ["when", "if", "because", "due to"]
                ):
                    flows.append(sentence.strip())

        elif flow_type == "requirements":
            # Look for requirement-style statements
            sentences = text.split(".")
            for sentence in sentences:
                if any(
                    word in sentence.lower()
                    for word in ["should", "must", "needs to", "expected"]
                ):
                    flows.append(sentence.strip())

        return [f for f in flows if f]  # Remove empty flows

    def get_component_analysis(self, component_name):
        """Get analysis for a specific component with retries"""
        for attempt in range(self.max_retries):
            try:
                # Get all issues
                issues_data = self.process_production_issues(component_name)
                if not issues_data:
                    return {}

                # Extract all unique component names
                all_components = set()
                for issue in issues_data:
                    if isinstance(issue.get('components'), list):
                        all_components.update(issue['components'])

                # Filter for case-insensitive component match
                component_data = [
                    issue
                    for issue in issues_data
                    if isinstance(issue.get('components'), list)
                    and any(
                        c.lower() == component_name.lower() for c in issue['components']
                    )
                ]

                customer_flows = {}
                for idx, issue in enumerate(component_data):
                    customer = issue.get("customer", None)

                    if not customer:
                        continue

                    if customer not in customer_flows:
                        customer_flows[customer] = {
                            "Class 1": [],
                            "Class 2": [],
                            "Class 3": [],
                        }

                    # Extract priority from the GPT summary text which contains the priority emoji
                    gpt_summary = issue.get("gpt_summary", "")

                    if "🔴" in gpt_summary:
                        priority = "Class 1"
                    elif "🟧" in gpt_summary:
                        priority = "Class 2"
                    elif "🟡" in gpt_summary:
                        priority = "Class 3"
                    else:
                        continue

                    # Add the GPT summary to the appropriate priority list
                    if gpt_summary:
                        customer_flows[customer][priority].append(gpt_summary)

                return customer_flows

            except Exception as e:
                if attempt < self.max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff
                    time.sleep(wait_time)
                else:
                    raise

    def get_available_components(self):
        """Get list of all available components with retries"""
        for attempt in range(self.max_retries):
            try:
                # Get all components from JIRA
                projects = self.jira.projects()

                all_components = []
                for project in projects:
                    components = self.jira.project_components(project.key)
                    all_components.extend([comp.name for comp in components])

                return all_components

            except Exception as e:
                if attempt < self.max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff
                    time.sleep(wait_time)
                else:
                    raise

    def format_slack_message(self, analysis):
        def create_message_batch(blocks, batch_number, total_batches):
            header = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f":bar_chart: Analysis Results (Part {batch_number}/{total_batches})",
                        "emoji": True,
                    },
                }
            ]
            if batch_number == total_batches:
                header.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":white_check_mark: Analysis complete!",
                        },
                    }
                )
            return header + blocks

        if not analysis or not isinstance(analysis, dict):
            return []

        all_blocks = []
        for customer, priorities in analysis.items():
            customer_blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"Analysis for {customer}",
                        "emoji": True,
                    },
                }
            ]

            # Process each priority in order
            for priority in ["Class 1", "Class 2", "Class 3"]:
                if priorities[priority]:  # If there are issues in this priority
                    for item in priorities[priority]:
                        # Extract the title and Jira link
                        title_parts = item.split("\n", 1)
                        title = title_parts[0] if len(title_parts) > 0 else ""
                        content_parts = (
                            title_parts[1].split("\n", 1)
                            if len(title_parts) > 1
                            else ["", ""]
                        )
                        jira_link = content_parts[0]
                        details = content_parts[1] if len(content_parts) > 1 else ""

                        # Create a section with title and link
                        customer_blocks.append(
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"{title}\n{jira_link}",
                                },
                            }
                        )

                        # Add details in a code block for clean formatting
                        if details.strip():
                            formatted_details = (
                                details.strip()
                                .replace("*Impact:*", "*IMPACT*")
                                .replace("*Fix:*", "*FIX*")
                                .replace("*Test:*", "*TEST*")
                            )

                            customer_blocks.append(
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": f"```{formatted_details}```",
                                    },
                                }
                            )

                        # Add a small divider between issues
                        customer_blocks.append({"type": "divider"})

            all_blocks.extend(customer_blocks)

        # Split into batches of 45 blocks
        batch_size = 45
        batches = [
            all_blocks[i : i + batch_size]
            for i in range(0, len(all_blocks), batch_size)
        ]

        return [
            create_message_batch(batch, i + 1, len(batches))
            for i, batch in enumerate(batches)
        ]
