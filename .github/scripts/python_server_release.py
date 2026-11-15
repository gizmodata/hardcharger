#!/usr/bin/env python3
"""Decide whether Python CI can test its pinned, published server download.

An unreleased server is normal on a development PR, but must fail a publishing
run. Authentication, transport and malformed-response errors always fail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def fetch_release(*, repository: str, tag: str, token: str) -> dict | None:
    request = Request(
        url=f"https://api.github.com/repos/{repository}/releases/tags/{quote(tag, safe='')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(url=request, timeout=30) as response:
            return json.load(fp=response)
    except HTTPError as error:
        if error.code != 404:
            raise
        # A private repository without access also answers 404. Confirm repository
        # access instead of treating an authorization failure as an unreleased tag.
        request.full_url = f"https://api.github.com/repos/{repository}"
        with urlopen(url=request, timeout=30) as response:
            json.load(fp=response)
        return None


def available(*, release: dict | None, tag: str, required: bool) -> bool:
    if release is not None:
        if release.get("tag_name") != tag or not isinstance(release.get("draft"), bool):
            raise ValueError("GitHub returned an unexpected release document")
        if not release["draft"]:
            return True
    if required:
        raise ValueError(f"server release {tag} must be published before this run")
    print(
        f"Server {tag} is not published yet; download tests wait for publication.", file=sys.stderr
    )
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    release = fetch_release(
        repository=os.environ["GITHUB_REPOSITORY"], tag=args.tag, token=os.environ["GH_TOKEN"]
    )
    run = available(release=release, tag=args.tag, required=args.required)
    print(f"available={str(run).lower()}")


if __name__ == "__main__":
    main()
