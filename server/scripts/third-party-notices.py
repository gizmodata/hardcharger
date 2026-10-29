#!/usr/bin/env python3
"""THIRD_PARTY_NOTICES.md: the third-party code in Hardcharger's release binaries.

The components, the versions they were reviewed at and where their licence texts come from
are in server/third-party-notices/components.json; the texts themselves, fetched once from
those sources at those versions, in server/third-party-notices/texts/. From them this writes
THIRD_PARTY_NOTICES.md at the top of the repository, which every release artifact ships and
the binaries print with --licenses.

    server/scripts/third-party-notices.py            # write THIRD_PARTY_NOTICES.md
    server/scripts/third-party-notices.py --check    # CI: fail on any drift (no network)
    server/scripts/third-party-notices.py --fetch    # fetch the texts again, then write

--check fails when a dependency recipe pins a version other than the one components.json
records (a bump must be reviewed here: update the pin's "value", run --fetch, read the
diff), when a dependency the build declares belongs to no component, when a text is
missing, or when THIRD_PARTY_NOTICES.md is not what components.json and texts/ produce.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "server" / "third-party-notices"
MANIFEST = DATA / "components.json"
TEXTS = DATA / "texts"
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.md"

# Where the build declares its dependencies: each must be some component's (or not shipped).
FETCHCONTENT_FILES = ["server/CMakeLists.txt", "third_party/chargewire/cpp/CMakeLists.txt"]
EXTENSIONS_FILE = "server/scripts/duckdb-extensions-ios.cmake"
EXTENSION_DEPS_FILE = "server/scripts/build-extension-deps.sh"
VCPKG_FILE = ".github/windows-vcpkg-packages.txt"


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def pin_values(manifest: dict) -> dict[str, str]:
    """The recorded pins, and the values derived from them (postgres_tag: 18_6)."""
    values = {name: pin["value"] for name, pin in manifest["pins"].items()}
    for name, rule in manifest.get("derived", {}).items():
        old, new = rule["replace"]
        values[name] = values[rule["from"]].replace(old, new)
    return values


def render(template: str, values: dict[str, str]) -> str:
    return template.format_map(values)


def text_path(component: dict, text: dict) -> Path:
    return TEXTS / component["id"] / text["file"]


def extract(content: str, text: dict) -> str:
    """The part of a fetched file a text names (`between` two line patterns, inclusive),
    with a `strip` pattern removed from each line's start."""
    lines = content.replace("\r\n", "\n").split("\n")
    if "between" in text:
        start_pattern, end_pattern = (re.compile(p) for p in text["between"])
        start = next(i for i, line in enumerate(lines) if start_pattern.search(line))
        end = next(i for i in range(start, len(lines)) if end_pattern.search(lines[i]))
        lines = lines[start : end + 1]
    if "strip" in text:
        strip = re.compile(text["strip"])
        lines = [strip.sub("", line, count=1) for line in lines]
    return "\n".join(lines).rstrip() + "\n"


def fetch(manifest: dict) -> None:
    values = pin_values(manifest)
    for component in manifest["components"]:
        for text in component["texts"]:
            url = render(template=text["url"], values=values)
            request = urllib.request.Request(url=url, headers={"User-Agent": "hardcharger-notices"})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (https only)
                content = response.read().decode("utf-8")
            target = text_path(component=component, text=text)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(extract(content=content, text=text), encoding="utf-8")
            print(f"fetched {url}", file=sys.stderr)


def fence_for(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"~{3,}", text)), default=0)
    return "~" * max(4, longest + 1)


