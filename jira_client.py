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
        print(f"Connecting to Jira server: {jira_config['server']}")
        print(f"Using email: {jira_config['email']}")
        print(f"API token length: {len(jira_config['api_token'])}")

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
            logger.info(
                f"Processing production issues for component: '{component_name}'"
            )

            # First, verify the component exists and get its exact name
            all_components = self.get_available_components()
            matching_component = next(
                (c for c in all_components if c.lower() == component_name.lower()), None
            )

            if matching_component:
                logger.info(f"Found exact component match: {matching_component}")
                component_name = matching_component
            else:
                logger.warning(
                    f"No exact component match found for '{component_name}'. Available components: {all_components}"
                )

            # Construct JQL query with less restrictions
            jql = f"""
                type in (Bug, "Production Issue", Defect)
                AND component = "{component_name}"
                ORDER BY created DESC
            """
            logger.info(f"Executing JQL query: {jql}")

            # Search for issues
            issues = self.jira.search_issues(jql)
            total_issues = len(issues) if issues else 0
            logger.info(f"Found {total_issues} issues for component '{component_name}'")

            if total_issues > 0:
                first_issue = issues[0]
                logger.info("First issue details:")
                logger.info(f"- Key: {first_issue.key}")
                logger.info(f"- Summary: {getattr(first_issue.fields, 'summary', '')}")
                logger.info(
                    f"- Components: {[c.name for c in getattr(first_issue.fields, 'components', [])]}"
                )
                logger.info(
                    f"- Priority: {getattr(first_issue.fields, 'priority', '')}"
                )
                logger.info(f"- Status: {getattr(first_issue.fields, 'status', '')}")
                logger.info(
                    f"- Customer field: {getattr(first_issue.fields, 'customfield_11602', None)}"
                )
            else:
                logger.info("No issues found, returning empty DataFrame")
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
                    return (
                        issue.key,
                        getattr(issue.fields, "summary", ""),
                        [
                            c.name
                            for c in getattr(issue.fields, "components", [])
                            if hasattr(c, "name")
                        ],
                        (
                            getattr(issue.fields, "customfield_11602", None)[0].value
                            if getattr(issue.fields, "customfield_11602", None)
                            else None
                        ),
                        getattr(issue.fields, "description", None),
                        final_gpt_summary,
                    )
                except:
                    final_gpt_summary = f"{title_link}\n"
                    return (
                        issue.key,
                        getattr(issue.fields, "summary", ""),
                        [
                            c.name
                            for c in getattr(issue.fields, "components", [])
                            if hasattr(c, "name")
                        ],
                        (
                            getattr(issue.fields, "customfield_11602", None)[0].value
                            if getattr(issue.fields, "customfield_11602", None)
                            else None
                        ),
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
                logger.info(
                    f"Starting analysis for component: {component_name} (Attempt {attempt + 1}/{self.max_retries})"
                )

                # Get all issues
                logger.info("Fetching issues from JIRA...")
                df = self.process_production_issues(component_name)
                if not df:
                    logger.info(f"No issues found for component: {component_name}")
                    return {}

                # Extract all unique component names
                all_components = set()
                for components in df["components"]:
                    if isinstance(components, list):
                        all_components.update(components)

                logger.info(f"Found components in issues: {all_components}")

                # Filter for case-insensitive component match
                component_data = [
                    issue
                    for issue in df
                    if isinstance(issue["components"], list)
                    and any(
                        c.lower() == component_name.lower() for c in issue["components"]
                    )
                ]
                logger.info(
                    f"After filtering, found {len(component_data)} issues for component {component_name}"
                )

                customer_flows = {}
                for idx, issue in enumerate(component_data):
                    customer = issue.get("customer", None)
                    logger.info(f"Processing issue {idx+1}: Customer = {customer}")

                    if not customer:
                        logger.info("Skipping issue - no customer found")
                        continue

                    if customer not in customer_flows:
                        customer_flows[customer] = {
                            "Class 1": [],
                            "Class 2": [],
                            "Class 3": [],
                        }

                    # Extract priority from the GPT summary text which contains the priority emoji
                    gpt_summary = issue.get("gpt_summary", "")
                    logger.info(f"GPT Summary length: {len(gpt_summary)}")

                    if "🔴" in gpt_summary:
                        priority = "Class 1"
                    elif "🟧" in gpt_summary:
                        priority = "Class 2"
                    elif "🟡" in gpt_summary:
                        priority = "Class 3"
                    else:
                        logger.info("Skipping issue - no valid priority emoji found")
                        continue

                    logger.info(f"Adding {priority} issue to customer {customer}")
                    # Add the GPT summary to the appropriate priority list
                    if gpt_summary:
                        customer_flows[customer][priority].append(gpt_summary)

                logger.info(f"Final customer flows: {list(customer_flows.keys())}")
                logger.info(
                    f"Total issues by customer: {[(c, sum(len(p) for p in f.values())) for c, f in customer_flows.items()]}"
                )
                return customer_flows

            except Exception as e:
                logger.error(
                    f"Error in get_component_analysis (Attempt {attempt + 1}): {str(e)}"
                )
                if attempt < self.max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error("Max retries reached, giving up.")
                    raise

    def get_available_components(self):
        """Get list of all available components with retries"""
        for attempt in range(self.max_retries):
            try:
                # Get all components from JIRA
                projects = self.jira.projects()
                logger.info(f"Found projects: {[p.key for p in projects]}")

                all_components = []
                for project in projects:
                    components = self.jira.project_components(project.key)
                    logger.info(
                        f"Components in {project.key}: {[c.name for c in components]}"
                    )
                    all_components.extend([comp.name for comp in components])

                if not all_components:
                    logger.info("Warning: No components found in any project")
                else:
                    logger.info(f"All available components: {all_components}")

                return all_components

            except Exception as e:
                logger.error(
                    f"Error getting components (Attempt {attempt + 1}): {str(e)}"
                )
                if attempt < self.max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error("Max retries reached, giving up.")
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
