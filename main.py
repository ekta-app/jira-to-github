import json
import os
import csv
import pathlib
import subprocess
import sys
import time
from github import Github, Auth, Issue
import requests

# --- Configuration ---
GITHUB_TOKEN = os.getenv("GITHUB_PAT")
if not GITHUB_TOKEN:
    sys.exit(69)

REPO_OWNER = "ekta-app"
REPO_NAME = "ekta"

g = Github(auth=Auth.Token(GITHUB_TOKEN), per_page=100)
repo = g.get_repo(f"{REPO_OWNER}/{REPO_NAME}")

JIRA_WORKSPACE = "https://ekta-app.atlassian.net"
PROJECT_KEY = "EKTA"

BASE_COL = ord('A')-1


def col(_col: str):
    idx = 0
    for i, c in enumerate(_col):
        idx += (ord(c) - BASE_COL) * (26 ** (len(_col) - i - 1))
    return idx-1


COL_SUMMARY = col("A")
COL_KEY = col("B")
COL_TYPE = col("D")
COL_STATUS = col("E")
COL_PRIORITY = col("L")
COL_IS_RESOLVED = col("M")
COL_ASSIGNEE = col("N")
COL_DESCRIPTION = col("AB")
COL_BLOCKED_BY = [col("AR"), col("AS")]
COL_DUPLICATED_BY = [col("AW"), col("AX"), col("AY")]
COL_CAUSED_BY = [col("BA")]
COL_RELATED_TO = [col("BF")]
COL_ATTACHMENT = [col("BI"), col("BJ")]
COL_CIRCUIT = [col("BT"), col("BU"), col("BV"), col("BW"), col("BX")]
COL_REQUESTER = col("CR")
COL_PARENT_KEY = col("EC")

# --- User Mapping ---
with open("users.json") as f:
    USER_MAP = json.load(f)

unmapped_users = set()


class JiraIssue:
    def __init__(self, row: list[str]) -> None:
        self.title = row[COL_SUMMARY]
        self.key = row[COL_KEY]
        self.type = row[COL_TYPE]
        self.priority = row[COL_PRIORITY]
        self.is_resolved = row[COL_IS_RESOLVED] == "Done"
        self.assignee = row[COL_ASSIGNEE]
        self.description = row[COL_DESCRIPTION]
        self.blocked_by = [row[x] for x in COL_BLOCKED_BY if row[x] != ""]
        self.related_to = [row[x] for x in COL_RELATED_TO if row[x] != ""]
        self.circuit = [row[x] for x in COL_CIRCUIT if row[x] != "" and row[x] != "EKTA"]
        self.requester = row[COL_REQUESTER]
        self.parent = row[COL_PARENT_KEY]


# JIRA - GitHub translation
PRIORITY_MAP = {
    "Critical": "Urgent",
    "High": "High",
    "Medium": "Medium",
    "Low": "Low",
}
REQUESTER_MAP = {
    "Team": "Team",
    "Championship": "Circuit manager",
    "EKTA": "Ekta",
    "Competition": "Competition",
}

GH_PRIORITY_FIELD_ID = 14550550
GH_CIRCUIT_FIELD_ID = 45110981
GH_REQUESTER_FIELD_ID = 44279774

# Jira to GitHub issue mgmt
class JiraToGithubMapping:
    j2g_map: dict[str, int] = {}

    def get_gh_issue_number(self, jira_key: str):
        with open("jira_to_gh_issue_map.json") as f:
            self.j2g_map = json.load(f)
            try:
                return self.j2g_map[jira_key]
            except:
                return None

    def set_gh_issue_number(self, jira_key: str, gh_number: int):
        with open("jira_to_gh_issue_map.json") as f:
            self.j2g_map = json.load(f)
        self.j2g_map[jira_key] = gh_number
        with open("jira_to_gh_issue_map.json", "w") as f:
            json.dump(self.j2g_map, f)

j2g_mapping = JiraToGithubMapping()


# --- Main Orchestration ---