def generate(manifest: dict) -> str:
    values = pin_values(manifest)
    components = manifest["components"]
    out = [
        "# Third-party software in Hardcharger",
        "",
        "Hardcharger's release binaries, `hardcharger-server` and the command-line client `hardcharger`, "
        "contain the third-party software below, linked in statically. Each component is listed with the "
        "version the release builds, its licence (an SPDX expression where there is one), the binaries and platforms it is in, "
        "and the full text of its licence and notices. Hardcharger itself is under the Apache License 2.0 "
        "(`LICENSE`).",
        "",
        "The web UI that `hardcharger-server` embeds has notices of its own, for the JavaScript packages "
        "it is built from: `THIRD_PARTY_NOTICES-web-ui.txt` beside this file, `/ui/third-party-notices.txt` "
        "on a server with the web UI on, and the end of `hardcharger-server --licenses`.",
        "",
        "Not listed: the C and C++ runtimes and operating-system libraries the binaries use from the system "
        "they run on, and the parts of the GCC runtime (libstdc++, libgcc) or the Microsoft C runtime that "
        "the Linux and Windows builds link statically, which their licences (the GCC Runtime Library "
        "Exception, the Visual Studio redistribution terms) allow without notices.",
        "",
        "Generated by `server/scripts/third-party-notices.py` from `server/third-party-notices/`; do not edit.",
        "",
        "## Summary",
        "",
        "| Component | Version | Licence | In | Platforms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in components:
        out.append(
            f"| [{c['name']}](#{c['id']}) | "
            f"{render(template=c['version'], values=values)} | {c['license']} | "
            f"{', '.join(f'`{b}`' for b in c['in'])} | {c['platforms']} |"
        )
    group = None
    for c in components:
        if c["group"] != group:
            group = c["group"]
            out += ["", f"## {group}"]
        out += [
            "",
            f'<a id="{c["id"]}"></a>',
            "",
            f"### {c['name']}",
            "",
            f"- Version: {render(template=c['version'], values=values)}",
            f"- Licence: {c['license']}",
            f"- Source: <{render(template=c['source'], values=values)}>",
            f"- In: {', '.join(f'`{b}`' for b in c['in'])}; platforms: {c['platforms']}",
        ]
        if "note" in c:
            out += ["", render(template=c["note"], values=values)]
        for text in c["texts"]:
            body = text_path(component=c, text=text).read_text(encoding="utf-8").rstrip()
            fence = fence_for(text=body)
            out += ["", f"{text['file']}:", "", f"{fence}text", body, fence]
    return "\n".join(out) + "\n"


def read_pin(pin: dict) -> list[str]:
    content = (ROOT / pin["file"]).read_text(encoding="utf-8")
    return re.findall(pin["pattern"], content, flags=re.MULTILINE)


def check(manifest: dict) -> list[str]:
    problems: list[str] = []
    pins = manifest["pins"]
    components = manifest["components"]

    # Every recipe pins what was reviewed.
    for name, pin in pins.items():
        try:
            found = read_pin(pin=pin)
        except OSError as error:
            problems.append(f"pin {name}: cannot read {pin['file']}: {error}")
            continue
        if len(found) != 1:
            problems.append(f"pin {name}: {pin['file']} has {len(found)} matches of {pin['pattern']!r}, not 1")
        elif found[0] != pin["value"]:
            problems.append(
                f"{pin['file']} pins {name} {found[0]}, but THIRD_PARTY_NOTICES.md is for {pin['value']}: review "
                f"the new version's licence, set pins.{name}.value in {MANIFEST.relative_to(ROOT)}, run "
                f"server/scripts/third-party-notices.py --fetch and commit THIRD_PARTY_NOTICES.md"
            )
    used = {p for c in components for p in c["pins"]}
    for name in pins:
        if name not in used:
            problems.append(f"pin {name} belongs to no component")
    for c in components:
        for p in c["pins"]:
            if p not in pins:
                problems.append(f"component {c['id']} names an unknown pin {p}")

    # Every dependency the build declares belongs to a component (or is not shipped).
    covered: dict[str, set[str]] = {}
    for c in components:
        for kind, names in c.get("covers", {}).items():
            covered.setdefault(kind, set()).update(names)
    not_shipped = manifest.get("not_shipped", {})

    def require(kind: str, names: list[str], where: str) -> None:
        for name in names:
            if name not in covered.get(kind, set()) and name not in not_shipped.get(kind, {}):
                problems.append(
                    f"{where} declares {name}, which no component in {MANIFEST.relative_to(ROOT)} covers "
                    f"({kind}): add it, with its licence"
                )

    for file in FETCHCONTENT_FILES:
        content = (ROOT / file).read_text(encoding="utf-8")
        require(kind="fetchcontent", names=re.findall(r"FetchContent_Declare\(\s*([\w.+-]+)", content), where=file)
    content = (ROOT / EXTENSIONS_FILE).read_text(encoding="utf-8")
    require(
        kind="duckdb_extensions",
        names=re.findall(r"^duckdb_extension_load\((\w+)", content, flags=re.MULTILINE),
        where=EXTENSIONS_FILE,
    )
    content = (ROOT / EXTENSION_DEPS_FILE).read_text(encoding="utf-8")
    require(
        kind="extension_deps",
        names=re.findall(r"^([A-Z]+)_VERSION=", content, flags=re.MULTILINE),
        where=EXTENSION_DEPS_FILE,
    )
    content = (ROOT / VCPKG_FILE).read_text(encoding="utf-8")
    require(
        kind="vcpkg",
        names=[re.sub(r"\[.*$", "", line.strip()) for line in content.splitlines() if line.strip()],
        where=VCPKG_FILE,
    )

    # Every text is there, and THIRD_PARTY_NOTICES.md is what they make.
    missing = False
    for c in components:
        for text in c["texts"]:
            if not text_path(component=c, text=text).is_file():
                problems.append(f"{text_path(component=c, text=text).relative_to(ROOT)} is missing (run --fetch)")
                missing = True
    if not missing:
        expected = generate(manifest=manifest)
        actual = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if actual != expected:
            problems.append(
                "THIRD_PARTY_NOTICES.md is not what server/third-party-notices/ generates: run "
                "server/scripts/third-party-notices.py and commit the result"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on drift; writes nothing")
    mode.add_argument("--fetch", action="store_true", help="fetch every text again, then write")
    args = parser.parse_args()
    manifest = load_manifest()
    if args.check:
        problems = check(manifest=manifest)
        for problem in problems:
            print(f"third-party notices: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"third-party notices: {len(manifest['components'])} components, every pin and text in step")
        return 0
    if args.fetch:
        fetch(manifest=manifest)
    OUTPUT.write_text(generate(manifest=manifest), encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
