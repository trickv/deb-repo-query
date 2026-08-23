#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests>=2.32",
# ]
# ///
"""Discover and query packages in remote Debian repositories."""
import argparse
import functools
import gzip
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence, Tuple
import requests

# Comprehensive list of common codenames
COMMON_CODENAMES = [
    # Debian
    "sarge", "etch", "lenny", "squeeze", "wheezy", "jessie", "stretch", "buster", "bullseye", "bookworm", "trixie", "forky", "sid", "experimental",
    "stable", "testing", "unstable", "oldstable", "oldoldstable",
    # Ubuntu
    "precise", "trusty", "xenial", "bionic", "focal", "jammy", "noble", "oracular", "plucky",
    # Common custom/third-party
    "main", "stable", "repo", "packages", "current", "release",
    "nodesource", "docker", "kubernetes", "cloud-sdk", "opensky", "aptly",
    "raspbian", "raspberrypi",
]

Stanza = Dict[str, str]

# ---------------------------------------------------------------- parsing ---

def parse_stanzas(text: str) -> List[Stanza]:
    """Parse an RFC822-style Packages file into one dict per package stanza."""
    stanzas: List[Stanza] = []
    current: Stanza = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
                current = {}
        elif line[0] in " \t":
            continue  # continuation line; none of the fields we want are folded
        elif ":" in line:
            key, value = line.split(":", 1)
            current[key.strip()] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas

def package_names(stanzas: Sequence[Stanza]) -> List[str]:
    return sorted({s["Package"] for s in stanzas if "Package" in s})

# ----------------------------------------------------- version comparison ---
# Debian versions do not sort lexicographically: 1.100 outranks 1.96, and a '~'
# sorts *before* the empty string so 1.0~rc1 comes before 1.0. This is a direct
# port of dpkg's verrevcmp().

def _char_order(c: str) -> int:
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    return ord(c) + 256

def _compare_fragment(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _char_order(a[i]) if i < len(a) else 0
            bc = _char_order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0

def _split_version(version: str) -> Tuple[int, str, str]:
    epoch = 0
    if ":" in version:
        head, version = version.split(":", 1)
        try:
            epoch = int(head)
        except ValueError:
            pass
    revision = ""
    if "-" in version:
        version, revision = version.rsplit("-", 1)
    return epoch, version, revision

def compare_versions(a: str, b: str) -> int:
    """Compare two Debian version strings the way dpkg --compare-versions does."""
    epoch_a, upstream_a, revision_a = _split_version(a)
    epoch_b, upstream_b, revision_b = _split_version(b)
    if epoch_a != epoch_b:
        return -1 if epoch_a < epoch_b else 1
    return _compare_fragment(upstream_a, upstream_b) or _compare_fragment(revision_a, revision_b)

version_key = functools.cmp_to_key(compare_versions)

# --------------------------------------------------------------- fetching ---

def _fetch(url: str, timeout: int = 10) -> Optional[bytes]:
    """GET a URL, transparently gunzipping it. None on any failure."""
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    data = r.content
    if url.endswith(".gz"):
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            return None
    return data

def _packages_paths(release: str) -> List[List[str]]:
    """Packages files listed in a Release file, as groups of equivalent choices.

    A Release file repeats every path once per checksum algorithm, and usually
    lists both Packages and Packages.gz, so this de-duplicates and prefers the
    compressed variant -- roughly a sixth of the requests and bytes.
    """
    preferred: Dict[str, str] = {}
    for path, _arch in re.findall(r"\s\S+\s\d+\s(\S+/binary-(\S+)/Packages(?:\.gz)?)", release):
        key = path[:-3] if path.endswith(".gz") else path
        if key not in preferred or path.endswith(".gz"):
            preferred[key] = path
    return [[path] for path in preferred.values()]

def _guessed_paths(release: str) -> List[List[str]]:
    """Fallback for Release files that don't enumerate their Packages files."""
    components: List[str] = []
    architectures: List[str] = []
    for line in release.splitlines():
        if line.startswith("Components:"):
            components = line.split(":", 1)[1].split()
        elif line.startswith("Architectures:"):
            architectures = line.split(":", 1)[1].split()
    return [
        [f"{comp}/binary-{arch}/Packages.gz", f"{comp}/binary-{arch}/Packages"]
        for comp in components or ["main"]
        for arch in architectures or ["amd64", "armhf"]
    ]

def fetch_stanzas(base_url: str, codename: Optional[str], workers: int = 4) -> Optional[List[Stanza]]:
    """Every package stanza in one release, or in a flat repo when codename is None."""
    if codename is None:
        for ext in (".gz", ""):
            data = _fetch(f"{base_url}/Packages{ext}", timeout=5)
            if data:
                stanzas = parse_stanzas(data.decode("utf-8", errors="ignore"))
                if stanzas:
                    return stanzas
        return None

    dist_url = f"{base_url}/dists/{codename}"
    release = None
    for filename in ("InRelease", "Release"):
        data = _fetch(f"{dist_url}/{filename}")
        if data:
            release = data.decode("utf-8", errors="ignore")
            break
    if not release:
        return None

    groups = _packages_paths(release) or _guessed_paths(release)

    def fetch_group(paths: Sequence[str]) -> List[Stanza]:
        for path in paths:
            data = _fetch(f"{dist_url}/{path}")
            if data:
                return parse_stanzas(data.decode("utf-8", errors="ignore"))
        return []

    stanzas: List[Stanza] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(groups)))) as pool:
        for result in pool.map(fetch_group, groups):
            stanzas.extend(result)
    return stanzas or None

