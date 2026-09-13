import base64
import logging
import os

import requests


logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }


def search_repo(query: str) -> dict:
    """Search Team 5687's robot code.

    Args:
        query: Code, class, method, subsystem, constant,
            or other term to search for. Use a short identifier or a
            qualifier such as filename:Robot.h, not a natural-language
            sentence. Read relevant matching paths before searching again.

    Returns:
        Matching files and their GitHub URLs.
    """

    response = requests.get(
        "https://api.github.com/search/code",
        headers=_headers(),
        params={
            "q": f"{query} repo:{os.environ['GITHUB_OWNER']}/{os.environ['GITHUB_REPO']}",
            "per_page": 10,
        },
        timeout=(5, 10),
    )

    response.raise_for_status()

    data = response.json()
    matches = [
        {
            "path": item["path"],
            "url": item["html_url"],
        }
        for item in data["items"]
    ]
    total_count = data.get("total_count", len(matches))
    incomplete = data.get("incomplete_results", False)
    logger.info(
        "GitHub search matches=%d total=%d incomplete=%s",
        len(matches), total_count, incomplete,
    )
    return {
        "matches": matches,
        "total_count": total_count,
        "incomplete_results": incomplete,
        "next_step": (
            "Read the most relevant matching file paths before searching again."
            if matches else
            "No matches were returned. Try a shorter identifier or filename once; "
            "if that also finds nothing, ask the user for a class or file path."
        ),
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
        f"{os.environ['GITHUB_OWNER']}/{os.environ['GITHUB_REPO']}/contents/{path}",
        headers=_headers(),
        timeout=(5, 10),
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict) or data.get("type") != "file":
        raise ValueError("The requested path is not a file")
    if data.get("encoding") != "base64" or "content" not in data:
        raise ValueError("GitHub did not return readable file contents")

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
        "truncated": len(contents) > 50_000,
    }
