#!/usr/bin/env python3
"""Fetch PR data from GitHub for all repos in ros2.repos.

Supports a local JSON cache so subsequent runs only re-fetch repos
that have been updated since the last fetch.
"""

import json
import re
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml
from github import Github

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

ROS2_REPOS_URL = (
    "https://raw.githubusercontent.com/ros2/ros2/rolling/ros2.repos"
)

EXCLUDED_LABELS = {"backlog", "help wanted", "more-information-needed"}
EXCLUDED_REPOS = {
    "ros2/safety_working_group",
    "ros2/rmw_iceoryx",
    "ros2/rclc",
    "ros2/cartographer_ros",
    "ros2/cartographer",
    "ros2/domain_bridge",
    "ros2/ros1_bridge",
}

# Regex to detect ci.ros2.org job URLs in PR comments
CI_URL_PATTERN = re.compile(
    r"https?://ci\.ros2\.org/job/([\w-]+)/(\d+)/?([\w-]*)"
)

STALE_DAYS = 14
DEFAULT_CACHE_DIR = Path(".cache")


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class CIRun:
    """A CI run triggered by a maintainer via comment."""
    job_name: str
    build_number: int
    url: str
    platform: str  # e.g. "Linux", "Windows", "Linux-aarch64"


@dataclass
class CITrigger:
    """A CI trigger event from a PR comment."""
    commenter: str
    commenter_url: str
    comment_date: str  # ISO format string for JSON serialization
    comment_url: str
    runs: list[CIRun] = field(default_factory=list)


@dataclass
class PRData:
    """Enriched pull request data."""
    number: int
    title: str
    url: str
    repo_full_name: str
    author: str
    author_url: str
    created_at: str  # ISO format
    updated_at: str  # ISO format
    labels: list[str]
    draft: bool
    # Review info
    review_state: str  # "approved", "changes_requested", "commented", "none"
    reviewers: list[dict]  # [{name, state, url}]
    # CI info
    ci_triggers: list[CITrigger] = field(default_factory=list)
    # People
    assignees: list[str] = field(default_factory=list)
    requested_reviewers: list[str] = field(default_factory=list)
    # Computed
    days_since_update: int = 0
    category: str = "needs_review"


@dataclass
class RepoStats:
    """Summary stats for a repository."""
    full_name: str
    url: str
    open_pr_count: int = 0
    open_issue_count: int = 0


@dataclass
class DashboardData:
    """All data needed to render the dashboard."""
    generated_at: str = ""  # ISO format
    prs: list[PRData] = field(default_factory=list)
    repos: list[RepoStats] = field(default_factory=list)
    rate_limit_remaining: int = 0
    rate_limit_total: int = 0

    @property
    def ready_to_merge(self) -> list[PRData]:
        return [p for p in self.prs if p.category == "ready_to_merge"]

    @property
    def ci_triggered(self) -> list[PRData]:
        return [p for p in self.prs if p.ci_triggers]

    @property
    def needs_review(self) -> list[PRData]:
        return [p for p in self.prs if p.category == "needs_review"]

    @property
    def changes_requested(self) -> list[PRData]:
        return [p for p in self.prs if p.category == "changes_requested"]

    @property
    def stale(self) -> list[PRData]:
        return [p for p in self.prs if p.days_since_update >= STALE_DAYS]

    @property
    def needs_ci(self) -> list[PRData]:
        return [p for p in self.prs if not p.ci_triggers and p.category != "changes_requested"]

    @property
    def scoreboard(self) -> list[dict]:
        """Compute maintainer engagement scoreboard from PR data.

        Point system:
          🔬 CI Trigger:        3 pts per CI run triggered
          ✅ Approval:          5 pts per approval given
          🔧 Changes Requested: 3 pts per changes-requested review
          👀 Review (comment):  2 pts per comment-only review
          🦸 Stale PR Rescue:   5 bonus pts for reviewing a 14+ day stale PR
          🌐 Cross-Repo:        2 bonus pts per unique repo reviewed (breadth)
        """
        scores: dict[str, dict] = {}
        cutoff = datetime.fromisoformat(self.generated_at) - timedelta(days=365)

        def _get(user: str) -> dict:
            if user not in scores:
                scores[user] = {
                    "user": user,
                    "ci_triggers": 0,
                    "approvals": 0,
                    "changes_requested": 0,
                    "reviews": 0,
                    "stale_rescues": 0,
                    "repos_touched": set(),
                    "total": 0,
                }
            return scores[user]

        for pr in self.prs:
            is_stale = pr.days_since_update >= STALE_DAYS

            # CI triggers (last 12 months only)
            for trigger in pr.ci_triggers:
                trigger_date = datetime.fromisoformat(trigger.comment_date)
                if trigger_date < cutoff:
                    continue
                s = _get(trigger.commenter)
                s["ci_triggers"] += 1
                s["repos_touched"].add(pr.repo_full_name)

            # Reviews
            for reviewer in pr.reviewers:
                name = reviewer["name"]
                state = reviewer["state"]
                s = _get(name)
                s["repos_touched"].add(pr.repo_full_name)

                if state == "approved":
                    s["approvals"] += 1
                    if is_stale:
                        s["stale_rescues"] += 1
                elif state == "changes_requested":
                    s["changes_requested"] += 1
                    if is_stale:
                        s["stale_rescues"] += 1
                else:
                    s["reviews"] += 1

        # Compute totals
        result = []
        for s in scores.values():
            cross_repo_count = len(s["repos_touched"])
            s["cross_repo_count"] = cross_repo_count
            s["total"] = (
                s["ci_triggers"] * 3
                + s["approvals"] * 5
                + s["changes_requested"] * 3
                + s["reviews"] * 2
                + s["stale_rescues"] * 5
                + cross_repo_count * 2
            )
            # Convert set to count for JSON compat
            del s["repos_touched"]
            result.append(s)

        result.sort(key=lambda x: x["total"], reverse=True)
        return result


