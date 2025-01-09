from jira import JIRA
import pandas as pd
from datetime import datetime
import re
import openai
import concurrent.futures


class JiraAnalyzer:
    def __init__(self, jira_config):
        print(f"Connecting to Jira server: {jira_config['server']}")
        print(f"Using email: {jira_config['email']}")
        print(f"API token length: {len(jira_config['api_token'])}")

        try:
            self.jira = JIRA(
                server=jira_config["server"],
                basic_auth=(jira_config["email"], jira_config["api_token"]),
                validate=True,
                options={"verify": True, "headers": {"Accept": "application/json"}},
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

    ####
    # We're missing data in the issues, specifically the Customer field is a Date.
    # Need to figure out how to get the right data out of the Issue.
    ####
    def process_production_issues(self, component_name=None):
        """Fetch and process production issues using JQL"""
        if not component_name:
            return pd.DataFrame(columns=self.fields_to_analyze + ["key"])

        jql = f"""
            type = Bug
            AND created >= -180d
            AND priority IN ("Class 1", "Class 2")
            AND component = "{component_name}"
            ORDER BY created DESC
        """
        try:
            data = []
            start_at = 0
            chunk_size = 50
            
            # Get initial batch of issues
            issues = self.jira.search_issues(
                jql,
                startAt=start_at,
                maxResults=chunk_size,
                fields="summary,description,components,customfield_11554,customfield_11596,customfield_11602",
            )
            
            # Return empty DataFrame if no issues found
            if not issues:
                return pd.DataFrame(columns=self.fields_to_analyze + ["key"])

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
                    final_gpt_summary = f"{title_link}\n{gpt_text}\n"  # Added extra newline at end
                    return (
                        issue.key,
                        getattr(issue.fields, "summary", ""),
                        [c.name for c in getattr(issue.fields, "components", []) if hasattr(c, "name")],
                        (getattr(issue.fields, "customfield_11602", None)[0].value
                         if getattr(issue.fields, "customfield_11602", None)
                         else None),
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
                            getattr(issue.fields, "customfield_11602", None)[
                                0
                            ].value
                            if getattr(issue.fields, "customfield_11602", None)
                            else None
                        ),
                        getattr(issue.fields, "description", None),
                        final_gpt_summary,
                    )

            with concurrent.futures.ThreadPoolExecutor() as executor:
                results = list(executor.map(summarize_issue, issues))
            
            for key, summary, components, customer, desc, gpt_summary in results:
                data.append({
                    "key": key,
                    "summary": summary,
                    "components": components,
                    "customer": customer,
                    "description": desc,
                    "gpt_summary": gpt_summary,
                })

            return pd.DataFrame(data)
            
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

    def get_component_analysis(self, component_name, force_refresh=False):
        """Get analysis for a specific component"""
        try:
            print(f"Analyzing component: {component_name}")

            # Get all issues
            df = self.process_production_issues(component_name)
            if df.empty:
                return {"components": []}

            # Extract all unique component names
            all_components = set()
            for components in df["components"]:
                if isinstance(components, list):
                    all_components.update(components)

            print(f"Found components: {all_components}")

            # Filter for exact component match
            component_data = df[
                df["components"].apply(
                    lambda x: isinstance(x, list) and component_name in x
                )
            ]
            print(
                f"Found {len(component_data)} issues for component {component_name}",
                flush=True,
            )

            customer_flows = {}
            for _, issue in component_data.iterrows():
                customer = issue.get("customer", None)
                if not customer:
                    continue

                if customer not in customer_flows:
                    customer_flows[customer] = {
                        "user_flows": set(),
                        "technical_flows": set(),
                        "requirements": set(),
                        "gpt_summary": set(),
                    }

                # Extract flows
                if issue.get("steps_to_reproduce"):
                    customer_flows[customer]["user_flows"].update(
                        self.extract_flows(issue["steps_to_reproduce"], "steps")
                    )

                if issue.get("root_cause"):
                    customer_flows[customer]["technical_flows"].update(
                        self.extract_flows(issue["root_cause"], "root_cause")
                    )

                if issue.get("expected_results"):
                    customer_flows[customer]["requirements"].update(
                        self.extract_flows(issue["expected_results"], "requirements")
                    )

                if issue.get("gpt_summary"):
                    customer_flows[customer]["gpt_summary"].add(issue["gpt_summary"])

            # Convert sets to lists for JSON serialization
            for customer in customer_flows:
                customer_flows[customer] = {
                    k: list(v) for k, v in customer_flows[customer].items()
                }

            return customer_flows

        except Exception as e:
            print(f"Error in get_component_analysis: {str(e)}")
            raise

    def get_available_components(self):
        """Get list of all available components"""
        try:
            # Get all components from JIRA
            projects = self.jira.projects()
            print(f"Found projects: {[p.key for p in projects]}")  # Debug print
            
            all_components = []
            for project in projects:
                components = self.jira.project_components(project.key)
                print(f"Components in {project.key}: {[c.name for c in components]}")  # Debug print
                all_components.extend([comp.name for comp in components])
            
            if not all_components:
                print("Warning: No components found in any project")
            
            return all_components
        except Exception as e:
            print(f"Error getting components: {e}")
            if hasattr(e, 'response'):
                print(f"Response status: {e.response.status_code}")
                print(f"Response text: {e.response.text}")
            return []

    def format_slack_message(self, analysis):
        def create_message_batch(blocks, batch_number, total_batches):
            header = [{
                "type": "header",
                "text": {"type": "plain_text", "text": f"� Analysis Results (Part {batch_number}/{total_batches})"}
            }]
            if batch_number == total_batches:
                header.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "✅ Analysis complete!"}
                })
            return header + blocks

        if not analysis or not isinstance(analysis, dict):
            return []

        all_blocks = []
        for customer, flows in analysis.items():
            if not isinstance(flows, dict):
                continue
            
            customer_blocks = [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Analysis for {customer}"}
                }
            ]
            
            for flow_type, flow_list in flows.items():
                if flow_list and isinstance(flow_list, (list, set)):
                    # Only include GPT summaries to keep messages focused
                    if flow_type == "gpt_summary":
                        for item in flow_list:
                            customer_blocks.append({
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": item}
                            })
            
            all_blocks.extend(customer_blocks)
            all_blocks.append({"type": "divider"})

        # Split into batches of 45 blocks (leaving room for headers)
        batch_size = 45
        batches = [all_blocks[i:i + batch_size] for i in range(0, len(all_blocks), batch_size)]
        
        return [create_message_batch(batch, i+1, len(batches)) 
                for i, batch in enumerate(batches)]
