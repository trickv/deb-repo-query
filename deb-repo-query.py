#!/usr/bin/env python3
import sys
import requests
import re
import gzip
from typing import List, Set, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def get_packages_from_text(text: str) -> Set[str]:
    packages = set()
    for line in text.splitlines():
        if line.startswith("Package:"):
            pkg_name = line.split(":", 1)[1].strip()
            packages.add(pkg_name)
    return packages

def query_dist(base_url: str, codename: str) -> Optional[List[str]]:
    base_dist_url = f"{base_url.rstrip('/')}/dists/{codename}"
    content = None
    
    # Try Release then InRelease
    for filename in ["Release", "InRelease"]:
        try:
            r = requests.get(f"{base_dist_url}/{filename}", timeout=10)
            if r.status_code == 200:
                content = r.text
                break
        except:
            continue
            
    if not content:
        return None

    try:
        components = []
        architectures = []
        
        for line in content.splitlines():
            if line.startswith("Components:"):
                components = line.split(":", 1)[1].strip().split()
            if line.startswith("Architectures:"):
                architectures = line.split(":", 1)[1].strip().split()
        
        # Fallback to infer from file list in Release
        files = re.findall(r'\s\S+\s\d+\s(\S+/binary-(\S+)/Packages(?:\.gz)?)', content)
        found_paths = []
        for path, arch in files:
            found_paths.append(path)
            comp = path.split('/')[0]
            if comp not in components: components.append(comp)
            if arch not in architectures: architectures.append(arch)

        packages = set()
        if found_paths:
            for path in found_paths:
                pkg_url = f"{base_url.rstrip('/')}/dists/{codename}/{path}"
                try:
                    pkg_r = requests.get(pkg_url, timeout=10)
                    if pkg_r.status_code == 200:
                        data = pkg_r.content
                        if path.endswith(".gz"):
                            try:
                                data = gzip.decompress(data)
                            except: continue
                        packages.update(get_packages_from_text(data.decode('utf-8', errors='ignore')))
                except:
                    continue
        else:
            # Try to guess paths
            for comp in components or ["main"]:
                for arch in architectures or ["amd64", "armhf"]:
                    for ext in [".gz", ""]:
                        pkg_url = f"{base_url.rstrip('/')}/dists/{codename}/{comp}/binary-{arch}/Packages{ext}"
                        try:
                            pkg_r = requests.get(pkg_url, timeout=10)
                            if pkg_r.status_code == 200:
                                data = pkg_r.content
                                if ext == ".gz": data = gzip.decompress(data)
                                packages.update(get_packages_from_text(data.decode('utf-8', errors='ignore')))
                                break
                        except: continue
        return sorted(list(packages)) if packages else None
    except:
        return None

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <repo_url> [codename]")
        sys.exit(1)
    
    url = sys.argv[1].rstrip('/')
    specified_codename = sys.argv[2] if len(sys.argv) > 2 else None
    
    found_any = False

    # 1. Try flat repository
    if not specified_codename:
        try:
            for ext in [".gz", ""]:
                pkg_url = f"{url}/Packages{ext}"
                r = requests.get(pkg_url, timeout=5)
                if r.status_code == 200:
                    data = r.content
                    if ext == ".gz": data = gzip.decompress(data)
                    pkgs = get_packages_from_text(data.decode('utf-8', errors='ignore'))
                    if pkgs:
                        for p in sorted(list(pkgs)):
                            print(f"./{p}")
                        found_any = True
                        break
        except: pass

    # 2. Build list of codenames to probe
    to_probe = set()
    if specified_codename:
        to_probe.add(specified_codename)
    else:
        for cn in COMMON_CODENAMES:
            to_probe.add(cn)
        # Try to extract potential codename from URL
        parts = url.split('/')
        if len(parts) > 3:
            to_probe.add(parts[-2])
            to_probe.add(parts[-1])
        # Try to extract from domain
        domain_parts = parts[2].split('.')
        for dp in domain_parts:
            if dp not in ["s3", "amazonaws", "com", "org", "net", "edu"]:
                to_probe.add(dp)
                for sub in dp.split('-'):
                    if len(sub) > 2:
                        to_probe.add(sub)

    # 3. Check for directory listing in dists/
    try:
        dists_r = requests.get(f"{url}/dists/", timeout=5)
        if dists_r.status_code == 200 and "<a href=" in dists_r.text:
            discovered = re.findall(r'href="([^/"]+)/?"', dists_r.text)
            for d in discovered:
                if d not in ["..", "."]:
                    to_probe.add(d)
    except: pass

    # 4. Probe in parallel
    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_cn = {executor.submit(query_dist, url, cn): cn for cn in to_probe if cn}
        for future in as_completed(future_to_cn):
            cn = future_to_cn[future]
            try:
                pkgs = future.result()
                if pkgs:
                    results[cn] = pkgs
            except: pass

    for cn in sorted(results.keys()):
        for p in results[cn]:
            print(f"{cn}/{p}")
        found_any = True
    
    if not found_any:
        if specified_codename:
            print(f"Could not find packages for codename '{specified_codename}'")
        else:
            print("No releases or packages found. Try specifying a codename if you know one.")
            sys.exit(1)

if __name__ == "__main__":
    main()
