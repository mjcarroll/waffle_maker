#!/usr/bin/env python3
"""Generate the ROS 2 Waffle Dashboard static site.

Usage:
    uv run generate_site.py --output-dir public/
    uv run generate_site.py --output-dir public/ --no-cache
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from fetch_data import fetch_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
TEMPLATE_DIR = SCRIPT_DIR / "templates"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate ROS 2 Waffle Dashboard")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR / "public",
        help="Directory to write generated site (default: public/)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=SCRIPT_DIR / ".cache",
        help="Directory for JSON cache (default: .cache/)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore existing cache and re-fetch everything",
    )
    args = parser.parse_args()

    # Get GitHub token
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API_KEY")
    if not token:
        logger.error("Set GITHUB_TOKEN or GITHUB_API_KEY environment variable")
        return 1

    # Fetch data
    logger.info("Fetching PR data from GitHub...")
    data = fetch_all(token, cache_dir=args.cache_dir, no_cache=args.no_cache)
    logger.info(
        "Fetched %d PRs across %d repos (rate limit: %d/%d remaining)",
        len(data.prs),
        len(data.repos),
        data.rate_limit_remaining,
        data.rate_limit_total,
    )

    # Collect unique repos and labels for filter dropdowns
    repos_sorted = sorted({pr.repo_full_name for pr in data.prs})
    labels_sorted = sorted({l for pr in data.prs for l in pr.labels})

    # Render template
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
    )
    template = env.get_template("index.html")
    html = template.render(
        data=data,
        repos_sorted=repos_sorted,
        labels_sorted=labels_sorted,
    )

    # Write output
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "index.html"
    output_file.write_text(html, encoding="utf-8")

    # Copy CSS alongside HTML
    import shutil
    css_src = TEMPLATE_DIR / "style.css"
    css_dst = output_dir / "style.css"
    shutil.copy2(css_src, css_dst)

    logger.info("Dashboard written to %s", output_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
