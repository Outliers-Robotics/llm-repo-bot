import os
import base64
import requests


OWNER = os.environ["GITHUB_OWNER"]
REPO = os.environ["GITHUB_REPO"]

HEADERS = {
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
}


def search_repo(query: str) -> dict:
    """Search Team 5687's robot code.

    Args:
        query: Code, class, method, subsystem, constant,
            or other term to search for.

    Returns:
        Matching files and their GitHub URLs.
    """

    response = requests.get(
        "https://api.github.com/search/code",
        headers=HEADERS,
        params={
            "q": f"{query} repo:{OWNER}/{REPO}",
            "per_page": 10,
        },
        timeout=15,
    )

    response.raise_for_status()

    return {
        "matches": [
            {
                "path": item["path"],
                "url": item["html_url"],
            }
            for item in response.json()["items"]
        ]
    }


def read_file(path: str) -> dict:
    """Read a file from Team 5687's robot repository.

    Args:
        path: Repository-relative path to the file.

    Returns:
        File contents and GitHub URL.
    """

    response = requests.get(
        f"https://api.github.com/repos/"
        f"{OWNER}/{REPO}/contents/{path}",
        headers=HEADERS,
        timeout=15,
    )

    response.raise_for_status()

    data = response.json()

    contents = base64.b64decode(
        data["content"]
    ).decode(
        "utf-8",
        errors="replace",
    )

    return {
        "path": path,
        "url": data["html_url"],
        "content": contents[:50_000],
    }
