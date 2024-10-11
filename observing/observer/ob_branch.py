
# This script monitors GitHub repository branches and generates reports on their changes.
# It compares the current state of branches with a previously stored state, identifies new, updated, deleted, and rebased branches,
# and checks for commits merged into the main branch without an associated pull request.

import os
from github import Github
import requests
import sqlite3
import ast

# Fetches the current state of repositories (owner, name, branch, commit hash).
def fetch_current_repo_state(repo_family, github_client):
    current_state = []
    for repo_full_name in repo_family:
        repo = github_client.get_repo(repo_full_name)
        branches = repo.get_branches()
        for branch in branches:
            current_state.append({
                "repo_owner": repo.owner.login,
                "repo_name": repo.name,
                "branch_name": branch.name,
                "commit_hash": branch.commit.sha
            })
    return current_state

# Loads the previous state of branches from a SQLite database.
def load_previous_state(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT repo_owner, repo_name, branch_name, commit_hash
        FROM branch_state
    ''')
    previous_state = [
        {"repo_owner": row[0], "repo_name": row[1], "branch_name": row[2], "commit_hash": row[3]}
        for row in cursor.fetchall()
    ]
    conn.close()
    return previous_state

# Converts a list of commit objects to a list of dictionaries with commit details.
def convert_commits(paginated_commits):
    return [{"name": commit.commit.message.split('\n')[0], "link": commit.html_url, "sha": commit.sha} for commit in paginated_commits]

def find_base_branch(repo, new_branch_name):
    """
    Find a suitable base branch for the new branch.

    Parameters:
    repo: The repository object.
    new_branch_name: The name of the new branch.

    Returns:
    The name of the base branch or the default branch if none is found.
    """
    branches = repo.get_branches()
    for branch in branches:
        if branch.name == new_branch_name:
            continue
        comparison = repo.compare(branch.name, new_branch_name)
        if comparison.behind_by == 0:
            return branch.name
    return repo.default_branch


# Compares the current and previous states of branches to identify changes.
def compare_states(current_state, previous_state, github_client):
    new_branches = []
    updated_branches = []
    deleted_branches = []
    rebased_branches = []
    current_branch_keys = {(b['repo_owner'], b['repo_name'], b['branch_name']) for b in current_state}
    
    for current_branch in current_state:
        repo_full_name = f"{current_branch['repo_owner']}/{current_branch['repo_name']}"
        repo = github_client.get_repo(repo_full_name)
    
        previous_branch = next((b for b in previous_state 
                                if b["repo_owner"] == current_branch["repo_owner"] 
                                and b["repo_name"] == current_branch["repo_name"] 
                                and b["branch_name"] == current_branch["branch_name"]), None)
        
        if previous_branch is None:
            base_branch = find_base_branch(repo, current_branch["branch_name"])
            comparison = repo.compare(base_branch, current_branch["branch_name"])
            if comparison.commits:
                new_branches.append({
                    "repo_owner": current_branch["repo_owner"],
                    "repo_name": current_branch["repo_name"],
                    "branch_name": current_branch["branch_name"],
                    "commit_hash": current_branch["commit_hash"],
                    "commits": convert_commits(comparison.commits)
                })
        elif current_branch["commit_hash"] != previous_branch["commit_hash"]:
            comparison = repo.compare(previous_branch["commit_hash"], current_branch["commit_hash"])
            if comparison.commits:
                updated_branches.append({
                    "repo_owner": current_branch["repo_owner"],
                    "repo_name": current_branch["repo_name"],
                    "branch_name": current_branch["branch_name"],
                    "current_commit_hash": current_branch["commit_hash"],
                    "previous_commit_hash": previous_branch["commit_hash"],
                    "commits": convert_commits(comparison.commits)
                })
                if is_rebased(comparison):
                    rebased_branches.append({
                        "repo_owner": current_branch["repo_owner"],
                        "repo_name": current_branch["repo_name"],
                        "branch_name": current_branch["branch_name"],
                        "commits": convert_commits(comparison.commits)
                    })
    
    for previous_branch in previous_state:
        if (previous_branch['repo_owner'], previous_branch['repo_name'], previous_branch['branch_name']) not in current_branch_keys:
            deleted_branches.append(previous_branch)
    
    return new_branches, updated_branches, deleted_branches, rebased_branches

# Determines if a branch has been rebased by checking the base commit SHA.
def is_rebased(comparison):
    base_commit_sha = comparison.base_commit.sha
    comparison_commit_shas = {commit.sha for commit in comparison.commits}
    
    return base_commit_sha not in comparison_commit_shas

# Finds commits merged into the main branch without an associated pull request.
def find_merged_commits_without_pr(main_repo_name, current_state, previous_state, github_client):
    merged_without_pr = []

    repo = github_client.get_repo(main_repo_name)
    main_branch_name = repo.default_branch

    previous_main_branch = next((b for b in previous_state if b["repo_owner"] == main_repo_name.split('/')[0] and 
                                 b["repo_name"] == main_repo_name.split('/')[1] and 
                                 b["branch_name"] == main_branch_name), None)

    previous_commit_hash = previous_main_branch["commit_hash"] if previous_main_branch else None
    new_commits = fetch_commits(main_repo_name, main_branch_name, previous_commit_hash, github_client)
    
    pulls = repo.get_pulls(state="closed", base=main_branch_name)

    pr_commit_shas = set()
    for pr in pulls:
        pr_commits = pr.get_commits()
        pr_commit_shas.update(commit.sha for commit in pr_commits)

    for commit in new_commits:
        commit_sha = commit.get("sha")
        if commit_sha and commit_sha not in pr_commit_shas:
            merged_without_pr.append(commit)

    return merged_without_pr

# Fetches commits from a specified branch, optionally since a specific commit.
def fetch_commits(repo_full_name, branch_name, since_commit, github_client):
    repo = github_client.get_repo(repo_full_name)
    commits = []
    for commit in repo.get_commits(sha=branch_name):
        if since_commit and commit.sha == since_commit:
            break
        commits.append({
            "name": commit.commit.message.split('\n')[0],
            "link": commit.html_url,
            "sha": commit.sha
        })
    return commits

# Retrieves the GitHub profile image URL for a given repository owner.
def get_github_profile_image(repo_owner):
    url = f"https://api.github.com/users/{repo_owner}"
    response = requests.get(url)
    
    if response.status_code == 200:
        user_data = response.json()
        return user_data.get("avatar_url")
    return None

# Generates a report of branch changes and movements.
def generate_report(new_branches, updated_branches, deleted_branches, rebased_branches):
    fields = []

    if new_branches:
        new_field = {
            "name": "\n\n🌿 **New branches and commits** 🌿\n\n\n",
            "value": "",
            "inline": False
        }
        for branch in new_branches:
            if not branch["commits"]:
                continue
            avatar = get_github_profile_image(branch["repo_owner"])
            repo_full_name = f"{branch['repo_owner']}/{branch['repo_name']}"
            branch_url = f"https://github.com/{repo_full_name}/tree/{branch['branch_name']}"
            new_field["value"] += f"\n* *branch* : [{branch['branch_name']} [{repo_full_name}]]({branch_url})\n"
            for i, commit in enumerate(branch["commits"]):
                new_field["value"] += f"\n * [{commit['name']}]({commit['link']})" if i else f" * [{commit['name']}]({commit['link']})"
        fields.append(new_field)

    if updated_branches:
        updated_field = {
            "name": "\n\n🌿 **Updated branches and commits** 🌿\n",
            "value": "",
            "inline": False
        }
        for branch in updated_branches:
            repo_full_name = f"{branch['repo_owner']}/{branch['repo_name']}"
            branch_url = f"https://github.com/{repo_full_name}/tree/{branch['branch_name']}"
            updated_field["value"] += f"\n* *branch* : [{branch['branch_name']} [{repo_full_name}]]({branch_url})\n"
            for i, commit in enumerate(branch["commits"]):
                updated_field["value"] += f"\n * [{commit['name']}]({commit['link']})" if i else f" * [{commit['name']}]({commit['link']})"
        fields.append(updated_field)

    if deleted_branches:
        deleted_field = {
            "name": "\n\n🌿 **Deleted branches** 🌿\n",
            "value": "",
            "inline": False
        }
        for branch in deleted_branches:
            repo_full_name = f"{branch['repo_owner']}/{branch['repo_name']}"
            branch_url = f"https://github.com/{repo_full_name}/tree/{branch['branch_name']}"
            deleted_field["value"] += f"\n* *branch* : [{branch['branch_name']} [{repo_full_name}]]({branch_url})\n"
        fields.append(deleted_field)

    if rebased_branches:
        rebased_field = {
            "name": "\n\n🌿 **Rebased branches and commits** 🌿\n",
            "value": "",
            "inline": False
        }
        for branch in rebased_branches:
            if not branch["commits"]:
                continue
            repo_full_name = f"{branch['repo_owner']}/{branch['repo_name']}"
            branch_url = f"https://github.com/{repo_full_name}/tree/{branch['branch_name']}"
            rebased_field["value"] += f"\n* *branch* : [{branch['branch_name']} [{repo_full_name}]]({branch_url})\n"
            for i, commit in enumerate(branch["commits"]):
                rebased_field["value"] += f"\n * [{commit['name']}]({commit['link']})" if i else f" * [{commit['name']}]({commit['link']})"
        fields.append(rebased_field)
    embed = {
        "title": "🌟 __ BRANCH REPORT __ 🌟",
        "description": "This is a report of branch movements.",
        "color": 642600,  # Hex color code in decimal
        "fields": fields,
    }
    if not fields:
        embed = None

    return embed

# Generates a report for commits merged into the main branch without a pull request.
def generate_merged_commits_without_pr_report(merged_commits_without_pr):
    field = {
        "name": "The following commits were merged into the main branch of the repo without an associated pull request\n\n",
        "value": "",
        "inline": False
    }

    if merged_commits_without_pr:
        for i, commit in enumerate(merged_commits_without_pr):
            field["value"] += f"\n* [{commit['name']}]({commit['link']})" if i else f"* [{commit['name']}]({commit['link']})"
    else:
        field["value"] += "No commits were merged without a pull request.\n"

    embed = {
        "title": "🔥 __ MERGED COMMITS WITHOUT PR __ 🔥",
        "color": 12910592,  # Hex color code in decimal
        "fields": [field],
        "thumbnail": {
            "url": "https://example.com/image.png"
        },
    }
    if not merged_commits_without_pr:
        embed = None

    return embed

# Main function to generate and post branch reports.
def branch_movements(db_dir, git_access_token, main_repo_name, forks):

    github_client = Github(git_access_token)
    if isinstance(forks, str):
        forks = ast.literal_eval(forks)
    repo_family = [main_repo_name] + forks

    db_path = os.path.join(db_dir, 'repo_fam.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    current_state = fetch_current_repo_state(repo_family, github_client)
    previous_state = load_previous_state(db_path)
    new_branches, updated_branches, deleted_branches, rebased_branches = compare_states(current_state, previous_state, github_client)
    merged_without_pr = find_merged_commits_without_pr(main_repo_name, current_state, previous_state, github_client)

    merged_commits_without_pr_sha = [commit["sha"] for commit in merged_without_pr]
    rebased_branches_result = [
        cur for cur in rebased_branches
        for commit in cur["commits"]
        if commit["sha"] not in merged_commits_without_pr_sha
    ]
    report = generate_report(new_branches, updated_branches, deleted_branches, rebased_branches_result)

    merged_commits_without_pr_report = generate_merged_commits_without_pr_report(merged_without_pr)
    return report, merged_commits_without_pr_report