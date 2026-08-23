# deb-repo-query

A lightweight, zero-configuration CLI tool to discover and list packages in remote Debian repositories.

## Overview

`deb-repo-query` is designed for situations where you have a Debian repository URL but don't know the available release codenames (e.g., `bullseye`, `focal`, `stable`). It is particularly useful for repositories hosted on S3 buckets where directory listing is disabled, making it impossible to browse the `dists/` directory via a web browser.

It was also a fun project to throw at Gemini CLI to see if it could do it. (It did alright).

## Features

- **Automatic Codename Discovery**: Probes for common Debian/Ubuntu codenames and intelligently guesses potential names based on the repository URL and domain.
- **Parallel Probing**: Uses multi-threading to quickly scan for dozens of possible releases simultaneously.
- **S3-Friendly**: Specifically built to handle "hidden" repository structures where `404 Not Found` is returned for directory indexes.
- **Supports All Repo Types**: Works with both standard (`dists/` based) and "flat" repositories.
- **Rich Parsing**: Automatically detects and parses `InRelease`, `Release`, and compressed `Packages.gz` files.
- **Simple Output**: Prints results in a clean `<release>/<package>` format, perfect for piping into other tools.
- **Zero Install**: Inline PEP 723 dependencies mean `uv run` handles Python and `requests` for you.

## Requirements

Only [uv](https://docs.astral.sh/uv/). The script declares its own dependencies inline
([PEP 723](https://peps.python.org/pep-0723/)), so `uv` fetches a suitable Python and installs
`requests` into a cached, throwaway environment on first run. There is nothing to install,
no virtualenv to activate, and nothing added to your system Python.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv yet
```

## Usage

### Basic Query
List all packages in all discovered releases:
```bash
./deb-repo-query.py https://s3.opensky-network.org/website-public-repos/debian
```

The `#!/usr/bin/env -S uv run --script` shebang means you can just execute the file. You can
also run it explicitly, or straight from GitHub without cloning:

```bash
uv run deb-repo-query.py https://s3.opensky-network.org/website-public-repos/debian
uv run https://raw.githubusercontent.com/trickv/deb-repo-query/main/deb-repo-query.py \
    https://s3.opensky-network.org/website-public-repos/debian
```

### Specific Release
If you already know the codename, you can provide it as an optional second argument:
```bash
./deb-repo-query.py https://deb.nodesource.com/node_20.x nodistro
```

### Without uv
If you'd rather not use `uv`, the script is plain Python 3.9+ with a single dependency:
```bash
pip install requests
python3 deb-repo-query.py <repo_url> [codename]
```

## How it Compares

| Feature | `deb-repo-query` | `apt` / `sources.list` | `chdist` (devscripts) |
| :--- | :--- | :--- | :--- |
| **Setup Required** | None | Root access / config files | Manual setup |
| **Discovery** | **Automatic (Probes)** | Manual (Requires known codename) | Manual |
| **Speed** | Fast (Parallel probing) | Slow (Sequential `apt update`) | Moderate |
| **Best For** | Investigating unknown repos | System-wide package management | Testing dependencies safely |

### What Makes it Unique?
Most Debian tools assume you already have the correct `deb` line for your `sources.list`. `deb-repo-query` flips the workflow by actively "hunting" for the repository's contents using a probing engine. This makes it a unique diagnostic tool for DevOps and Security engineers who need to audit or explore third-party repositories without committing to a full system configuration.

## Author

This tool was authored by **Gemini CLI**, an interactive AI-powered programming assistant.

## License

MIT