# -------------------------------------------------------------- discovery ---

def discover_codenames(url: str) -> List[str]:
    """Candidate release names: the common list, hints from the URL, plus any
    that a browsable dists/ index gives away."""
    to_probe = set(COMMON_CODENAMES)

    parts = url.split("/")
    if len(parts) > 3:
        to_probe.add(parts[-2])
        to_probe.add(parts[-1])
    if len(parts) > 2:
        for domain_part in parts[2].split("."):
            if domain_part not in ["s3", "amazonaws", "com", "org", "net", "edu"]:
                to_probe.add(domain_part)
                for sub in domain_part.split("-"):
                    if len(sub) > 2:
                        to_probe.add(sub)

    listing = _fetch(f"{url}/dists/", timeout=5)
    if listing:
        text = listing.decode("utf-8", errors="ignore")
        if "<a href=" in text:
            for name in re.findall(r'href="([^/"]+)/?"', text):
                if name not in ["..", "."]:
                    to_probe.add(name)

    return sorted(name for name in to_probe if name)

def probe_releases(url: str) -> Dict[str, List[Stanza]]:
    """Probe every candidate codename in parallel; keep the ones that answered."""
    results: Dict[str, List[Stanza]] = {}
    candidates = discover_codenames(url)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_stanzas, url, cn, 2): cn for cn in candidates}
        for future in as_completed(futures):
            try:
                stanzas = future.result()
            except Exception:
                continue
            if stanzas:
                results[futures[future]] = stanzas
    return results

def collect_releases(url: str, codename: Optional[str]) -> Dict[str, List[Stanza]]:
    """Resolve a codename to release -> stanzas. '.' means the flat repo,
    None means probe for everything."""
    if codename is None:
        return probe_releases(url)
    stanzas = fetch_stanzas(url, None if codename == "." else codename, workers=8)
    return {codename: stanzas} if stanzas else {}

# ----------------------------------------------------------------- output ---

def print_packages(url: str, codename: Optional[str]) -> int:
    found = False

    # An unqualified query might be pointed at a flat repo, which has no dists/.
    if codename is None:
        flat = fetch_stanzas(url, None)
        if flat:
            for name in package_names(flat):
                print(f"./{name}")
            found = True

    for release, stanzas in sorted(collect_releases(url, codename).items()):
        for name in package_names(stanzas):
            print(f"{release}/{name}")
        found = True

    if not found:
        if codename:
            print(f"Could not find packages for codename '{codename}'", file=sys.stderr)
        else:
            print("No releases or packages found. Try specifying a codename if you know one.", file=sys.stderr)
        return 1
    return 0

def print_versions(url: str, codename: Optional[str], package: str) -> int:
    rows: List[Tuple[str, str, str]] = []
    for release, stanzas in sorted(collect_releases(url, codename).items()):
        architectures: Dict[str, set] = {}
        for stanza in stanzas:
            if stanza.get("Package") == package:
                architectures.setdefault(stanza.get("Version", "?"), set()).add(
                    stanza.get("Architecture", "?")
                )
        for version in sorted(architectures, key=version_key, reverse=True):
            rows.append((release, version, " ".join(sorted(architectures[version]))))

    if not rows:
        where = f"'{codename}'" if codename else "any release"
        print(f"No versions of '{package}' found in {where}.", file=sys.stderr)
        return 1

    # Only name the release when more than one is in play.
    if len({release for release, _, _ in rows}) > 1:
        labels = [f"{release}/{version}" for release, version, _ in rows]
    else:
        labels = [version for _, version, _ in rows]

    width = max(len(label) for label in labels)
    for label, (_, _, architectures) in zip(labels, rows):
        print(f"{label.ljust(width)}  {architectures}".rstrip())
    return 0

# ------------------------------------------------------------------- main ---

EXAMPLES = """\
examples:
  # every package in every release the tool can find
  %(prog)s https://pkgs.tailscale.com/stable/debian

  # every package in one known release
  %(prog)s https://pkgs.tailscale.com/stable/debian trixie

  # every version of one package in one release
  %(prog)s https://pkgs.tailscale.com/stable/debian trixie/tailscale

  # every version of one package, across every release
  %(prog)s https://pkgs.tailscale.com/stable/debian /tailscale
"""

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover and list packages in remote Debian repositories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("repo_url", help="base URL of the Debian repository")
    parser.add_argument(
        "target",
        nargs="?",
        help="a codename (trixie) to list that release's packages, or "
             "<codename>/<package> (trixie/tailscale) to list that package's "
             "available versions. Omit the codename (/tailscale) to search every "
             "release, or use './tailscale' for a flat repository.",
    )
    args = parser.parse_args()

    url = args.repo_url.rstrip("/")
    codename: Optional[str] = None
    package: Optional[str] = None
    if args.target and "/" in args.target:
        head, package = args.target.split("/", 1)
        codename = head.strip() or None
        if not package:
            parser.error(f"no package name in '{args.target}'")
    elif args.target:
        codename = args.target

    if package:
        return print_versions(url, codename, package)
    return print_packages(url, codename)

if __name__ == "__main__":
    sys.exit(main())