# ── Cache ────────────────────────────────────────────────────────────────────

class Cache:
    """Per-repo JSON file cache for dashboard data.

    Each repo is stored as a separate JSON file under cache_dir/,
    named by replacing '/' with '__' (e.g. ament__ament_cmake.json).
    A _meta.json file tracks fetched_at timestamps.

    This produces minimal git diffs when only some repos change.
    """

    META_FILE = "_meta.json"

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR):
        self.cache_dir = cache_dir
        self._meta: dict = self._load_meta()
        self._migrate_legacy()

    @staticmethod
    def _repo_filename(full_name: str) -> str:
        """Convert 'org/repo' to 'org__repo.json'."""
        return full_name.replace("/", "__") + ".json"

    def _repo_path(self, full_name: str) -> Path:
        return self.cache_dir / self._repo_filename(full_name)

    def _load_meta(self) -> dict:
        meta_file = self.cache_dir / self.META_FILE
        if meta_file.exists():
            try:
                raw = json.loads(meta_file.read_text(encoding="utf-8"))
                logger.info("Loaded cache meta from %s", meta_file)
                return raw
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Cache meta corrupt (%s), starting fresh", e)
        return {"fetched_at": {}}

    def _migrate_legacy(self):
        """Migrate from single-file cache to per-repo files."""
        legacy = self.cache_dir / "dashboard_cache.json"
        if not legacy.exists():
            return
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            logger.info("Migrating legacy cache to per-repo files...")
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            # Migrate fetched_at
            if "fetched_at" in raw:
                self._meta["fetched_at"] = raw["fetched_at"]
                self._save_meta()

            # Migrate per-repo data
            for full_name, repo_data in raw.get("repos", {}).items():
                repo_file = self._repo_path(full_name)
                repo_file.write_text(
                    json.dumps(repo_data, indent=2, default=str),
                    encoding="utf-8",
                )

            legacy.unlink()
            logger.info("Legacy cache migrated and removed")
        except Exception as e:
            logger.warning("Could not migrate legacy cache: %s", e)

    def _save_meta(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta_file = self.cache_dir / self.META_FILE
        meta_file.write_text(
            json.dumps(self._meta, indent=2, default=str),
            encoding="utf-8",
        )

    def save(self):
        """Save meta file (repo files are saved individually)."""
        self._save_meta()
        logger.info("Cache meta saved to %s", self.cache_dir / self.META_FILE)

    def get_repo_pushed_at(self, full_name: str) -> str | None:
        """Get the pushed_at timestamp we last saw for a repo."""
        return self._meta.get("fetched_at", {}).get(full_name)

    def set_repo_pushed_at(self, full_name: str, pushed_at: str):
        self._meta.setdefault("fetched_at", {})[full_name] = pushed_at

    def get_repo_prs(self, full_name: str) -> list[dict] | None:
        """Get cached PR data for a repo."""
        repo_file = self._repo_path(full_name)
        if not repo_file.exists():
            return None
        try:
            data = json.loads(repo_file.read_text(encoding="utf-8"))
            return data.get("prs")
        except (json.JSONDecodeError, KeyError):
            return None

    def get_repo_stats(self, full_name: str) -> dict | None:
        repo_file = self._repo_path(full_name)
        if not repo_file.exists():
            return None
        try:
            data = json.loads(repo_file.read_text(encoding="utf-8"))
            return data.get("stats")
        except (json.JSONDecodeError, KeyError):
            return None

    def set_repo_data(self, full_name: str, prs: list[dict], stats: dict):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        repo_file = self._repo_path(full_name)
        repo_file.write_text(
            json.dumps({"prs": prs, "stats": stats}, indent=2, default=str),
            encoding="utf-8",
        )

    def remove_repo(self, full_name: str):
        self._meta.get("fetched_at", {}).pop(full_name, None)
        repo_file = self._repo_path(full_name)
        if repo_file.exists():
            repo_file.unlink()


# ── Helpers ──────────────────────────────────────────────────────────────────

def fetch_ros2_repos() -> list[dict]:
    """Download ros2.repos and extract GitHub repo info."""
    logger.info("Fetching ros2.repos from %s", ROS2_REPOS_URL)
    resp = requests.get(ROS2_REPOS_URL, timeout=30)
    resp.raise_for_status()
    data = yaml.safe_load(resp.text)

    repos = []
    seen = set()
    for _key, entry in data.get("repositories", {}).items():
        url = entry.get("url", "")
        if "github.com" not in url:
            continue
        match = re.search(r"github\.com[/:]([^/]+)/([^/.]+)", url)
        if not match:
            continue
        full_name = f"{match.group(1)}/{match.group(2)}"
        if full_name in seen or full_name in EXCLUDED_REPOS:
            continue
        seen.add(full_name)
        html_url = f"https://github.com/{full_name}"
        repos.append({"full_name": full_name, "url": html_url})

    logger.info("Found %d GitHub repos in ros2.repos", len(repos))
    return repos


def _platform_from_job_name(job_name: str) -> str:
    """Map ci.ros2.org job names to human-readable platform labels."""
    name = job_name.lower()
    if "aarch64" in name:
        return "Linux-aarch64"
    if "rhel" in name:
        return "Linux-RHEL"
    if "windows" in name or "win" in name:
        return "Windows"
    if "macos" in name or "osx" in name:
        return "macOS"
    if "linux" in name:
        return "Linux"
    return job_name


def extract_ci_triggers(pr) -> list[dict]:
    """Scan PR issue comments for ci.ros2.org job URLs.

    Returns list of dicts (JSON-serializable) instead of dataclasses.
    """
    triggers = []
    try:
        comments = pr.get_issue_comments()
    except Exception:
        logger.warning("Could not fetch comments for PR #%s", pr.number)
        return triggers

    for comment in comments:
        body = comment.body or ""
        ci_urls = CI_URL_PATTERN.findall(body)
        if not ci_urls:
            continue

        runs = []
        for job_name, build_num, suffix in ci_urls:
            url = f"https://ci.ros2.org/job/{job_name}/{build_num}/"
            platform = _platform_from_job_name(suffix if suffix else job_name)
            runs.append({
                "job_name": job_name,
                "build_number": int(build_num),
                "url": url,
                "platform": platform,
            })

        triggers.append({
            "commenter": comment.user.login,
            "commenter_url": comment.user.html_url,
            "comment_date": comment.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "comment_url": comment.html_url,
            "runs": runs,
        })

    return triggers


def _aggregate_review_state(pr) -> tuple[str, list[dict]]:
    """Determine the overall review state for a PR."""
    latest_per_user = {}

    try:
        reviews = pr.get_reviews()
        for review in reviews:
            user = review.user.login
            if review.state in ("APPROVED", "CHANGES_REQUESTED"):
                latest_per_user[user] = {
                    "name": user,
                    "url": review.user.html_url,
                    "state": review.state.lower(),
                }
            elif user not in latest_per_user:
                latest_per_user[user] = {
                    "name": user,
                    "url": review.user.html_url,
                    "state": review.state.lower(),
                }
    except Exception:
        logger.warning("Could not fetch reviews for PR #%s", pr.number)
        return "none", []

    reviewers = list(latest_per_user.values())

    if not reviewers:
        return "none", reviewers

    states = {r["state"] for r in reviewers}
    if "changes_requested" in states:
        return "changes_requested", reviewers
    if "approved" in states:
        return "approved", reviewers
    return "commented", reviewers


def _classify_pr(review_state: str) -> str:
    """Assign a category to a PR based on review state."""
    if review_state == "approved":
        return "ready_to_merge"
    if review_state == "changes_requested":
        return "changes_requested"
    return "needs_review"


def _log_rate_limit(gh: Github, label: str = ""):
    """Log rate limit with human-friendly time-to-reset."""
    rate = gh.get_rate_limit().rate
    now = datetime.now(timezone.utc)
    reset_dt = rate.reset.replace(tzinfo=timezone.utc)
    delta = reset_dt - now
    minutes = max(0, int(delta.total_seconds() // 60))
    seconds = max(0, int(delta.total_seconds() % 60))
    logger.info(
        "Rate limit%s: %d/%d remaining (resets in %dm %ds at %s)",
        f" {label}" if label else "",
        rate.remaining,
        rate.limit,
        minutes,
        seconds,
        rate.reset.strftime("%H:%M:%S UTC"),
    )
    return rate


def _fetch_repo_prs(gh_repo, full_name: str, now: datetime) -> list[dict]:
    """Fetch all open non-draft PRs for a repo and return as dicts."""
    prs = []
    try:
        pulls = gh_repo.get_pulls(state="open", sort="updated", direction="desc")
    except Exception:
        logger.warning("Could not fetch PRs for %s", full_name)
        return prs

    for pr in pulls:
        if pr.draft:
            continue

        labels = [l.name for l in pr.labels]
        if EXCLUDED_LABELS.intersection(labels):
            continue

        review_state, reviewers = _aggregate_review_state(pr)
        ci_triggers = extract_ci_triggers(pr)

        updated = pr.updated_at.replace(tzinfo=timezone.utc)
        days_since = (now - updated).days

        pr_dict = {
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
            "repo_full_name": full_name,
            "author": pr.user.login,
            "author_url": pr.user.html_url,
            "created_at": pr.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "updated_at": updated.isoformat(),
            "labels": labels,
            "draft": pr.draft,
            "review_state": review_state,
            "reviewers": reviewers,
            "ci_triggers": ci_triggers,
            "assignees": [a.login for a in pr.assignees],
            "requested_reviewers": [r.login for r in pr.get_review_requests()[0]],
            "days_since_update": days_since,
            "category": _classify_pr(review_state),
        }
        prs.append(pr_dict)

    return prs


# ── Main fetch ───────────────────────────────────────────────────────────────

def fetch_all(
    token: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    no_cache: bool = False,
) -> DashboardData:
    """Fetch PR data for all repos in ros2.repos with local caching.

    Args:
        token: GitHub personal access token.
        cache_dir: Directory for the JSON cache file.
        no_cache: If True, ignore existing cache and re-fetch everything.

    Returns:
        DashboardData with all PR and repo information.
    """
    gh = Github(token, per_page=100)
    rate = _log_rate_limit(gh, "before fetch")

    cache = Cache(cache_dir)
    if no_cache:
        logger.info("Cache disabled, fetching all repos fresh")

    repo_infos = fetch_ros2_repos()
    now = datetime.now(timezone.utc)

    all_prs: list[dict] = []
    all_repos: list[dict] = []
    fetched = 0
    cached = 0

    for repo_info in repo_infos:
        full_name = repo_info["full_name"]

        try:
            gh_repo = gh.get_repo(full_name)
        except Exception:
            logger.warning("Could not access repo %s, skipping", full_name)
            continue

        pushed_at = gh_repo.pushed_at.isoformat() if gh_repo.pushed_at else ""
        cached_pushed_at = cache.get_repo_pushed_at(full_name)

        # Check if we can use cached data
        if (
            not no_cache
            and cached_pushed_at == pushed_at
            and cache.get_repo_prs(full_name) is not None
        ):
            # Repo hasn't changed, use cache
            pr_dicts = cache.get_repo_prs(full_name)
            stats = cache.get_repo_stats(full_name)
            logger.info(
                "  %-40s  → cache hit (%d PRs)", full_name, len(pr_dicts)
            )
            cached += 1

            # Recompute days_since_update for cached PRs
            for pr_dict in pr_dicts:
                updated = datetime.fromisoformat(pr_dict["updated_at"])
                pr_dict["days_since_update"] = (now - updated).days

        else:
            # Repo changed or not in cache — full fetch
            logger.info(
                "  %-40s  → fetching...", full_name
            )
            pr_dicts = _fetch_repo_prs(gh_repo, full_name, now)
            stats = {
                "full_name": full_name,
                "url": repo_info["url"],
                "open_pr_count": gh_repo.open_issues_count,
                "open_issue_count": 0,
            }
            cache.set_repo_data(full_name, pr_dicts, stats)
            cache.set_repo_pushed_at(full_name, pushed_at)
            cache.save()  # Save after each repo so Ctrl+C doesn't lose progress
            fetched += 1

            # Log quota after each fetched repo
            _log_rate_limit(gh, f"after {full_name}")

        all_prs.extend(pr_dicts)
        if stats:
            all_repos.append(stats)

    # Save cache
    cache.save()

    # Log final rate limit
    rate = _log_rate_limit(gh, "after fetch")

    # Build DashboardData with proper dataclass instances
    dashboard = _build_dashboard(all_prs, all_repos, now)
    dashboard.rate_limit_remaining = rate.remaining
    dashboard.rate_limit_total = rate.limit

    logger.info(
        "Done: %d PRs across %d repos (fetched=%d, cached=%d)",
        len(dashboard.prs), len(dashboard.repos), fetched, cached,
    )
    return dashboard


def load_from_cache(cache_dir: Path = DEFAULT_CACHE_DIR) -> DashboardData:
    """Load dashboard data entirely from cache, no GitHub API calls.

    Reads all per-repo cache files and assembles a DashboardData object.
    Useful for regenerating the static site without hitting rate limits.
    """
    cache = Cache(cache_dir)
    now = datetime.now(timezone.utc)

    all_prs: list[dict] = []
    all_repos: list[dict] = []

    # Find all repo cache files (skip _meta.json)
    if not cache_dir.exists():
        logger.warning("Cache directory %s does not exist", cache_dir)
        return DashboardData(generated_at=now.isoformat())

    for repo_file in sorted(cache_dir.glob("*.json")):
        if repo_file.name == Cache.META_FILE:
            continue
        try:
            data = json.loads(repo_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Skipping corrupt cache file %s: %s", repo_file, e)
            continue

        prs = data.get("prs", [])
        stats = data.get("stats")

        # Recompute days_since_update
        for pr_dict in prs:
            updated = datetime.fromisoformat(pr_dict["updated_at"])
            pr_dict["days_since_update"] = (now - updated).days

        all_prs.extend(prs)
        if stats:
            all_repos.append(stats)

    dashboard = _build_dashboard(all_prs, all_repos, now)
    logger.info(
        "Loaded from cache: %d PRs across %d repos",
        len(dashboard.prs), len(dashboard.repos),
    )
    return dashboard


def _build_dashboard(
    all_prs: list[dict], all_repos: list[dict], now: datetime
) -> DashboardData:
    """Build a DashboardData from raw PR and repo dicts."""
    dashboard = DashboardData(generated_at=now.isoformat())

    for pr_dict in all_prs:
        ci_triggers = [
            CITrigger(
                commenter=t["commenter"],
                commenter_url=t["commenter_url"],
                comment_date=t["comment_date"],
                comment_url=t["comment_url"],
                runs=[CIRun(**r) for r in t["runs"]],
            )
            for t in pr_dict.get("ci_triggers", [])
        ]
        dashboard.prs.append(PRData(
            number=pr_dict["number"],
            title=pr_dict["title"],
            url=pr_dict["url"],
            repo_full_name=pr_dict["repo_full_name"],
            author=pr_dict["author"],
            author_url=pr_dict["author_url"],
            created_at=pr_dict["created_at"],
            updated_at=pr_dict["updated_at"],
            labels=pr_dict["labels"],
            draft=pr_dict["draft"],
            review_state=pr_dict["review_state"],
            reviewers=pr_dict["reviewers"],
            ci_triggers=ci_triggers,
            assignees=pr_dict.get("assignees", []),
            requested_reviewers=pr_dict.get("requested_reviewers", []),
            days_since_update=pr_dict["days_since_update"],
            category=pr_dict["category"],
        ))

    for rs in all_repos:
        dashboard.repos.append(RepoStats(
            full_name=rs["full_name"],
            url=rs["url"],
            open_pr_count=rs.get("open_pr_count", 0),
            open_issue_count=rs.get("open_issue_count", 0),
        ))

    return dashboard