def process_migration(csv_file_path: pathlib.Path):
    jira_issues: list[JiraIssue] = []

    # Read the JIRA CSV file
    with open(csv_file_path, mode='r', encoding='utf-8-sig') as f:
        raw_reader = csv.reader(f)
        next(raw_reader)

        for row in raw_reader:
            jira_issues.append(JiraIssue(row))

    # Read the GitHub JSON file
    with open("gh_issues.json") as f:
        gh_issues_local = json.load(f)

    # Pass 1: Create or Reopen Issues
    for jira_issue in jira_issues:
        if jira_issue.is_resolved:
            print(f"{jira_issue.key}: already resolved.")
            continue
        if j2g_mapping.get_gh_issue_number(jira_issue.key):
            print(f"{jira_issue.key}: already processed.")
            continue

        matching_gh_issue = None
        # gh_issues = g.search_issues(f"is:issue repo:{REPO_OWNER}/{REPO_NAME} {jira_issue.title}")
        potential_gh_issues_local = [i for i in gh_issues_local if i["title"].strip() == jira_issue.title]
        if len(potential_gh_issues_local) > 0:
            matching_gh_issue = potential_gh_issues_local[0]

            print(f"{jira_issue.key}: found matching issue number {matching_gh_issue["number"]}", end="")

            # is it already reopened?
            if matching_gh_issue["state"] == "open":
                print(f"...but issue has already been reopened. Moving on")
                j2g_mapping.set_gh_issue_number(jira_issue.key, matching_gh_issue["number"])
                continue

            # is it actually fixed?
            res = subprocess.run(["git", "log", f"--grep='^#{matching_gh_issue["number"]}[: ]'", f"--grep='^EKTA-{jira_issue.key}[: ]'", "-1", "--format=%H"], cwd=pathlib.Path.home()/"source"/"ekta", capture_output=True)
            if res.stdout.strip():
                print(f"...but issue is fixed in commit {res.stdout.decode().strip()}. Moving on")
                j2g_mapping.set_gh_issue_number(jira_issue.key, matching_gh_issue["number"])
                continue

        # Issue fields
        issue_opts = {}
        # custom fields
        issue_field_values = []
        if jira_issue.type != "Sub-task":
            issue_opts["type"] = jira_issue.type
        if jira_issue.priority:
            issue_field_values.append({
                "field_id": GH_PRIORITY_FIELD_ID,
                "value": PRIORITY_MAP[jira_issue.priority]
            })
        if jira_issue.assignee:
            issue_opts["assignees"] = [USER_MAP[jira_issue.assignee]]
        if len(jira_issue.circuit) > 0:
            issue_field_values.append({
                "field_id": GH_CIRCUIT_FIELD_ID,
                "value": jira_issue.circuit,
            })
        if jira_issue.requester:
            issue_field_values.append({
                "field_id": GH_REQUESTER_FIELD_ID,
                "value": REQUESTER_MAP[jira_issue.requester]
            })

        if len(issue_field_values) > 0:
            issue_opts["issue_field_values"] = issue_field_values

        if matching_gh_issue:
            # curl -L \
            #   -X PATCH \
            #   -H "Accept: application/vnd.github+json" \
            #   -H "Authorization: Bearer <YOUR-TOKEN>" \
            #   -H "X-GitHub-Api-Version: 2026-03-10" \
            #   https://api.github.com/repos/OWNER/REPO/issues/ISSUE_NUMBER \
            #   -d '{"title":"Found a bug","body":"I'\''m having a problem with this.","assignees":["octocat"],"milestone":1,"state":"open","labels":["bug"]}'
            r = requests.patch(f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/issues/{matching_gh_issue["number"]}",
                               json={"state": "open", **issue_opts},
                               headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2026-03-10"})
            if r.status_code != 200:
                print(f"Failed to update issue {matching_gh_issue["number"]}")
                sys.exit(69)

            time.sleep(1)
            gh_issue = repo.get_issue(matching_gh_issue["number"])
            gh_issue.create_comment(f"Reopened from Jira migration. Original Jira ticket: {JIRA_WORKSPACE}/browse/{jira_issue.key}")
            time.sleep(1)
            print("...migrated.")
            j2g_mapping.set_gh_issue_number(jira_issue.key, gh_issue.number)
        else:
            body = f"*Migrated from Jira: {JIRA_WORKSPACE}/browse/{jira_issue.key}*"
            if jira_issue.description:
                body = f"{jira_issue.description}\n\n---\n{body}"

            # curl -L \
            #   -X POST \
            #   -H "Accept: application/vnd.github+json" \
            #   -H "Authorization: Bearer <YOUR-TOKEN>" \
            #   -H "X-GitHub-Api-Version: 2026-03-10" \
            #   https://api.github.com/repos/OWNER/REPO/issues \
            #   -d '{"title":"Found a bug","body":"I'\''m having a problem with this.","assignees":["octocat"],"milestone":1,"labels":["bug"]}'
            r = requests.post(f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/issues",
                              json={ "title": jira_issue.title, "body": body, **issue_opts },
                              headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2026-03-10"})
            if r.status_code != 201:
                print(f"Failed to create issue for {jira_issue.key}")
                sys.exit(69)
            created_issue = r.json()

            time.sleep(1)
            print(f"{jira_issue.key}: new issue created {created_issue["number"]}")
            j2g_mapping.set_gh_issue_number(jira_issue.key, created_issue["number"])

    # Pass 2: related to, blocked by, parent link

    for jira_issue in jira_issues:
        if jira_issue.is_resolved:
            print(f"{jira_issue.key}: already resolved.")
            continue

        with open("relationships_processed.json") as f:
            processed = json.load(f)

        gh_issue_num = j2g_mapping.get_gh_issue_number(jira_issue.key)
        if gh_issue_num is None:
            print(f"{jira_issue.key}: No GitHub issue found in mapping.")
            sys.exit(69)
        gh_issue = None

        processed_stuff = []

        if len(jira_issue.related_to) > 0:
            if "related" in processed.get(jira_issue.key, []):
                print(f"{jira_issue.key}: done with related.")
            else:
                # curl -L \
                #   -X POST \
                #   -H "Accept: application/vnd.github+json" \
                #   -H "Authorization: Bearer <YOUR-TOKEN>" \
                #   -H "X-GitHub-Api-Version: 2026-03-10" \
                #   https://api.github.com/repos/OWNER/REPO/issues/ISSUE_NUMBER/relates_to \
                #   -d '{"issue_id":1}'
                for jira_related_to in jira_issue.related_to:
                    gh_related_to_num = j2g_mapping.get_gh_issue_number(jira_related_to)
                    if gh_related_to_num is None:
                        print(f"{jira_issue.key}/#{gh_issue_num}: Could not find related to issue {jira_related_to}")
                    else:
                        gh_related_to = repo.get_issue(gh_related_to_num)
                        time.sleep(1)
                        r = requests.post(f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/issues/{gh_issue_num}/relates_to",
                                        json={"issue_id": gh_related_to.id},
                                        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2026-03-10"})
                        time.sleep(1)
                        if r.status_code != 201:
                            print(f"Failed to set {gh_related_to_num} as related to {gh_issue_num}")

                processed_stuff.append("related")

        if len(jira_issue.blocked_by) > 0:
            if "blocked" in processed.get(jira_issue.key, []):
                print(f"{jira_issue.key}: done with blocked.")
            else:
                # curl -L \
                #   -X POST \
                #   -H "Accept: application/vnd.github+json" \
                #   -H "Authorization: Bearer <YOUR-TOKEN>" \
                #   -H "X-GitHub-Api-Version: 2026-03-10" \
                #   https://api.github.com/repos/OWNER/REPO/issues/ISSUE_NUMBER/dependencies/blocked_by \
                #   -d '{"issue_id":1}'
                for jira_blocked_by in jira_issue.blocked_by:
                    gh_blocked_by_num = j2g_mapping.get_gh_issue_number(jira_blocked_by)
                    if gh_blocked_by_num is None:
                        print(f"{jira_issue.key}/#{gh_issue_num}: Could not find related to issue {jira_blocked_by}")
                    else:
                        gh_blocked_by = repo.get_issue(gh_blocked_by_num)
                        r = requests.post(f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/issues/{gh_issue_num}/dependencies/blocked_by",
                                        json={"issue_id": gh_blocked_by.id},
                                        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2026-03-10"})
                        time.sleep(1)
                        if r.status_code != 201:
                            print(f"Failed to set {gh_blocked_by} as blocking {gh_issue_num}")

                processed_stuff.append("blocked")

        if jira_issue.parent:
            if "parent" in processed.get(jira_issue.key, []):
                print(f"{jira_issue.key}: done with parent.")
            else:
                gh_parent_issue_num = j2g_mapping.get_gh_issue_number(jira_issue.parent)
                if gh_parent_issue_num is None:
                    print(f"{jira_issue.key}: No GitHub issue found in mapping.")
                else:
                    gh_issue = repo.get_issue(gh_issue_num)
                    if gh_issue.parent_issue_url:
                        print(f"{gh_issue_num} already has a parent. Moving on")
                    else:
                        gh_parent_issue = repo.get_issue(gh_parent_issue_num)
                        time.sleep(1)

                        # need issue ID
                        time.sleep(1)

                        gh_parent_issue.add_sub_issue(gh_issue.id)
                        time.sleep(1)

                processed_stuff.append("parent")

        if len(processed_stuff) > 0:
            processed[jira_issue.key] = processed_stuff

        with open("relationships_processed.json", "w") as f:
            json.dump(processed, f)

    print("\n--- Migration Complete ---")


def fetch_all_github_issues():
    issue_data = []
    for gh_issue in g.get_repo("ekta-app/ekta").get_issues(state="all"):
        if gh_issue.pull_request is None:
            issue_data.append(gh_issue.raw_data)

    with open("gh_issues.json", "w") as f:
        json.dump(issue_data, f, indent=4)


if __name__ == "__main__":
    process_migration(pathlib.Path.home() / "Documents" / "Ekta" / "Jira" / "Jira 2026-08-22.csv")

