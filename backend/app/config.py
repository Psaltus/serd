import os
from typing import Any, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Where an emulator is assumed to be listening when SERDSAFE_LOCAL_STACK says
# "on" without saying where.
DEFAULT_LOCAL_ENDPOINT = "http://localhost:4566"


class Settings(BaseSettings):
    """Application configuration.

    Every field below is filled from the environment variable of the same name,
    upper-cased — AWS_REGION sets `aws_region`, SERDSAFE_PLATFORM_ENV sets
    `serdsafe_platform_env`. That is what BaseSettings is for, so no field needs
    an os.getenv of its own.

    Values are resolved in this order, first match winning:

        1. real environment variables — how a deployed container is configured
        2. backend/.env — a local convenience, and absent in deployment
        3. the defaults written here

    So the environment always beats the file, and the file being missing is
    normal rather than a problem. Reading os.getenv into a default would not
    add anything: it runs once at import, is invisible to that ordering, and
    skips the type conversion that turns "99" into an int or "true" into a
    bool.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        # Parameter Store hands every value back as a string. Validating on
        # assignment is what turns "120" into an int rather than leaving the
        # wrong type on an int field.
        validate_assignment=True,
        # A deployed container's environment carries plenty that is nothing to
        # do with us; unrecognised names are not an error.
        extra="ignore",
    )

    # --- Bootstrap -----------------------------------------------------------
    # Needed before Parameter Store can be reached, so it cannot come from it.
    aws_region: str = "us-east-1"

    # Points every AWS call at an emulator instead of real AWS. Either the
    # endpoint itself (http://localhost:4566) or any other non-empty value,
    # which means "on, at the usual port". Unset in a deployment.
    # Optional[str] rather than str: absent is its normal state, and None says
    # so honestly. See `aws_endpoint_url` below.
    serdsafe_local_stack: Optional[str] = None

    # Which deployment this container belongs to: dev, staging, prod. It names
    # both the Parameter Store path everything below is read from
    # (/<env>/s3_bucket and so on) and the CloudWatch log group. Empty here
    # rather than absent so that a missing value produces our own explanation
    # at start-up instead of a pydantic traceback — see
    # parameters.require_platform_env.
    serdsafe_platform_env: str = ""

    # --- Supplied by Parameter Store at start-up -----------------------------
    # See parameters.ALLOWED_KEYS. These values are what remains in force if a
    # given parameter is absent.
    s3_bucket: str = ""
    s3_prefix: str = "uploads/"

    # How long a key that S3 said was missing is remembered, so repeated
    # requests for the same bad name do not each cost a HEAD call.
    negative_cache_seconds: int = 15

    # --- Logging -------------------------------------------------------------
    # Normal verbosity: CRITICAL, ERROR, WARNING, INFO or DEBUG.
    log_level: str = "INFO"
    # Overrides log_level with DEBUG when set. Kept separate so switching
    # debugging on cannot be undone by whatever log_level happens to say, and
    # so it can be flipped on a running container - see /api/logging.
    debug: bool = False

    cloudwatch_logs_enabled: bool = True
    # Normally left empty so the group is derived from the platform environment
    # - see `log_group` below. Set it only to put a deployment's logs somewhere
    # that does not follow the convention.
    cloudwatch_log_group: str = ""
    # Defaults to the container's host name, which keeps concurrent tasks in
    # separate streams.
    cloudwatch_log_stream: str = ""

    @field_validator("s3_prefix")
    @classmethod
    def _normalise_prefix(cls, value: str) -> str:
        """Accept a prefix in whatever shape it arrives and make it usable.

        Two things this saves. A prefix written without its trailing slash
        would otherwise concatenate straight onto the file name and quietly
        produce "uploadsreport.pdf" instead of "uploads/report.pdf". And
        Parameter Store cannot store an empty string, so "/" is the only way a
        deployment can say "serve the bucket root" - it is turned into the
        empty prefix that actually means that.
        """
        value = (value or "").strip().lstrip("/")
        if not value:
            return ""
        return value if value.endswith("/") else value + "/"

    @property
    def aws_endpoint_url(self) -> Optional[str]:
        """Where AWS API calls go, or None for real AWS.

        A value that looks like a URL is used as-is, so the emulator can live
        anywhere - notably `http://host.docker.internal:4566` when it runs on
        the host and this does not. Anything else non-empty (`true`, `1`) means
        "on, at the usual port".
        """
        value = (self.serdsafe_local_stack or "").strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            return value.rstrip("/")
        return DEFAULT_LOCAL_ENDPOINT

    @property
    def use_local_stack(self) -> bool:
        return self.aws_endpoint_url is not None

    @property
    def aws_client_kwargs(self) -> dict[str, Any]:
        """Arguments shared by every boto3 client in this app.

        One place decides where AWS calls go, so a client added later cannot
        quietly be built against real AWS while the rest point at an emulator.
        """
        kwargs: dict[str, Any] = {"region_name": self.aws_region}
        endpoint = self.aws_endpoint_url
        if endpoint:
            kwargs["endpoint_url"] = endpoint
            # An emulator ignores credentials, but botocore still refuses to
            # sign without finding some. Placeholders are supplied only when
            # the environment offers nothing, so a machine that does have
            # credentials carries on using its own.
            if not (os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")):
                kwargs["aws_access_key_id"] = "localstack"
                kwargs["aws_secret_access_key"] = "localstack"
        return kwargs

    @property
    def platform_env(self) -> str:
        """The environment name, without surrounding slashes or whitespace."""
        return self.serdsafe_platform_env.strip().strip("/")

    @property
    def param_path(self) -> str:
        """Parameter Store path for this environment, e.g. /prod."""
        return f"/{self.platform_env}"

    @property
    def log_group(self) -> str:
        """CloudWatch log group, derived from the environment unless overridden."""
        if self.cloudwatch_log_group:
            return self.cloudwatch_log_group
        return f"/{self.platform_env}/serdsafely/backend"


settings = Settings()
