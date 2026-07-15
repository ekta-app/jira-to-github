import json
import os
import csv
import requests

# --- Configuration ---
GITHUB_TOKEN = os.getenv("GITHUB_PAT")
REPO_OWNER = "ekta-app"
REPO_NAME = "ekta"
JIRA_WORKSPACE = "https://ekta-app.atlassian.net"
PROJECT_KEY = "EKTA"

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

BASE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

# --- CSV Column Mapping ---
# Verify these match the exact headers in row 1 of your Jira CSV export
COL_KEY = "Issue key"
COL_SUMMARY = "Summary"
COL_DESCRIPTION = "Description"
COL_ASSIGNEE = "Assignee"  # Some exports use "Assignee Id" or "Assignee Email"
COL_TYPE = "Issue Type"
COL_STATUS = "Status"
COL_REQUESTER = "Custom field (Requester)"
COL_CIRCUIT = "Custom field (Circuit)"  # can have multiple
COL_GITHUB_LINK = "Custom field (GitHub Link)"  # Update to wherever you stored the original GH link
COL_PARENT_KEY = "Parent id"  # Or "Epic Link" depending on your hierarchy setup
COL_BLOCKED_BY = "Inward issue link (Blocks)"  # can have multiple
COL_DUPLICATED_BY = "Inward issue link (Duplicate)"  # can have multiple
COL_CAUSED_BY = "Inward issue link (Problem/Incident)"
COL_RELATED_TO = "Inward issue link (Relates)"

# --- User Mapping ---
with open("users.json") as f:
    USER_MAP = json.load(f)

unmapped_users = set()
jira_to_gh_issue_map = {}  # Maps Jira Key (e.g. EKTA-12) to new GitHub Issue Number

# --- API Helpers ---


def reopen_github_issue(issue_number, jira_key):
    """Reopens an existing GitHub issue and links it to Jira."""
    url = f"{BASE_URL}/issues/{issue_number}"
    payload = {"state": "open"}
    res = requests.patch(url, json=payload, headers=HEADERS)

    if res.status_code == 200:
        comment_url = f"{url}/comments"
        requests.post(comment_url, json={
                      "body": f"Reopened from Jira migration. Original Jira ticket: {JIRA_WORKSPACE}/browse/{jira_key}"}, headers=HEADERS)
        print(f"Reopened GitHub Issue #{issue_number} for {jira_key}")
    else:
        print(f"Failed to reopen #{issue_number}: {res.text}")


def create_github_issue(row):
    """Creates a new GitHub issue mapping all specified CSV fields."""
    jira_key = row.get(COL_KEY)

    # 1. Map Labels
    labels = []
    if row.get(COL_TYPE):
        labels.append(f"type: {row[COL_TYPE].lower()}")
    if row.get(COL_STATUS):
        labels.append(f"status: {row[COL_STATUS].lower()}")
    if row.get(COL_REQUESTER):
        labels.append(f"requester: {row[COL_REQUESTER].lower()}")
    if row.get(COL_CIRCUIT):
        labels.append(f"circuit: {row[COL_CIRCUIT].lower()}")

    # 2. Map Assignee
    gh_assignee = None
    jira_assignee = row.get(COL_ASSIGNEE)
    if jira_assignee:
        if jira_assignee in USER_MAP:
            gh_assignee = USER_MAP[jira_assignee]
        else:
            unmapped_users.add(jira_assignee)

    # 3. Construct Body (Maintaining external links)
    description = row.get(COL_DESCRIPTION, "No description provided.")
    body = f"{description}\n\n---\n*Migrated from Jira: [{jira_key}]({JIRA_WORKSPACE}/browse/{jira_key})*"

    payload = {
        "title": f"[{jira_key}] {row.get(COL_SUMMARY, 'Untitled')}",
        "body": body,
        "labels": labels,
    }

    if gh_assignee:
        payload["assignees"] = [gh_assignee]

    res = requests.post(f"{BASE_URL}/issues", json=payload, headers=HEADERS)

    if res.status_code == 201:
        new_issue_num = res.json().get("number")
        jira_to_gh_issue_map[jira_key] = new_issue_num
        print(f"Created GitHub Issue #{new_issue_num} for {jira_key}")
        return new_issue_num
    else:
        print(f"Failed to create issue for {jira_key}: {res.text}")
        return None

# --- Beta API Helpers for Relationships ---


def link_sub_issue(parent_gh_id, child_gh_id):
    """Uses GitHub's Beta Sub-issues API/GraphQL to establish hierarchy."""
    # Fallback: Add a comment on the parent referencing the child.
    url = f"{BASE_URL}/issues/{parent_gh_id}/comments"
    requests.post(url, json={"body": f"Tracking sub-task: #{child_gh_id}"}, headers=HEADERS)


def link_blocked_by(blocked_gh_id, blocker_gh_id):
    """Establishes a blocked-by relationship."""
    # Fallback: Add a comment.
    url = f"{BASE_URL}/issues/{blocked_gh_id}/comments"
    requests.post(url, json={"body": f"Blocked by: #{blocker_gh_id}"}, headers=HEADERS)

# --- Main Orchestration ---


def process_migration(csv_file_path):
    issues = []

    # Read the CSV file into memory
    with open(csv_file_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            issues.append(row)

    # Pass 1: Create or Reopen Issues
    for row in issues:
        jira_key = row.get(COL_KEY)
        github_link = row.get(COL_GITHUB_LINK)

        if github_link:
            # Note: You will need to extract the raw issue number from the link format here
            # e.g., if the link is "https://github.com/ekta-app/ekta/issues/123", extract "123"
            gh_issue_num = 123  # Replace with parsed number
            reopen_github_issue(gh_issue_num, jira_key)
            jira_to_gh_issue_map[jira_key] = gh_issue_num
        else:
            create_github_issue(row)

    # Pass 2: Map Hierarchy and Relationships
    for row in issues:
        current_jira_key = row.get(COL_KEY)
        current_gh_id = jira_to_gh_issue_map.get(current_jira_key)

        if not current_gh_id:
            continue

        # Handle Epic/Parent Links
        parent_key = row.get(COL_PARENT_KEY)
        if parent_key and parent_key in jira_to_gh_issue_map:
            parent_gh_id = jira_to_gh_issue_map[parent_key]
            link_sub_issue(parent_gh_id, current_gh_id)

        # Handle Issue Links (Blocked By)
        blocker_key = row.get(COL_BLOCKED_BY)
        if blocker_key and blocker_key in jira_to_gh_issue_map:
            blocker_gh_id = jira_to_gh_issue_map[blocker_key]
            link_blocked_by(current_gh_id, blocker_gh_id)

    print("\n--- Migration Complete ---")
    if unmapped_users:
        print("NOTE: The following Jira users were not in your mapping and were left unassigned:")
        for u in unmapped_users:
            if u.strip():  # Ignore empty strings
                print(f"- {u}")


if __name__ == "__main__":
    process_migration("jira_export.csv")
