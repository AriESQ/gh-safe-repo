class SafeRepoError(Exception):
    """Base exception for gh-safe-repo."""


class APIError(SafeRepoError):
    """GitHub API call failed."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class AuthError(SafeRepoError):
    """Authentication failed or no credentials found."""


class ConfigError(SafeRepoError):
    """Configuration is invalid or missing."""


class RepoExistsError(SafeRepoError):
    """Repository already exists."""

    def __init__(self, owner, repo):
        super().__init__(f"Repository {owner}/{repo} already exists")
        self.owner = owner
        self.repo = repo
